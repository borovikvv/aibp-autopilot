"""Tests for the human approval gate (issue #20)."""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning import db as sl_db
from aibp.self_learning.safety import requires_approval


@pytest.fixture()
def temp_db(tmp_path):
    from aibp.self_learning import telegram_lock
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"), \
         patch.object(telegram_lock, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield tmp_path


DECISION = {
    "decision": "promote",
    "reason": "shadow +20%, P(shadow>control)=0.97",
    "control_engagement": {"mean": 0.10, "n": 20},
    "shadow_engagement": {"mean": 0.12, "n": 20},
    "effect_size": 0.20,
    "p_value": 0.97,
}


def _insert_experiment(experiment_type: str, status: str = "shadow_running") -> int:
    with sl_db.sqlite_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO experiments_log
                (started_at, experiment_type, hypothesis, policy_before, policy_after,
                 applies_to, status, assignment_mode)
            VALUES (?, ?, 'hyp', 'v_before', 'v_after', 'stage', ?, 'interleave')
            """,
            (datetime.now(UTC).isoformat(), experiment_type, status),
        )
        return cur.lastrowid


def _insert_policy(version: str = "v_after") -> None:
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO policies (version, created_at, created_by, yaml_content, json_blob,
                                  applies_to, status)
            VALUES (?, ?, 'test', '', ?, 'stage', 'draft')
            """,
            (version, datetime.now(UTC).isoformat(),
             json.dumps({"version": version, "rubric_weights": {"anti_hype": 1.3}})),
        )


def _get_experiment(exp_id: int) -> dict:
    with sl_db.sqlite_conn() as conn:
        return dict(conn.execute(
            "SELECT * FROM experiments_log WHERE id = ?", (exp_id,)
        ).fetchone())


# ═══════════════════════════════════════════════════════════════════
# requires_approval
# ═══════════════════════════════════════════════════════════════════

def test_high_risk_types_require_approval():
    policy = {"safety": {"approval_required_for": ["post_param", "regex_gate"]}}
    with patch("aibp.self_learning.safety.load_policy", return_value=policy):
        assert requires_approval("post_param") is True
        assert requires_approval("regex_gate") is True
        assert requires_approval("rubric_weight") is False
        assert requires_approval("cta") is False


def test_default_when_config_missing():
    with patch("aibp.self_learning.safety.load_policy", return_value={}):
        assert requires_approval("post_param") is True
        assert requires_approval("rubric_weight") is False


# ═══════════════════════════════════════════════════════════════════
# promote_experiment routing
# ═══════════════════════════════════════════════════════════════════

def test_high_risk_promote_parks_as_pending_approval(temp_db):
    from aibp.self_learning import decision_engine

    exp_id = _insert_experiment("regex_gate")
    experiment = _get_experiment(exp_id)
    policy_path = temp_db / "policy.yaml"

    with patch("aibp.self_learning.safety.load_policy",
               return_value={"safety": {"approval_required_for": ["regex_gate"]}}), \
         patch.object(decision_engine, "POLICY_PATH", policy_path), \
         patch("aibp.self_learning.approvals.send_approval_request", return_value=True) as send:
        assert decision_engine.promote_experiment(experiment, DECISION) is True

    updated = _get_experiment(exp_id)
    assert updated["status"] == "pending_approval"
    assert not policy_path.exists()  # policy NOT applied
    send.assert_called_once()


def test_low_risk_promote_applies_directly(temp_db):
    from aibp.self_learning import decision_engine

    exp_id = _insert_experiment("rubric_weight")
    _insert_policy()
    experiment = _get_experiment(exp_id)
    policy_path = temp_db / "policy.yaml"

    with patch("aibp.self_learning.safety.load_policy",
               return_value={"safety": {"approval_required_for": ["regex_gate"]}}), \
         patch.object(decision_engine, "check_rate_limit", return_value=(True, "ok")), \
         patch.object(decision_engine, "POLICY_PATH", policy_path):
        assert decision_engine.promote_experiment(experiment, DECISION) is True

    assert _get_experiment(exp_id)["status"] == "promoted"
    assert policy_path.exists()  # policy applied without approval


def test_pending_survives_send_failure(temp_db):
    """If Telegram is down, the experiment still parks as pending_approval."""
    from aibp.self_learning import decision_engine

    exp_id = _insert_experiment("post_param")
    experiment = _get_experiment(exp_id)

    with patch("aibp.self_learning.safety.load_policy",
               return_value={"safety": {"approval_required_for": ["post_param"]}}), \
         patch("aibp.self_learning.approvals.send_approval_request",
               side_effect=RuntimeError("telegram down")):
        assert decision_engine.promote_experiment(experiment, DECISION) is True

    assert _get_experiment(exp_id)["status"] == "pending_approval"


# ═══════════════════════════════════════════════════════════════════
# Callback handling
# ═══════════════════════════════════════════════════════════════════

def _park_pending(experiment_type: str = "regex_gate") -> int:
    from aibp.self_learning.decision_engine import mark_pending_approval

    exp_id = _insert_experiment(experiment_type)
    mark_pending_approval(_get_experiment(exp_id), DECISION)
    return exp_id


def test_approve_callback_applies_policy(temp_db):
    from aibp.self_learning import decision_engine
    from aibp.self_learning.approvals import handle_callback

    exp_id = _park_pending()
    _insert_policy()
    policy_path = temp_db / "policy.yaml"

    with patch.object(decision_engine, "POLICY_PATH", policy_path):
        assert handle_callback(f"exp_approve:{exp_id}") == "approved"

    assert _get_experiment(exp_id)["status"] == "promoted"
    assert policy_path.exists()
    assert "anti_hype" in policy_path.read_text(encoding="utf-8")


def test_reject_callback_marks_rejected(temp_db):
    from aibp.self_learning.approvals import handle_callback

    exp_id = _park_pending()
    assert handle_callback(f"exp_reject:{exp_id}") == "rejected"

    updated = _get_experiment(exp_id)
    assert updated["status"] == "rejected"
    assert "[rejected by human]" in updated["decision_reason"]


def test_stale_or_garbage_callbacks_ignored(temp_db):
    from aibp.self_learning.approvals import handle_callback

    assert handle_callback("exp_approve:9999") == "ignored"   # no such experiment
    assert handle_callback("exp_approve:abc") == "ignored"    # not an id
    assert handle_callback("something_else") == "ignored"     # unknown prefix

    # Already-processed experiment cannot be double-applied
    exp_id = _park_pending()
    assert handle_callback(f"exp_reject:{exp_id}") == "rejected"
    assert handle_callback(f"exp_approve:{exp_id}") == "ignored"


# ═══════════════════════════════════════════════════════════════════
# getUpdates conflict handling (issue #24)
# ═══════════════════════════════════════════════════════════════════

_SETTINGS = type("S", (), {"telegram_bot_token": "TOKEN", "telegram_alert_chat_id": "999"})()


def test_poller_skips_when_lock_is_busy(temp_db):
    """If the engagement collector holds the getUpdates lock, the poller skips
    without calling getUpdates at all."""
    import asyncio
    from contextlib import contextmanager
    from unittest.mock import AsyncMock

    from aibp.self_learning import approvals

    @contextmanager
    def busy_lock():
        yield False

    with patch.object(approvals, "get_settings", return_value=_SETTINGS), \
         patch.object(approvals, "getupdates_lock", busy_lock), \
         patch.object(approvals, "_get_updates", new=AsyncMock()) as get_updates:
        result = asyncio.run(approvals.process_callbacks_async())

    assert result == 0
    get_updates.assert_not_called()


def test_poller_alerts_on_409_conflict(temp_db):
    """A 409 from Telegram must alert the owner instead of failing silently."""
    import asyncio
    from unittest.mock import AsyncMock

    from aibp.self_learning import approvals

    with patch.object(approvals, "get_settings", return_value=_SETTINGS), \
         patch.dict("os.environ", {"TELEGRAM_METRICS_CHAT_ID": "123"}), \
         patch.object(approvals, "_get_updates",
                      new=AsyncMock(side_effect=approvals.GetUpdatesConflictError("conflict"))), \
         patch.object(approvals, "_send_alert", new=AsyncMock()) as alert:
        result = asyncio.run(approvals.process_callbacks_async())

    assert result == 0
    alert.assert_awaited_once()
    # The alert names the fix (set the metrics chat)
    assert "TELEGRAM_METRICS_CHAT_ID" in alert.await_args.args[2]


def test_poller_processes_callback_under_real_lock(temp_db):
    """End-to-end: with the lock free, a callback_query update is handled."""
    import asyncio
    from unittest.mock import AsyncMock

    from aibp.self_learning import approvals, decision_engine

    exp_id = _park_pending()
    _insert_policy()
    updates = [{"update_id": 5,
                "callback_query": {"id": "cb1", "data": f"exp_reject:{exp_id}"}}]

    with patch.object(approvals, "get_settings", return_value=_SETTINGS), \
         patch.dict("os.environ", {"TELEGRAM_METRICS_CHAT_ID": "123"}), \
         patch.object(approvals, "_get_updates", new=AsyncMock(return_value=updates)), \
         patch.object(approvals, "_answer_callback", new=AsyncMock()) as answer, \
         patch.object(decision_engine, "POLICY_PATH", temp_db / "policy.yaml"):
        result = asyncio.run(approvals.process_callbacks_async())

    assert result == 1
    answer.assert_awaited()
    assert _get_experiment(exp_id)["status"] == "rejected"
