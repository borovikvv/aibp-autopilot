"""Tests for the Thompson sampling bandit (issue #18)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import pytest

from aibp.self_learning import db as sl_db
from aibp.self_learning import bandit


@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield


# ═══════════════════════════════════════════════════════════════════
# Posterior state
# ═══════════════════════════════════════════════════════════════════

def test_ensure_arms_creates_uniform_priors(temp_db):
    bandit.ensure_arms("rubric", ["a", "b"])
    arms = bandit.get_arms("rubric")
    assert arms == {"a": (1.0, 1.0), "b": (1.0, 1.0)}


def test_ensure_arms_does_not_reset_existing(temp_db):
    bandit.ensure_arms("rubric", ["a"])
    bandit.record_outcome("rubric", "a", success=True)
    bandit.ensure_arms("rubric", ["a", "b"])
    arms = bandit.get_arms("rubric")
    assert arms["a"] == (2.0, 1.0)  # not reset
    assert arms["b"] == (1.0, 1.0)


def test_record_outcome_updates_posterior(temp_db):
    bandit.record_outcome("rubric", "a", success=True)
    bandit.record_outcome("rubric", "a", success=True)
    bandit.record_outcome("rubric", "a", success=False)
    alpha, beta = bandit.get_arms("rubric")["a"]
    assert (alpha, beta) == (3.0, 2.0)


def test_multipliers_are_bounded(temp_db):
    rubrics = ["a", "b", "c"]
    for _ in range(50):
        multipliers = bandit.sample_rubric_multipliers(rubrics)
        assert set(multipliers) == set(rubrics)
        for m in multipliers.values():
            assert 0.5 <= m <= 1.5


# ═══════════════════════════════════════════════════════════════════
# Convergence on synthetic data
# ═══════════════════════════════════════════════════════════════════

def test_bandit_converges_to_best_arm(temp_db):
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

def _insert_post(feed_item_id: int, rubric: str, posted_days_ago: float,
                 views: int, subs: int = 1000) -> None:
    posted = datetime.now(timezone.utc) - timedelta(days=posted_days_ago)
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO post_features
                (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                 strategy_rubric, policy_version, policy_blob)
            VALUES (?, ?, 'morning', 'prod', 'main', ?, 'v1', '{}')
            """,
            (feed_item_id, posted.isoformat(), rubric),
        )
        conn.execute(
            """
            INSERT INTO engagement_metrics (feed_item_id, measured_at, views, subscribers_at)
            VALUES (?, ?, ?, ?)
            """,
            (feed_item_id, (posted + timedelta(hours=48)).isoformat(), views, subs),
        )


def test_update_from_engagement_scores_against_trailing_median(temp_db):
    # 6 baseline posts at 100 views, then one strong (200) and one weak (50)
    for i in range(6):
        _insert_post(i + 1, "baseline_rubric", posted_days_ago=20 - i, views=100)
    _insert_post(7, "strong_rubric", posted_days_ago=10, views=200)
    _insert_post(8, "weak_rubric", posted_days_ago=9, views=50)

    recorded = bandit.update_from_engagement()
    assert recorded > 0

    arms = bandit.get_arms("rubric")
    s_alpha, s_beta = arms["strong_rubric"]
    w_alpha, w_beta = arms["weak_rubric"]
    assert (s_alpha, s_beta) == (2.0, 1.0)  # one success
    assert (w_alpha, w_beta) == (1.0, 2.0)  # one failure


def test_update_from_engagement_is_idempotent(temp_db):
    for i in range(6):
        _insert_post(i + 1, "r", posted_days_ago=20 - i, views=100)
    _insert_post(7, "r", posted_days_ago=10, views=200)

    first = bandit.update_from_engagement()
    second = bandit.update_from_engagement()
    assert first > 0
    assert second == 0  # nothing new to record


def test_update_skips_posts_without_baseline(temp_db):
    """Fewer than MIN_BASELINE_POSTS preceding posts → no observation."""
    _insert_post(1, "r", posted_days_ago=5, views=100)
    _insert_post(2, "r", posted_days_ago=4, views=200)
    assert bandit.update_from_engagement() == 0


def test_fresh_posts_below_horizon_not_scored(temp_db):
    for i in range(6):
        _insert_post(i + 1, "r", posted_days_ago=20 - i, views=100)
    _insert_post(7, "r", posted_days_ago=0.5, views=999)  # younger than 48h

    bandit.update_from_engagement()
    with sl_db.sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT feed_item_id FROM bandit_observations"
        ).fetchall()
    assert 7 not in {r["feed_item_id"] for r in rows}
