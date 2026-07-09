"""Tests for the composite reward (issue #37, ADR-0010).

Hermetic: the reward module reads via ``fetch_all`` (subscriber series + the
all-posted boundaries) and ``get_snapshot_at_horizon`` from the PG layer. These
tests seed an in-memory fake and patch those names so no PostgreSQL (or SQLite)
is needed. Pure reward math tests need no fixture.

Covers: pure reward math, Δsubs attribution edge cases (2 posts/day window
clipping, overnight growth, churn), subscriber-series interpolation, click
degradation when PostgreSQL is unreachable, and the decision-engine /
bandit integration.
"""
import sys
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import reward as reward_mod
from aibp.self_learning.reward import (
    DEFAULT_REWARD_WEIGHTS,
    compute_post_reward,
    compute_rewards_for_posts,
    compute_subs_delta,
    subs_at,
)

NOW = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


class RewardFake:
    """In-memory stand-in for the PG reads reward.compute_rewards_for_posts uses.

    Holds posts + snapshots; supplies the subscriber series, the all-posted
    boundary list, and horizon snapshots exactly as the PG layer would return
    them (as plain dicts).
    """

    def __init__(self):
        self.snapshots: dict[int, list[dict]] = {}
        self.posts: dict[int, dict] = {}

    def add_post(self, feed_item_id, posted_at, channel="main"):
        self.posts[feed_item_id] = {"feed_item_id": feed_item_id,
                                    "posted_at": posted_at, "target_channel": channel}

    def add_snapshot(self, feed_item_id, measured_at, views, subs=1000, forwards=0):
        self.snapshots.setdefault(feed_item_id, []).append({
            "views": views, "forwards": forwards, "replies": 0,
            "reactions_count": 0, "subscribers_at": subs,
            "measured_at": measured_at,
        })

    # ── patched helpers ─────────────────────────────────────────────
    def load_subscriber_series(self):
        rows = []
        for fid, snaps in self.snapshots.items():
            if self.posts.get(fid, {}).get("target_channel") != "main":
                continue
            for s in snaps:
                if s["subscribers_at"] is not None:
                    rows.append((s["measured_at"], s["subscribers_at"]))
        rows.sort()
        return rows

    def all_posted_main(self):
        return [p["posted_at"] for p in self.posts.values()
                if p["target_channel"] == "main"]

    def snapshot_at_horizon(self, feed_item_id, hours=48):
        snaps = self.snapshots.get(feed_item_id)
        if not snaps:
            return None
        post = self.posts.get(feed_item_id)
        if post is None:
            return None
        posted_at = post["posted_at"]
        horizon = posted_at + timedelta(hours=hours)
        # nearest snapshot to posted_at + hours
        nearest = min(snaps, key=lambda s: abs(s["measured_at"] - horizon))
        return dict(nearest)

    def fetch_all(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if "SELECT em.measured_at, em.subscribers_at" in sql_stripped:
            return [{"measured_at": t, "subscribers_at": v} for t, v in self.load_subscriber_series()]
        if "SELECT posted_at FROM post_features WHERE target_channel = 'main'" in sql_stripped:
            return [{"posted_at": t} for t in self.all_posted_main()]
        raise AssertionError(f"unexpected fetch_all: {sql!r}")

    def patch_all(self):
        return [
            patch.object(reward_mod, "fetch_all", self.fetch_all),
            patch.object(reward_mod, "load_subscriber_series", self.load_subscriber_series),
            patch.object(reward_mod, "get_snapshot_at_horizon", self.snapshot_at_horizon),
        ]


@pytest.fixture()
def fake():
    return RewardFake()


@contextmanager
def patched(fake):
    """Apply RewardFake's read patches for the duration of the block."""
    with ExitStack() as stack:
        for p in fake.patch_all():
            stack.enter_context(p)
        yield


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


def test_rewards_for_posts_end_to_end(fake):
    fake.add_post(1, NOW)
    fake.add_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1000, forwards=2)
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]

    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks), patched(fake):
        scored = compute_rewards_for_posts(posts, policy={})

    assert len(scored) == 1
    p = scored[0]
    assert p["views"] == 300
    assert p["forwards"] == 2
    assert p["clicks"] == 0
    assert p["reward"] == pytest.approx(
        (300 * 1.0 + 2 * 25.0) / 1000 + p["reward_components"]["subs_delta"]
    )


def test_rewards_skips_posts_without_snapshot(fake):
    fake.add_post(1, NOW)  # no snapshot
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]
    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks), patched(fake):
        assert compute_rewards_for_posts(posts, policy={}) == []


def test_rewards_window_clipped_by_unscored_neighbor(fake):
    """The next-post boundary must come from ALL main posts, even ones not
    in the scored subset (interleaving: control clips variant's window)."""
    evening = NOW + timedelta(hours=8)
    fake.add_post(1, NOW)
    fake.add_post(2, evening)  # different policy — not passed in, still clips
    fake.add_snapshot(1, NOW, views=0, subs=1000)
    fake.add_snapshot(1, NOW + timedelta(hours=8), views=200, subs=1004)
    fake.add_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1020)
    fake.add_snapshot(2, NOW + timedelta(hours=48), views=100, subs=1020)

    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]
    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks), patched(fake):
        scored = compute_rewards_for_posts(posts, policy={})

    assert scored[0]["subs_delta"] == pytest.approx(4)  # 1000→1004, not →1020


def test_clicks_degrade_gracefully_when_pg_down(fake):
    """PG unreachable → clicks component is 0 for all posts, reward still computed."""
    fake.add_post(1, NOW)
    fake.add_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1000)
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]
    # No click patching: fetch_clicks_at_horizon hits real PG config and fails → {}
    with patched(fake):
        scored = compute_rewards_for_posts(posts, policy={})
    assert len(scored) == 1
    assert scored[0]["clicks"] == 0


# ═══════════════════════════════════════════════════════════════════
# Integration: decision engine + bandit use the composite reward
# ═══════════════════════════════════════════════════════════════════

def test_decision_engine_reward_rates(fake):
    from aibp.self_learning.decision_engine import compute_reward_rates

    fake.add_post(1, NOW)
    fake.add_snapshot(1, NOW + timedelta(hours=48), views=300, subs=1000, forwards=2)
    posts = [{"feed_item_id": 1, "posted_at": NOW.isoformat()}]

    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks), patched(fake):
        rates = compute_reward_rates(posts, policy={})

    assert len(rates) == 1
    assert rates[0] > 0.3  # views alone give 0.3; forwards push it above


def test_bandit_scores_posts_by_composite_reward(fake):
    from aibp.self_learning.bandit import _load_scored_posts

    posted = datetime.now(UTC) - timedelta(hours=72)
    fake.add_post(1, posted, channel="main")
    fake.add_snapshot(1, posted + timedelta(hours=48), views=300, subs=1000, forwards=4)

    with patch.object(reward_mod, "fetch_clicks_at_horizon", side_effect=_no_clicks), \
         patch("aibp.self_learning.bandit.fetch_all", return_value=[
             {"feed_item_id": 1, "posted_at": posted.isoformat(), "strategy_rubric": "anti_hype"}]), \
         patched(fake):
        scored = _load_scored_posts()

    assert len(scored) == 1
    assert scored[0]["rate"] == scored[0]["reward"]
    assert scored[0]["rate"] == pytest.approx((300 + 4 * 25.0) / 1000)
