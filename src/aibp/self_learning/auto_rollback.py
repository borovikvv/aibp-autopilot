"""Auto-Rollback — revert promoted policy if engagement drops.

Daily cron: checks promoted experiments, rolls back if engagement drops.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
import yaml

from aibp.self_learning.db import sqlite_conn, log_autopilot_event
from aibp.self_learning.safety import pause_autopilot
from aibp.utils.config import PROJECT_ROOT, get_settings, load_policy
from aibp.publishing.publisher import send_message

log = structlog.get_logger()
POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"


def get_promoted_experiments_in_window(hours: int) -> list[dict]:
    """Get experiments promoted within the last N hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, finished_at, policy_before, policy_after, experiment_type
            FROM experiments_log
            WHERE status = 'promoted' AND finished_at >= ?
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_avg_engagement_for_policy(policy_version: str, days: int) -> float | None:
    """Get average engagement rate for posts with this policy version."""
    with sqlite_conn() as conn:
        row = conn.execute(
            """
            SELECT AVG(CAST(em.views AS FLOAT) / NULLIF(em.subscribers_at, 0)) as avg_rate
            FROM post_features pf
            JOIN engagement_metrics em ON em.feed_item_id = pf.feed_item_id
            WHERE pf.policy_version = ?
              AND em.measured_at >= ?
            """,
            (policy_version, (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()),
        ).fetchone()
        return row["avg_rate"] if row and row["avg_rate"] is not None else None


async def send_alert(message: str) -> None:
    """Send alert to author via Telegram."""
    s = get_settings()
    if not s.telegram_alert_chat_id:
        return
    await send_message(
        bot_token=s.telegram_bot_token,
        chat_id=s.telegram_alert_chat_id,
        text=f"⚠️ AIBP Autopilot Alert\n\n{message}",
    )


def rollback_experiment(experiment: dict, reason: str) -> None:
    """Revert policy to previous version."""
    # Load previous policy
    with sqlite_conn() as conn:
        row = conn.execute(
            "SELECT json_blob, yaml_content FROM policies WHERE version = ?",
            (experiment["policy_before"],),
        ).fetchone()
        if not row:
            log.error("cannot_rollback_policy_not_found", version=experiment["policy_before"])
            return

    prev_policy = json.loads(row["json_blob"])

    # Write previous policy to production
    with open(POLICY_PATH, "w", encoding="utf-8") as f:
        yaml.dump(prev_policy, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # Update experiment
    with sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE experiments_log
            SET status = 'rolled_back',
                rolled_back_at = ?,
                rollback_reason = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), reason, experiment["id"]),
        )

    log_autopilot_event("rollback", experiment_id=experiment["id"], details={"reason": reason})
    log.warning("experiment_rolled_back", id=experiment["id"], reason=reason)


async def run_async() -> int:
    """Main entry point."""
    policy = load_policy()
    safety = policy.get("safety", {})

    # Check 48h window
    rollback_48h_threshold = safety.get("rollback_48h_engagement_pct", 85) / 100
    experiments_48h = get_promoted_experiments_in_window(48)

    for exp in experiments_48h:
        before_eng = get_avg_engagement_for_policy(exp["policy_before"], days=7)
        after_eng = get_avg_engagement_for_policy(exp["policy_after"], days=2)

        if before_eng and after_eng:
            ratio = after_eng / before_eng if before_eng > 0 else 1
            if ratio < rollback_48h_threshold:
                reason = f"48h engagement {ratio:.0%} of baseline (threshold {rollback_48h_threshold:.0%})"
                rollback_experiment(exp, reason)
                await send_alert(f"Experiment #{exp['id']} rolled back.\nReason: {reason}")

                # Check if kill switch should activate
                from aibp.self_learning.safety import check_kill_switch
                should_kill, kill_reason = check_kill_switch()
                if should_kill:
                    pause_autopilot(kill_reason or "kill_switch")
                    await send_alert(f"🚨 KILL SWITCH ACTIVATED\n{kill_reason}")

    # Check 7d window
    rollback_7d_threshold = safety.get("rollback_7d_engagement_pct", 90) / 100
    experiments_7d = get_promoted_experiments_in_window(168)  # 7 days

    for exp in experiments_7d:
        if exp["id"] in [e["id"] for e in experiments_48h]:
            continue  # already checked in 48h window
        before_eng = get_avg_engagement_for_policy(exp["policy_before"], days=14)
        after_eng = get_avg_engagement_for_policy(exp["policy_after"], days=7)

        if before_eng and after_eng:
            ratio = after_eng / before_eng if before_eng > 0 else 1
            if ratio < rollback_7d_threshold:
                reason = f"7d engagement {ratio:.0%} of baseline (threshold {rollback_7d_threshold:.0%})"
                rollback_experiment(exp, reason)
                await send_alert(f"Experiment #{exp['id']} rolled back.\nReason: {reason}")

    return 0


def run() -> int:
    import asyncio
    return asyncio.run(run_async())


if __name__ == "__main__":
    raise SystemExit(run())
