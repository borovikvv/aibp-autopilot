"""Tests for the Thompson sampling bandit (issue #18).

Hermetic: the bandit reads/writes via ``execute``/``fetch_all`` from
``aibp.db.connection``. These tests patch those names on the ``bandit`` module
with an in-memory fake so no PostgreSQL (or SQLite) is needed.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import pytest

from aibp.self_learning import bandit
from aibp.self_learning import db as sl_db


class FakePG:
    """Minimal in-memory stand-in for the PostgreSQL helper layer.

    Only the statements the bandit/offer code issues are understood; anything
    else raises so we notice if the code drifts.
    """

    def __init__(self):
        self.arms: dict[tuple[str, str], dict] = {}
        self.observations: set[tuple[int, str]] = set()
        self.scored_posts: list[dict] = []

    def execute(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("INSERT INTO bandit_arms") and "DO NOTHING" in sql_stripped:
            dim = params[0]
            arm = params[1]
            self.arms.setdefault((dim, arm), {"alpha": 1.0, "beta": 1.0})
            return 1
        if sql_stripped.startswith("UPDATE bandit_arms SET alpha = alpha"):
            dim = params[3]
            arm = params[4]
            a = self.arms.setdefault((dim, arm), {"alpha": 1.0, "beta": 1.0})
            a["alpha"] += params[0]
            a["beta"] += params[1]
            return 1
        if sql_stripped.startswith("INSERT INTO bandit_observations") and "DO NOTHING" in sql_stripped:
            feed_item_id = params[0]
            dim = params[1]
            self.observations.add((feed_item_id, dim))
            return 1
        raise AssertionError(f"unexpected execute: {sql!r}")

    def fetch_all(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("SELECT arm_id, alpha, beta FROM bandit_arms"):
            dim = params[0]
            return [{"arm_id": arm, "alpha": r["alpha"], "beta": r["beta"]}
                    for (d, arm), r in self.arms.items() if d == dim]
        if sql_stripped.startswith("SELECT feed_item_id FROM bandit_observations"):
            dim = params[0]
            return [{"feed_item_id": fid} for (fid, d) in self.observations if d == dim]
        raise AssertionError(f"unexpected fetch_all: {sql!r}")

    def fetch_one(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("SELECT 1 FROM bandit_observations"):
            feed_item_id, dim = params[0], params[1]
            return {"?column?": 1} if (feed_item_id, dim) in self.observations else None
        raise AssertionError(f"unexpected fetch_one: {sql!r}")


@pytest.fixture()
def fake_pg():
    pg = FakePG()
    with patch.object(bandit, "execute", pg.execute), \
         patch.object(bandit, "fetch_all", pg.fetch_all), \
         patch.object(bandit, "get_arms", wraps=bandit.get_arms):
        yield pg


# ═══════════════════════════════════════════════════════════════════
# Posterior state
# ═══════════════════════════════════════════════════════════════════

def test_ensure_arms_creates_uniform_priors(fake_pg):
    bandit.ensure_arms("rubric", ["a", "b"])
    arms = bandit.get_arms("rubric")
    assert arms == {"a": (1.0, 1.0), "b": (1.0, 1.0)}


def test_ensure_arms_does_not_reset_existing(fake_pg):
    bandit.ensure_arms("rubric", ["a"])
    bandit.record_outcome("rubric", "a", success=True)
    bandit.ensure_arms("rubric", ["a", "b"])
    arms = bandit.get_arms("rubric")
    assert arms["a"] == (2.0, 1.0)  # not reset
    assert arms["b"] == (1.0, 1.0)


def test_record_outcome_updates_posterior(fake_pg):
    bandit.record_outcome("rubric", "a", success=True)
    bandit.record_outcome("rubric", "a", success=True)
    bandit.record_outcome("rubric", "a", success=False)
    alpha, beta = bandit.get_arms("rubric")["a"]
    assert (alpha, beta) == (3.0, 2.0)


def test_multipliers_are_bounded(fake_pg):
    rubrics = ["a", "b", "c"]
    for _ in range(50):
        multipliers = bandit.sample_rubric_multipliers(rubrics)
        assert set(multipliers) == set(rubrics)
        for m in multipliers.values():
            assert 0.5 <= m <= 1.5


# ═══════════════════════════════════════════════════════════════════
# Convergence on synthetic data
# ═══════════════════════════════════════════════════════════════════

def test_bandit_converges_to_best_arm(fake_pg):
    """Two arms with success probabilities 0.7 vs 0.3: after a few hundred
    Thompson-driven pulls, the good arm dominates both in posterior mean
    and in pull count."""
    rng = np.random.default_rng(2026)
    true_probs = {"good": 0.7, "bad": 0.3}
    arms = list(true_probs)
    bandit.ensure_arms("rubric", arms)

    pulls = {a: 0 for a in arms}
    for _ in range(300):
        samples = bandit.sample_thompson("rubric", arms, rng=rng)
        chosen = max(samples, key=samples.get)
        pulls[chosen] += 1
        success = rng.random() < true_probs[chosen]
        bandit.record_outcome("rubric", chosen, success)

    posteriors = bandit.get_arms("rubric")
    mean = {a: alpha / (alpha + beta) for a, (alpha, beta) in posteriors.items()}
    assert mean["good"] > mean["bad"]
    assert pulls["good"] > 0.6 * (pulls["good"] + pulls["bad"]), (
        f"good arm pulled only {pulls['good']} of {sum(pulls.values())} times"
    )


# ═══════════════════════════════════════════════════════════════════
# Outcome collection from engagement data
# ═══════════════════════════════════════════════════════════════════

def _make_post(feed_item_id: int, rubric: str, reward: float) -> dict:
    """A scored post as compute_rewards_for_posts would return it."""
    posted = datetime.now(UTC) - timedelta(days=10)
    return {"feed_item_id": feed_item_id, "posted_at": posted.isoformat(),
            "strategy_rubric": rubric, "rate": reward, "reward": reward}


def _stub_load_scored_posts(posts):
    """Patch bandit._load_scored_posts to return canned scored posts.

    _load_scored_posts normally runs a SELECT then compute_rewards_for_posts;
    for unit testing the scoring logic itself we bypass PG entirely.
    """
    return patch.object(bandit, "_load_scored_posts", return_value=posts)


def test_update_from_engagement_scores_against_trailing_median(fake_pg):
    # 6 baseline posts at reward 0.10, then one strong (0.20) and one weak (0.05)
    posts = [_make_post(i + 1, "baseline_rubric", 0.10) for i in range(6)]
    posts.append(_make_post(7, "strong_rubric", 0.20))
    posts.append(_make_post(8, "weak_rubric", 0.05))

    with _stub_load_scored_posts(posts):
        recorded = bandit.update_from_engagement()
    assert recorded >= 2

    arms = bandit.get_arms("rubric")
    s_alpha, s_beta = arms["strong_rubric"]
    w_alpha, w_beta = arms["weak_rubric"]
    assert (s_alpha, s_beta) == (2.0, 1.0)  # one success
    assert (w_alpha, w_beta) == (1.0, 2.0)  # one failure


def test_update_from_engagement_is_idempotent(fake_pg):
    posts = [_make_post(i + 1, "r", 0.10) for i in range(6)]
    posts.append(_make_post(7, "r", 0.20))

    with _stub_load_scored_posts(posts):
        first = bandit.update_from_engagement()
        second = bandit.update_from_engagement()
    assert first >= 1
    assert second == 0  # nothing new to record


def test_update_skips_posts_without_baseline(fake_pg):
    """Fewer than MIN_BASELINE_POSTS preceding posts → no observation."""
    posts = [_make_post(1, "r", 0.10), _make_post(2, "r", 0.20)]
    with _stub_load_scored_posts(posts):
        assert bandit.update_from_engagement() == 0


# NOTE (issue #43): the 48h engagement horizon filter is enforced by an SQL
# condition inside `_load_scored_posts()` (the `WHERE posted_at <= now() -
# interval '<ENGAGEMENT_HORIZON_HOURS> hours'` clause in bandit.py), not by
# Python logic. It therefore cannot be meaningfully unit-tested here — the
# in-memory FakePG would just echo back whatever we stubbed — and is covered
# instead by integration tests that run against PostgreSQL.

# references kept to satisfy import analyzers; sl_db is the module under test
_ = sl_db
