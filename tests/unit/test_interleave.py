"""Tests for interleaving experiments (ADR-0007, issue #13).

Covers:
  - Deterministic day-parity assignment
  - Policy resolution for control/variant days
  - End-to-end simulation: interleaved posts with a known effect reproduce
    the correct promote/reject decision via make_decision
"""
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import db as sl_db
from aibp.self_learning.interleave import (
    CONTROL,
    VARIANT,
    assignment_for_date,
    resolve_policy_for_today,
)


# ═══════════════════════════════════════════════════════════════════
# Assignment determinism
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
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield


def _insert_experiment(policy_before: str, policy_after: str, started_days_ago: int = 8) -> int:
    started = (datetime.now(timezone.utc) - timedelta(days=started_days_ago)).isoformat()
    with sl_db.sqlite_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO experiments_log
                (started_at, experiment_type, hypothesis, policy_before, policy_after,
                 applies_to, status, assignment_mode)
            VALUES (?, 'rubric_weight', 'test hypothesis', ?, ?, 'stage',
                    'shadow_running', 'interleave')
            """,
            (started, policy_before, policy_after),
        )
        return cur.lastrowid


def _insert_post(feed_item_id: int, policy_version: str, posted_days_ago: int,
                 views: int, subs: int, channel: str = "main") -> None:
    posted = (datetime.now(timezone.utc) - timedelta(days=posted_days_ago)).isoformat()
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO post_features
                (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                 policy_version, policy_blob)
            VALUES (?, ?, 'morning', 'prod', ?, ?, '{}')
            """,
            (feed_item_id, posted, channel, policy_version),
        )
        conn.execute(
            """
            INSERT INTO engagement_metrics (feed_item_id, measured_at, views, subscribers_at)
            VALUES (?, ?, ?, ?)
            """,
            (feed_item_id, posted, views, subs),
        )


# ═══════════════════════════════════════════════════════════════════
# Policy resolution
# ═══════════════════════════════════════════════════════════════════

def test_resolve_policy_no_active_experiment(temp_db):
    default = {"version": "v_prod", "rubric_weights": {"anti_hype": 1.0}}
    assert resolve_policy_for_today(default, today=date(2026, 1, 1)) is default


def test_resolve_policy_control_day_returns_default(temp_db):
    default = {"version": "v_prod"}
    _insert_experiment("v_prod", "v_variant")
    result = resolve_policy_for_today(default, today=date(2026, 1, 2))  # even → control
    assert result is default


def test_resolve_policy_variant_day_returns_shadow_policy(temp_db):
    default = {"version": "v_prod"}
    variant_policy = {"version": "v_20260101000000", "rubric_weights": {"anti_hype": 1.3}}
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO policies (version, created_at, created_by, yaml_content, json_blob,
                                  applies_to, status)
            VALUES ('v_variant', ?, 'test', '', ?, 'stage', 'draft')
            """,
            (datetime.now(timezone.utc).isoformat(), json.dumps(variant_policy)),
        )
    _insert_experiment("v_prod", "v_variant")

    result = resolve_policy_for_today(default, today=date(2026, 1, 1))  # odd → variant
    # Version is normalized to the experiment's policy_after so that
    # post_features.policy_version matches decision_engine queries
    assert result["version"] == "v_variant"
    assert result["rubric_weights"]["anti_hype"] == 1.3


def test_resolve_policy_variant_missing_falls_back_to_default(temp_db):
    default = {"version": "v_prod"}
    _insert_experiment("v_prod", "v_nonexistent")
    result = resolve_policy_for_today(default, today=date(2026, 1, 1))
    assert result is default


# ═══════════════════════════════════════════════════════════════════
# End-to-end simulation: known effect → correct decision
# ═══════════════════════════════════════════════════════════════════

def _simulate_interleaved_posts(effect_pct: float, n_per_group: int = 15,
                                base_views: int = 100, subs: int = 1000) -> None:
    """Insert interleaved control/variant posts with a known effect size."""
    feed_id = 1
    for i in range(n_per_group):
        noise = (i % 5) - 2  # deterministic small noise: -2..+2 views
        _insert_post(feed_id, "v_control", posted_days_ago=7 - (i % 7),
                     views=base_views + noise, subs=subs)
        feed_id += 1
        variant_views = int(base_views * (1 + effect_pct / 100)) + noise
        _insert_post(feed_id, "v_variant", posted_days_ago=7 - (i % 7),
                     views=variant_views, subs=subs)
        feed_id += 1


def test_interleaving_with_strong_effect_promotes(temp_db):
    """Simulated +30% variant effect on the same audience → promote."""
    from aibp.self_learning.decision_engine import make_decision

    exp_id = _insert_experiment("v_control", "v_variant", started_days_ago=8)
    _simulate_interleaved_posts(effect_pct=30)

    with sl_db.sqlite_conn() as conn:
        experiment = dict(conn.execute(
            "SELECT * FROM experiments_log WHERE id = ?", (exp_id,)
        ).fetchone())

    decision = make_decision(experiment)
    assert decision["decision"] == "promote"


def test_interleaving_with_no_effect_rejects(temp_db):
    """No real effect → reject (no significant improvement)."""
    from aibp.self_learning.decision_engine import make_decision

    exp_id = _insert_experiment("v_control", "v_variant", started_days_ago=8)
    _simulate_interleaved_posts(effect_pct=0)

    with sl_db.sqlite_conn() as conn:
        experiment = dict(conn.execute(
            "SELECT * FROM experiments_log WHERE id = ?", (exp_id,)
        ).fetchone())

    decision = make_decision(experiment)
    assert decision["decision"] == "reject"


def test_decision_ignores_test_channel_posts(temp_db):
    """Test-channel posts must not contribute data to decisions (ADR-0007)."""
    from aibp.self_learning.decision_engine import get_engagement_for_policy_version

    _insert_post(1, "v_control", posted_days_ago=3, views=100, subs=1000, channel="main")
    _insert_post(2, "v_control", posted_days_ago=3, views=5, subs=10, channel="test")

    posts = get_engagement_for_policy_version("v_control")
    assert len(posts) == 1
    assert posts[0]["target_channel"] == "main"


def test_decision_ignores_posts_before_experiment_start(temp_db):
    """Posts published before the experiment started are excluded."""
    from aibp.self_learning.decision_engine import get_engagement_for_policy_version

    since = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    _insert_post(1, "v_control", posted_days_ago=10, views=100, subs=1000)  # before
    _insert_post(2, "v_control", posted_days_ago=2, views=100, subs=1000)   # after

    posts = get_engagement_for_policy_version("v_control", since=since)
    assert len(posts) == 1
    assert posts[0]["feed_item_id"] == 2
