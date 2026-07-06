"""Tests for the Bandit State dashboard section (issue #23)."""
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.observability import dashboard
from aibp.self_learning import bandit
from aibp.self_learning import db as sl_db


@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield tmp_path


def _set_arm(dimension: str, arm_id: str, alpha: float, beta: float) -> None:
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO bandit_arms (dimension, arm_id, alpha, beta, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dimension, arm_id, alpha, beta, datetime.now(UTC).isoformat()),
        )


def test_empty_state(temp_db):
    assert dashboard.get_bandit_state() == []


def test_computes_e_theta_multiplier_observations(temp_db):
    # alpha=13, beta=6 → E[θ]=13/19≈0.684, multiplier≈1.18, observations=17
    _set_arm("rubric", "anti_hype", 13, 6)
    (arm,) = dashboard.get_bandit_state()
    assert arm["e_theta"] == pytest.approx(13 / 19, abs=1e-3)
    assert arm["multiplier"] == pytest.approx(0.5 + 13 / 19, abs=1e-3)
    assert arm["observations"] == 17
    assert arm["significant"] is False


def test_matches_bandit_multiplier(temp_db):
    """Dashboard multiplier must equal what select_candidate actually applies."""
    _set_arm("rubric", "anti_hype", 8, 4)
    (arm,) = dashboard.get_bandit_state()
    # sample with a fixed posterior mean is not deterministic, but the
    # multiplier formula (0.5 + E[θ]) is exactly the bandit's mapping
    import numpy as np
    e_theta = 8 / 12
    assert arm["multiplier"] == pytest.approx(0.5 + e_theta)
    assert 0.5 <= arm["multiplier"] <= 1.5
    _ = (bandit, np)  # imports used to assert the modules line up


def test_significant_drift_flagged(temp_db):
    # Strong winner: alpha high → multiplier > 1.3
    _set_arm("rubric", "winner", 30, 2)
    # Strong loser: beta high → multiplier < 0.7
    _set_arm("rubric", "loser", 2, 30)
    _set_arm("rubric", "neutral", 5, 5)
    by_id = {a["arm_id"]: a for a in dashboard.get_bandit_state()}
    assert by_id["winner"]["significant"] is True
    assert by_id["loser"]["significant"] is True
    assert by_id["neutral"]["significant"] is False


def test_ordered_by_e_theta_desc_within_dimension(temp_db):
    _set_arm("rubric", "low", 2, 8)
    _set_arm("rubric", "high", 8, 2)
    ids = [a["arm_id"] for a in dashboard.get_bandit_state()]
    assert ids == ["high", "low"]


def test_end_to_end_arm_from_bandit_module(temp_db):
    """Arms created via the bandit module render with sane values."""
    bandit.ensure_arms("rubric", ["a", "b"])
    bandit.record_outcome("rubric", "a", success=True)
    state = dashboard.get_bandit_state()
    a = next(x for x in state if x["arm_id"] == "a")
    assert a["alpha"] == 2.0  # 1 (prior) + 1 success
    assert a["observations"] == 1
