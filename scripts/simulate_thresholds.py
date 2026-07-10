#!/usr/bin/env python3
"""Simulate how new tiered thresholds would have changed past decisions (issue #42).

Recomputes per-post reward rates for each finished experiment and re-runs
compute_decision under (a) old flat thresholds and (b) new tiered thresholds.

CAVEAT: recomputed rates use the composite reward (issue #37), while historical
decisions were made on views/subs — this compares thresholds on today's metric,
not a literal replay of past decisions. rolled_back experiments are listed
separately (they were promoted and then failed — a threshold that re-promotes
them is not a win).
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aibp.db.connection import fetch_all
from aibp.self_learning.decision_engine import (
    compute_decision,
    compute_reward_rates,
    get_engagement_for_policy_version,
)
from aibp.self_learning.tiers import load_tier_config
from aibp.utils.config import load_policy

OLD_FLAT = {"promote_probability": 0.95, "min_effect_pct": 5, "experiment_window_days": 14}


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def run() -> int:
    policy = load_policy()
    experiments = fetch_all(
        """
        SELECT id, started_at, finished_at, experiment_type, status,
               policy_before, policy_after
        FROM experiments_log
        WHERE status IN ('promoted', 'rejected', 'rolled_back')
        ORDER BY finished_at DESC
        """
    )

    if not experiments:
        print("No finished experiments — insufficient history for simulation.")
        return 0

    print(f"Simulating thresholds over {len(experiments)} finished experiments.\n")
    print("CAVEAT: recomputed rates use the composite reward (#37); historical")
    print("decisions used views/subs. This compares thresholds on today's metric.\n")

    print(f"{'ID':>4}  {'Type':<16} {'Status':<12} {'Old':<10} {'New':<10} {'Old P':>7} {'New P':>7}")
    print("-" * 80)

    for exp in experiments:
        started = _as_dt(exp["started_at"])
        exp_age = (datetime.now(UTC) - started).days
        tier = load_tier_config(exp["experiment_type"], policy=policy)

        control_posts = get_engagement_for_policy_version(exp["policy_before"], since=started)
        shadow_posts = get_engagement_for_policy_version(exp["policy_after"], since=started)
        control_rates = compute_reward_rates(control_posts, policy=policy)
        shadow_rates = compute_reward_rates(shadow_posts, policy=policy)

        if len(control_rates) < 5 or len(shadow_rates) < 5:
            print(f"{exp['id']:>4}  {exp['experiment_type']:<16} {exp['status']:<12} "
                  f"{'n/a':<10} {'n/a':<10}  (insufficient data: ctrl={len(control_rates)}, shdw={len(shadow_rates)})")
            continue

        old_decision = compute_decision(control_rates, shadow_rates, exp_age,
                                        promote_probability=OLD_FLAT["promote_probability"],
                                        min_effect=OLD_FLAT["min_effect_pct"] / 100,
                                        give_up_days=OLD_FLAT["experiment_window_days"] + 7)
        new_decision = compute_decision(control_rates, shadow_rates, exp_age,
                                        promote_probability=tier["promote_probability"],
                                        min_effect=tier["min_effect_pct"] / 100,
                                        give_up_days=tier["experiment_window_days"] + 7)

        print(f"{exp['id']:>4}  {exp['experiment_type']:<16} {exp['status']:<12} "
              f"{old_decision['decision']:<10} {new_decision['decision']:<10} "
              f"{old_decision['p_value'] or 0:>7.3f} {new_decision['p_value'] or 0:>7.3f}")

    print("\nNote: rolled_back experiments were promoted then failed in production.")
    print("A threshold that re-promotes them is NOT a win — it would repeat the failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
