"""Shadow Test Runner — starts interleave experiments (ADR-0007).

Daily cron: takes 'draft' experiments, marks them 'shadow_running'. The prod
generation pipeline then alternates policies by day in the main channel
(see aibp.self_learning.interleave). The stage policy file is still written so
the test channel can serve as a generation quality gate — its engagement data
is NOT used for decisions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
import yaml

from aibp.db.connection import execute, fetch_all, fetch_one
from aibp.self_learning.db import log_autopilot_event
from aibp.self_learning.safety import check_rate_limit, is_autopilot_paused
from aibp.utils.config import PROJECT_ROOT

log = structlog.get_logger()

POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"


def get_draft_experiments() -> list[dict]:
    """Get experiments with status='draft'."""
    return fetch_all(
        """
        SELECT id, started_at, experiment_type, hypothesis,
               policy_before, policy_after, applies_to
        FROM experiments_log
        WHERE status = 'draft'
        ORDER BY started_at ASC
        """
    )


def get_shadow_policy(policy_version: str) -> dict | None:
    """Load the experiment VARIANT policy dict by version from PostgreSQL.

    Despite the legacy "shadow" name, this is the interleave variant (drives
    statistics), not the preview policy written to config/policy.stage.yaml
    (see ADR-0007 / issue #22). The json_blob column is jsonb, so psycopg2
    returns it as a dict already.
    """
    row = fetch_one(
        "SELECT json_blob FROM policies WHERE version = %s",
        (policy_version,),
    )
    return row["json_blob"] if row else None


def apply_policy_to_stage(policy: dict) -> None:
    """Write the PREVIEW policy for the test channel (human QA), not the
    interleave variant used for statistics.

    ADR-0007: `config/policy.stage.yaml` is a preview so a human can eyeball
    generation under the new policy in the `test` channel. It does NOT drive
    any promote/reject decision. The statistical VARIANT lives in SQLite
    `policies` (loaded by `self_learning.interleave.resolve_policy_for_today`)
    and runs in the `main` channel on odd days. Do not confuse the two.
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

    # Apply to stage (quality gate only — engagement from the test channel
    # is excluded from decisions, see ADR-0007)
    apply_policy_to_stage(shadow_policy)

    # Update experiment status; assignment_mode is set explicitly because
    # legacy DBs backfill the column with DEFAULT 'cross_channel'
    execute(
        """
        UPDATE experiments_log
        SET status = 'shadow_running', started_at = %s, assignment_mode = 'interleave'
        WHERE id = %s
        """,
        (datetime.now(UTC), experiment["id"]),
    )

    log_autopilot_event("change_applied", experiment_id=experiment["id"],
                       details={"policy_version": experiment["policy_after"]})
    log.info("shadow_started", experiment=experiment["id"], policy=experiment["policy_after"])
    return True


def check_expired_shadows() -> None:
    """Mark shadow experiments older than 7 days as ready for decision."""
    week_ago = datetime.now(UTC) - timedelta(days=7)
    rows = fetch_all(
        """
        SELECT id, started_at FROM experiments_log
        WHERE status = 'shadow_running' AND started_at < %s
        """,
        (week_ago,),
    )
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
