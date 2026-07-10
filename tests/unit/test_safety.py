"""Tests for self-learning safety module.

Hermetic: rate limiter / kill switch read event counts via
``safety.count_events`` (which wraps a PG ``fetch_one``). These tests patch
``count_events`` and ``log_autopilot_event`` with an in-memory list so no
PostgreSQL (or SQLite) is needed.
"""
import sys
from pathlib import Path
from unittest.mock import patch

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


# In-memory event store shared between the patched writer and the patched
# counter, so the safety logic operates on real counts.
def _event_store():
    return []


_SAFETY_POLICY = {
    "safety": {
        "max_changes_per_day": 1,
        "max_changes_per_week": 3,
        "max_rollbacks_per_week": 3,
    },
}


def test_rate_limit_logic():
    """Test that rate limiter correctly counts events."""
    from aibp.self_learning import safety

    events = _event_store()

    def fake_count(event_type, since):
        return sum(1 for et, _ in events if et == event_type)

    def fake_log(event_type, experiment_id=None, details=None):
        from datetime import UTC, datetime
        events.append((event_type, datetime.now(UTC)))

    with patch.object(safety, "load_policy", return_value=_SAFETY_POLICY), \
         patch.object(safety, "count_events", side_effect=fake_count), \
         patch.object(safety, "log_autopilot_event", side_effect=fake_log):
        # Insert some events
        safety.log_autopilot_event("change_applied", details={"test": True})
        safety.log_autopilot_event("change_applied", details={"test": True})

        # Should be rate limited (default 1/day)
        allowed, reason = safety.check_rate_limit("change_applied")
        assert allowed is False
        assert "Daily limit" in reason


def test_kill_switch_logic():
    """Test that kill switch activates after 3 rollbacks."""
    from aibp.self_learning import safety

    events = _event_store()

    def fake_count(event_type, since):
        return sum(1 for et, _ in events if et == event_type)

    def fake_log(event_type, experiment_id=None, details=None):
        from datetime import UTC, datetime
        events.append((event_type, datetime.now(UTC)))

    with patch.object(safety, "load_policy", return_value=_SAFETY_POLICY), \
         patch.object(safety, "count_events", side_effect=fake_count), \
         patch.object(safety, "log_autopilot_event", side_effect=fake_log):
        # Add 3 rollbacks
        for _ in range(3):
            safety.log_autopilot_event("rollback")

        should_kill, reason = safety.check_kill_switch()
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
