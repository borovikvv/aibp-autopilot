"""Database initialization and migrations."""
from __future__ import annotations

import psycopg2

from aibp.utils.config import PROJECT_ROOT, get_settings

SCHEMA_FILE = PROJECT_ROOT / "config" / "schema.sql"


def init_db() -> None:
    """Create all tables from schema.sql. Idempotent."""
    s = get_settings()
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")

    conn = psycopg2.connect(s.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(schema_sql)
        conn.commit()
        print(f"✅ Schema applied from {SCHEMA_FILE}")
    finally:
        conn.close()


def check_connection() -> bool:
    """Verify DB is reachable and schema is applied."""
    s = get_settings()
    try:
        conn = psycopg2.connect(s.database_url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM feed_items")
            cur.fetchone()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return False


def load_rss_feeds_to_db() -> int:
    """Load RSS feeds from YAML into rss_feeds table. Returns count."""
    from aibp.db.connection import execute_returning, fetch_one
    from aibp.utils.config import load_rss_feeds

    config = load_rss_feeds()
    count = 0
    for feed in config.get("feeds", []):
        existing = fetch_one("SELECT id FROM rss_feeds WHERE url = %s", (feed["url"],))
        if existing:
            continue
        execute_returning(
            """
            INSERT INTO rss_feeds (name, url, category, weight, lang, enabled)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING id
            """,
            (
                feed["name"],
                feed["url"],
                feed.get("category", "news"),
                feed.get("weight", 1.0),
                feed.get("lang", "en"),
            ),
        )
        count += 1
    return count


if __name__ == "__main__":
    init_db()
    n = load_rss_feeds_to_db()
    print(f"✅ Loaded {n} new RSS feeds")
    print("✅ DB ready")
