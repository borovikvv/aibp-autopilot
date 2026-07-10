"""Risk-tiered experiment thresholds (issue #42).

Experiment types are split into low-risk (auto-applied, auto-rollback) and
high-risk (human approval gate) tiers, each with its own promote probability,
minimum effect, and experiment window. This replaces the single flat threshold
that made nearly every experiment fail to promote at the channel's sample size
(n≈28 per group at 2 posts/day over 14 days).
"""
from __future__ import annotations

from aibp.utils.config import load_policy

# Conservative defaults: when a type is unknown, treat it as high-risk.
DEFAULT_TIERS = {
    "low_risk": {
        "experiment_types": ["rubric_weight", "cta", "source_score", "visual"],
        "promote_probability": 0.90,
        "min_effect_pct": 5,
        "experiment_window_days": 14,
    },
    "high_risk": {
        "experiment_types": ["post_param", "regex_gate"],
        "promote_probability": 0.95,
        "min_effect_pct": 5,
        "experiment_window_days": 28,
    },
}

# Flat-field fallback (old policy versions stored in the policies table).
_FLAT_DEFAULTS = {"promote_probability": 0.95, "min_effect_pct": 5, "experiment_window_days": 14}


def load_tier_config(experiment_type: str, policy: dict | None = None) -> dict:
    """Return {promote_probability, min_effect_pct, experiment_window_days} for the type.

    Looks up experiment_type in safety.experiment_tiers[*].experiment_types.
    Defaults to high_risk values when the type is unknown (safer).
    Falls back to flat safety.* fields when experiment_tiers is absent
    (old policy versions stored in the policies table).
    """
    if policy is None:
        policy = load_policy()
    safety = policy.get("safety", {})
    tiers = safety.get("experiment_tiers")

    if tiers:
        for tier_name in ("low_risk", "high_risk"):
            tier = tiers.get(tier_name, {})
            if experiment_type in tier.get("experiment_types", []):
                defaults = DEFAULT_TIERS[tier_name]
                return {
                    "promote_probability": tier.get("promote_probability", defaults["promote_probability"]),
                    "min_effect_pct": tier.get("min_effect_pct", defaults["min_effect_pct"]),
                    "experiment_window_days": tier.get("experiment_window_days", defaults["experiment_window_days"]),
                }
        # Known tiers structure but type not listed → high_risk (conservative)
        high = tiers.get("high_risk", DEFAULT_TIERS["high_risk"])
        return {
            "promote_probability": high.get("promote_probability", 0.95),
            "min_effect_pct": high.get("min_effect_pct", 5),
            "experiment_window_days": high.get("experiment_window_days", 28),
        }

    # Old policy without experiment_tiers → flat fields
    return {
        "promote_probability": safety.get("promote_probability", _FLAT_DEFAULTS["promote_probability"]),
        "min_effect_pct": safety.get("min_effect_pct", _FLAT_DEFAULTS["min_effect_pct"]),
        "experiment_window_days": safety.get("experiment_window_days", _FLAT_DEFAULTS["experiment_window_days"]),
    }
