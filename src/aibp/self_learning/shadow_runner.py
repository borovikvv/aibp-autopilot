"""Shadow Test Runner — applies policy variant to stage, runs 7-day test.

Daily cron: takes 'draft' experiments, applies to stage prompt, marks 'shadow_running'.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import structlog
import yaml

from aibp.self_learning.db import log_autopilot_event, sqlite_conn
from aibp.self_learning.safety import check_rate_limit, is_autopilot_paused
from aibp.utils.config import PROJECT_ROOT

log = structlog.get_logger()

POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"


def get_draft_experiments() -> list[dict]:
    """Get experiments with status='draft'."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, experiment_type, hypothesis,
                   policy_before, policy_after, applies_to
            FROM experiments_log
            WHERE status = 'draft'
            ORDER BY started_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_shadow_policy(policy_version: str) -> dict | None:
    """Load policy dict by version from SQLite."""
    with sqlite_conn() as conn:
        row = conn.execute(
            "SELECT json_blob FROM policies WHERE version = ?",
            (policy_version,),
        ).fetchone()
        if row:
            return json.loads(row["json_blob"])
    return None


def apply_policy_to_stage(policy: dict) -> None:
    """Apply policy to stage environment (test channel).

    For MVP: we apply by writing a stage-specific policy.yaml.stage
    that the generation pipeline reads when pipeline_env='stage'.
    """
    stage_path = PROJECT_ROOT / "config" / "policy.stage.yaml"
    with open(stage_path, "w", encoding="utf-8") as f:
        yaml.dump(policy, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    log.info("stage_policy_written", path=str(stage_path))


def start_shadow(experiment: dict) -> bool:
    """Start one shadow experiment."""
    # Check rate limit
    allowed, reason = check_rate_limit("change_applied")
    if not allowed:
        log.info("rate_limited", experiment=experiment["id"], reason=reason)
        return False

    # Load shadow policy
    shadow_policy = get_shadow_policy(experiment["policy_after"])
    if not shadow_policy:
        log.error("shadow_policy_not_found", version=experiment["policy_after"])
        return False

    # Apply to stage
    apply_policy_to_stage(shadow_policy)

    # Update experiment status
    with sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE experiments_log
            SET status = 'shadow_running', started_at = ?
            WHERE id = ?
            """,
            (datetime.now(UTC).isoformat(), experiment["id"]),
        )

    log_autopilot_event("change_applied", experiment_id=experiment["id"],
                       details={"policy_version": experiment["policy_after"]})
    log.info("shadow_started", experiment=experiment["id"], policy=experiment["policy_after"])
    return True


def check_expired_shadows() -> None:
    """Mark shadow experiments older than 7 days as ready for decision."""
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at FROM experiments_log
            WHERE status = 'shadow_running' AND started_at < ?
            """,
            (week_ago,),
        ).fetchall()
        for row in rows:
            log.info("shadow_ready_for_decision", experiment=row["id"], started=row["started_at"])


def run() -> int:
    """Main entry point."""
    if is_autopilot_paused():
        log.warning("autopilot_paused_skipping")
        return 0

    # Start new shadow experiments (1 per day max)
    drafts = get_draft_experiments()
    started = 0
    for exp in drafts:
        if started >= 1:  # max 1 per day
            break
        if start_shadow(exp):
            started += 1

    # Check for expired shadows
    check_expired_shadows()

    log.info("shadow_runner_complete", started=started, drafts=len(drafts))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
