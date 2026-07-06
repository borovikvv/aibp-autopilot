"""Tests for decision_engine — the only function that auto-affects prod without human review.

This is P0 critical path: covers compute_decision (pure function) with:
  - Clear promote case (variant >> control)
  - Clear reject case (variant << control)
  - Continue case (insufficient data, < give_up_days)
  - Reject after give_up_days with insufficient data
  - Boundary values of the bootstrap criterion (ADR-0008)
  - False positive rate regression (random data → < 5-8% promote)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import random

import pytest

from aibp.self_learning.decision_engine import compute_decision, compute_engagement_rates

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_rates(mean: float, n: int, spread: float = 0.05) -> list[float]:
    """Generate n engagement rates around mean with small deterministic spread."""
    if n == 0:
        return []
    offsets = [spread * ((i / max(n - 1, 1)) * 2 - 1) for i in range(n)]
    return [mean + o for o in offsets]


def _make_rates_with_effect(control_mean: float, effect_pct: float, n: int, noise: float = 0.02) -> tuple[list[float], list[float]]:
    """Generate control and shadow rates where shadow is effect_pct better."""
    control_offsets = [noise * ((i / max(n - 1, 1)) * 2 - 1) for i in range(n)]
    control = [control_mean + o for o in control_offsets]
    shadow_mean = control_mean * (1 + effect_pct / 100)
    shadow = [shadow_mean + o for o in control_offsets]
    return control, shadow


# ═══════════════════════════════════════════════════════════════════
# Clear cases
# ═══════════════════════════════════════════════════════════════════

def test_clear_promote_case():
    """Shadow is 30% better than control → promote."""
    control, shadow = _make_rates_with_effect(control_mean=0.10, effect_pct=30, n=20)
    result = compute_decision(control, shadow, exp_age_days=14)
    assert result["decision"] == "promote"
    assert result["effect_size"] == pytest.approx(0.30, abs=0.02)
    assert result["p_value"] is not None
    assert result["p_value"] >= 0.95  # P(shadow > control)


def test_clear_reject_worse_case():
    """Shadow is 20% worse than control → reject."""
    control, shadow = _make_rates_with_effect(control_mean=0.10, effect_pct=-20, n=20)
    result = compute_decision(control, shadow, exp_age_days=14)
    assert result["decision"] == "reject"
    assert "worse than control" in result["reason"]


def test_reject_no_improvement():
    """Shadow and control are statistically identical → reject (no improvement)."""
    control = _make_rates(mean=0.10, n=20, spread=0.03)
    shadow = list(control)  # identical
    result = compute_decision(control, shadow, exp_age_days=14)
    assert result["decision"] == "reject"
    assert "no significant improvement" in result["reason"]


# ═══════════════════════════════════════════════════════════════════
# Insufficient data cases
# ═══════════════════════════════════════════════════════════════════

def test_insufficient_data_under_give_up_continue():
    """Less than 5 posts, experiment younger than give_up_days → continue (wait)."""
    control = _make_rates(mean=0.10, n=3)
    shadow = _make_rates(mean=0.12, n=3)
    result = compute_decision(control, shadow, exp_age_days=5)
    assert result["decision"] == "continue"
    assert result["reason"] == "insufficient_data_extending"


def test_insufficient_data_after_give_up_reject():
    """Less than 5 posts, experiment past give_up_days → reject (gave up)."""
    control = _make_rates(mean=0.10, n=3)
    shadow = _make_rates(mean=0.12, n=3)
    result = compute_decision(control, shadow, exp_age_days=15)
    assert result["decision"] == "reject"
    assert result["reason"] == "insufficient_data_after_14d"


def test_give_up_days_is_configurable():
    """A longer window shifts the give-up point (make_decision passes window+7)."""
    control = _make_rates(mean=0.10, n=3)
    shadow = _make_rates(mean=0.12, n=3)
    result = compute_decision(control, shadow, exp_age_days=15, give_up_days=21)
    assert result["decision"] == "continue"


def test_insufficient_control_only():
    control = _make_rates(mean=0.10, n=4)
    shadow = _make_rates(mean=0.12, n=10)
    result = compute_decision(control, shadow, exp_age_days=5)
    assert result["decision"] == "continue"


def test_insufficient_shadow_only():
    control = _make_rates(mean=0.10, n=10)
    shadow = _make_rates(mean=0.12, n=4)
    result = compute_decision(control, shadow, exp_age_days=5)
    assert result["decision"] == "continue"


def test_empty_rates():
    result_young = compute_decision([], [], exp_age_days=3)
    assert result_young["decision"] == "continue"

    result_old = compute_decision([], [], exp_age_days=20)
    assert result_old["decision"] == "reject"
    assert result_old["reason"] == "insufficient_data_after_14d"


# ═══════════════════════════════════════════════════════════════════
# Boundary value tests (bootstrap criterion, ADR-0008)
# ═══════════════════════════════════════════════════════════════════

def test_small_but_certain_improvement_promotes():
    """+8% with tiny variance → P(shadow>control)≈1 and effect >= 5% → promote.

    This is the intended behavioral change vs the old criterion, which
    demanded >= 10% regardless of certainty.
    """
    n = 30
    control = [0.100 + (0.001 if i % 2 else -0.001) for i in range(n)]
    shadow = [0.108 + (0.001 if i % 2 else -0.001) for i in range(n)]
    result = compute_decision(control, shadow, exp_age_days=14)
    assert result["decision"] == "promote"


def test_tiny_improvement_below_min_effect_rejects():
    """+2% is below min_effect (5%) → reject even with high certainty."""
    n = 30
    control = [0.100 + (0.001 if i % 2 else -0.001) for i in range(n)]
    shadow = [0.102 + (0.001 if i % 2 else -0.001) for i in range(n)]
    result = compute_decision(control, shadow, exp_age_days=14)
    assert result["decision"] == "reject"
    assert "no significant improvement" in result["reason"]


def test_large_but_uncertain_improvement_rejects():
    """+15% mean but variance so high that P(shadow>control) < 0.95 → reject."""
    control = [0.02, 0.30, 0.05, 0.25, 0.08, 0.28, 0.03, 0.22, 0.06, 0.27]
    shadow = [c * 1.15 for c in control]
    random.seed(7)
    shadow = shadow[3:] + shadow[:3]  # decorrelate pairing
    result = compute_decision(control, shadow, exp_age_days=14)
    assert result["decision"] == "reject"


def test_min_effect_is_configurable():
    """Raising min_effect above the observed effect flips promote → reject."""
    control, shadow = _make_rates_with_effect(control_mean=0.10, effect_pct=8, n=30, noise=0.001)
    promoted = compute_decision(control, shadow, exp_age_days=14)
    assert promoted["decision"] == "promote"
    rejected = compute_decision(control, shadow, exp_age_days=14, min_effect=0.10)
    assert rejected["decision"] == "reject"


# ═══════════════════════════════════════════════════════════════════
# False positive rate regression test
# ═══════════════════════════════════════════════════════════════════

def test_false_positive_rate_under_5_percent():
    """With random data (no real effect), promote rate must stay low.

    This is the most important test: if decision_engine has a bug that
    promotes too aggressively, bad changes will reach production.

    200 synthetic experiments, both groups drawn from the same distribution.
    With promote_probability=0.95 the theoretical false positive rate is ~5%;
    allow up to 8% for test-level randomness.
    """
    random.seed(2024)
    n_experiments = 200
    n_posts_per_group = 15
    promote_count = 0

    for _ in range(n_experiments):
        control = [max(0.001, random.gauss(mu=0.10, sigma=0.02)) for _ in range(n_posts_per_group)]
        shadow = [max(0.001, random.gauss(mu=0.10, sigma=0.02)) for _ in range(n_posts_per_group)]

        result = compute_decision(control, shadow, exp_age_days=14)
        if result["decision"] == "promote":
            promote_count += 1

    false_positive_rate = promote_count / n_experiments
    assert false_positive_rate < 0.08, (
        f"False positive rate too high: {false_positive_rate:.1%} "
        f"({promote_count}/{n_experiments} promoted with no real effect). "
        f"Expected < 8%."
    )


def test_true_positive_rate_on_realistic_effect():
    """+30% real effect at n=28 (14-day window) must be detected most of the time.

    Guards against the original problem: a criterion so strict the autopilot
    can never promote anything.
    """
    random.seed(2025)
    n_experiments = 100
    n_posts_per_group = 28
    promote_count = 0

    for _ in range(n_experiments):
        control = [max(0.001, random.gauss(mu=0.10, sigma=0.02)) for _ in range(n_posts_per_group)]
        shadow = [max(0.001, random.gauss(mu=0.13, sigma=0.02)) for _ in range(n_posts_per_group)]

        result = compute_decision(control, shadow, exp_age_days=14)
        if result["decision"] == "promote":
            promote_count += 1

    assert promote_count / n_experiments > 0.8, (
        f"Power too low: only {promote_count}/{n_experiments} of +30% effects promoted"
    )


# ═══════════════════════════════════════════════════════════════════
# compute_engagement_rates helper tests
# ═══════════════════════════════════════════════════════════════════

def test_compute_engagement_rates_basic():
    posts = [
        {"latest_views": 100, "latest_subs": 1000},
        {"latest_views": 200, "latest_subs": 1000},
        {"latest_views": 150, "latest_subs": 1500},
    ]
    rates = compute_engagement_rates(posts)
    assert len(rates) == 3
    assert rates[0] == pytest.approx(0.1)
    assert rates[1] == pytest.approx(0.2)
    assert rates[2] == pytest.approx(0.1)


def test_compute_engagement_rates_handles_none():
    """Posts with None/0 subscribers should be skipped."""
    posts = [
        {"latest_views": None, "latest_subs": 1000},  # views=None→0, valid → 0.0
        {"latest_views": 100, "latest_subs": None},   # subs=None → skipped
        {"latest_views": 100, "latest_subs": 0},      # subs=0 → skipped
    ]
    rates = compute_engagement_rates(posts)
    assert len(rates) == 1
    assert rates[0] == pytest.approx(0.0)


def test_compute_engagement_rates_empty():
    assert compute_engagement_rates([]) == []


# ═══════════════════════════════════════════════════════════════════
# Return value structure tests
# ═══════════════════════════════════════════════════════════════════

def test_decision_result_has_all_required_fields():
    """Every decision result must have these keys for downstream consumers."""
    control = _make_rates(mean=0.10, n=10)
    shadow = _make_rates(mean=0.12, n=10)
    result = compute_decision(control, shadow, exp_age_days=7)

    required_keys = {"decision", "reason", "control_engagement", "shadow_engagement", "effect_size", "p_value"}
    assert set(result.keys()) == required_keys

    assert "mean" in result["control_engagement"]
    assert "n" in result["control_engagement"]
    assert "mean" in result["shadow_engagement"]
    assert "n" in result["shadow_engagement"]


def test_decision_result_insufficient_data_has_none_stats():
    """When data is insufficient, effect_size and p_value should be None."""
    control = _make_rates(mean=0.10, n=3)
    shadow = _make_rates(mean=0.12, n=3)
    result = compute_decision(control, shadow, exp_age_days=5)

    assert result["effect_size"] is None
    assert result["p_value"] is None


def test_decision_is_deterministic():
    """Same inputs → same decision and probability (seeded bootstrap)."""
    control, shadow = _make_rates_with_effect(control_mean=0.10, effect_pct=12, n=20)
    r1 = compute_decision(control, shadow, exp_age_days=14)
    r2 = compute_decision(control, shadow, exp_age_days=14)
    assert r1 == r2
