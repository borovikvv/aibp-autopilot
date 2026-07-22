"""Tests for the human approval gate (issue #20).

Hermetic: the approval/decision-engine flow reads/writes ``experiments_log`` and
``policies`` via the PG ``execute``/``fetch_one`` helpers. These tests patch
those names with an in-memory fake so no PostgreSQL (or SQLite) is needed.
"""
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.self_learning.safety import requires_approval


class ApprovalFake:
    """In-memory experiments_log + policies tables.

    Understands only the statements the decision_engine/approvals/safety code
    issues for the approval flow; anything else raises so drift is caught.
    """

    def __init__(self):
        self.experiments: dict[int, dict] = {}
        self.policies: dict[str, dict] = {}
        self.events: list[tuple] = []
        self._next_id = 1

    # ── test helpers (seed the store directly) ─────────────────────
    def insert_experiment(self, experiment_type, status="shadow_running"):
        exp_id = self._next_id
        self._next_id += 1
        self.experiments[exp_id] = {
            "id": exp_id,
            "started_at": datetime.now(UTC).isoformat(),
            "experiment_type": experiment_type,
            "hypothesis": "hyp",
            "policy_before": "v_before",
            "policy_after": "v_after",
            "applies_to": "stage",
            "status": status,
            "assignment_mode": "interleave",
            "control_engagement": None,
            "shadow_engagement": None,
            "effect_size": None,
            "p_value": None,
            "decision_reason": None,
        }
        return exp_id

    def get_experiment(self, exp_id):
        return dict(self.experiments[exp_id])

    def insert_policy(self, version="v_after"):
        self.policies[version] = {
            "version": version,
            "json_blob": {"version": version, "rubric_weights": {"anti_hype": 1.3}},
            "yaml_content": "",
        }

    # ── PG helper stand-ins ────────────────────────────────────────
    def execute(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("INSERT INTO autopilot_events"):
            self.events.append(params)
            return 1
        if sql_stripped.startswith("UPDATE experiments_log"):
            exp_id = self._find_id_in_params(sql_stripped, params)
            exp = self.experiments[exp_id]
            self._apply_update(sql_stripped, params, exp)
            return 1
        raise AssertionError(f"unexpected execute: {sql!r}")

    def fetch_one(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if "SELECT * FROM experiments_log WHERE id = %s AND status = 'pending_approval'" in sql_stripped:
            exp = self.experiments.get(params[0])
            return dict(exp) if exp and exp["status"] == "pending_approval" else None
        if "SELECT json_blob, yaml_content FROM policies WHERE version = %s" in sql_stripped:
            p = self.policies.get(params[0])
            return dict(p) if p else None
        raise AssertionError(f"unexpected fetch_one: {sql!r}")

    def _find_id_in_params(self, sql, params):
        # WHERE id = %s is the last param for these UPDATEs
        return params[-1]

    def _apply_update(self, sql, params, exp):
        if "status = 'pending_approval'" in sql:
            exp["status"] = "pending_approval"
            exp["control_engagement"] = params[0]
            exp["shadow_engagement"] = params[1]
            exp["effect_size"] = params[2]
            exp["p_value"] = params[3]
            exp["decision_reason"] = params[4]
        elif "status = 'promoted'" in sql:
            exp["status"] = "promoted"
            exp["control_engagement"] = params[1]
            exp["shadow_engagement"] = params[2]
            exp["effect_size"] = params[3]
            exp["p_value"] = params[4]
            exp["decision_reason"] = params[5]
        elif "status = 'rejected'" in sql:
            if "'rejected', finished_at = %s" in sql:  # approvals reject path
                exp["status"] = "rejected"
                exp["decision_reason"] = (exp.get("decision_reason") or "") + " [rejected by human]"
            else:  # decision_engine.reject_experiment path
                exp["status"] = "rejected"
                exp["control_engagement"] = params[1]
                exp["shadow_engagement"] = params[2]
                exp["effect_size"] = params[3]
                exp["p_value"] = params[4]
                exp["decision_reason"] = params[5]

    def patches(self):
        from aibp.self_learning import approvals, decision_engine, safety
        from aibp.self_learning import db as sl_db
        return [
            patch.object(decision_engine, "execute", self.execute),
            patch.object(decision_engine, "fetch_one", self.fetch_one),
            patch.object(approvals, "execute", self.execute),
            patch.object(approvals, "fetch_one", self.fetch_one),
            patch.object(sl_db, "execute", self.execute),
            # safety.log_autopilot_event → sl_db.log_autopilot_event → execute
            patch.object(safety, "log_autopilot_event", lambda *a, **kw: self.events.append(a)),
        ]


@pytest.fixture()
def fake():
    return ApprovalFake()


DECISION = {
    "decision": "promote",
    "reason": "shadow +20%, P(shadow>control)=0.97",
    "control_engagement": {"mean": 0.10, "n": 20},
    "shadow_engagement": {"mean": 0.12, "n": 20},
    "effect_size": 0.20,
    "p_value": 0.97,
}


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

def test_high_risk_promote_parks_as_pending_approval(fake, tmp_path):
    from aibp.self_learning import decision_engine

    exp_id = fake.insert_experiment("regex_gate")
    policy_path = tmp_path / "policy.yaml"

    with patch("aibp.self_learning.safety.load_policy",
               return_value={"safety": {"approval_required_for": ["regex_gate"]}}), \
         patch.object(decision_engine, "POLICY_PATH", policy_path), \
         patch("aibp.self_learning.approvals.send_approval_request", return_value=True) as send:
        for p in fake.patches():
            p.start()
        try:
            assert decision_engine.promote_experiment(fake.get_experiment(exp_id), DECISION) is True
        finally:
            for p in fake.patches():
                p.stop()

    assert fake.get_experiment(exp_id)["status"] == "pending_approval"
    assert not policy_path.exists()  # policy NOT applied
    send.assert_called_once()


def test_low_risk_promote_applies_directly(fake, tmp_path):
    from aibp.self_learning import decision_engine

    exp_id = fake.insert_experiment("rubric_weight")
    fake.insert_policy()
    policy_path = tmp_path / "policy.yaml"

    with patch("aibp.self_learning.safety.load_policy",
               return_value={"safety": {"approval_required_for": ["regex_gate"]}}), \
         patch.object(decision_engine, "check_rate_limit", return_value=(True, "ok")), \
         patch.object(decision_engine, "POLICY_PATH", policy_path):
        for p in fake.patches():
            p.start()
        try:
            assert decision_engine.promote_experiment(fake.get_experiment(exp_id), DECISION) is True
        finally:
            for p in fake.patches():
                p.stop()

    assert fake.get_experiment(exp_id)["status"] == "promoted"
    assert policy_path.exists()  # policy applied without approval


def test_pending_survives_send_failure(fake):
    """If Telegram is down, the experiment still parks as pending_approval."""
    from aibp.self_learning import decision_engine

    exp_id = fake.insert_experiment("post_param")

    with patch("aibp.self_learning.safety.load_policy",
               return_value={"safety": {"approval_required_for": ["post_param"]}}), \
         patch("aibp.self_learning.approvals.send_approval_request",
               side_effect=RuntimeError("telegram down")):
        for p in fake.patches():
            p.start()
        try:
            assert decision_engine.promote_experiment(fake.get_experiment(exp_id), DECISION) is True
        finally:
            for p in fake.patches():
                p.stop()

    assert fake.get_experiment(exp_id)["status"] == "pending_approval"


# ═══════════════════════════════════════════════════════════════════
# Callback handling
# ═══════════════════════════════════════════════════════════════════

def _park_pending(fake, experiment_type="regex_gate"):
    from aibp.self_learning.decision_engine import mark_pending_approval

    exp_id = fake.insert_experiment(experiment_type)
    for p in fake.patches():
        p.start()
    try:
        mark_pending_approval(fake.get_experiment(exp_id), DECISION)
    finally:
        for p in fake.patches():
            p.stop()
    return exp_id


def test_approve_callback_applies_policy(fake, tmp_path):
    from aibp.self_learning import decision_engine
    from aibp.self_learning.approvals import handle_callback

    exp_id = _park_pending(fake)
    fake.insert_policy()
    policy_path = tmp_path / "policy.yaml"

    with patch.object(decision_engine, "POLICY_PATH", policy_path):
        for p in fake.patches():
            p.start()
        try:
            assert handle_callback(f"exp_approve:{exp_id}") == "approved"
        finally:
            for p in fake.patches():
                p.stop()

    assert fake.get_experiment(exp_id)["status"] == "promoted"
    assert policy_path.exists()
    assert "anti_hype" in policy_path.read_text(encoding="utf-8")


def test_reject_callback_marks_rejected(fake):
    from aibp.self_learning.approvals import handle_callback

    exp_id = _park_pending(fake)
    for p in fake.patches():
        p.start()
    try:
        assert handle_callback(f"exp_reject:{exp_id}") == "rejected"
    finally:
        for p in fake.patches():
            p.stop()

    updated = fake.get_experiment(exp_id)
    assert updated["status"] == "rejected"
    assert "[rejected by human]" in updated["decision_reason"]


def test_stale_or_garbage_callbacks_ignored(fake):
    from aibp.self_learning.approvals import handle_callback

    for p in fake.patches():
        p.start()
    try:
        assert handle_callback("exp_approve:9999") == "ignored"   # no such experiment
        assert handle_callback("exp_approve:abc") == "ignored"    # not an id
        assert handle_callback("something_else") == "ignored"     # unknown prefix
    finally:
        for p in fake.patches():
            p.stop()

    # Already-processed experiment cannot be double-applied
    exp_id = _park_pending(fake)
    for p in fake.patches():
        p.start()
    try:
        assert handle_callback(f"exp_reject:{exp_id}") == "rejected"
        assert handle_callback(f"exp_approve:{exp_id}") == "ignored"
    finally:
        for p in fake.patches():
            p.stop()


# ═══════════════════════════════════════════════════════════════════
# getUpdates conflict handling (issue #24)
# ═══════════════════════════════════════════════════════════════════

_SETTINGS = type("S", (), {"telegram_bot_token": "TOKEN", "telegram_alert_chat_id": "999"})()


def test_poller_skips_when_lock_is_busy():
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


def test_poller_alerts_on_409_conflict():
    """A 409 from Telegram must alert the owner instead of failing silently."""
    import asyncio
    from unittest.mock import AsyncMock

    from aibp.self_learning import approvals

    with patch.object(approvals, "get_settings", return_value=_SETTINGS), \
         patch.object(approvals, "_get_updates",
                      new=AsyncMock(side_effect=approvals.GetUpdatesConflictError("conflict"))), \
         patch.object(approvals, "_send_alert", new=AsyncMock()) as alert:
        result = asyncio.run(approvals.process_callbacks_async())

    assert result == 0
    alert.assert_awaited_once()
    # The alert explains the 409 and names getUpdates as the contended resource
    assert "getUpdates" in alert.await_args.args[2]


def test_poller_processes_callback_under_real_lock(fake, tmp_path):
    """End-to-end: with the lock free, a callback_query update is handled."""
    import asyncio
    from unittest.mock import AsyncMock

    from aibp.self_learning import approvals, decision_engine

    exp_id = _park_pending(fake)
    fake.insert_policy()
    updates = [{"update_id": 5,
                "callback_query": {"id": "cb1", "data": f"exp_reject:{exp_id}"}}]

    with patch.object(approvals, "get_settings", return_value=_SETTINGS), \
         patch.object(approvals, "_get_updates", new=AsyncMock(return_value=updates)), \
         patch.object(approvals, "_answer_callback", new=AsyncMock()), \
         patch.object(decision_engine, "POLICY_PATH", tmp_path / "policy.yaml"):
        for p in fake.patches():
            p.start()
        try:
            result = asyncio.run(approvals.process_callbacks_async())
        finally:
            for p in fake.patches():
                p.stop()

    assert result == 1
    assert fake.get_experiment(exp_id)["status"] == "rejected"
