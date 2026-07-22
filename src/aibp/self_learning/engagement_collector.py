"""Engagement Collector — reads real post views + subscriber count, stores in PostgreSQL.

Cron: every 4h via Hermes.

Views come from the public web preview `https://t.me/s/<username>`: it renders
every post with its real `views` counter and a `data-post="<user>/<msg_id>"`
anchor that joins straight to feed_items.published_message_id. One page holds
~20 posts, so the whole recent window is fetched in one or two requests and
turned into a {message_id: views} map — no per-post API call.

Why not the Bot API: it has NO way to read a channel post's view count.
`copyMessage` returns only a MessageId (no views), and a copied/forwarded
message starts its own counter at 0; `getUpdates` only sees a post at
publish time (views=0) and drops out of the ~100-update window within ~48h.
Both silently yielded zeros for 11 days (issue #49). See ADR-0005.

Forwards/reactions are NOT in the web preview. At this channel's scale they
are ~0 (reward weight × 0 = 0); if they ever matter, ADR-0005 Tier 3 (MTProto)
is the documented upgrade. Subscriber count still comes from the Bot API
(getChatMembersCount). Clicks are collected separately by the redirect service.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime

import httpx
import structlog
from psycopg2.extras import Json

from aibp.db.connection import execute, fetch_all
from aibp.utils.config import get_settings
from aibp.utils.summary import parse_summary

log = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"
TME_PREVIEW = "https://t.me/s"

# Regex for validating chat_id format (negative number for channels/groups)
CHAT_ID_RE = re.compile(r"^-?\d+$")

# Web-preview parsing: each post exposes data-post="<username>/<msg_id>" and a
# views span. Counts may be abbreviated ("1.2K", "3M").
_POST_ANCHOR_RE = re.compile(r'data-post="[^"/]+/(\d+)"')
_VIEWS_RE = re.compile(r'tgme_widget_message_views">([\d.,]+[KMB]?)<')


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


def _post_age_seconds(posted_at) -> float | None:
    """Seconds since a post's posted_at, or None if it can't be determined."""
    if posted_at is None or not hasattr(posted_at, "tzinfo"):
        return None
    dt = posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


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
# Views via public web preview (t.me/s/<username>)
# ═══════════════════════════════════════════════════════════════════

def _parse_count(raw: str) -> int:
    """Parse a Telegram view counter: '8' → 8, '1.2K' → 1200, '3M' → 3_000_000."""
    s = raw.strip().replace(",", "").replace(" ", "")
    if not s:
        return 0
    mult = 1
    if s[-1] in "KMB":
        mult = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[s[-1]]
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def parse_views_from_html(html: str) -> dict[int, int]:
    """Build {message_id: views} from one t.me/s preview page.

    The preview lists posts in document order; each post's data-post anchor
    precedes its views span, so we pair each anchor with the first views span
    that appears before the next anchor. A post with no views span (just
    posted) is recorded as 0 so the caller can tell "seen but no views yet"
    from "not on this page" (absent from the map).
    """
    anchors = list(_POST_ANCHOR_RE.finditer(html))
    views_map: dict[int, int] = {}
    for i, m in enumerate(anchors):
        msg_id = int(m.group(1))
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        vm = _VIEWS_RE.search(html, m.end(), end)
        views_map[msg_id] = _parse_count(vm.group(1)) if vm else 0
    return views_map


async def fetch_views_map(
    username: str,
    min_message_id: int = 0,
    max_pages: int = 30,
) -> dict[int, int]:
    """Fetch views for a public channel via its web preview, paginating back.

    Walks `t.me/s/<username>?before=<oldest_id>` until the oldest post on a
    page is <= min_message_id (window covered), a page repeats/empties, or
    max_pages is hit (safety bound for a full-history rebuild).

    Returns {message_id: views} across all fetched pages.
    """
    views: dict[int, int] = {}
    before: int | None = None
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; aibp-metrics/1.0)"},
        ) as client:
            for _ in range(max_pages):
                params = {"before": before} if before else {}
                resp = await client.get(f"{TME_PREVIEW}/{username}", params=params)
                resp.raise_for_status()
                page = parse_views_from_html(resp.text)
                if not page:
                    break
                new_ids = set(page) - set(views)
                if not new_ids:
                    break  # pagination did not advance
                views.update(page)
                oldest = min(page)
                if oldest <= min_message_id:
                    break
                before = oldest
                await asyncio.sleep(0.3)  # be gentle on t.me
    except httpx.HTTPError as e:
        log.error("web_preview_http_error", username=username, error=str(e))
    except Exception as e:
        log.error("web_preview_error", username=username, error=str(e))
    return views


def build_metrics(views: int, subscribers_at: int) -> dict:
    """Engagement row from a web-preview view count.

    forwards/replies/reactions are unavailable via the web preview (see module
    docstring) — stored as 0 rather than guessed.
    """
    return {
        "views": views,
        "forwards": 0,
        "replies": 0,
        "reactions_count": 0,
        "reactions_breakdown": "[]",
        "subscribers_at": subscribers_at,
    }


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
        "policy_blob": summary,
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

    alert_chat_id = s.telegram_alert_chat_id
    prod_username = s.telegram_channel_username_prod

    if not prod_username:
        log.error(
            "no_channel_username",
            hint="Set TELEGRAM_CHANNEL_USERNAME_PROD in .env — required to read "
                 "post views from the t.me/s web preview (see ADR-0005).",
        )
        if alert_chat_id:
            await _send_alert(
                s.telegram_bot_token, alert_chat_id,
                "Engagement collector cannot read views: TELEGRAM_CHANNEL_USERNAME_PROD "
                "is not set in .env. Subscriber count is still collected."
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
    existing = {row["feed_item_id"] for row in fetch_all(
        "SELECT feed_item_id FROM post_features"
    )}

    new_features = 0
    for item in recent_posts:
        if item["id"] in existing:
            continue
        try:
            features = extract_features_for_post(item)
            execute(
                """
                INSERT INTO post_features
                    (feed_item_id, posted_at, slot, pipeline_env, target_channel,
                     strategy_rubric, topic_cluster, source_domain, source_url,
                     char_count, paragraph_count, bold_count, emoji_count,
                     has_image, visual_kind, scheduled_hour, cta_variant,
                     policy_version, policy_blob)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (feed_item_id) DO UPDATE SET
                    posted_at = EXCLUDED.posted_at,
                    slot = EXCLUDED.slot,
                    policy_version = EXCLUDED.policy_version,
                    policy_blob = EXCLUDED.policy_blob
                """,
                (
                    features["feed_item_id"],
                    features["posted_at"],
                    features["slot"],
                    features["pipeline_env"],
                    features["target_channel"],
                    features["strategy_rubric"],
                    features["topic_cluster"],
                    features["source_domain"],
                    features["source_url"],
                    features["char_count"],
                    features["paragraph_count"],
                    features["bold_count"],
                    features["emoji_count"],
                    features["has_image"],
                    features["visual_kind"],
                    features["scheduled_hour"],
                    features["cta_variant"],
                    features["policy_version"],
                    Json(features["policy_blob"])
                    if isinstance(features["policy_blob"], (dict, list))
                    else features["policy_blob"],
                ),
            )
            new_features += 1
        except Exception as e:
            log.error("feature_extraction_failed", item_id=item["id"], error=str(e))

    log.info("features_stored", new=new_features, total=len(recent_posts))

    # Only the prod (main) channel has a public web preview. Test-channel
    # posts don't drive statistics (ADR-0007), so they only carry a
    # subscriber count, no views.
    prod_posts = [p for p in recent_posts if p.get("target_channel", "main") != "test"]

    views_map: dict[int, int] = {}
    if prod_username and prod_posts:
        window_min = min(int(p["published_message_id"]) for p in prod_posts)
        views_map = await fetch_views_map(prod_username, min_message_id=window_min)
        log.info("views_fetched", posts_in_preview=len(views_map), window_min=window_min)

    metrics_collected = 0
    metrics_missing = 0  # prod post older than 1h but absent from the preview

    for item in recent_posts:
        is_test = item.get("target_channel") == "test"
        subs = (subs_test if is_test else subs_prod) or 0
        msg_id = int(item["published_message_id"])

        if is_test:
            # No public preview — record subscriber count only.
            metrics = build_metrics(0, subs)
        elif msg_id in views_map:
            metrics = build_metrics(views_map[msg_id], subs)
            metrics_collected += 1
        else:
            # Post exists in the DB but the preview didn't return it. For a
            # post older than ~1h that means the source is broken (wrong
            # username, layout change) — surface it instead of writing a
            # silent zero (the bug behind issue #49).
            age = _post_age_seconds(item.get("posted_at"))
            if age is not None and age > 3600:
                metrics_missing += 1
                log.warning("post_views_missing", feed_item_id=item["id"], message_id=msg_id)
            continue

        execute(
            """
            INSERT INTO engagement_metrics
                (feed_item_id, measured_at, views, forwards, replies,
                 reactions_count, reactions_breakdown, subscribers_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                item["id"],
                datetime.now(UTC),
                metrics["views"],
                metrics["forwards"],
                metrics["replies"],
                metrics["reactions_count"],
                metrics["reactions_breakdown"],
                metrics["subscribers_at"],
            ),
        )

    log.info(
        "engagement_collected",
        posts=len(recent_posts),
        prod_collected=metrics_collected,
        prod_missing=metrics_missing,
    )

    # A total blackout on prod views (posts exist, preview yielded nothing) is
    # the issue-#49 failure mode — alert rather than fail silent.
    if prod_username and prod_posts and metrics_collected == 0:
        log.error("all_prod_views_missing", prod_posts=len(prod_posts))
        if alert_chat_id:
            await _send_alert(
                s.telegram_bot_token, alert_chat_id,
                f"Engagement collector got 0 views for {len(prod_posts)} prod posts from "
                f"t.me/s/{prod_username}. Check the username and the preview layout."
            )

    # Feed the bandit (issue #18): posts past the 48h horizon are scored
    # against the trailing median. Never fail the collection run over it.
    try:
        from aibp.self_learning.bandit import update_from_engagement
        update_from_engagement()
    except Exception as e:
        log.warning("bandit_update_failed", error=str(e))

    return 0


def run() -> int:
    return asyncio.run(run_async())


if __name__ == "__main__":
    raise SystemExit(run())
