"""RSS Collector — fetches feeds via feedparser, inserts into feed_items.

Cron: every 1h via Hermes.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import structlog
from dateutil import parser as date_parser

from aibp.db.connection import db_conn, fetch_one, execute_returning
from aibp.utils.config import load_rss_feeds

log = structlog.get_logger()


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    parsed = urlparse(url)
    return parsed.netloc.lower().lstrip("www.")


def _parse_date(entry: dict) -> datetime | None:
    """Parse publication date from RSS entry."""
    for field_name in ("published_parsed", "updated_parsed"):
        if entry.get(field_name):
            try:
                t = entry[field_name]
                return datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    for field_name in ("published", "updated"):
        raw = entry.get(field_name)
        if raw:
            try:
                return date_parser.parse(raw).astimezone(timezone.utc)
            except (ValueError, TypeError):
                continue
    return None


def _extract_text(entry: dict, max_chars: int = 2000) -> str:
    """Extract clean text excerpt from RSS entry."""
    for field_name in ("summary", "description", "content"):
        raw = entry.get(field_name)
        if not raw:
            continue
        if isinstance(raw, list):
            raw = " ".join(str(c.get("value", "")) for c in raw if isinstance(c, dict))
        # Strip HTML tags (simple, good enough for RSS)
        import re
        clean = re.sub(r"<[^>]+>", " ", str(raw))
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            return clean[:max_chars]
    return ""


def collect_feed(feed: dict, settings: dict) -> int:
    """Fetch one RSS feed, insert new items. Returns count of new items."""
    url = feed["url"]
    name = feed.get("name", url)
    max_items = settings.get("max_items_per_feed", 20)
    max_age_days = settings.get("max_age_days", 14)
    user_agent = settings.get("user_agent", "AIBP-Autopilot/1.0")

    log.info("fetching_feed", feed=name, url=url)
    parsed = feedparser.parse(
        url,
        agent=user_agent,
    )

    if parsed.bozo and not parsed.entries:
        log.warning("feed_parse_error", feed=name, error=str(parsed.bozo_exception))
        return 0

    new_count = 0
    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_days * 86400)

    for entry in parsed.entries[:max_items]:
        link = entry.get("link")
        if not link:
            continue

        # Dedup by URL hash
        url_h = hashlib.sha256(link.encode()).hexdigest()
        existing = fetch_one("SELECT id FROM feed_items WHERE url_hash = %s", (url_h,))
        if existing:
            continue

        pub_date = _parse_date(entry)
        if pub_date and pub_date.timestamp() < cutoff:
            continue  # too old

        title = entry.get("title", "")[:500]
        text = _extract_text(entry)
        domain = _extract_domain(link)
        source_lang = feed.get("lang", "en")
        dupe_key = f"rss:{url_h[:16]}"

        try:
            execute_returning(
                """
                INSERT INTO feed_items (
                    url, url_hash, title, text, source, source_domain,
                    source_lang, source_published_at, status, category,
                    dupe_key, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s, %s, now()
                )
                RETURNING id
                """,
                (
                    link, url_h, title, text, name, domain,
                    source_lang, pub_date, feed.get("category", "news"),
                    dupe_key,
                ),
            )
            new_count += 1
        except Exception as e:
            log.error("insert_failed", feed=name, url=link, error=str(e))

    log.info("feed_done", feed=name, new_items=new_count)
    return new_count


def run() -> int:
    """Main entry point — collect all enabled feeds."""
    config = load_rss_feeds()
    settings = config.get("settings", {})
    total_new = 0
    errors = 0

    for feed in config.get("feeds", []):
        try:
            total_new += collect_feed(feed, settings)
        except Exception as e:
            log.error("feed_failed", feed=feed.get("name"), error=str(e))
            errors += 1
        # Be polite to RSS servers
        time.sleep(1)

    log.info("collection_complete", total_new=total_new, errors=errors)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
