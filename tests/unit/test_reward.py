"""Tests for the composite reward (issue #37, ADR-0010).

Covers: pure reward math, Δsubs attribution edge cases (2 posts/day window
clipping, overnight growth, churn), subscriber-series interpolation, click
degradation when PostgreSQL is unreachable, and the decision-engine /
bandit integration.
"""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import db as sl_db
from aibp.self_learning import reward as reward_mod
from aibp.self_learning.reward import (
    DEFAULT_REWARD_WEIGHTS,
    compute_post_reward,
    compute_rewards_for_posts,
    compute_subs_delta,
    subs_at,
)


@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield


NOW = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def _insert_post(feed_item_id: int, posted_at: datetime, channel: str = "main",
                 rubric: str = "anti_hype") -> None:
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO post_features
                (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                 strategy_rubric, policy_version, policy_blob)
            VALUES (?, ?, 'morning', 'prod', ?, ?, 'v1', '{}')
            """,
            (feed_item_id, posted_at.isoformat(), channel, rubric),
        )


def _insert_snapshot(feed_item_id: int, measured_at: datetime, views: int,
                     subs: int = 1000, forwards: int = 0) -> None:
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO engagement_metrics
                (feed_item_id, measured_at, views, forwards, subscribers_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (feed_item_id, measured_at.isoformat(), views, forwards, subs),
        )


# ═══════════════════════════════════════════════════════════════════
# subs_at — interpolation
# ═══════════════════════════════════════════════════════════════════

def test_subs_at_interpolates_linearly():
    series = [(NOW, 1000), (NOW + timedelta(hours=10), 1100)]
    assert subs_at(series, NOW + timedelta(hours=5)) == 1050


def test_subs_at_clamps_outside_range():
    series = [(NOW, 1000), (NOW + timedelta(hours=10), 1100)]
    assert subs_at(series, NOW - timedelta(days=1)) == 1000
    assert subs_at(series, NOW + timedelta(days=1)) == 1100


def test_subs_at_empty_series():
    assert subs_at([], NOW) is None


# ═══════════════════════════════════════════════════════════════════
# compute_subs_delta — attribution edge cases
# ═══════════════════════════════════════════════════════════════════

def test_subs_delta_full_window():
    series = [(NOW, 1000), (NOW + timedelta(hours=24), 1010)]
    delta = compute_subs_delta(NOW, series, attribution_hours=24)
    assert delta == 10


def test_subs_delta_clipped_by_next_post():
    """2 posts/day: the morning post's 24h window must stop at the evening
    post — growth after 18:00 belongs to the evening post, not both."""
    series = [
        (NOW, 1000),                            # 10:00, morning post
        (NOW + timedelta(hours=8), 1004),       # 18:00, evening post
        (NOW + timedelta(hours=24), 1020),      # next morning
    ]
    evening = NOW + timedelta(hours=8)
    morning_delta = compute_subs_delta(NOW, series, 24, next_post_at=evening)
    assert morning_delta == 4  # only 10:00→18:00, not the full +20


def test_subs_delta_overnight_growth_credited_to_evening_post():
    """Growth between the evening post and the next morning post goes to
    the evening post — its window reaches the morning post."""
    evening = NOW + timedelta(hours=8)          # 18:00
    next_morning = NOW + timedelta(hours=24)    # 10:00 next day
    series = [(evening, 1004), (next_morning, 1020)]
    delta = compute_subs_delta(evening, series, 24, next_post_at=next_morning)
    assert delta == 16


def test_subs_delta_negative_churn_is_kept():
    series = [(NOW, 1000), (NOW + timedelta(hours=24), 980)]
    assert compute_subs_delta(NOW, series, 24) == -20


def test_subs_delta_empty_series_is_none():
    assert compute_subs_delta(NOW, [], 24) is None


def test_subs_delta_next_post_before_posted_at_gives_zero():
    series = [(NOW, 1000), (NOW + timedelta(hours=24), 1010)]
    assert compute_subs_delta(NOW, series, 24, next_post_at=NOW) == 0.0


# ═══════════════════════════════════════════════════════════════════
# compute_post_reward — pure math
# ═══════════════════════════════════════════════════════════════════

def test_reward_components_sum():
    result = compute_post_reward(
        views=300, forwards=2, clicks=5, subs_delta=4, subscribers=1000,
        weights=DEFAULT_REWARD_WEIGHTS,
    )
    c = result["components"]
    assert c["views"] == pytest.approx(0.3)
    assert c["forwards"] == pytest.approx(2 * 25.0 / 1000)
    assert c["clicks"] == pytest.approx(5 * 15.0 / 1000)
    assert c["subs_delta"] == pytest.approx(4 * 50.0 / 1000)
    assert result["reward"] == pytest.approx(sum(c.values()))


def test_reward_none_without_subscribers():
    assert compute_post_reward(300, 0, 0, 0, 0, DEFAULT_REWARD_WEIGHTS) is None
    assert compute_post_reward(300, 0, 0, None, 0, DEFAULT_REWARD_WEIGHTS) is None


def test_reward_missing_subs_delta_counts_as_zero():
    with_none = compute_post_reward(300, 0, 0, None, 1000, DEFAULT_REWARD_WEIGHTS)
    with_zero = compute_post_reward(300, 0, 0, 0, 1000, DEFAULT_REWARD_WEIGHTS)
    assert with_none["reward"] == with_zero["reward"]


def test_reward_can_go_negative_on_heavy_churn():
    result = compute_post_reward(100, 0, 0, -50, 1000, DEFAULT_REWARD_WEIGHTS)
    assert result["reward"] < 0


# ═══════════════════════════════════════════════════════════════════
# compute_rewards_for_posts — I/O wrapper
# ═══════════════════════════════════════════════════════════════════

def _no_clicks(items, horizon_hours=48):
    return {}


def test_rewards_for_posts_end_to_end(temp_db):
    _insert_post(1, NOW)
    _insert_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1000, forwards=2)
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]

    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks):
        scored = compute_rewards_for_posts(posts, policy={})

    assert len(scored) == 1
    p = scored[0]
    assert p["views"] == 300
    assert p["forwards"] == 2
    assert p["clicks"] == 0
    assert p["reward"] == pytest.approx(
        (300 * 1.0 + 2 * 25.0) / 1000 + p["reward_components"]["subs_delta"]
    )


def test_rewards_skips_posts_without_snapshot(temp_db):
    _insert_post(1, NOW)  # no snapshot
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]
    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks):
        assert compute_rewards_for_posts(posts, policy={}) == []


def test_rewards_window_clipped_by_unscored_neighbor(temp_db):
    """The next-post boundary must come from ALL main posts, even ones not
    in the scored subset (interleaving: control clips variant's window)."""
    evening = NOW + timedelta(hours=8)
    _insert_post(1, NOW)
    _insert_post(2, evening)  # different policy — not passed in, still clips
    _insert_snapshot(1, NOW, views=0, subs=1000)
    _insert_snapshot(1, NOW + timedelta(hours=8), views=200, subs=1004)
    _insert_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1020)
    _insert_snapshot(2, NOW + timedelta(hours=48), views=100, subs=1020)

    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]
    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks):
        scored = compute_rewards_for_posts(posts, policy={})

    assert scored[0]["subs_delta"] == pytest.approx(4)  # 1000→1004, not →1020


def test_clicks_degrade_gracefully_when_pg_down(temp_db):
    """PG unreachable → clicks component is 0 for all posts, reward still computed."""
    _insert_post(1, NOW)
    _insert_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1000)
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]
    # No patching: fetch_clicks_at_horizon hits real PG config and fails → {}
    scored = compute_rewards_for_posts(posts, policy={})
    assert len(scored) == 1
    assert scored[0]["clicks"] == 0


# ═══════════════════════════════════════════════════════════════════
# Integration: decision engine + bandit use the composite reward
# ═══════════════════════════════════════════════════════════════════

def test_decision_engine_reward_rates(temp_db):
    from aibp.self_learning.decision_engine import compute_reward_rates

    _insert_post(1, NOW)
    _insert_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1000, forwards=2)
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]

    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks):
        rates = compute_reward_rates(posts, policy={})

    assert len(rates) == 1
    assert rates[0] > 0.3  # views alone give 0.3; forwards push it above


def test_bandit_scores_posts_by_composite_reward(temp_db):
    from aibp.self_learning.bandit import _load_scored_posts

    posted = datetime.now(UTC) - timedelta(hours=72)
    _insert_post(1, posted, rubric="anti_hype")
    _insert_snapshot(1, posted + timedelta(hours=48), views=300, subs=1000, forwards=4)

    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks):
        scored = _load_scored_posts()

    assert len(scored) == 1
    assert scored[0]["rate"] == scored[0]["reward"]
    assert scored[0]["rate"] == pytest.approx((300 + 4 * 25.0) / 1000)
