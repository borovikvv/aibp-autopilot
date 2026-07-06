"""Tests for fixed-horizon engagement snapshots (issue #14, time-decay bias).

Covers get_snapshot_at_horizon boundary cases and the regression scenario:
two posts of equal quality but different ages must show equal engagement
rate at the fixed horizon.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import db as sl_db
from aibp.self_learning.db import get_snapshot_at_horizon


@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield


def _insert_post(feed_item_id: int, posted_at: datetime,
                 policy_version: str = "v1", channel: str = "main") -> None:
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO post_features
                (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                 policy_version, policy_blob)
            VALUES (?, ?, 'morning', 'prod', ?, ?, '{}')
            """,
            (feed_item_id, posted_at.isoformat(), channel, policy_version),
        )


def _insert_snapshot(feed_item_id: int, measured_at: datetime,
                     views: int, subs: int = 1000) -> None:
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO engagement_metrics (feed_item_id, measured_at, views, subscribers_at)
            VALUES (?, ?, ?, ?)
            """,
            (feed_item_id, measured_at.isoformat(), views, subs),
        )


NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# get_snapshot_at_horizon boundary cases
# ═══════════════════════════════════════════════════════════════════

def test_exact_horizon_snapshot_is_chosen(temp_db):
    posted = NOW - timedelta(hours=100)
    _insert_post(1, posted)
    _insert_snapshot(1, posted + timedelta(hours=4), views=50)
    _insert_snapshot(1, posted + timedelta(hours=48), views=100)
    _insert_snapshot(1, posted + timedelta(hours=96), views=180)

    snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 100


def test_no_snapshot_at_horizon_takes_nearest_before(temp_db):
    """Only earlier snapshots exist → the closest one before is used."""
    posted = NOW - timedelta(hours=100)
    _insert_post(1, posted)
    _insert_snapshot(1, posted + timedelta(hours=4), views=50)
    _insert_snapshot(1, posted + timedelta(hours=40), views=90)

    snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 90


def test_no_snapshot_at_horizon_takes_nearest_after(temp_db):
    """Only later snapshots exist → the closest one after is used."""
    posted = NOW - timedelta(hours=100)
    _insert_post(1, posted)
    _insert_snapshot(1, posted + timedelta(hours=60), views=130)
    _insert_snapshot(1, posted + timedelta(hours=96), views=180)

    snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 130


def test_nearest_wins_across_both_sides(temp_db):
    """44h and 56h snapshots exist → 44h is closer to the 48h horizon."""
    posted = NOW - timedelta(hours=100)
    _insert_post(1, posted)
    _insert_snapshot(1, posted + timedelta(hours=44), views=95)
    _insert_snapshot(1, posted + timedelta(hours=56), views=120)

    snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 95


def test_no_snapshots_returns_none(temp_db):
    _insert_post(1, NOW - timedelta(hours=100))
    assert get_snapshot_at_horizon(1, hours=48) is None


def test_unknown_post_returns_none(temp_db):
    assert get_snapshot_at_horizon(999, hours=48) is None


# ═══════════════════════════════════════════════════════════════════
# Regression: equal-quality posts of different ages → equal rates
# ═══════════════════════════════════════════════════════════════════

def test_equal_quality_posts_of_different_ages_have_equal_rates(temp_db):
    """An old post accumulated more total views, but at the 48h horizon both
    posts are identical — MAX/last would have made the old post 'win'."""
    from aibp.self_learning.decision_engine import (
        get_engagement_for_policy_version,
        compute_engagement_rates,
    )

    old_posted = NOW - timedelta(days=10)
    new_posted = NOW - timedelta(days=3)

    _insert_post(1, old_posted, policy_version="v1")
    _insert_snapshot(1, old_posted + timedelta(hours=48), views=100)
    _insert_snapshot(1, old_posted + timedelta(hours=120), views=180)  # decayed growth

    _insert_post(2, new_posted, policy_version="v1")
    _insert_snapshot(2, new_posted + timedelta(hours=48), views=100)

    posts = get_engagement_for_policy_version("v1")
    rates = compute_engagement_rates(posts)

    assert len(rates) == 2
    assert rates[0] == pytest.approx(rates[1])


def test_pattern_miner_uses_horizon_snapshot(temp_db):
    """pattern_miner.load_post_data must not pick the last snapshot."""
    from aibp.self_learning.pattern_miner import load_post_data

    posted = NOW - timedelta(days=5)
    _insert_post(1, posted)
    _insert_snapshot(1, posted + timedelta(hours=48), views=100)
    _insert_snapshot(1, posted + timedelta(hours=110), views=170)

    with patch("aibp.self_learning.pattern_miner.datetime") as mock_dt:
        mock_dt.now.return_value = NOW
        posts = load_post_data(days=7)

    assert len(posts) == 1
    assert posts[0]["latest_views"] == 100
