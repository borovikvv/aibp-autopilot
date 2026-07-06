"""Human approval gate for high-risk experiments (issue #20).

ADR-0001 rejected a human gate in favor of full autopilot. For a
revenue-bearing channel that is too risky for changes to post structure and
quality gates, so those experiment types (policy safety.approval_required_for)
now park as status='pending_approval' and a Telegram message with
approve/reject inline buttons goes to the alert chat.

    python -m aibp.self_learning.approvals            # process button taps
    python -m aibp.self_learning.approvals --remind   # re-send pending requests

Callback polling uses getUpdates limited to callback_query. Note: getUpdates
is exclusive per bot — run this from the same scheduler cadence as the
engagement collector's getUpdates fallback, never concurrently.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import structlog

from aibp.self_learning.db import sqlite_conn, log_autopilot_event
from aibp.utils.config import get_settings

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"

APPROVE_PREFIX = "exp_approve"
REJECT_PREFIX = "exp_reject"


# ═══════════════════════════════════════════════════════════════════
# Outgoing: approval request with inline buttons
# ═══════════════════════════════════════════════════════════════════

def _format_request(experiment: dict, decision: dict) -> str:
    effect = decision.get("effect_size")
    prob = decision.get("p_value")
    return (
        "🔬 <b>Эксперимент ждёт подтверждения</b>\n\n"
        f"ID: {experiment['id']}\n"
        f"Тип: {experiment['experiment_type']} (высокий риск)\n"
        f"Гипотеза: {experiment.get('hypothesis') or '—'}\n"
        f"Эффект: {f'{effect:+.1%}' if effect is not None else '—'}, "
        f"P(variant&gt;control): {f'{prob:.3f}' if prob is not None else '—'}\n"
        f"Policy: <code>{experiment['policy_before']}</code> → "
        f"<code>{experiment['policy_after']}</code>\n\n"
        "Применить к прод-каналу?"
    )


def _approval_keyboard(experiment_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Применить", "callback_data": f"{APPROVE_PREFIX}:{experiment_id}"},
            {"text": "❌ Отклонить", "callback_data": f"{REJECT_PREFIX}:{experiment_id}"},
        ]]
    }


def send_approval_request(experiment: dict, decision: dict) -> bool:
    """Send the approve/reject message to the alert chat."""
    from aibp.publishing.publisher import send_message

    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_alert_chat_id:
        log.error("approval_chat_not_configured",
                  hint="Set TELEGRAM_ALERT_CHAT_ID in .env")
        return False

    result = asyncio.run(send_message(
        bot_token=s.telegram_bot_token,
        chat_id=s.telegram_alert_chat_id,
        text=_format_request(experiment, decision),
        reply_markup=_approval_keyboard(experiment["id"]),
    ))
    ok = bool(result.get("ok"))
    if not ok:
        log.error("approval_request_failed", response=str(result)[:200])
    return ok


# ═══════════════════════════════════════════════════════════════════
# Incoming: callback handling
# ═══════════════════════════════════════════════════════════════════

def _load_pending_experiment(experiment_id: int) -> dict | None:
    with sqlite_conn() as conn:
        row = conn.execute(
            "SELECT * FROM experiments_log WHERE id = ? AND status = 'pending_approval'",
            (experiment_id,),
        ).fetchone()
        return dict(row) if row else None


def _stored_decision(experiment: dict) -> dict:
    """Rebuild the decision dict from columns stored at pending time."""
    return {
        "control_engagement": json.loads(experiment.get("control_engagement") or "{}"),
        "shadow_engagement": json.loads(experiment.get("shadow_engagement") or "{}"),
        "effect_size": experiment.get("effect_size"),
        "p_value": experiment.get("p_value"),
        "reason": (experiment.get("decision_reason") or "") + " [approved by human]",
    }


def handle_callback(callback_data: str) -> str:
    """Process one button tap. Returns approved | rejected | ignored."""
    try:
        prefix, raw_id = callback_data.split(":", 1)
        experiment_id = int(raw_id)
    except (ValueError, AttributeError):
        return "ignored"
    if prefix not in (APPROVE_PREFIX, REJECT_PREFIX):
        return "ignored"

    experiment = _load_pending_experiment(experiment_id)
    if experiment is None:
        log.info("approval_callback_stale", experiment=experiment_id)
        return "ignored"

    if prefix == APPROVE_PREFIX:
        from aibp.self_learning.decision_engine import apply_promotion
        if apply_promotion(experiment, _stored_decision(experiment)):
            log_autopilot_event("approval_granted", experiment_id=experiment_id)
            return "approved"
        log.error("approval_apply_failed", experiment=experiment_id)
        return "ignored"

    from datetime import datetime, timezone
    with sqlite_conn() as conn:
        conn.execute(
            """
            UPDATE experiments_log
            SET status = 'rejected', finished_at = ?,
                decision_reason = COALESCE(decision_reason, '') || ' [rejected by human]'
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), experiment_id),
        )
    log_autopilot_event("approval_rejected", experiment_id=experiment_id)
    return "rejected"


async def _get_updates(bot_token: str, offset: int | None = None) -> list[dict]:
    params: dict = {"allowed_updates": json.dumps(["callback_query"]), "timeout": 0}
    if offset is not None:
        params["offset"] = offset
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{TELEGRAM_API}/bot{bot_token}/getUpdates", params=params)
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []


async def _answer_callback(bot_token: str, callback_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(
            f"{TELEGRAM_API}/bot{bot_token}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
        )


async def process_callbacks_async() -> int:
    """Poll pending callback queries and act on them. Returns actions taken."""
    s = get_settings()
    if not s.telegram_bot_token:
        log.error("no_bot_token")
        return 0

    updates = await _get_updates(s.telegram_bot_token)
    processed = 0
    max_update_id = None

    for update in updates:
        max_update_id = update["update_id"]
        callback = update.get("callback_query")
        if not callback:
            continue
        outcome = handle_callback(callback.get("data", ""))
        if outcome != "ignored":
            processed += 1
        answer = {"approved": "✅ Применено к прод-политике",
                  "rejected": "❌ Эксперимент отклонён",
                  "ignored": "Уже обработано или неактуально"}[outcome]
        await _answer_callback(s.telegram_bot_token, callback["id"], answer)

    # Acknowledge processed updates so they are not re-delivered
    if max_update_id is not None:
        await _get_updates(s.telegram_bot_token, offset=max_update_id + 1)

    if processed:
        log.info("approval_callbacks_processed", count=processed)
    return processed


def resend_pending_requests() -> int:
    """Re-send approval messages for all pending experiments (--remind)."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM experiments_log WHERE status = 'pending_approval'"
        ).fetchall()
    sent = 0
    for row in rows:
        experiment = dict(row)
        if send_approval_request(experiment, _stored_decision(experiment)):
            sent += 1
    return sent


def run() -> int:
    """Cron entry point — process button taps."""
    asyncio.run(process_callbacks_async())
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--remind":
        print(f"Re-sent {resend_pending_requests()} pending approval request(s)")
        raise SystemExit(0)
    raise SystemExit(run())
