"""Integration test: full self-learning cycle draft → shadow_running → promote/reject.

This test validates that all self-learning modules work together end-to-end:
  1. Policy Updater creates an experiment from a hypothesis (draft)
  2. Shadow Runner starts the experiment (shadow_running)
  3. Decision Engine evaluates it (promote or reject)
  4. Safety rails (rate limiter, kill switch) don't block valid operations

Requires a live PostgreSQL with pgvector: set TEST_DATABASE_URL (issue #43).
The self-learning state now lives in the main PostgreSQL store, so this
exercises the real PG path — migrations 0009/0010, EXTRACT(EPOCH ...) horizon
query, jsonb Json-adapter, ON CONFLICT upserts.

Quick start:
  docker run --rm -d --name aibp-test-pg -e POSTGRES_DB=aibp_test \
    -e POSTGRES_USER=aibp -e POSTGRES_PASSWORD=aibp -p 5433:5432 \
    pgvector/pgvector:pg16
  TEST_DATABASE_URL=postgresql://aibp:aibp@localhost:5433/aibp_test \
    PYTHON=.venv/bin/python make test-integration
"""
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from psycopg2.extras import Json

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set — integration tests need a live PostgreSQL with pgvector",
)


# ─── Fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def pg_db():
    """Point the connection pool at TEST_DATABASE_URL, apply schema + migrations, clean tables."""
    from aibp.db import connection as conn_mod
    from aibp.db.init_db import init_db
    from aibp.db.migrate import apply_migrations
    from aibp.utils.config import get_settings

    # Force the pool to use the test database
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    # Reset cached settings + pool so they pick up the new DATABASE_URL
    import aibp.utils.config as cfg_mod
    cfg_mod._settings = None
    if conn_mod._pool is not None:
        conn_mod._pool.closeall()
        conn_mod._pool = None

    # Apply base schema + all migrations (creates feed_items + self-learning tables)
    init_db()
    apply_migrations()

    yield  # tests run here

    # Cleanup: drop all self-learning tables so the next test starts fresh
    from aibp.db.connection import execute
    for table in (
        "autopilot_events", "bandit_observations", "bandit_arms",
        "prompt_changes", "engagement_metrics", "post_features",
        "experiments_log", "policies", "competitor_posts",
    ):
        execute(f"DELETE FROM {table}")
    # Truncate feed_items too (migration 0001 created it)
    execute("DELETE FROM feed_items")

    # Reset pool after test
    if conn_mod._pool is not None:
        conn_mod._pool.closeall()
        conn_mod._pool = None
    cfg_mod._settings = None


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


# ─── Helpers ────────────────────────────────────────────────────────

def _insert_feed_item(feed_id: int):
    """Insert a minimal feed_items row so the FK post_features→feed_items(id) holds."""
    import hashlib

    from aibp.db.connection import execute
    url = f"https://example.com/test/{feed_id}"
    execute(
        """
        INSERT INTO feed_items (id, url, url_hash, title, status, dupe_key)
        VALUES (%s, %s, %s, %s, 'published', %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (feed_id, url, hashlib.sha256(url.encode()).hexdigest(), f"Test {feed_id}", f"test:{feed_id}"),
    )


def _insert_post_and_engagement(feed_id, posted_at, policy_version, views, subs=1000):
    """Insert a post_features row + its 48h horizon engagement snapshot."""
    from aibp.db.connection import execute
    _insert_feed_item(feed_id)
    execute(
        """
        INSERT INTO post_features
            (feed_item_id, posted_at, slot, pipeline_env, target_channel,
             strategy_rubric, char_count, paragraph_count, bold_count,
             emoji_count, has_image, scheduled_hour, policy_version, policy_blob)
        VALUES (%s, %s, 'morning', 'prod', 'main', 'anti_hype',
                1000, 4, 1, 0, 0, 10, %s, %s)
        ON CONFLICT (feed_item_id) DO UPDATE SET
            posted_at = EXCLUDED.posted_at, policy_version = EXCLUDED.policy_version
        """,
        (feed_id, posted_at, policy_version, Json({})),
    )
    execute(
        """
        INSERT INTO engagement_metrics
            (feed_item_id, measured_at, views, forwards, replies,
             reactions_count, subscribers_at)
        VALUES (%s, %s, %s, 0, 0, 0, %s)
        """,
        (feed_id, posted_at + timedelta(hours=48), views, subs),
    )


# ─── Tests ──────────────────────────────────────────────────────────

def test_experiment_lifecycle_promote(pg_db, temp_policy):
    """Full cycle: hypothesis → draft → shadow_running → promoted."""
    from aibp.db.connection import execute, fetch_one
    from aibp.self_learning.decision_engine import make_decision
    from aibp.self_learning.policy_updater import create_experiment
    from aibp.self_learning.safety import is_autopilot_paused
    from aibp.self_learning.shadow_runner import start_shadow

    assert not is_autopilot_paused()

    # ── Step 1: Create experiment ──
    hypothesis = {
        "experiment_type": "rubric_weight",
        "hypothesis": "Increase anti_hype weight because anti_hype posts get more views",
        "change_spec": {"rubric": "anti_hype", "new_weight": 1.5},
        "expected_effect": "+15% engagement",
        "confidence": 0.8,
    }
    exp_id = create_experiment(hypothesis, temp_policy)
    assert exp_id is not None

    row = fetch_one(
        "SELECT status, experiment_type, policy_before, policy_after FROM experiments_log WHERE id = %s",
        (exp_id,),
    )
    assert row["status"] == "draft"
    assert row["experiment_type"] == "rubric_weight"
    assert row["policy_after"] != "v_test_initial"

    # ── Step 2: Start shadow ──
    exp = fetch_one("SELECT * FROM experiments_log WHERE id = %s", (exp_id,))
    with patch("aibp.self_learning.shadow_runner.apply_policy_to_stage"):
        started = start_shadow(exp)
    assert started is True

    row = fetch_one("SELECT status, started_at, policy_after FROM experiments_log WHERE id = %s", (exp_id,))
    assert row["status"] == "shadow_running"

    # ── Step 3: Insert interleaved engagement data ──
    control_version = temp_policy["version"]
    shadow_version = row["policy_after"]
    now = datetime.now(UTC)

    # Control: 10 posts, mean views 100 (engagement rate 0.10)
    for i in range(10):
        posted = now - timedelta(days=i + 1)
        _insert_post_and_engagement(1000 + i, posted, control_version, views=100)

    # Variant: 10 posts, 30% better (views 130, rate 0.13)
    for i in range(10):
        posted = now - timedelta(days=i + 1, hours=12)
        _insert_post_and_engagement(2000 + i, posted, shadow_version, views=130)

    # ── Step 4: Make experiment 15 days old (past 14d window) ──
    old_started = datetime.now(UTC) - timedelta(days=15)
    execute("UPDATE experiments_log SET started_at = %s WHERE id = %s", (old_started, exp_id))

    exp = fetch_one("SELECT * FROM experiments_log WHERE id = %s", (exp_id,))
    decision = make_decision(exp)

    assert decision["decision"] == "promote"
    assert decision["effect_size"] == pytest.approx(0.30, abs=0.02)
    assert decision["p_value"] >= 0.95


def test_experiment_lifecycle_reject_insufficient_data(pg_db, temp_policy):
    """Experiment with no engagement data after give-up → reject."""
    from aibp.db.connection import execute, fetch_one
    from aibp.self_learning.decision_engine import make_decision
    from aibp.self_learning.policy_updater import create_experiment

    hypothesis = {
        "experiment_type": "post_param",
        "hypothesis": "Shorter morning posts get more engagement",
        "change_spec": {"slot": "morning", "param": "target_chars", "new_value": [600, 1000]},
        "expected_effect": "+10% engagement",
        "confidence": 0.6,
    }
    exp_id = create_experiment(hypothesis, temp_policy)
    assert exp_id is not None

    # post_param is high_risk → window 28d + 7d grace = 35d give-up (issue #42).
    # 40 days > 35d → past give-up point → reject (insufficient_data_after_14d).
    old_started = datetime.now(UTC) - timedelta(days=40)
    execute(
        "UPDATE experiments_log SET started_at = %s, status = 'shadow_running' WHERE id = %s",
        (old_started, exp_id),
    )
    exp = fetch_one("SELECT * FROM experiments_log WHERE id = %s", (exp_id,))

    decision = make_decision(exp)
    assert decision["decision"] == "reject"
    assert decision["reason"] == "insufficient_data_after_14d"


def test_rate_limiter_blocks_excessive_changes(pg_db, temp_policy):
    """Rate limiter blocks after max_changes_per_day events."""
    from aibp.self_learning.db import log_autopilot_event
    from aibp.self_learning.safety import check_rate_limit

    allowed, _ = check_rate_limit("change_applied")
    assert allowed is True

    log_autopilot_event("change_applied", details={"test": True})

    allowed, reason = check_rate_limit("change_applied")
    assert allowed is False
    assert "Daily limit" in reason


def test_kill_switch_after_3_rollbacks(pg_db, temp_policy):
    """Kill switch activates after 3 rollbacks in 7 days."""
    from aibp.self_learning.db import log_autopilot_event
    from aibp.self_learning.safety import check_kill_switch

    for _ in range(3):
        log_autopilot_event("rollback")

    should_kill, reason = check_kill_switch()
    assert should_kill is True
    assert "3 rollbacks" in reason


def test_policy_versioning(pg_db, temp_policy):
    """Policy versions are deterministic and tracked in PG."""
    from aibp.db.connection import fetch_one
    from aibp.self_learning.db import policy_version, save_policy_version

    v1 = policy_version(temp_policy)
    v2 = policy_version(temp_policy)
    assert v1 == v2

    modified = dict(temp_policy)
    modified["version"] = "v_modified"
    v3 = policy_version(modified)
    assert v3 != v1

    save_policy_version(modified, yaml_content="version: v_modified", applies_to="stage")
    row = fetch_one("SELECT version, applies_to, status FROM policies WHERE version = %s", (v3,))
    assert row is not None
    assert row["applies_to"] == "stage"
    assert row["status"] == "draft"
