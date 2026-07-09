"""Tests for interleaving experiments (ADR-0007, issue #13).

Hermetic: interleave reads the active experiment + policy via the PG
``fetch_one`` helper; the end-to-end decision tests read engagement via
``fetch_all``/``fetch_one`` and ``get_snapshot_at_horizon``. These tests patch
those names with an in-memory fake so no PostgreSQL (or SQLite) is needed.

Covers:
  - Deterministic day-parity assignment
  - Policy resolution for control/variant days
  - End-to-end simulation: interleaved posts with a known effect reproduce
    the correct promote/reject decision via make_decision
"""
import contextlib
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning.interleave import (
    CONTROL,
    VARIANT,
    assignment_for_date,
    resolve_policy_for_today,
)

# ═══════════════════════════════════════════════════════════════════
# Assignment determinism (pure functions, no DB)
# ═══════════════════════════════════════════════════════════════════

def test_assignment_is_deterministic_by_day_parity():
    # 2026-01-01 is day 1 (odd) → variant; 2026-01-02 is day 2 → control
    assert assignment_for_date(date(2026, 1, 1)) == VARIANT
    assert assignment_for_date(date(2026, 1, 2)) == CONTROL
    # Same date always yields the same arm
    assert assignment_for_date(date(2026, 7, 6)) == assignment_for_date(date(2026, 7, 6))


def test_assignment_alternates_daily():
    d = date(2026, 3, 1)
    arms = [assignment_for_date(d + timedelta(days=i)) for i in range(10)]
    for a, b in zip(arms, arms[1:]):
        assert a != b


# ═══════════════════════════════════════════════════════════════════
# Policy resolution — patched fetch_one / load_policy_version
# ═══════════════════════════════════════════════════════════════════

def _experiment_row(policy_before="v_prod", policy_after="v_variant"):
    return {"id": 1, "started_at": datetime(2026, 1, 1, tzinfo=UTC),
            "experiment_type": "rubric_weight", "hypothesis": "test hypothesis",
            "policy_before": policy_before, "policy_after": policy_after,
            "assignment_mode": "interleave"}


def test_resolve_policy_no_active_experiment():
    default = {"version": "v_prod", "rubric_weights": {"anti_hype": 1.0}}
    with patch("aibp.self_learning.interleave.get_active_interleave_experiment",
               return_value=None):
        assert resolve_policy_for_today(default, today=date(2026, 1, 1)) is default


def test_resolve_policy_control_day_returns_default():
    default = {"version": "v_prod"}
    with patch("aibp.self_learning.interleave.get_active_interleave_experiment",
               return_value=_experiment_row()):
        result = resolve_policy_for_today(default, today=date(2026, 1, 2))  # even → control
    assert result is default


def test_resolve_policy_variant_day_returns_shadow_policy():
    default = {"version": "v_prod"}
    variant_policy = {"version": "v_20260101000000", "rubric_weights": {"anti_hype": 1.3}}
    with patch("aibp.self_learning.interleave.get_active_interleave_experiment",
               return_value=_experiment_row()), \
         patch("aibp.self_learning.interleave.load_policy_version", return_value=variant_policy):
        result = resolve_policy_for_today(default, today=date(2026, 1, 1))  # odd → variant
    # Version is normalized to the experiment's policy_after so that
    # post_features.policy_version matches decision_engine queries
    assert result["version"] == "v_variant"
    assert result["rubric_weights"]["anti_hype"] == 1.3


def test_resolve_policy_variant_missing_falls_back_to_default():
    default = {"version": "v_prod"}
    with patch("aibp.self_learning.interleave.get_active_interleave_experiment",
               return_value=_experiment_row("v_prod", "v_nonexistent")), \
         patch("aibp.self_learning.interleave.load_policy_version", return_value=None):
        result = resolve_policy_for_today(default, today=date(2026, 1, 1))
    assert result is default


# ═══════════════════════════════════════════════════════════════════
# End-to-end simulation: known effect → correct decision
# ═══════════════════════════════════════════════════════════════════

class InterleaveFake:
    """In-memory posts + snapshots for the decision-engine end-to-end tests."""

    def __init__(self):
        self.posts: dict[int, dict] = {}
        self.snapshots: dict[int, list[dict]] = {}

    def add_post(self, feed_item_id, policy_version, posted_days_ago, views, subs,
                 channel="main"):
        posted = datetime.now(UTC) - timedelta(days=posted_days_ago)
        self.posts[feed_item_id] = {
            "feed_item_id": feed_item_id, "posted_at": posted, "slot": "morning",
            "target_channel": channel, "policy_version": policy_version,
        }
        # engagement measured at posted_at (the 48h horizon nearest snapshot)
        self.snapshots.setdefault(feed_item_id, []).append({
            "views": views, "forwards": 0, "replies": 0, "reactions_count": 0,
            "subscribers_at": subs, "measured_at": posted,
        })

    def fetch_all(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if "SELECT pf.feed_item_id, pf.posted_at, pf.slot, pf.target_channel" in sql_stripped \
                and "pf.policy_version = %s" in sql_stripped:
            pv = params[0]
            rows = [dict(p) for p in self.posts.values()
                    if p["policy_version"] == pv and p["target_channel"] == "main"]
            if len(params) > 1 and params[1] is not None:
                since = _as_dt(params[1])
                rows = [p for p in rows if _as_dt(p["posted_at"]) >= since]
            return rows
        if "SELECT posted_at FROM post_features WHERE target_channel = 'main'" in sql_stripped:
            return [{"posted_at": p["posted_at"]} for p in self.posts.values()
                    if p["target_channel"] == "main"]
        if "SELECT em.measured_at, em.subscribers_at" in sql_stripped:
            out = []
            for fid, snaps in self.snapshots.items():
                if self.posts.get(fid, {}).get("target_channel") != "main":
                    continue
                for s in snaps:
                    if s["subscribers_at"] is not None:
                        out.append({"measured_at": s["measured_at"], "subscribers_at": s["subscribers_at"]})
            out.sort(key=lambda r: r["measured_at"])
            return out
        raise AssertionError(f"unexpected fetch_all: {sql!r}")

    def fetch_one(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if "FROM engagement_metrics em" in sql_stripped and "ABS(EXTRACT(EPOCH" in sql_stripped:
            fid = params[0]
            post = self.posts.get(fid)
            snaps = self.snapshots.get(fid)
            if not post or not snaps:
                return None
            horizon = post["posted_at"] + timedelta(hours=48)
            nearest = min(snaps, key=lambda s: abs(s["measured_at"] - horizon))
            return dict(nearest)
        raise AssertionError(f"unexpected fetch_one: {sql!r}")

    def load_subscriber_series(self):
        rows = self.fetch_all("SELECT em.measured_at, em.subscribers_at FROM engagement_metrics em")
        return [(_as_dt(r["measured_at"]), r["subscribers_at"]) for r in rows]

    def patches(self):
        from aibp.self_learning import decision_engine, reward
        return [
            patch.object(decision_engine, "fetch_all", self.fetch_all),
            patch.object(decision_engine, "fetch_one", self.fetch_one),
            patch.object(decision_engine, "get_snapshot_at_horizon", self.snapshot_at_horizon),
            patch.object(reward, "fetch_all", self.fetch_all),
            patch.object(reward, "load_subscriber_series", self.load_subscriber_series),
            patch.object(reward, "get_snapshot_at_horizon", self.snapshot_at_horizon),
        ]

    def snapshot_at_horizon(self, feed_item_id, hours=48):
        post = self.posts.get(feed_item_id)
        snaps = self.snapshots.get(feed_item_id)
        if not post or not snaps:
            return None
        horizon = post["posted_at"] + timedelta(hours=hours)
        nearest = min(snaps, key=lambda s: abs(s["measured_at"] - horizon))
        return dict(nearest)


def _as_dt(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@contextlib.contextmanager
def patched(fake):
    with contextlib.ExitStack() as stack:
        for p in fake.patches():
            stack.enter_context(p)
        yield


def _simulate_interleaved_posts(fake, effect_pct, n_per_group=15, base_views=100, subs=1000):
    """Insert interleaved control/variant posts with a known effect size."""
    feed_id = 1
    for i in range(n_per_group):
        noise = (i % 5) - 2  # deterministic small noise: -2..+2 views
        fake.add_post(feed_id, "v_control", posted_days_ago=7 - (i % 7),
                      views=base_views + noise, subs=subs)
        feed_id += 1
        variant_views = int(base_views * (1 + effect_pct / 100)) + noise
        fake.add_post(feed_id, "v_variant", posted_days_ago=7 - (i % 7),
                      views=variant_views, subs=subs)
        feed_id += 1


_POLICY = {"safety": {"experiment_window_days": 7, "promote_probability": 0.95,
                       "min_effect_pct": 5}}


def test_interleaving_with_strong_effect_promotes():
    """Simulated +30% variant effect on the same audience → promote."""
    from aibp.self_learning.decision_engine import make_decision

    fake = InterleaveFake()
    _simulate_interleaved_posts(fake, effect_pct=30)
    experiment = {
        "id": 1, "started_at": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
        "policy_before": "v_control", "policy_after": "v_variant",
    }

    with patch("aibp.self_learning.decision_engine.load_policy", return_value=_POLICY), \
         patch("aibp.self_learning.reward.load_policy", return_value=_POLICY), \
         patched(fake):
        decision = make_decision(experiment)
    assert decision["decision"] == "promote"


def test_interleaving_with_no_effect_rejects():
    """No real effect → reject (no significant improvement)."""
    from aibp.self_learning.decision_engine import make_decision

    fake = InterleaveFake()
    _simulate_interleaved_posts(fake, effect_pct=0)
    experiment = {
        "id": 1, "started_at": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
        "policy_before": "v_control", "policy_after": "v_variant",
    }

    with patch("aibp.self_learning.decision_engine.load_policy", return_value=_POLICY), \
         patch("aibp.self_learning.reward.load_policy", return_value=_POLICY), \
         patched(fake):
        decision = make_decision(experiment)
    assert decision["decision"] == "reject"


def test_decision_ignores_test_channel_posts():
    """Test-channel posts must not contribute data to decisions (ADR-0007)."""
    from aibp.self_learning.decision_engine import get_engagement_for_policy_version

    fake = InterleaveFake()
    fake.add_post(1, "v_control", posted_days_ago=3, views=100, subs=1000, channel="main")
    fake.add_post(2, "v_control", posted_days_ago=3, views=5, subs=10, channel="test")

    with patched(fake):
        posts = get_engagement_for_policy_version("v_control")
    assert len(posts) == 1
    assert posts[0]["target_channel"] == "main"


def test_decision_ignores_posts_before_experiment_start():
    """Posts published before the experiment started are excluded."""
    from aibp.self_learning.decision_engine import get_engagement_for_policy_version

    fake = InterleaveFake()
    fake.add_post(1, "v_control", posted_days_ago=10, views=100, subs=1000)  # before
    fake.add_post(2, "v_control", posted_days_ago=2, views=100, subs=1000)   # after

    since = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    with patched(fake):
        posts = get_engagement_for_policy_version("v_control", since=since)
    assert len(posts) == 1
    assert posts[0]["feed_item_id"] == 2
