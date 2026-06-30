"""Telegram publisher — polls feed_items for approved posts and publishes.

Cron: every 5 minutes via Hermes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import structlog

from aibp.db.connection import fetch_all, execute, fetch_one
from aibp.utils.config import get_settings

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"


async def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    disable_preview: bool = True,
) -> dict:
    """Send Telegram message via Bot API."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


async def send_photo(
    bot_token: str,
    chat_id: str,
    photo_url: str,
    caption: str,
    parse_mode: str = "HTML",
) -> dict:
    """Send photo with caption via Bot API."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendPhoto"
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": parse_mode,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload)
        return resp.json()


def get_channel_id(target_channel: str) -> str:
    """Map target_channel name to Telegram chat ID from settings."""
    s = get_settings()
    if target_channel == "test":
        return s.telegram_channel_id_test
    return s.telegram_channel_id_prod


async def publish_one(item: dict) -> bool:
    """Publish one feed_item. Returns True on success."""
    s = get_settings()
    chat_id = get_channel_id(item["target_channel"])
    post_text = item["post_draft"]

    if not post_text or not chat_id:
        log.error("missing_data", item_id=item["id"], has_text=bool(post_text), has_chat=bool(chat_id))
        return False

    log.info("publishing", item_id=item["id"], channel=item["target_channel"], chat_id=chat_id)

    try:
        if item.get("need_image") and item.get("image_url"):
            result = await send_photo(
                bot_token=s.telegram_bot_token,
                chat_id=chat_id,
                photo_url=item["image_url"],
                caption=post_text,
            )
        else:
            result = await send_message(
                bot_token=s.telegram_bot_token,
                chat_id=chat_id,
                text=post_text,
            )
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
               telegram_file_id, pipeline_env, target_channel, used_as
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
