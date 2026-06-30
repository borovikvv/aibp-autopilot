"""Tests for self-learning safety module."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_rate_limit_logic():
    """Test that rate limiter correctly counts events."""
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.safety import check_rate_limit

    # Use temp DB
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(sl_db, "get_db_path", return_value=Path(tmpdir) / "test.db"):
            sl_db.init_db()

            # Insert some events
            from datetime import datetime, timezone
            sl_db.log_autopilot_event("change_applied", details={"test": True})
            sl_db.log_autopilot_event("change_applied", details={"test": True})

            # Should be rate limited (default 1/day)
            allowed, reason = check_rate_limit("change_applied")
            assert allowed is False
            assert "Daily limit" in reason


def test_kill_switch_logic():
    """Test that kill switch activates after 3 rollbacks."""
    from aibp.self_learning import db as sl_db
    from aibp.self_learning.safety import check_kill_switch

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.object(sl_db, "get_db_path", return_value=Path(tmpdir) / "test.db"):
            sl_db.init_db()

            # Add 3 rollbacks
            for _ in range(3):
                sl_db.log_autopilot_event("rollback")

            should_kill, reason = check_kill_switch()
            assert should_kill is True
            assert "3 rollbacks" in reason


def test_policy_version_hash():
    """Test that policy version is deterministic."""
    from aibp.self_learning.db import policy_version

    p1 = {"a": 1, "b": 2}
    p2 = {"b": 2, "a": 1}  # same content, different order
    assert policy_version(p1) == policy_version(p2)


def test_validate_change_spec_rubric_weight():
    """Test that rubric weight change is validated."""
    from aibp.self_learning.policy_updater import validate_change_spec

    current_policy = {"rubric_weights": {"anti_hype": 1.0, "process_under_ai": 1.0}}

    # Valid change
    hyp = {
        "experiment_type": "rubric_weight",
        "change_spec": {"rubric": "anti_hype", "new_weight": 1.3},
    }
    valid, _ = validate_change_spec(hyp, current_policy)
    assert valid is True

    # Invalid: unknown rubric
    hyp = {
        "experiment_type": "rubric_weight",
        "change_spec": {"rubric": "nonexistent", "new_weight": 1.3},
    }
    valid, _ = validate_change_spec(hyp, current_policy)
    assert valid is False

    # Invalid: weight out of range
    hyp = {
        "experiment_type": "rubric_weight",
        "change_spec": {"rubric": "anti_hype", "new_weight": 5.0},
    }
    valid, _ = validate_change_spec(hyp, current_policy)
    assert valid is False
