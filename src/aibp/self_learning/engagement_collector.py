"""Engagement Collector — fetches views/forwards from TG Bot API, stores in SQLite.

Cron: every 4h via Hermes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import structlog

from aibp.db.connection import fetch_all
from aibp.self_learning.db import sqlite_conn
from aibp.utils.config import get_settings

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"


async def get_chat_members_count(bot_token: str, chat_id: str) -> int | None:
    """Get current subscriber count."""
    url = f"{TELEGRAM_API}/bot{bot_token}/getChatMembersCount"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={"chat_id": chat_id})
        data = resp.json()
    if data.get("ok"):
        return data.get("result")
    return None


async def get_message_views(
    bot_token: str,
    chat_id: str,
    message_id: str,
) -> dict | None:
    """Get view count for a specific message via getUpdates.

    Note: TG Bot API only returns views for channel posts in getUpdates.
    For older messages, we'd need MTProto. This is best-effort.
    """
    # Try forward message trick — too risky. Use getUpdates approach.
    # Actually, the cleanest way is to track views via updates from the start.
    # For MVP, we accept that views are only available for recent posts.
    return None


async def collect_engagement_for_post(
    bot_token: str,
    item: dict,
    subscribers_at: int,
) -> dict | None:
    """Collect engagement for one post. Returns metrics dict or None."""
    chat_id = item["target_channel_id"]
    message_id = item["published_message_id"]

    if not chat_id or not message_id:
        return None

    # TG Bot API doesn't have direct "getMessageViews". We rely on:
    # 1. channel_post updates that contain views field
    # 2. For older posts, we use the last known views from getUpdates history

    # For MVP: we can only get views if the post is recent (within 48h)
    # and appears in getUpdates. This is a known limitation.
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates"
    params = {
        "offset": -100,  # last 100 updates
        "allowed_updates": "channel_post",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=params)
        data = resp.json()

    if not data.get("ok"):
        return None

    for update in data.get("result", []):
        cp = update.get("channel_post", {})
        if (
            cp.get("chat", {}).get("id") == int(chat_id)
            and str(cp.get("message_id")) == str(message_id)
        ):
            return {
                "views": cp.get("views", 0),
                "forwards": cp.get("forwards", 0),
                "replies": cp.get("replies", {}).get("total_count", 0) if isinstance(cp.get("replies"), dict) else 0,
                "reactions_count": sum(
                    r.get("total_count", 0) for r in cp.get("reactions", []) or []
                ),
                "reactions_breakdown": json.dumps(cp.get("reactions", []), ensure_ascii=False),
                "subscribers_at": subscribers_at,
            }

    return None


def extract_features_for_post(item: dict) -> dict:
    """Extract features from a feed_items row for post_features table."""
    import re
    post = item.get("post_draft") or ""
    summary = item.get("summary") or {}
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except json.JSONDecodeError:
            summary = {}

    return {
        "feed_item_id": item["id"],
        "posted_at": item["posted_at"],
        "slot": (item.get("used_as") or "").replace("_post", "").replace("morning", "morning").replace("evening", "evening") or "unknown",
        "pipeline_env": item.get("pipeline_env", "prod"),
        "target_channel": item.get("target_channel", "main"),
        "strategy_rubric": summary.get("strategy_rubric") if isinstance(summary, dict) else None,
        "topic_cluster": summary.get("topic_cluster") if isinstance(summary, dict) else None,
        "source_domain": item.get("source_domain"),
        "source_url": item.get("url"),
        "char_count": len(post),
        "paragraph_count": len([p for p in re.split(r"\n\s*\n", post) if p.strip()]),
        "bold_count": len(re.findall(r"<b>.*?</b>", post, re.DOTALL)),
        "emoji_count": len(re.findall(r"[🗞🚀🔥💡✅⚡🤖]", post)),
        "has_image": 1 if item.get("image_url") else 0,
        "visual_kind": summary.get("visual_kind") if isinstance(summary, dict) else None,
        "scheduled_hour": int(item["posted_at"].astimezone().hour) if item.get("posted_at") else None,
        "policy_version": summary.get("policy_version", "unknown") if isinstance(summary, dict) else "unknown",
        "policy_blob": json.dumps(summary, ensure_ascii=False) if isinstance(summary, dict) else "{}",
    }


async def run_async() -> int:
    """Main entry point."""
    s = get_settings()
    if not s.telegram_bot_token:
        log.error("no_bot_token")
        return 1

    # Get current subscribers count for both channels
    subs_prod = await get_chat_members_count(s.telegram_bot_token, s.telegram_channel_id_prod)
    subs_test = await get_chat_members_count(s.telegram_bot_token, s.telegram_channel_id_test)
    log.info("subscribers", prod=subs_prod, test=subs_test)

    # Get posts from last 7 days that don't have features yet
    from aibp.db.connection import fetch_all
    recent_posts = fetch_all(
        """
        SELECT id, url, title, post_draft, summary, source_domain, source_published_at,
               posted_at, published_message_id, pipeline_env, target_channel, used_as, need_image, image_url
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
        features = extract_features_for_post(item)
        with sqlite_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO post_features
                    (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                     strategy_rubric, topic_cluster, source_domain, source_url,
                     char_count, paragraph_count, bold_count, emoji_count,
                     has_image, visual_kind, scheduled_hour, policy_version, policy_blob)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(features.values()),
            )
        new_features += 1
    log.info("features_stored", new=new_features, total=len(recent_posts))

    # Collect engagement for all recent posts
    # Map target_channel to actual chat ID
    def channel_id(target: str) -> str:
        return s.telegram_channel_id_test if target == "test" else s.telegram_channel_id_prod

    metrics_collected = 0
    for item in recent_posts:
        item_with_chat = {**item, "target_channel_id": channel_id(item.get("target_channel", "main"))}
        subs = subs_test if item.get("target_channel") == "test" else subs_prod
        metrics = await collect_engagement_for_post(s.telegram_bot_token, item_with_chat, subs or 0)

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

    log.info("engagement_collected", posts=len(recent_posts), metrics=metrics_collected)
    return 0


def run() -> int:
    return asyncio.run(run_async())


if __name__ == "__main__":
    raise SystemExit(run())
