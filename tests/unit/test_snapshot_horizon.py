"""Tests for fixed-horizon engagement snapshots (issue #14, time-decay bias).

Hermetic: ``get_snapshot_at_horizon`` (and the decision-engine / pattern-miner
helpers that call it) read via the PG ``fetch_one``/``fetch_all`` helpers. These
tests seed an in-memory store and patch those names on every module that bound
them, so no PostgreSQL (or SQLite) is needed.

Covers get_snapshot_at_horizon boundary cases and the regression scenario:
two posts of equal quality but different ages must show equal engagement
rate at the fixed horizon.
"""
import contextlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import db as sl_db
from aibp.self_learning.db import get_snapshot_at_horizon

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


class HorizonFake:
    """In-memory posts + snapshots; supplies the nearest-snapshot SELECT that
    get_snapshot_at_horizon issues, plus the post_features SELECTs used by the
    decision engine and pattern miner."""

    def __init__(self):
        self.posts: dict[int, dict] = {}
        self.snapshots: dict[int, list[dict]] = {}

    def add_post(self, feed_item_id, posted_at, policy_version="v1", channel="main",
                 rubric="anti_hype"):
        self.posts[feed_item_id] = {
            "feed_item_id": feed_item_id, "posted_at": posted_at,
            "slot": "morning", "target_channel": channel,
            "policy_version": policy_version, "strategy_rubric": rubric,
        }

    def add_snapshot(self, feed_item_id, measured_at, views, subs=1000):
        self.snapshots.setdefault(feed_item_id, []).append({
            "views": views, "forwards": 0, "replies": 0, "reactions_count": 0,
            "subscribers_at": subs, "measured_at": measured_at,
        })

    # ── PG helper stand-ins ─────────────────────────────────────────
    def fetch_one(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if "FROM engagement_metrics em" in sql_stripped and "ABS(EXTRACT(EPOCH" in sql_stripped:
            feed_item_id = params[0]
            hours = float(params[1])
            post = self.posts.get(feed_item_id)
            snaps = self.snapshots.get(feed_item_id)
            if not post or not snaps:
                return None
            horizon = post["posted_at"] + timedelta(hours=hours)
            nearest = min(snaps, key=lambda s: abs(s["measured_at"] - horizon))
            return dict(nearest)
        raise AssertionError(f"unexpected fetch_one: {sql!r}")

    def fetch_all(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        # get_engagement_for_policy_version
        if "SELECT pf.feed_item_id, pf.posted_at, pf.slot, pf.target_channel" in sql_stripped \
                and "pf.policy_version = %s" in sql_stripped:
            pv = params[0]
            rows = [dict(p) for p in self.posts.values()
                    if p["policy_version"] == pv and p["target_channel"] == "main"]
            # optional `since` param
            if len(params) > 1 and params[1] is not None:
                since = params[1]
                since_dt = _as_dt(since)
                rows = [p for p in rows if _as_dt(p["posted_at"]) >= since_dt]
            return rows
        # reward.compute_rewards_for_posts all-posted boundaries
        if "SELECT posted_at FROM post_features WHERE target_channel = 'main'" in sql_stripped:
            return [{"posted_at": p["posted_at"]} for p in self.posts.values()
                    if p["target_channel"] == "main"]
        # reward subscriber series
        if "SELECT em.measured_at, em.subscribers_at" in sql_stripped:
            out = []
            for fid, snaps in self.snapshots.items():
                if self.posts.get(fid, {}).get("target_channel") != "main":
                    continue
                for s in snaps:
                    if s["subscribers_at"] is not None:
                        out.append({"measured_at": s["measured_at"],
                                    "subscribers_at": s["subscribers_at"]})
            out.sort(key=lambda r: r["measured_at"])
            return out
        # pattern_miner.load_post_data
        if "pf.target_channel = 'main'" in sql_stripped and "pf.posted_at >= %s" in sql_stripped:
            since = _as_dt(params[0])
            return [dict(p) for p in self.posts.values()
                    if p["target_channel"] == "main" and _as_dt(p["posted_at"]) >= since]
        raise AssertionError(f"unexpected fetch_all: {sql!r}")

    def patches(self):
        """Patch fetch_one/fetch_all on every module that bound them."""
        from aibp.self_learning import decision_engine, pattern_miner, reward
        targets = [
            (sl_db, "fetch_one"),
            (decision_engine, "fetch_one"),
            (decision_engine, "fetch_all"),
            (pattern_miner, "fetch_all"),
            (reward, "fetch_all"),
            (reward, "load_subscriber_series", self.load_subscriber_series),
        ]
        ctxs = []
        for t in targets:
            if len(t) == 3:
                ctxs.append(patch.object(t[0], t[1], t[2]))
            else:
                mod, name = t
                if name == "fetch_one":
                    ctxs.append(patch.object(mod, name, self.fetch_one))
                else:
                    ctxs.append(patch.object(mod, name, self.fetch_all))
        return ctxs

    def load_subscriber_series(self):
        rows = self.fetch_all("SELECT em.measured_at, em.subscribers_at FROM engagement_metrics em")
        return [(_as_dt(r["measured_at"]), r["subscribers_at"]) for r in rows]


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@pytest.fixture()
def fake():
    return HorizonFake()


@contextlib.contextmanager
def patched(fake):
    with contextlib.ExitStack() as stack:
        for p in fake.patches():
            stack.enter_context(p)
        yield


# ═══════════════════════════════════════════════════════════════════
# get_snapshot_at_horizon boundary cases
# ═══════════════════════════════════════════════════════════════════

def test_exact_horizon_snapshot_is_chosen(fake):
    posted = NOW - timedelta(hours=100)
    fake.add_post(1, posted)
    fake.add_snapshot(1, posted + timedelta(hours=4), views=50)
    fake.add_snapshot(1, posted + timedelta(hours=48), views=100)
    fake.add_snapshot(1, posted + timedelta(hours=96), views=180)

    with patched(fake):
        snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 100


def test_no_snapshot_at_horizon_takes_nearest_before(fake):
    """Only earlier snapshots exist → the closest one before is used."""
    posted = NOW - timedelta(hours=100)
    fake.add_post(1, posted)
    fake.add_snapshot(1, posted + timedelta(hours=4), views=50)
    fake.add_snapshot(1, posted + timedelta(hours=40), views=90)

    with patched(fake):
        snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 90


def test_no_snapshot_at_horizon_takes_nearest_after(fake):
    """Only later snapshots exist → the closest one after is used."""
    posted = NOW - timedelta(hours=100)
    fake.add_post(1, posted)
    fake.add_snapshot(1, posted + timedelta(hours=60), views=130)
    fake.add_snapshot(1, posted + timedelta(hours=96), views=180)

    with patched(fake):
        snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 130


def test_nearest_wins_across_both_sides(fake):
    """44h and 56h snapshots exist → 44h is closer to the 48h horizon."""
    posted = NOW - timedelta(hours=100)
    fake.add_post(1, posted)
    fake.add_snapshot(1, posted + timedelta(hours=44), views=95)
    fake.add_snapshot(1, posted + timedelta(hours=56), views=120)

    with patched(fake):
        snap = get_snapshot_at_horizon(1, hours=48)
    assert snap["views"] == 95


def test_no_snapshots_returns_none(fake):
    fake.add_post(1, NOW - timedelta(hours=100))
    with patched(fake):
        assert get_snapshot_at_horizon(1, hours=48) is None


def test_unknown_post_returns_none(fake):
    with patched(fake):
        assert get_snapshot_at_horizon(999, hours=48) is None


# ═══════════════════════════════════════════════════════════════════
# Regression: equal-quality posts of different ages → equal rates
# ═══════════════════════════════════════════════════════════════════

def test_equal_quality_posts_of_different_ages_have_equal_rates(fake):
    """An old post accumulated more total views, but at the 48h horizon both
    posts are identical — MAX/last would have made the old post 'win'."""
    from aibp.self_learning.decision_engine import (
        compute_engagement_rates,
        get_engagement_for_policy_version,
    )

    old_posted = NOW - timedelta(days=10)
    new_posted = NOW - timedelta(days=3)

    fake.add_post(1, old_posted, policy_version="v1")
    fake.add_snapshot(1, old_posted + timedelta(hours=48), views=100)
    fake.add_snapshot(1, old_posted + timedelta(hours=120), views=180)  # decayed growth

    fake.add_post(2, new_posted, policy_version="v1")
    fake.add_snapshot(2, new_posted + timedelta(hours=48), views=100)

    with patched(fake):
        posts = get_engagement_for_policy_version("v1")
        rates = compute_engagement_rates(posts)

    assert len(rates) == 2
    assert rates[0] == pytest.approx(rates[1])


def test_pattern_miner_uses_horizon_snapshot(fake):
    """pattern_miner.load_post_data must not pick the last snapshot."""
    from aibp.self_learning import pattern_miner

    posted = NOW - timedelta(days=5)
    fake.add_post(1, posted)
    fake.add_snapshot(1, posted + timedelta(hours=48), views=100)
    fake.add_snapshot(1, posted + timedelta(hours=110), views=170)

    with patch("aibp.self_learning.pattern_miner.datetime") as mock_dt, patched(fake):
        mock_dt.now.return_value = NOW
        # fromisoformat still needs to be the real one for the SELECT param parse
        mock_dt.side_effect = datetime
        posts = pattern_miner.load_post_data(days=7)

    assert len(posts) == 1
    assert posts[0]["latest_views"] == 100
