"""Tests for the redirect-service health check (issue #21)."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.tracking import healthcheck


# _state_path() now derives from PROJECT_ROOT / "data/..." (no SQLite DB path).
# Keep each test hermetic by pointing the failure-counter file at a tmp dir.
@pytest.fixture()
def temp_state(tmp_path):
    state_file = tmp_path / "redirect_health.state"
    with patch.object(healthcheck, "_state_path", return_value=state_file):
        yield tmp_path


# ═══════════════════════════════════════════════════════════════════
# Failure counter persistence
# ═══════════════════════════════════════════════════════════════════

def test_state_path_in_data_dir():
    """_state_path() is relative to the project data directory (no DB path)."""
    from aibp.utils.config import PROJECT_ROOT

    assert healthcheck._state_path() == PROJECT_ROOT / "data" / "redirect_health.state"


def test_counter_defaults_to_zero(temp_state):
    assert healthcheck._read_failures() == 0


def test_counter_roundtrip(temp_state):
    healthcheck._write_failures(4)
    assert healthcheck._read_failures() == 4


def test_corrupt_counter_reads_zero(temp_state):
    healthcheck._state_path().write_text("garbage")
    assert healthcheck._read_failures() == 0


# ═══════════════════════════════════════════════════════════════════
# run() outcomes
# ═══════════════════════════════════════════════════════════════════

def test_healthy_resets_counter(temp_state):
    healthcheck._write_failures(2)
    with patch.object(healthcheck, "check_once", return_value=True), \
         patch.object(healthcheck, "_send_alert") as alert:
        assert healthcheck.run() == 0
    assert healthcheck._read_failures() == 0
    alert.assert_not_called()


def test_single_failure_does_not_alert(temp_state):
    with patch.object(healthcheck, "check_once", return_value=False), \
         patch.object(healthcheck, "_send_alert") as alert:
        assert healthcheck.run() == 1
    assert healthcheck._read_failures() == 1
    alert.assert_not_called()


def test_alerts_exactly_at_threshold(temp_state):
    """Three consecutive failures → one alert, on the third."""
    with patch.object(healthcheck, "check_once", return_value=False), \
         patch.object(healthcheck, "_send_alert") as alert:
        assert healthcheck.run() == 1  # 1
        assert healthcheck.run() == 1  # 2
        assert alert.call_count == 0
        assert healthcheck.run() == 1  # 3 → alert
        assert alert.call_count == 1
        # 4th failure must NOT re-alert (no paging spam)
        assert healthcheck.run() == 1
        assert alert.call_count == 1


def test_recovery_after_alert_resets(temp_state):
    with patch.object(healthcheck, "check_once", return_value=False), \
         patch.object(healthcheck, "_send_alert"):
        for _ in range(3):
            healthcheck.run()
    assert healthcheck._read_failures() == 3
    with patch.object(healthcheck, "check_once", return_value=True), \
         patch.object(healthcheck, "_send_alert"):
        assert healthcheck.run() == 0
    assert healthcheck._read_failures() == 0


# ═══════════════════════════════════════════════════════════════════
# check_once
# ═══════════════════════════════════════════════════════════════════

def test_check_once_true_on_200(temp_state):
    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch.object(healthcheck.urllib.request, "urlopen", return_value=FakeResp()):
        assert healthcheck.check_once() is True


def test_check_once_false_on_exception(temp_state):
    with patch.object(healthcheck.urllib.request, "urlopen", side_effect=OSError("refused")):
        assert healthcheck.check_once() is False
