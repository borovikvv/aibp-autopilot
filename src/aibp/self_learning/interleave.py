"""Interleaving assignment — alternate policies by day in the main channel.

ADR-0007: cross-channel shadow comparison is statistically invalid (different
audiences). Instead, an active experiment alternates policies by day-of-year
parity in the main channel: even days → control (policy_before, current
config/policy.yaml), odd days → variant (policy_after from SQLite policies).

Assignment is a pure function of the date, so it needs no extra state and
survives restarts.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import structlog

from aibp.db.connection import fetch_one

log = structlog.get_logger()

MSK = ZoneInfo("Europe/Moscow")

CONTROL = "control"
VARIANT = "variant"


def assignment_for_date(d: date) -> str:
    """Deterministic arm assignment: even day-of-year → control, odd → variant."""
    return CONTROL if d.timetuple().tm_yday % 2 == 0 else VARIANT


def get_active_interleave_experiment() -> dict | None:
    """Return the running interleave experiment, if any (at most one expected)."""
    row = fetch_one(
        """
        SELECT id, started_at, experiment_type, hypothesis,
               policy_before, policy_after, assignment_mode
        FROM experiments_log
        WHERE status = 'shadow_running' AND assignment_mode = 'interleave'
        ORDER BY started_at DESC
        LIMIT 1
        """
    )
    return row


def load_policy_version(version: str) -> dict | None:
    """Load policy dict by version from PostgreSQL policies table.

    The json_blob column is jsonb, so psycopg2 returns it as a dict already.
    """
    row = fetch_one(
        "SELECT json_blob FROM policies WHERE version = %s",
        (version,),
    )
    return row["json_blob"] if row else None


def resolve_policy_for_today(default_policy: dict, today: date | None = None) -> dict:
    """Return the policy the prod pipeline must use today.

    No active interleave experiment, or a control day → default_policy
    (config/policy.yaml). Variant day → policy_after of the active experiment.
    Falls back to default_policy if the variant version is missing in SQLite.
    """
    if today is None:
        today = datetime.now(MSK).date()

    experiment = get_active_interleave_experiment()
    if experiment is None:
        return default_policy

    arm = assignment_for_date(today)
    if arm == CONTROL:
        log.info("interleave_control_day", experiment=experiment["id"], date=today.isoformat())
        return default_policy

    variant_policy = load_policy_version(experiment["policy_after"])
    if variant_policy is None:
        log.error(
            "interleave_variant_policy_missing",
            experiment=experiment["id"],
            version=experiment["policy_after"],
        )
        return default_policy

    variant_policy["version"] = experiment["policy_after"]
    log.info(
        "interleave_variant_day",
        experiment=experiment["id"],
        date=today.isoformat(),
        policy=experiment["policy_after"],
    )
    return variant_policy
