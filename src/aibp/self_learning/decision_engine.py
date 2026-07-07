"""Decision Engine — compare variant vs control, decide promote/rollback.

Daily cron: takes 'shadow_running' experiments older than the experiment
window (policy safety.experiment_window_days, default 14), estimates
P(variant > control) via bootstrap (ADR-0008), decides promote or reject.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import structlog

from aibp.self_learning.db import get_snapshot_at_horizon, log_autopilot_event, sqlite_conn
from aibp.self_learning.safety import check_rate_limit, is_autopilot_paused
from aibp.utils.config import PROJECT_ROOT, load_policy

log = structlog.get_logger()

POLICY_PATH = PROJECT_ROOT / "config" / "policy.yaml"


def get_ready_experiments() -> list[dict]:
    """Get shadow_running experiments older than the experiment window."""
    policy = load_policy()
    window_days = policy.get("safety", {}).get("experiment_window_days", 7)
    window_ago = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, experiment_type, hypothesis,
                   policy_before, policy_after, assignment_mode
            FROM experiments_log
            WHERE status = 'shadow_running' AND started_at < ?
            """,
            (window_ago,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_engagement_for_policy_version(policy_version: str, since: str | None = None) -> list[dict]:
    """Get engagement data for main-channel posts published with this policy version.

    ADR-0007: only the main channel counts — the test channel has a different
    (near-zero) audience, so its engagement is excluded from decisions. With
    interleaving, control and variant posts are separated by policy_version
    within the same channel; `since` restricts to posts published after the
    experiment started.

    Engagement is taken at a fixed horizon (posted_at + 48h, nearest snapshot)
    rather than MAX/last, so posts of different ages compare fairly (issue #14).
    """
    query = """
        SELECT pf.feed_item_id, pf.posted_at, pf.slot, pf.target_channel
        FROM post_features pf
        WHERE pf.policy_version = ?
          AND pf.target_channel = 'main'
    """
    params: list = [policy_version]
    if since is not None:
        query += " AND pf.posted_at >= ?"
        params.append(since)

    with sqlite_conn() as conn:
        posts = [dict(r) for r in conn.execute(query, params).fetchall()]

    result = []
    for post in posts:
        snapshot = get_snapshot_at_horizon(post["feed_item_id"])
        if snapshot is None:
            continue
        post["latest_views"] = snapshot["views"]
        post["latest_subs"] = snapshot["subscribers_at"]
        result.append(post)
    return result


def compute_engagement_rates(posts: list[dict]) -> list[float]:
    """Legacy engagement rate (views / subscribers) for each post.

    Kept for observability comparisons; decisions use compute_reward_rates
    (issue #37, ADR-0010). Skips posts where subscribers is None or 0.
    """
    rates = []
    for p in posts:
        subs = p.get("latest_subs")
        if not subs or subs <= 0:
            continue
        views = p.get("latest_views") or 0
        rates.append(views / subs)
    return rates


def compute_reward_rates(posts: list[dict], policy: dict | None = None) -> list[float]:
    """Composite reward per post (issue #37): views + forwards + clicks + Δsubs,
    weighted and normalized by subscribers. Posts that can't be scored
    (no snapshot / no subscriber count) are skipped."""
    from aibp.self_learning.reward import compute_rewards_for_posts

    return [p["reward"] for p in compute_rewards_for_posts(posts, policy=policy)]


def compute_decision(
    control_rates: list[float],
    shadow_rates: list[float],
    exp_age_days: int,
    promote_probability: float = 0.95,
    min_effect: float = 0.05,
    give_up_days: int = 14,
    n_bootstrap: int = 4000,
) -> dict:
    """Pure function: given engagement data, return promote/reject/continue decision.

    This is separated from make_decision() for testability — no I/O, no SQLite,
    no time-dependent calls. All inputs are explicit.

    ADR-0008: the frequentist criterion (Welch p<0.05 + Cohen's d>0.3 + >=10%)
    was practically unreachable at n≈14 posts per group — the autopilot could
    never change anything. It is replaced by a bootstrap estimate of
    P(shadow > control):

        - If n < 5 in either group AND exp_age < give_up_days → continue
        - If n < 5 in either group AND exp_age >= give_up_days → reject (gave up)
        - If P(shadow > control) >= promote_probability AND relative effect
          >= min_effect → promote
        - If relative effect < -min_effect AND P(shadow > control) <= 0.5
          → reject (clearly worse)
        - Otherwise → reject (no significant improvement)

    Returns:
        dict with keys: decision, reason, control_engagement, shadow_engagement,
                        effect_size (relative effect, fraction),
                        p_value (P(shadow > control), bootstrap probability)
    """
    import numpy as np

    # Insufficient data check
    if len(control_rates) < 5 or len(shadow_rates) < 5:
        if exp_age_days < give_up_days:
            return {
                "decision": "continue",
                "reason": "insufficient_data_extending",
                "control_engagement": {
                    "mean": float(np.mean(control_rates)) if control_rates else 0,
                    "n": len(control_rates),
                },
                "shadow_engagement": {
                    "mean": float(np.mean(shadow_rates)) if shadow_rates else 0,
                    "n": len(shadow_rates),
                },
                "effect_size": None,
                "p_value": None,
            }
        return {
            "decision": "reject",
            "reason": "insufficient_data_after_14d",
            "control_engagement": {
                "mean": float(np.mean(control_rates)) if control_rates else 0,
                "n": len(control_rates),
            },
            "shadow_engagement": {
                "mean": float(np.mean(shadow_rates)) if shadow_rates else 0,
                "n": len(shadow_rates),
            },
            "effect_size": None,
            "p_value": None,
        }

    control = np.asarray(control_rates, dtype=float)
    shadow = np.asarray(shadow_rates, dtype=float)

    control_mean = float(control.mean())
    shadow_mean = float(shadow.mean())
    rel_effect = (shadow_mean / control_mean - 1) if control_mean > 0 else 0.0
    shadow_better_pct = rel_effect * 100

    # Bootstrap P(shadow > control): resample both groups with replacement,
    # compare means. Seeded RNG → deterministic for identical inputs.
    rng = np.random.default_rng(42)
    control_means = rng.choice(control, size=(n_bootstrap, len(control))).mean(axis=1)
    shadow_means = rng.choice(shadow, size=(n_bootstrap, len(shadow))).mean(axis=1)
    p_shadow_better = float((shadow_means > control_means).mean())

    # Decision rules (order matters!)
    if p_shadow_better >= promote_probability and rel_effect >= min_effect:
        decision = "promote"
        reason = (f"shadow +{shadow_better_pct:.1f}%, "
                  f"P(shadow>control)={p_shadow_better:.3f}")
    elif rel_effect < -min_effect and p_shadow_better <= 0.5:
        decision = "reject"
        reason = f"shadow {shadow_better_pct:.1f}%, worse than control"
    else:
        decision = "reject"
        reason = (f"no significant improvement (+{shadow_better_pct:.1f}%, "
                  f"P(shadow>control)={p_shadow_better:.3f})")

    return {
        "decision": decision,
        "reason": reason,
        "control_engagement": {"mean": control_mean, "n": len(control_rates)},
        "shadow_engagement": {"mean": shadow_mean, "n": len(shadow_rates)},
        "effect_size": round(rel_effect, 3),
        "p_value": round(p_shadow_better, 4),
    }


def make_decision(experiment: dict) -> dict:
    """Fetch engagement data for experiment and run compute_decision.

    This is the I/O wrapper: reads from SQLite, computes experiment age,
    then delegates to the pure compute_decision function.
    """
    # Both groups are restricted to the experiment period so they face the
    # same audience and seasonality (ADR-0007 interleaving).
    since = experiment["started_at"]
    control_posts = get_engagement_for_policy_version(experiment["policy_before"], since=since)
    shadow_posts = get_engagement_for_policy_version(experiment["policy_after"], since=since)

    policy = load_policy()
    control_rates = compute_reward_rates(control_posts, policy=policy)
    shadow_rates = compute_reward_rates(shadow_posts, policy=policy)

    exp_age_days = (datetime.now(UTC) -
                    datetime.fromisoformat(experiment["started_at"])).days

    safety = policy.get("safety", {})
    window_days = safety.get("experiment_window_days", 14)
    return compute_decision(
        control_rates,
        shadow_rates,
        exp_age_days,
        promote_probability=safety.get("promote_probability", 0.95),
        min_effect=safety.get("min_effect_pct", 5) / 100,
        give_up_days=window_days + 7,
    )


def apply_promotion(experiment: dict, decision: dict) -> bool:
    """Write the winning policy to production and finalize the experiment."""
    # Load shadow policy
    with sqlite_conn() as conn:
        row = conn.execute(
            "SELECT json_blob, yaml_content FROM policies WHERE version = ?",
            (experiment["policy_after"],),
        ).fetchone()
        if not row:
            return False

    # Apply to production policy.yaml
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
                datetime.now(UTC).isoformat(),
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


def mark_pending_approval(experiment: dict, decision: dict) -> None:
    """Park a high-risk experiment until a human approves it (issue #20)."""
    with sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE experiments_log
            SET status = 'pending_approval',
                control_engagement = ?,
                shadow_engagement = ?,
                effect_size = ?,
                p_value = ?,
                decision_reason = ?
            WHERE id = ?
            """,
            (
                json.dumps(decision["control_engagement"]),
                json.dumps(decision["shadow_engagement"]),
                decision["effect_size"],
                decision["p_value"],
                decision["reason"],
                experiment["id"],
            ),
        )
    log_autopilot_event("approval_requested", experiment_id=experiment["id"],
                       details={"experiment_type": experiment["experiment_type"]})
    log.info("experiment_pending_approval", id=experiment["id"],
             type=experiment["experiment_type"])


def promote_experiment(experiment: dict, decision: dict) -> bool:
    """Promote the variant policy — directly, or via the human approval gate."""
    from aibp.self_learning.safety import requires_approval

    if requires_approval(experiment["experiment_type"]):
        mark_pending_approval(experiment, decision)
        try:
            from aibp.self_learning.approvals import send_approval_request
            send_approval_request(experiment, decision)
        except Exception as e:
            # The experiment stays pending_approval; the reminder can be
            # re-sent manually via `python -m aibp.self_learning.approvals --remind`
            log.error("approval_request_send_failed", experiment=experiment["id"], error=str(e))
        return True

    allowed, reason = check_rate_limit("change_applied")
    if not allowed:
        log.info("rate_limited_cannot_promote", experiment=experiment["id"], reason=reason)
        return False

    return apply_promotion(experiment, decision)


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
                datetime.now(UTC).isoformat(),
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
