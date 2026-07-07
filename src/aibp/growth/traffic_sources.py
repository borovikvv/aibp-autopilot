"""Traffic sources — per-source invite links + subscription attribution (issue #39).

Growth spend was unmeasurable: every subscriber looked the same, so the
actual cost-per-subscriber (CPS) of an ad buy or cross-promo could never be
computed. Now each traffic source gets its own invite link
(createChatInviteLink, name = source slug); when someone joins through it,
Telegram delivers a chat_member update carrying that link, and the join is
recorded against the source. cost_rub / joins = actual CPS.

chat_member updates arrive through the approvals getUpdates poller (the
single getUpdates owner, issue #24) — see process_callbacks_async().

Buying stays manual (prohibited actions: покупка/перевод денег): this module
only measures and reports.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from aibp.utils.config import get_settings

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"

SOURCE_KINDS = ("ad_buy", "cross_promo", "external", "other")


# ═══════════════════════════════════════════════════════════════════
# Source registry
# ═══════════════════════════════════════════════════════════════════

def create_source(slug: str, kind: str = "ad_buy", channel_username: str | None = None,
                  expected_subscribers: float | None = None,
                  expected_cps_rub: float | None = None,
                  notes: str | None = None) -> dict:
    """Create a traffic source with its own invite link to the main channel.

    Returns the source row (with invite_link). Raises on Telegram/PG errors —
    a source without a working invite link is useless, so no silent degrade.
    """
    if kind not in SOURCE_KINDS:
        raise ValueError(f"kind must be one of {SOURCE_KINDS}")
    s = get_settings()
    resp = httpx.post(
        f"{TELEGRAM_API}/bot{s.telegram_bot_token}/createChatInviteLink",
        json={"chat_id": s.telegram_channel_id_prod, "name": slug[:32]},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"createChatInviteLink failed: {data.get('description')}")
    invite_link = data["result"]["invite_link"]

    from aibp.db.connection import execute_returning
    row = execute_returning(
        """
        INSERT INTO traffic_sources
            (slug, kind, channel_username, invite_link,
             expected_subscribers, expected_cps_rub, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (slug, kind, channel_username, invite_link,
         expected_subscribers, expected_cps_rub, notes),
    )
    if row is None:
        raise RuntimeError(f"traffic source insert returned no row for '{slug}'")
    log.info("traffic_source_created", slug=slug, kind=kind, invite_link=invite_link)
    return row


def set_source(slug: str, cost_rub: float | None = None, status: str | None = None,
               ad_posted_at: str | None = None) -> int:
    """Update the manual fields after the human pays / the ad goes live."""
    from aibp.db.connection import execute
    updated = 0
    if cost_rub is not None:
        updated += execute(
            "UPDATE traffic_sources SET cost_rub = %s, updated_at = now() WHERE slug = %s",
            (cost_rub, slug))
    if status is not None:
        updated += execute(
            "UPDATE traffic_sources SET status = %s, updated_at = now() WHERE slug = %s",
            (status, slug))
    if ad_posted_at is not None:
        updated += execute(
            "UPDATE traffic_sources SET ad_posted_at = %s, updated_at = now() WHERE slug = %s",
            (ad_posted_at, slug))
    return updated


def list_sources() -> list[dict]:
    from aibp.db.connection import fetch_all
    return fetch_all("SELECT * FROM traffic_sources ORDER BY created_at DESC")


# ═══════════════════════════════════════════════════════════════════
# Join attribution (chat_member updates)
# ═══════════════════════════════════════════════════════════════════

_JOINED_STATUSES = ("member", "administrator", "creator")
_GONE_STATUSES = ("left", "kicked")


def parse_chat_member_join(update: dict, prod_chat_id: str) -> dict | None:
    """Extract (invite_link, user_id, joined_at) from a chat_member update.

    Returns None for anything that is not a fresh join into the main channel
    via an invite link: other chats, leaves/kicks, status changes of existing
    members, or joins without a link (direct @username subscriptions).
    """
    cm = update.get("chat_member")
    if not cm:
        return None
    if str(cm.get("chat", {}).get("id", "")) != str(prod_chat_id):
        return None
    old_status = (cm.get("old_chat_member") or {}).get("status")
    new_status = (cm.get("new_chat_member") or {}).get("status")
    if new_status not in _JOINED_STATUSES or old_status not in _GONE_STATUSES:
        return None
    invite_link = (cm.get("invite_link") or {}).get("invite_link")
    if not invite_link:
        return None
    user_id = (cm.get("new_chat_member") or {}).get("user", {}).get("id")
    if user_id is None:
        return None
    joined_at = datetime.fromtimestamp(cm.get("date", 0), tz=UTC).isoformat()
    return {"invite_link": invite_link, "user_id": user_id, "joined_at": joined_at}


def record_join(invite_link: str, user_id: int, joined_at: str) -> bool:
    """Attribute a join to its source. False when the link is not one of ours."""
    from aibp.db.connection import execute, fetch_one
    source = fetch_one(
        "SELECT id, slug FROM traffic_sources WHERE invite_link = %s", (invite_link,))
    if source is None:
        log.info("join_via_unknown_invite_link", invite_link=invite_link)
        return False
    execute(
        """
        INSERT INTO invite_joins (source_id, user_id, joined_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (source_id, user_id) DO NOTHING
        """,
        (source["id"], user_id, joined_at),
    )
    log.info("join_attributed", source=source["slug"], user_id=user_id)
    return True


def handle_chat_member_update(update: dict) -> bool:
    """Full pipeline for one update; never raises (PG down → join lost, warn)."""
    try:
        join = parse_chat_member_join(update, get_settings().telegram_channel_id_prod)
        if join is None:
            return False
        return record_join(**join)
    except Exception as e:
        log.warning("chat_member_update_failed", error=str(e))
        return False


# ═══════════════════════════════════════════════════════════════════
# CPS reporting (consumed by the weekly growth report)
# ═══════════════════════════════════════════════════════════════════

def compute_cps(cost_rub: float, joins: int) -> float | None:
    """Actual cost per subscriber; None until there is at least one join."""
    return (cost_rub / joins) if joins > 0 else None


def cps_summary() -> list[dict]:
    """Joins and actual CPS per source. Empty list when PG is unreachable."""
    try:
        from aibp.db.connection import fetch_all
        rows = fetch_all(
            """
            SELECT ts.slug, ts.kind, ts.channel_username, ts.status,
                   ts.cost_rub::float AS cost_rub,
                   ts.expected_subscribers::float AS expected_subscribers,
                   ts.expected_cps_rub::float AS expected_cps_rub,
                   COUNT(ij.id) AS joins
            FROM traffic_sources ts
            LEFT JOIN invite_joins ij ON ij.source_id = ts.id
            GROUP BY ts.id
            ORDER BY ts.created_at DESC
            """
        )
    except Exception as e:
        log.warning("cps_summary_unavailable", error=str(e))
        return []
    for r in rows:
        r["actual_cps_rub"] = compute_cps(r["cost_rub"] or 0, r["joins"])
    return rows


def cps_report_lines(rows: list[dict]) -> list[str]:
    """Markdown section for the weekly growth report: forecast vs actuals."""
    lines = ["## Источники трафика — фактический CPS"]
    if not rows:
        lines.append("Источников пока нет (aibp source-add / aibp ad-plan).")
        return lines
    for r in rows:
        channel = f" (@{r['channel_username']})" if r.get("channel_username") else ""
        lines.append(f"### {r['slug']}{channel} — {r['kind']}, {r['status']}")
        lines.append(f"- Подписок по ссылке: **{r['joins']}**")
        cost = r.get("cost_rub") or 0
        if cost:
            actual = r.get("actual_cps_rub")
            actual_str = f"{actual:.0f} ₽/подписчик" if actual is not None else "— (0 подписок)"
            lines.append(f"- Затраты: {cost:.0f} ₽ → фактический CPS: **{actual_str}**")
        if r.get("expected_subscribers") is not None:
            lines.append(f"- Прогноз был: ~{r['expected_subscribers']:.0f} подписчиков"
                         + (f" при CPS ≤ {r['expected_cps_rub']:.0f} ₽"
                            if r.get("expected_cps_rub") is not None else ""))
        lines.append("")
    return lines
