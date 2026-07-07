"""Telegram publisher — polls feed_items for approved posts and publishes.

Cron: every 5 minutes via Hermes.
"""
from __future__ import annotations

import asyncio

import httpx
import structlog

from aibp.db.connection import execute, fetch_all
from aibp.utils.config import get_settings
from aibp.utils.summary import parse_summary

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"

# Bot API limits (verified 2026-07, core.telegram.org/bots/api):
#   sendMessage.text   : 1–4096 chars
#   sendPhoto.caption  : 0–1024 chars
# There is no method for "media + >1024 chars in one message"; for long posts
# we attach the image as a large link-preview on the text message instead
# (LinkPreviewOptions, Bot API 7.0). See ADR-0009.
CAPTION_LIMIT = 1024


async def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    disable_preview: bool = True,
    reply_markup: dict | None = None,
    link_preview_options: dict | None = None,
) -> dict:
    """Send Telegram message via Bot API.

    reply_markup: optional inline keyboard (used by the approval gate).
    link_preview_options: LinkPreviewOptions dict (Bot API 7.0). When given it
    supersedes disable_web_page_preview — used to attach a large media preview
    to a long post (issue #28).
    """
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if link_preview_options is not None:
        payload["link_preview_options"] = link_preview_options
    else:
        payload["disable_web_page_preview"] = disable_preview
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


async def send_photo(
    bot_token: str,
    chat_id: str,
    photo_url: str,
    caption: str,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> dict:
    """Send photo with caption via Bot API."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": parse_mode,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


async def send_poll(
    bot_token: str,
    chat_id: str,
    question: str,
    options: list[str],
    is_anonymous: bool = True,
) -> dict:
    """Send a native poll (issue #33). A poll is its own message.

    Bot API limits: question 1-300 chars, 2-12 options, option text 1-100.
    """
    url = f"{TELEGRAM_API}/bot{bot_token}/sendPoll"
    payload = {
        "chat_id": chat_id,
        "question": question[:300],
        "options": [{"text": o[:100]} for o in options[:12]],
        "is_anonymous": is_anonymous,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


def source_button_markup(tracked_url: str) -> dict:
    """Inline keyboard with a tracked 'open source' button (issue #33)."""
    return {"inline_keyboard": [[{"text": "Открыть источник →", "url": tracked_url}]]}


def get_channel_id(target_channel: str) -> str:
    """Map target_channel name to Telegram chat ID from settings."""
    s = get_settings()
    if target_channel == "test":
        return s.telegram_channel_id_test
    return s.telegram_channel_id_prod


async def _publish_post_message(bot_token: str, chat_id: str, item: dict, post_text: str,
                                reply_markup: dict | None = None) -> dict:
    """Send the post as a rich message, choosing the path by length (issue #28).

    - media + text ≤ 1024 → sendPhoto with the full text as caption (one msg);
    - media + longer text → sendMessage with the image as a large link preview
      (one msg, up to 4096 chars);
    - no media, or a media send that fails → plain text message (fallback:
      a post without a picture beats a failed publish).

    reply_markup (issue #33): optional inline keyboard, e.g. a tracked source
    button. It is dropped only on the fallback text send if it caused a failure.
    """
    has_media = bool(item.get("need_image") and item.get("image_url"))

    if has_media:
        if len(post_text) <= CAPTION_LIMIT:
            result = await send_photo(
                bot_token=bot_token, chat_id=chat_id,
                photo_url=item["image_url"], caption=post_text,
                reply_markup=reply_markup,
            )
        else:
            result = await send_message(
                bot_token=bot_token, chat_id=chat_id, text=post_text,
                reply_markup=reply_markup,
                link_preview_options={
                    "url": item["image_url"],
                    "prefer_large_media": True,
                    "show_above_text": True,
                },
            )
        if result.get("ok"):
            return result
        log.warning("media_publish_failed_fallback_text",
                    item_id=item.get("id"), error=result.get("description"))

    return await send_message(bot_token=bot_token, chat_id=chat_id, text=post_text,
                              reply_markup=reply_markup)


def _post_extras(item: dict) -> tuple[dict | None, dict | None]:
    """Native elements from summary (issue #33): (source-button markup, poll)."""
    summary = parse_summary(item.get("summary"))
    reply_markup = None
    button_url = summary.get("source_button_url")
    if button_url:
        reply_markup = source_button_markup(button_url)
    poll = summary.get("poll") if isinstance(summary.get("poll"), dict) else None
    return reply_markup, poll


async def publish_one(item: dict) -> bool:
    """Publish one feed_item. Returns True on success."""
    s = get_settings()
    chat_id = get_channel_id(item["target_channel"])
    post_text = item["post_draft"]

    if not post_text or not chat_id:
        log.error("missing_data", item_id=item["id"], has_text=bool(post_text), has_chat=bool(chat_id))
        return False

    log.info("publishing", item_id=item["id"], channel=item["target_channel"], chat_id=chat_id)

    reply_markup, poll = _post_extras(item)

    try:
        result = await _publish_post_message(s.telegram_bot_token, chat_id, item, post_text,
                                             reply_markup=reply_markup)
    except Exception as e:
        log.error("telegram_api_error", item_id=item["id"], error=str(e))
        execute(
            """
            UPDATE feed_items
            SET publish_error = %s,
                publish_attempts = publish_attempts + 1,
                updated_at = now()
            WHERE id = %s
            """,
            (str(e)[:500], item["id"]),
        )
        return False

    if not result.get("ok"):
        error = result.get("description", "unknown error")
        log.error("telegram_error", item_id=item["id"], error=error)
        execute(
            """
            UPDATE feed_items
            SET publish_error = %s,
                publish_attempts = publish_attempts + 1,
                updated_at = now()
            WHERE id = %s
            """,
            (error[:500], item["id"]),
        )
        return False

    message_id = str(result.get("result", {}).get("message_id", ""))
    log.info("published", item_id=item["id"], message_id=message_id)

    # Native poll as a follow-up message (issue #33). Best-effort: a poll
    # failure must not undo an already-published post.
    if poll and poll.get("question") and poll.get("options"):
        try:
            poll_result = await send_poll(
                bot_token=s.telegram_bot_token, chat_id=chat_id,
                question=poll["question"], options=poll["options"],
            )
            if not poll_result.get("ok"):
                log.warning("poll_send_failed", item_id=item["id"],
                            error=poll_result.get("description"))
        except Exception as e:
            log.warning("poll_send_error", item_id=item["id"], error=str(e))

    # Mark as published
    execute(
        """
        UPDATE feed_items
        SET posted_at = now(),
            published_message_id = %s,
            is_used = true,
            status = 'published',
            publish_error = NULL,
            updated_at = now()
        WHERE id = %s
        """,
        (message_id, item["id"]),
    )
    return True


async def run_async() -> int:
    """Main loop — publish all due posts."""
    # Use the view v_publisher_queue
    due_posts = fetch_all(
        """
        SELECT id, title, post_draft, scheduled_at, need_image, image_url,
               telegram_file_id, pipeline_env, target_channel, used_as, summary
        FROM v_publisher_queue
        ORDER BY scheduler_priority ASC, scheduled_at ASC
        LIMIT 10
        """
    )

    if not due_posts:
        log.info("no_due_posts")
        return 0

    log.info("publishing_batch", count=len(due_posts))
    published = 0
    for item in due_posts:
        if await publish_one(item):
            published += 1
        # Small delay between posts to avoid rate limit
        await asyncio.sleep(2)

    log.info("publish_complete", published=published, total=len(due_posts))
    return 0 if published == len(due_posts) else 1


def run() -> int:
    """Sync entry point for cron."""
    return asyncio.run(run_async())


if __name__ == "__main__":
    raise SystemExit(run())
