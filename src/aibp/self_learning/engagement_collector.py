"""Engagement Collector — fetches views/forwards from TG Bot API, stores in SQLite.

Cron: every 4h via Hermes.

Strategy:
  1. Primary: copyMessage to a private "metrics" chat (bot's own chat with owner),
     read views/forwards/reactions from the copy, then delete it.
     This works for ANY post (not just recent 100 updates), and avoids 409 conflicts.
  2. Fallback: getUpdates scan (only for posts in last ~48h, may conflict with webhooks).

If copyMessage chat is not configured, falls back to getUpdates with explicit
409 handling and alerting.

See ADR-0005 for Bot API vs MTProto trade-offs.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from aibp.db.connection import fetch_all
from aibp.self_learning.db import sqlite_conn
from aibp.utils.config import get_settings
from aibp.utils.summary import parse_summary

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"

# Regex for validating chat_id format (negative number for channels/groups)
CHAT_ID_RE = re.compile(r"^-?\d+$")


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _parse_chat_id(chat_id: str | int | None) -> int | None:
    """Safely parse chat_id to int. Returns None if invalid.

    Handles: None, empty string, non-numeric strings, float-like strings.
    """
    if chat_id is None:
        return None
    if isinstance(chat_id, int):
        return chat_id
    s = str(chat_id).strip()
    if not s:
        return None
    if not CHAT_ID_RE.match(s):
        log.warning("invalid_chat_id", chat_id=chat_id)
        return None
    try:
        return int(s)
    except (ValueError, OverflowError):
        log.warning("chat_id_overflow", chat_id=chat_id)
        return None


async def _tg_call(
    client: httpx.AsyncClient,
    bot_token: str,
    method: str,
    payload: dict,
) -> dict:
    """Make Telegram Bot API call. Returns parsed JSON response."""
    url = f"{TELEGRAM_API}/bot{bot_token}/{method}"
    resp = await client.post(url, json=payload)
    return resp.json()


async def _send_alert(bot_token: str, alert_chat_id: str, message: str) -> None:
    """Send alert message to owner (best-effort, no error propagation)."""
    if not alert_chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await _tg_call(client, bot_token, "sendMessage", {
                "chat_id": alert_chat_id,
                "text": f"⚠️ AIBP Engagement Collector\n\n{message}",
            })
    except Exception as e:
        log.error("alert_failed", error=str(e))


# ═══════════════════════════════════════════════════════════════════
# Subscriber count
# ═══════════════════════════════════════════════════════════════════

async def get_chat_members_count(bot_token: str, chat_id: str) -> int | None:
    """Get current subscriber count for a channel/chat."""
    parsed = _parse_chat_id(chat_id)
    if parsed is None:
        log.error("invalid_chat_id_for_members_count", chat_id=chat_id)
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            data = await _tg_call(client, bot_token, "getChatMembersCount", {"chat_id": parsed})
        if data.get("ok"):
            return data.get("result")
        log.warning("members_count_failed", chat_id=chat_id, error=data.get("description"))
        return None
    except Exception as e:
        log.error("members_count_error", chat_id=chat_id, error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════════
# Primary strategy: copyMessage workaround
# ═══════════════════════════════════════════════════════════════════

async def get_views_via_copy(
    bot_token: str,
    source_chat_id: int,
    message_id: str | int,
    metrics_chat_id: int,
) -> dict | None:
    """Get views by copying message to a private metrics chat, then deleting.

    How it works:
      1. copyMessage from source channel to metrics chat → returns Message object
      2. The copied Message contains `views`, `forwards`, `reactions` fields
      3. deleteMessage to clean up

    Pros: works for ANY post (not just last 100 updates), no 409 conflicts.
    Cons: requires a private chat where bot can send+delete messages.

    Args:
        bot_token: Bot API token
        source_chat_id: Channel ID where the original post lives
        message_id: Message ID in the source channel
        metrics_chat_id: Private chat ID for temporary copies (your personal chat)

    Returns:
        dict with views, forwards, reactions_count, reactions_breakdown — or None.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Copy message to metrics chat
            copy_resp = await _tg_call(client, bot_token, "copyMessage", {
                "chat_id": metrics_chat_id,
                "from_chat_id": source_chat_id,
                "message_id": int(message_id),
            })

            if not copy_resp.get("ok"):
                error_desc = copy_resp.get("description", "unknown error")
                error_code = copy_resp.get("error_code")
                # Common errors:
                # 400 "message to copy not found" — post was deleted
                # 400 "chat not found" — wrong metrics_chat_id
                # 403 "bot was blocked by the user" — owner blocked the bot
                log.warning(
                    "copy_message_failed",
                    source_chat=source_chat_id,
                    message_id=message_id,
                    error_code=error_code,
                    error=error_desc,
                )
                return None

            copied_message = copy_resp.get("result", {})
            copied_message_id = copied_message.get("message_id")

            # Step 2: Extract engagement metrics from the copy
            # Note: views/forwards are only present for channel posts.
            # If the source is a channel, the copy retains these fields.
            metrics = {
                "views": copied_message.get("views", 0) or 0,
                "forwards": copied_message.get("forwards", 0) or 0,
                "replies": (
                    copied_message.get("replies", {}).get("total_count", 0)
                    if isinstance(copied_message.get("replies"), dict)
                    else 0
                ),
                "reactions_count": sum(
                    r.get("total_count", 0) for r in copied_message.get("reactions", []) or []
                ),
                "reactions_breakdown": json.dumps(
                    copied_message.get("reactions", []), ensure_ascii=False
                ),
            }

            # Step 3: Delete the copy to keep metrics chat clean
            if copied_message_id:
                await _tg_call(client, bot_token, "deleteMessage", {
                    "chat_id": metrics_chat_id,
                    "message_id": copied_message_id,
                })

            return metrics

    except httpx.HTTPError as e:
        log.error("copy_message_http_error", error=str(e))
        return None
    except Exception as e:
        log.error("copy_message_error", error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════════
# Fallback strategy: getUpdates scan
# ═══════════════════════════════════════════════════════════════════

async def get_views_via_updates(
    bot_token: str,
    chat_id: int,
    message_id: str | int,
    alert_chat_id: str = "",
) -> dict | None:
    """Fallback: scan getUpdates for the message. Only works for recent posts.

    Limitations:
      - Only finds posts in the last ~100 updates (~48h)
      - May return 409 Conflict if a webhook is set or another process uses getUpdates
      - Older posts are silently skipped (returns None)
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            data = await _tg_call(client, bot_token, "getUpdates", {
                "offset": -100,
                "allowed_updates": "channel_post",
            })
    except httpx.HTTPError as e:
        log.error("get_updates_http_error", error=str(e))
        return None

    if not data.get("ok"):
        error_code = data.get("error_code")
        error_desc = data.get("description", "")

        # 409 Conflict: another instance or webhook is using getUpdates
        if error_code == 409:
            log.error(
                "get_updates_conflict",
                error=error_desc,
                hint="Bot has webhook set OR another process uses getUpdates. "
                     "Configure TELEGRAM_METRICS_CHAT_ID to use copyMessage method instead.",
            )
            if alert_chat_id:
                await _send_alert(
                    bot_token, alert_chat_id,
                    "getUpdates conflict (409). Engagement collector cannot use fallback method.\n"
                    "Configure TELEGRAM_METRICS_CHAT_ID in .env to enable copyMessage method.\n"
                    f"Error: {error_desc}"
                )
        else:
            log.warning("get_updates_failed", error_code=error_code, error=error_desc)
        return None

    # Scan updates for matching message
    for update in data.get("result", []):
        cp = update.get("channel_post", {})
        if (
            cp.get("chat", {}).get("id") == chat_id
            and str(cp.get("message_id")) == str(message_id)
        ):
            return {
                "views": cp.get("views", 0) or 0,
                "forwards": cp.get("forwards", 0) or 0,
                "replies": (
                    cp.get("replies", {}).get("total_count", 0)
                    if isinstance(cp.get("replies"), dict)
                    else 0
                ),
                "reactions_count": sum(
                    r.get("total_count", 0) for r in cp.get("reactions", []) or []
                ),
                "reactions_breakdown": json.dumps(cp.get("reactions", []), ensure_ascii=False),
            }

    return None


# ═══════════════════════════════════════════════════════════════════
# Unified engagement collection
# ═══════════════════════════════════════════════════════════════════

async def collect_engagement_for_post(
    bot_token: str,
    item: dict,
    subscribers_at: int,
    metrics_chat_id: str = "",
    alert_chat_id: str = "",
) -> dict | None:
    """Collect engagement for one post.

    Tries copyMessage method first (if metrics_chat_id configured),
    falls back to getUpdates scan.

    Args:
        bot_token: Telegram bot token
        item: feed_items row dict (must have target_channel_id, published_message_id)
        subscribers_at: Current subscriber count for normalization
        metrics_chat_id: Private chat ID for copyMessage method (optional)
        alert_chat_id: Where to send alerts on 409 conflict (optional)

    Returns:
        dict with views, forwards, replies, reactions_count, reactions_breakdown,
        subscribers_at — or None if collection failed.
    """
    raw_chat_id = item.get("target_channel_id")
    message_id = item.get("published_message_id")

    if not raw_chat_id or not message_id:
        return None

    chat_id = _parse_chat_id(raw_chat_id)
    if chat_id is None:
        log.warning("skipping_invalid_chat_id", feed_item_id=item.get("id"), chat_id=raw_chat_id)
        return None

    metrics = None
    method_used = None

    # Primary: copyMessage (if configured)
    if metrics_chat_id:
        metrics_chat_int = _parse_chat_id(metrics_chat_id)
        if metrics_chat_int is not None:
            metrics = await get_views_via_copy(bot_token, chat_id, message_id, metrics_chat_int)
            method_used = "copyMessage"

    # Fallback: getUpdates
    if metrics is None:
        metrics = await get_views_via_updates(bot_token, chat_id, message_id, alert_chat_id)
        method_used = "getUpdates"

    if metrics is None:
        return None

    metrics["subscribers_at"] = subscribers_at
    metrics["method"] = method_used  # for debugging
    return metrics


# ═══════════════════════════════════════════════════════════════════
# Feature extraction (unchanged, refactored to use parse_summary)
# ═══════════════════════════════════════════════════════════════════

def extract_features_for_post(item: dict) -> dict:
    """Extract features from a feed_items row for post_features table."""
    post = item.get("post_draft") or ""
    summary = parse_summary(item.get("summary"))

    # Extract slot from used_as field (e.g., "morning_post" → "morning")
    used_as = item.get("used_as") or ""
    slot = "unknown"
    for s in ("morning", "evening", "weekly_digest", "weekly"):
        if s in used_as:
            slot = s
            break

    posted_at = item.get("posted_at")
    scheduled_hour = None
    if posted_at:
        try:
            if hasattr(posted_at, "astimezone"):
                scheduled_hour = posted_at.astimezone().hour
            else:
                scheduled_hour = int(str(posted_at)[11:13])
        except (ValueError, TypeError, IndexError):
            scheduled_hour = None

    return {
        "feed_item_id": item["id"],
        "posted_at": posted_at,
        "slot": slot,
        "pipeline_env": item.get("pipeline_env", "prod"),
        "target_channel": item.get("target_channel", "main"),
        "strategy_rubric": summary.get("strategy_rubric"),
        "topic_cluster": summary.get("topic_cluster"),
        "source_domain": item.get("source_domain"),
        "source_url": item.get("url"),
        "char_count": len(post),
        "paragraph_count": len([p for p in re.split(r"\n\s*\n", post) if p.strip()]),
        "bold_count": len(re.findall(r"<b>.*?</b>", post, re.DOTALL)),
        "emoji_count": len(re.findall(r"[🗞🚀🔥💡✅⚡🤖]", post)),
        "has_image": 1 if item.get("image_url") else 0,
        "visual_kind": summary.get("visual_kind"),
        "scheduled_hour": scheduled_hour,
        "cta_variant": summary.get("cta_variant"),
        "policy_version": summary.get("policy_version", "unknown"),
        "policy_blob": json.dumps(summary, ensure_ascii=False),
    }


# ═══════════════════════════════════════════════════════════════════
# Main runner
# ═══════════════════════════════════════════════════════════════════

async def run_async() -> int:
    """Main entry point — collect engagement for all recent posts."""
    s = get_settings()
    if not s.telegram_bot_token:
        log.error("no_bot_token")
        return 1

    # Optional: metrics chat for copyMessage method
    metrics_chat_id = os.getenv("TELEGRAM_METRICS_CHAT_ID", "") if (os := __import__("os")) else ""
    alert_chat_id = s.telegram_alert_chat_id

    if metrics_chat_id:
        log.info("using_copyMessage_method", metrics_chat=metrics_chat_id)
    else:
        log.warning(
            "copyMessage_not_configured",
            hint="Set TELEGRAM_METRICS_CHAT_ID in .env for reliable engagement collection. "
                 "Without it, falling back to getUpdates (only recent posts, 409 risk).",
        )
        if alert_chat_id:
            await _send_alert(
                s.telegram_bot_token, alert_chat_id,
                "Engagement collector is running without TELEGRAM_METRICS_CHAT_ID.\n"
                "Falling back to getUpdates method (limited to recent posts, may conflict with webhooks).\n"
                "Set TELEGRAM_METRICS_CHAT_ID in .env for reliable collection."
            )

    # Get current subscribers count for both channels
    subs_prod = await get_chat_members_count(s.telegram_bot_token, s.telegram_channel_id_prod)
    subs_test = await get_chat_members_count(s.telegram_bot_token, s.telegram_channel_id_test)
    log.info("subscribers", prod=subs_prod, test=subs_test)

    # Get posts from last 7 days
    recent_posts = fetch_all(
        """
        SELECT id, url, title, post_draft, summary, source_domain, source_published_at,
               posted_at, published_message_id, pipeline_env, target_channel, used_as,
               need_image, image_url
        FROM feed_items
        WHERE posted_at > now() - interval '7 days'
          AND published_message_id IS NOT NULL
          AND post_draft IS NOT NULL
        ORDER BY posted_at DESC
        LIMIT 50
        """
    )

    if not recent_posts:
        log.info("no_recent_posts")
        return 0

    # Store features for posts that don't have them yet
    with sqlite_conn() as conn:
        existing = {row["feed_item_id"] for row in conn.execute("SELECT feed_item_id FROM post_features")}

    new_features = 0
    for item in recent_posts:
        if item["id"] in existing:
            continue
        try:
            features = extract_features_for_post(item)
            with sqlite_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO post_features
                        (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                         strategy_rubric, topic_cluster, source_domain, source_url,
                         char_count, paragraph_count, bold_count, emoji_count,
                         has_image, visual_kind, scheduled_hour, cta_variant,
                         policy_version, policy_blob)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    tuple(features.values()),
                )
            new_features += 1
        except Exception as e:
            log.error("feature_extraction_failed", item_id=item["id"], error=str(e))

    log.info("features_stored", new=new_features, total=len(recent_posts))

    # Collect engagement for all recent posts
    def channel_id(target: str) -> str:
        return s.telegram_channel_id_test if target == "test" else s.telegram_channel_id_prod

    metrics_collected = 0
    metrics_failed = 0

    for item in recent_posts:
        item_with_chat = {**item, "target_channel_id": channel_id(item.get("target_channel", "main"))}
        subs = subs_test if item.get("target_channel") == "test" else subs_prod

        metrics = await collect_engagement_for_post(
            s.telegram_bot_token,
            item_with_chat,
            subs or 0,
            metrics_chat_id=metrics_chat_id,
            alert_chat_id=alert_chat_id,
        )

        if metrics:
            with sqlite_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO engagement_metrics
                        (feed_item_id, measured_at, views, forwards, replies,
                         reactions_count, reactions_breakdown, subscribers_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        datetime.now(timezone.utc).isoformat(),
                        metrics["views"],
                        metrics["forwards"],
                        metrics["replies"],
                        metrics["reactions_count"],
                        metrics["reactions_breakdown"],
                        metrics["subscribers_at"],
                    ),
                )
            metrics_collected += 1
        else:
            metrics_failed += 1

        # Rate limit: Telegram allows ~30 requests/sec, but be conservative
        await asyncio.sleep(0.5)

    log.info(
        "engagement_collected",
        posts=len(recent_posts),
        collected=metrics_collected,
        failed=metrics_failed,
        method="copyMessage" if metrics_chat_id else "getUpdates",
    )
    return 0


def run() -> int:
    return asyncio.run(run_async())


if __name__ == "__main__":
    raise SystemExit(run())
