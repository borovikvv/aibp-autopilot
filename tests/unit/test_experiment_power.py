"""Tests for the Experiment Power dashboard section (issue #42).

``get_experiment_power`` reads ``experiments_log`` via ``fetch_all`` and counts
main-channel posts per policy version via ``_count_posts_for_version`` (which
calls ``fetch_one``). The decision path (``compute_decision`` etc.) is only
invoked when both groups have >= 5 posts. These tests inject rows through the
dashboard module's helpers and mock ``datetime`` for deterministic
``days_to_decision``.
"""
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.observability import dashboard

# Frozen "now" used to derive exp_age_days / days_to_decision deterministically.
_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
# Young experiment: 2 days old → days_to_decision = 12 for a 14-day window.
_EXP_STARTED = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


def _exp_row(**overrides) -> dict:
    base = {
        "id": 1,
        "started_at": _EXP_STARTED,
        "experiment_type": "rubric_weight",  # low_risk → 14-day window
        "policy_before": "v1",
        "policy_after": "v2",
    }
    base.update(overrides)
    return base


class _FakeDateTime:
    """Stand-in for datetime in the dashboard module: fixed now() plus a
    working fromisoformat() so ISO-string started_at values still parse."""

    @classmethod
    def now(cls, tz=None):
        return _NOW

    @classmethod
    def fromisoformat(cls, s):
        return datetime.fromisoformat(s)


class _PatchCtx:
    """Composes all patches behind one context manager."""

    def __init__(self, experiments, policy_before_n, policy_after_n):
        self._stack = ExitStack()
        self._experiments = experiments
        self._before_n = policy_before_n
        self._after_n = policy_after_n

    def __enter__(self):
        s = self._stack
        s.enter_context(patch.object(dashboard, "fetch_all", return_value=self._experiments))
        s.enter_context(patch.object(
            dashboard, "_count_posts_for_version",
            side_effect=lambda v, **kw: self._before_n if v == "v1" else self._after_n,
        ))
        s.enter_context(patch.object(dashboard, "load_policy", return_value={"safety": {}}))
        s.enter_context(patch.object(dashboard, "datetime", _FakeDateTime))
        s.enter_context(patch(
            "aibp.self_learning.decision_engine.get_engagement_for_policy_version",
            return_value=[],
        ))
        return self._stack

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _patch_deps(experiments, policy_before_n=0, policy_after_n=0):
    """Return a single context manager that patches the dashboard's I/O surface
    and datetime.

    ``get_engagement_for_policy_version`` is imported inside the function from
    ``aibp.self_learning.decision_engine``, so it must be patched there to keep
    the >= 5-posts decision path off the real DB."""
    return _PatchCtx(experiments, policy_before_n, policy_after_n)


def test_empty_when_no_experiments():
    with _patch_deps([]):
        assert dashboard.get_experiment_power() == []


def test_result_shape_and_status_value():
    with _patch_deps([_exp_row()], policy_before_n=3, policy_after_n=4):
        (result,) = dashboard.get_experiment_power()
    expected_keys = {
        "id", "experiment_type", "started_at", "control_n", "shadow_n",
        "target_n", "days_to_decision", "current_p", "status",
    }
    assert set(result.keys()) == expected_keys
    assert result["status"] in ("on_track", "behind", "ready_to_decide")
    assert result["control_n"] == 3
    assert result["shadow_n"] == 4


def test_on_track_when_enough_data():
    # target_n = window*2 = 28; total_n=20 >= 0.7*28=19.6 → on_track.
    # 2-day-old experiment → days_to_decision = 12 > 0.
    with _patch_deps([_exp_row()], policy_before_n=10, policy_after_n=10):
        (result,) = dashboard.get_experiment_power()
    assert result["status"] == "on_track"
    assert result["target_n"] == 28
    assert result["days_to_decision"] == 12


def test_behind_when_low_data():
    # total_n=4 < 19.6 and days_to_decision>0 → behind.
    with _patch_deps([_exp_row()], policy_before_n=2, policy_after_n=2):
        (result,) = dashboard.get_experiment_power()
    assert result["status"] == "behind"


def test_current_p_none_when_few_posts():
    # < 5 posts/group → decision path skipped → current_p is None.
    with _patch_deps([_exp_row()], policy_before_n=3, policy_after_n=4):
        (result,) = dashboard.get_experiment_power()
    assert result["current_p"] is None


def test_current_p_computed_when_enough_posts():
    # Both groups >= 5 posts → decision path runs and yields a p_value.
    with _patch_deps([_exp_row()], policy_before_n=6, policy_after_n=6):
        with patch("aibp.self_learning.decision_engine.compute_reward_rates",
                   side_effect=lambda posts, policy=None: [0.1, 0.2, 0.15, 0.12, 0.18]):
            with patch("aibp.self_learning.decision_engine.compute_decision",
                       return_value={"p_value": 0.842}) as mock_dec:
                (result,) = dashboard.get_experiment_power()
    assert result["current_p"] == 0.842
    mock_dec.assert_called_once()


def test_ready_to_decide_when_window_elapsed():
    # started_at 14 days ago, window=14 → days_to_decision=0 → ready_to_decide.
    with _patch_deps([_exp_row(started_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC))],
                     policy_before_n=1, policy_after_n=1):
        (result,) = dashboard.get_experiment_power()
    assert result["days_to_decision"] == 0
    assert result["status"] == "ready_to_decide"


def test_iso_string_started_at_parsed():
    with _patch_deps([_exp_row(started_at="2026-07-01T10:00:00+00:00")],
                     policy_before_n=1, policy_after_n=1):
        (result,) = dashboard.get_experiment_power()
    assert result["days_to_decision"] == 0  # 14 days ago, window=14
    assert result["status"] == "ready_to_decide"
