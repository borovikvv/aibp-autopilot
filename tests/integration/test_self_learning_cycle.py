"""Integration test: full self-learning cycle draft → shadow_running → promote/reject.

This test validates that all self-learning modules work together end-to-end:
  1. Policy Updater creates an experiment from a hypothesis (draft)
  2. Shadow Runner starts the experiment (shadow_running)
  3. Decision Engine evaluates it (promote or reject)
  4. Safety rails (rate limiter, kill switch) don't block valid operations

Requires a live PostgreSQL: set TEST_DATABASE_URL (issue #43). The self-learning
state now lives in the main PostgreSQL store, so this exercises the real PG path.

TODO(issue #43): the body still uses the legacy SQLite fixtures
(`sl_db.sqlite_conn`, `get_db_path`, `init_db`). Convert the data setup to PG
(`aibp.db.connection` helpers + `apply_migrations()` against TEST_DATABASE_URL)
and drop the temp_sqlite_db fixture. Until then the test is skipped without PG.
"""
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# Integration tests need a real PostgreSQL; skip without the env var so plain
# `pytest` never fails on a dev machine without PG.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set — integration tests need a live PostgreSQL",
)


@pytest.fixture
def temp_sqlite_db():
    """Create a temporary SQLite database for self-learning."""
    from aibp.self_learning import db as sl_db

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_self_learning.db"
        with patch.object(sl_db, "get_db_path", return_value=db_path):
            sl_db.init_db()
            yield db_path


@pytest.fixture
def temp_policy():
    """Create a temporary policy dict."""
    return {
        "version": "v_test_initial",
        "autopilot_paused": False,
        "rubric_weights": {
            "process_under_ai": 1.0,
            "anti_hype": 1.0,
        },
        "post_params": {
            "morning": {
                "target_chars": [800, 1400],
                "paragraphs": [4, 5],
                "max_bold": 1,
                "max_emoji": 0,
                "scheduled_hour_msk": 10,
            }
        },
        "regex_gates": [],
        "source_scores": {"default": 0.0},
        "visual_policy": {
            "title_max_chars": 78,
            "blocks_range": [3, 5],
        },
        "safety": {
            "max_changes_per_day": 1,
            "max_changes_per_week": 3,
            "max_rollbacks_per_week": 3,
            "engagement_drop_24h_pct": 30,
            "engagement_drop_7d_pct": 50,
            "subscribers_drop_24h_pct": 5,
            "rollback_48h_engagement_pct": 85,
            "rollback_7d_engagement_pct": 90,
            "rollback_7d_subscribers_pct": 97,
        },
    }


def test_experiment_lifecycle_promote(temp_sqlite_db, temp_policy):
    """Full cycle: hypothesis → draft → shadow_running → promoted.

    Simulates an experiment where shadow policy is better than control.
    """
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.decision_engine import make_decision
    from aibp.self_learning.policy_updater import create_experiment
    from aibp.self_learning.safety import is_autopilot_paused
    from aibp.self_learning.shadow_runner import start_shadow

    # Verify autopilot is not paused at start
    assert not is_autopilot_paused()

    # ── Step 1: Create experiment from hypothesis ──
    hypothesis = {
        "experiment_type": "rubric_weight",
        "hypothesis": "Increase anti_hype weight because anti_hype posts get more views",
        "change_spec": {
            "rubric": "anti_hype",
            "new_weight": 1.5,
        },
        "expected_effect": "+15% engagement",
        "confidence": 0.8,
    }

    exp_id = create_experiment(hypothesis, temp_policy)
    assert exp_id is not None

    # Verify experiment is in draft state
    with sl_db.sqlite_conn() as conn:
        row = conn.execute(
            "SELECT status, experiment_type, policy_before, policy_after FROM experiments_log WHERE id = ?",
            (exp_id,),
        ).fetchone()
    assert row["status"] == "draft"
    assert row["experiment_type"] == "rubric_weight"
    assert row["policy_before"] == "v_test_initial"
    assert row["policy_after"] is not None
    assert row["policy_after"] != "v_test_initial"

    # ── Step 2: Start shadow test ──
    with sl_db.sqlite_conn() as conn:
        exp = dict(conn.execute(
            "SELECT * FROM experiments_log WHERE id = ?", (exp_id,)
        ).fetchone())

    # Mock the stage policy writing (we don't test file I/O here)
    with patch("aibp.self_learning.shadow_runner.apply_policy_to_stage"):
        started = start_shadow(exp)

    assert started is True

    # Verify experiment is now shadow_running
    with sl_db.sqlite_conn() as conn:
        row = conn.execute(
            "SELECT status, started_at, policy_after FROM experiments_log WHERE id = ?",
            (exp_id,),
        ).fetchone()
    assert row["status"] == "shadow_running"
    assert row["started_at"] is not None

    # ── Step 3: Simulate interleaved engagement data (ADR-0007) ──
    # Both groups live in the main channel and are separated by
    # policy_version; posts alternate days like real interleaving.
    # Control (policy_before): 10 posts with mean engagement 0.10
    # Variant (policy_after): 10 posts with mean engagement 0.13 (+30%)
    control_version = temp_policy["version"]
    shadow_version = row["policy_after"]

    now = datetime.now(UTC)
    with sl_db.sqlite_conn() as conn:
        # Control posts (even days back)
        for i in range(10):
            feed_id = 1000 + i
            posted = now - timedelta(days=i + 1)
            conn.execute(
                """INSERT OR REPLACE INTO post_features
                   (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                    strategy_rubric, char_count, paragraph_count, bold_count,
                    emoji_count, has_image, scheduled_hour, policy_version, policy_blob)
                   VALUES (?, ?, 'morning', 'prod', 'main', 'anti_hype',
                           1000, 4, 1, 0, 0, 10, ?, '{}')""",
                (feed_id, posted.isoformat(), control_version),
            )
            conn.execute(
                """INSERT INTO engagement_metrics
                   (feed_item_id, measured_at, views, forwards, replies,
                    reactions_count, subscribers_at)
                   VALUES (?, ?, ?, 0, 0, 0, 1000)""",
                (feed_id, (posted + timedelta(hours=48)).isoformat(), 100),  # 0.10
            )

        # Variant posts (30% better, same channel and audience)
        for i in range(10):
            feed_id = 2000 + i
            posted = now - timedelta(days=i + 1, hours=12)
            conn.execute(
                """INSERT OR REPLACE INTO post_features
                   (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                    strategy_rubric, char_count, paragraph_count, bold_count,
                    emoji_count, has_image, scheduled_hour, policy_version, policy_blob)
                   VALUES (?, ?, 'morning', 'prod', 'main', 'anti_hype',
                           1000, 4, 1, 0, 0, 10, ?, '{}')""",
                (feed_id, posted.isoformat(), shadow_version),
            )
            conn.execute(
                """INSERT INTO engagement_metrics
                   (feed_item_id, measured_at, views, forwards, replies,
                    reactions_count, subscribers_at)
                   VALUES (?, ?, ?, 0, 0, 0, 1000)""",
                (feed_id, (posted + timedelta(hours=48)).isoformat(), 130),  # 0.13
            )

    # ── Step 4: Run decision engine ──
    # Make experiment look 15 days old (past the 14-day window, ADR-0008),
    # so all simulated posts fall inside the experiment period.
    with sl_db.sqlite_conn() as conn:
        old_started = (datetime.now(UTC) - timedelta(days=15)).isoformat()
        conn.execute(
            "UPDATE experiments_log SET started_at = ? WHERE id = ?",
            (old_started, exp_id),
        )

    with sl_db.sqlite_conn() as conn:
        exp = dict(conn.execute(
            "SELECT * FROM experiments_log WHERE id = ?", (exp_id,)
        ).fetchone())

    decision = make_decision(exp)

    # With 30% improvement and low variance, should promote (ADR-0008:
    # effect_size is the relative effect, p_value is P(variant > control))
    assert decision["decision"] == "promote"
    assert decision["effect_size"] == pytest.approx(0.30, abs=0.02)
    assert decision["p_value"] >= 0.95


def test_experiment_lifecycle_reject_insufficient_data(temp_sqlite_db, temp_policy):
    """Experiment with insufficient data after 14 days → reject."""
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.decision_engine import make_decision
    from aibp.self_learning.policy_updater import create_experiment

    # Create experiment
    hypothesis = {
        "experiment_type": "post_param",
        "hypothesis": "Shorter morning posts get more engagement",
        "change_spec": {
            "slot": "morning",
            "param": "target_chars",
            "new_value": [600, 1000],
        },
        "expected_effect": "+10% engagement",
        "confidence": 0.6,
    }

    exp_id = create_experiment(hypothesis, temp_policy)
    assert exp_id is not None

    # Make experiment 25 days old (past give-up point: window 14d + 7d grace)
    with sl_db.sqlite_conn() as conn:
        old_started = (datetime.now(UTC) - timedelta(days=25)).isoformat()
        conn.execute(
            "UPDATE experiments_log SET started_at = ?, status = 'shadow_running' WHERE id = ?",
            (old_started, exp_id),
        )
        exp = dict(conn.execute(
            "SELECT * FROM experiments_log WHERE id = ?", (exp_id,)
        ).fetchone())

    # No engagement data inserted → insufficient
    decision = make_decision(exp)
    assert decision["decision"] == "reject"
    assert decision["reason"] == "insufficient_data_after_14d"


def test_rate_limiter_blocks_excessive_changes(temp_sqlite_db, temp_policy):
    """Rate limiter should block after max_changes_per_day experiments."""
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.policy_updater import create_experiment
    from aibp.self_learning.safety import check_rate_limit

    # First change should be allowed
    allowed, _ = check_rate_limit("change_applied")
    assert allowed is True

    # Simulate that one change was already applied today
    sl_db.log_autopilot_event("change_applied", details={"test": True})

    # Second change should be blocked (max_changes_per_day=1 in temp_policy safety)
    allowed, reason = check_rate_limit("change_applied")
    assert allowed is False
    assert "Daily limit" in reason


def test_kill_switch_after_3_rollbacks(temp_sqlite_db, temp_policy):
    """Kill switch should activate after 3 rollbacks in 7 days."""
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.safety import check_kill_switch

    # Log 3 rollbacks
    for _ in range(3):
        sl_db.log_autopilot_event("rollback")

    should_kill, reason = check_kill_switch()
    assert should_kill is True
    assert "3 rollbacks" in reason


def test_policy_versioning(temp_sqlite_db, temp_policy):
    """Policy versions should be deterministic and tracked."""
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.db import policy_version, save_policy_version

    v1 = policy_version(temp_policy)
    v2 = policy_version(temp_policy)
    assert v1 == v2  # same dict → same version

    # Modify policy
    modified = dict(temp_policy)
    modified["version"] = "v_modified"
    v3 = policy_version(modified)
    assert v3 != v1  # different dict → different version

    # Save and verify it's in DB
    save_policy_version(modified, yaml_content="version: v_modified", applies_to="stage")
    with sl_db.sqlite_conn() as conn:
        row = conn.execute(
            "SELECT version, applies_to, status FROM policies WHERE version = ?",
            (v3,),
        ).fetchone()
    assert row is not None
    assert row["applies_to"] == "stage"
    assert row["status"] == "draft"
