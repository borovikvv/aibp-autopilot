"""Safety module — rate limiter, anomaly guard, kill switch.

CRITICAL: This is the safety boundary for autopilot. Without these
checks, autopilot can destroy the channel by making bad changes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog
import yaml

from aibp.self_learning.db import sqlite_conn, log_autopilot_event
from aibp.utils.config import PROJECT_ROOT, load_policy

log = structlog.get_logger()

POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"


def is_autopilot_paused() -> bool:
    """Check if autopilot is paused (kill switch active)."""
    policy = load_policy()
    return bool(policy.get("autopilot_paused", False))


def pause_autopilot(reason: str) -> None:
    """Pause autopilot by setting flag in policy.yaml."""
    policy = load_policy()
    policy["autopilot_paused"] = True
    policy["_pause_reason"] = reason
    policy["_paused_at"] = datetime.now(timezone.utc).isoformat()

    with open(POLICY_PATH, "w", encoding="utf-8") as f:
        yaml.dump(policy, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    log_autopilot_event("kill_switch_activated", details={"reason": reason})
    log.error("autopilot_paused", reason=reason)


def resume_autopilot() -> bool:
    """Resume autopilot (manual action via CLI)."""
    policy = load_policy()
    if not policy.get("autopilot_paused"):
        return False

    policy["autopilot_paused"] = False
    policy.pop("_pause_reason", None)
    policy.pop("_paused_at", None)

    with open(POLICY_PATH, "w", encoding="utf-8") as f:
        yaml.dump(policy, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    log_autopilot_event("manual_resume")
    log.info("autopilot_resumed")
    return True


# ═══════════════════════════════════════════════════════════════════
# Rate Limiter
# ═══════════════════════════════════════════════════════════════════

def count_events(event_type: str, since: datetime) -> int:
    """Count autopilot events of type in time window."""
    with sqlite_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as n FROM autopilot_events WHERE event_type = ? AND event_at >= ?",
            (event_type, since.isoformat()),
        ).fetchone()
        return row["n"] if row else 0


def check_rate_limit(action_type: str = "change_applied") -> tuple[bool, str]:
    """Check if action is allowed under rate limit.

    Returns (allowed, reason).
    """
    policy = load_policy()
    safety = policy.get("safety", {})

    max_per_day = safety.get("max_changes_per_day", 1)
    max_per_week = safety.get("max_changes_per_week", 3)

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    today_count = count_events(action_type, day_ago)
    week_count = count_events(action_type, week_ago)

    if today_count >= max_per_day:
        return False, f"Daily limit reached: {today_count}/{max_per_day}"
    if week_count >= max_per_week:
        return False, f"Weekly limit reached: {week_count}/{max_per_week}"

    return True, f"OK (today: {today_count}/{max_per_day}, week: {week_count}/{max_per_week})"


# ═══════════════════════════════════════════════════════════════════
# Anomaly Guard
# ═══════════════════════════════════════════════════════════════════

def get_recent_engagement_stats(days: int = 7) -> dict:
    """Get avg engagement for recent posts."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT AVG(em.views) as avg_views,
                   AVG(em.subscribers_at) as avg_subs,
                   COUNT(DISTINCT em.feed_item_id) as n_posts
            FROM engagement_metrics em
            JOIN post_features pf ON em.feed_item_id = pf.feed_item_id
            WHERE em.measured_at >= ?
              AND pf.target_channel = 'main'
            """,
            ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),),
        ).fetchone()
        return dict(rows) if rows else {}


def check_anomaly() -> tuple[bool, str | None]:
    """Check for engagement anomalies. Returns (is_anomaly, reason)."""
    policy = load_policy()
    safety = policy.get("safety", {})

    drop_24h_threshold = safety.get("engagement_drop_24h_pct", 30) / 100
    drop_7d_threshold = safety.get("engagement_drop_7d_pct", 50) / 100

    stats_7d = get_recent_engagement_stats(7)
    stats_30d = get_recent_engagement_stats(30)

    if not stats_7d.get("avg_views") or not stats_30d.get("avg_views"):
        return False, None  # not enough data

    avg_7d = stats_7d["avg_views"]
    avg_30d = stats_30d["avg_views"]

    if avg_30d > 0:
        drop_7d = (avg_30d - avg_7d) / avg_30d
        if drop_7d > drop_7d_threshold:
            return True, f"engagement_drop_7d: {drop_7d:.1%} (threshold: {drop_7d_threshold:.0%})"

    # Check 24h vs 7d
    stats_24h = get_recent_engagement_stats(1)
    if stats_24h.get("avg_views") and avg_7d > 0:
        drop_24h = (avg_7d - stats_24h["avg_views"]) / avg_7d
        if drop_24h > drop_24h_threshold:
            return True, f"engagement_drop_24h: {drop_24h:.1%} (threshold: {drop_24h_threshold:.0%})"

    return False, None


# ═══════════════════════════════════════════════════════════════════
# Kill Switch
# ═══════════════════════════════════════════════════════════════════

def check_kill_switch() -> tuple[bool, str | None]:
    """Check if kill switch should activate.

    Activates if >= 3 rollbacks in last 7 days.
    Returns (activated, reason).
    """
    policy = load_policy()
    max_rollbacks = policy.get("safety", {}).get("max_rollbacks_per_week", 3)

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    rollback_count = count_events("rollback", week_ago)

    if rollback_count >= max_rollbacks:
        return True, f"Kill switch: {rollback_count} rollbacks in 7 days (limit: {max_rollbacks})"

    return False, None


def daily_safety_check() -> int:
    """Daily cron — check anomaly + kill switch. Pauses autopilot if needed."""
    if is_autopilot_paused():
        log.warning("autopilot_already_paused")
        return 0

    # Check anomaly
    is_anomaly, reason = check_anomaly()
    if is_anomaly:
        pause_autopilot(reason or "anomaly_detected")
        return 1

    # Check kill switch
    should_kill, reason = check_kill_switch()
    if should_kill:
        pause_autopilot(reason or "kill_switch_threshold")
        return 1

    log.info("safety_check_passed")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--resume":
        if resume_autopilot():
            print("✅ Autopilot resumed")
        else:
            print("ℹ️  Autopilot was not paused")
    elif len(sys.argv) > 1 and sys.argv[1] == "--status":
        print(f"Paused: {is_autopilot_paused()}")
        allowed, reason = check_rate_limit()
        print(f"Rate limit: {allowed} ({reason})")
        is_anomaly, anomaly_reason = check_anomaly()
        print(f"Anomaly: {is_anomaly} ({anomaly_reason})")
        should_kill, kill_reason = check_kill_switch()
        print(f"Kill switch: {should_kill} ({kill_reason})")
    else:
        raise SystemExit(daily_safety_check())
