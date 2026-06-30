"""Decision Engine — compare shadow vs control, decide promote/rollback.

Daily cron: takes 'shadow_running' experiments older than 7 days,
runs statistical test, decides promote or reject.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog
from scipy import stats as scipy_stats

from aibp.self_learning.db import sqlite_conn, log_autopilot_event
from aibp.self_learning.safety import is_autopilot_paused, check_rate_limit
from aibp.utils.config import PROJECT_ROOT, load_policy

log = structlog.get_logger()

POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"


def get_ready_experiments() -> list[dict]:
    """Get shadow_running experiments older than 7 days."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, experiment_type, hypothesis,
                   policy_before, policy_after
            FROM experiments_log
            WHERE status = 'shadow_running' AND started_at < ?
            """,
            (week_ago,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_engagement_for_policy_version(policy_version: str) -> list[dict]:
    """Get all engagement data for posts published with this policy version."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT pf.feed_item_id, pf.slot, pf.target_channel,
                   MAX(em.views) as latest_views,
                   MAX(em.subscribers_at) as latest_subs
            FROM post_features pf
            JOIN engagement_metrics em ON em.feed_item_id = pf.feed_item_id
            WHERE pf.policy_version = ?
              AND pf.target_channel IN ('main', 'test')
            GROUP BY pf.feed_item_id, pf.slot, pf.target_channel
            """,
            (policy_version,),
        ).fetchall()
        return [dict(r) for r in rows]


def compute_engagement_rates(posts: list[dict]) -> list[float]:
    """Compute engagement rate (views / subscribers) for each post."""
    rates = []
    for p in posts:
        views = p.get("latest_views") or 0
        subs = p.get("latest_subs") or 1
        if subs > 0:
            rates.append(views / subs)
    return rates


def make_decision(experiment: dict) -> dict:
    """Run statistical test and decide promote/reject/continue."""
    control_posts = get_engagement_for_policy_version(experiment["policy_before"])
    shadow_posts = get_engagement_for_policy_version(experiment["policy_after"])

    control_rates = compute_engagement_rates(control_posts)
    shadow_rates = compute_engagement_rates(shadow_posts)

    if len(control_rates) < 5 or len(shadow_rates) < 5:
        # Not enough data — extend window (max 14 days)
        exp_age_days = (datetime.now(timezone.utc) -
                        datetime.fromisoformat(experiment["started_at"])).days
        if exp_age_days < 14:
            return {"decision": "continue", "reason": "insufficient_data_extending"}
        return {"decision": "reject", "reason": "insufficient_data_after_14d"}

    # Welch's t-test
    t_stat, p_value = scipy_stats.ttest_ind(shadow_rates, control_rates, equal_var=False)

    # Cohen's d for effect size
    import numpy as np
    mean_diff = float(np.mean(shadow_rates) - np.mean(control_rates))
    pooled_std = float(np.sqrt(
        (np.var(shadow_rates, ddof=1) + np.var(control_rates, ddof=1)) / 2
    ))
    cohen_d = mean_diff / pooled_std if pooled_std > 0 else 0

    # Decision rules
    shadow_better_pct = (float(np.mean(shadow_rates)) / float(np.mean(control_rates)) - 1) * 100

    if shadow_better_pct >= 10 and p_value < 0.05 and cohen_d > 0.3:
        decision = "promote"
        reason = f"shadow +{shadow_better_pct:.1f}%, p={p_value:.4f}, d={cohen_d:.2f}"
    elif shadow_better_pct < -5:
        decision = "reject"
        reason = f"shadow {shadow_better_pct:.1f}%, worse than control"
    else:
        decision = "reject"
        reason = f"no significant improvement (+{shadow_better_pct:.1f}%, p={p_value:.4f})"

    return {
        "decision": decision,
        "reason": reason,
        "control_engagement": {"mean": float(np.mean(control_rates)), "n": len(control_rates)},
        "shadow_engagement": {"mean": float(np.mean(shadow_rates)), "n": len(shadow_rates)},
        "effect_size": round(cohen_d, 3),
        "p_value": round(p_value, 4),
    }


def promote_experiment(experiment: dict, decision: dict) -> bool:
    """Promote shadow policy to production."""
    allowed, reason = check_rate_limit("change_applied")
    if not allowed:
        log.info("rate_limited_cannot_promote", experiment=experiment["id"], reason=reason)
        return False

    # Load shadow policy
    with sqlite_conn() as conn:
        row = conn.execute(
            "SELECT json_blob, yaml_content FROM policies WHERE version = ?",
            (experiment["policy_after"],),
        ).fetchone()
        if not row:
            return False

    # Apply to production policy.yaml
    from aibp.self_learning.policy_updater import apply_policy_to_stage
    prod_policy = json.loads(row["json_blob"])
    prod_policy["version"] = experiment["policy_after"]

    with open(POLICY_PATH, "w", encoding="utf-8") as f:
        import yaml
        yaml.dump(prod_policy, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    # Update experiment
    with sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE experiments_log
            SET status = 'promoted',
                finished_at = ?,
                control_engagement = ?,
                shadow_engagement = ?,
                effect_size = ?,
                p_value = ?,
                decision_reason = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(decision["control_engagement"]),
                json.dumps(decision["shadow_engagement"]),
                decision["effect_size"],
                decision["p_value"],
                decision["reason"],
                experiment["id"],
            ),
        )

    log_autopilot_event("change_applied", experiment_id=experiment["id"],
                       details={"action": "promote", "policy": experiment["policy_after"]})
    log.info("experiment_promoted", id=experiment["id"], policy=experiment["policy_after"])
    return True


def reject_experiment(experiment: dict, decision: dict) -> None:
    """Mark experiment as rejected."""
    with sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE experiments_log
            SET status = 'rejected',
                finished_at = ?,
                control_engagement = ?,
                shadow_engagement = ?,
                effect_size = ?,
                p_value = ?,
                decision_reason = ?
            WHERE id = ?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(decision.get("control_engagement", {})),
                json.dumps(decision.get("shadow_engagement", {})),
                decision.get("effect_size"),
                decision.get("p_value"),
                decision["reason"],
                experiment["id"],
            ),
        )
    log.info("experiment_rejected", id=experiment["id"], reason=decision["reason"])


def run() -> int:
    """Main entry point."""
    if is_autopilot_paused():
        log.warning("autopilot_paused_skipping")
        return 0

    experiments = get_ready_experiments()
    if not experiments:
        log.info("no_ready_experiments")
        return 0

    log.info("deciding", count=len(experiments))

    for exp in experiments:
        decision = make_decision(exp)
        log.info("decision_made", experiment=exp["id"], decision=decision["decision"], reason=decision["reason"])

        if decision["decision"] == "promote":
            promote_experiment(exp, decision)
        elif decision["decision"] == "reject":
            reject_experiment(exp, decision)
        else:
            log.info("continuing_experiment", experiment=exp["id"])

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
