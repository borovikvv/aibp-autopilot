"""Scrape competitor channel posts from the public t.me/s/ web preview (issue #40).

A Telegram bot cannot read channels it is not a member of, but public channels
expose recent posts at https://t.me/s/<channel> — no token, no login. This
module fetches and parses those posts for the dedup-by-embeddings check.

Optional Firecrawl fallback (when FIRECRAWL_API_KEY is set) for datacenter-IP
blocks. Firecrawl is config-gated, not a hard dependency.
"""
from __future__ import annotations

import os
import re

import httpx
import structlog

log = structlog.get_logger()

TME_S_URL = "https://t.me/s/{username}"
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/scrape"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# Parse tgme_widget_message blocks: message id (data-post attr), datetime
# (tgme_widget_message_date datetime attr), text (tgme_widget_message_text).
_POST_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_POST_ID_RE = re.compile(r'data-post="([^"]+)"')
_DATETIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')


def parse_channel_html(html: str) -> list[dict]:
    """Parse a t.me/s/ HTML page into a list of post dicts."""
    # Split into message blocks by data-post attr
    blocks = re.split(r'(?=<div class="tgme_widget_message[^"]*"\s+data-post=")', html)
    posts = []
    for block in blocks:
        if "data-post" not in block:
            continue
        post_id_match = _POST_ID_RE.search(block)
        text_match = _POST_RE.search(block)
        dt_match = _DATETIME_RE.search(block)
        if not post_id_match:
            continue
        # data-post is "channel/123"
        message_id = post_id_match.group(1).split("/")[-1]
        text = ""
        if text_match:
            text = re.sub(r"<[^>]+>", " ", text_match.group(1))
            text = re.sub(r"\s+", " ", text).strip()
        posted_at = dt_match.group(1) if dt_match else None
        if text:
            posts.append({"message_id": message_id, "posted_at": posted_at, "text": text})
    return posts


def fetch_channel_posts(username: str, limit: int = 20) -> list[dict]:
    """Recent posts of a public channel via the t.me/s/ web preview.

    1. Primary: httpx GET with a browser-like UA; parse .tgme_widget_message blocks.
    2. Fallback (optional): Firecrawl API when FIRECRAWL_API_KEY is set and the
       direct fetch fails or returns no messages.
    3. Both failed → [] with a warning.
    """
    url = TME_S_URL.format(username=username.lstrip("@"))
    html = None

    try:
        resp = httpx.get(
            url, timeout=20, follow_redirects=True, headers={"User-Agent": BROWSER_UA}
        )
        if resp.status_code == 200 and "tgme_widget_message" in resp.text:
            html = resp.text
    except Exception as e:  # noqa: BLE001 — network errors are non-fatal here
        log.warning("tme_fetch_failed", channel=username, error=str(e))

    if html is None and os.getenv("FIRECRAWL_API_KEY"):
        try:
            resp = httpx.post(
                FIRECRAWL_URL,
                json={"url": url},
                headers={"Authorization": f"Bearer {os.getenv('FIRECRAWL_API_KEY')}"},
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                html = data.get("data", {}).get("html") or data.get("data", {}).get("markdown")
        except Exception as e:  # noqa: BLE001 — Firecrawl errors are non-fatal here
            log.warning("firecrawl_fetch_failed", channel=username, error=str(e))

    if html is None:
        log.warning("competitor_posts_unavailable", channel=username)
        return []

    posts = parse_channel_html(html)[:limit]
    log.info("competitor_posts_fetched", channel=username, count=len(posts))
    return posts
