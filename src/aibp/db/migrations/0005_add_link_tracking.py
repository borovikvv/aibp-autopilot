"""Migration 0005: Click-tracking tables (issue #15).

tracked_links maps short_id → target_url per post; link_clicks logs every
redirect hit. CTR (clicks/views) becomes the monetization metric the
self-learning loop can optimize, instead of views alone.
"""
from __future__ import annotations


def up(conn) -> None:
    """Create tracked_links and link_clicks."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tracked_links (
                short_id     text PRIMARY KEY,
                feed_item_id bigint REFERENCES feed_items(id),
                target_url   text NOT NULL,
                created_at   timestamptz DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_tracked_links_item
            ON tracked_links (feed_item_id)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS link_clicks (
                id           bigserial PRIMARY KEY,
                feed_item_id bigint,
                short_id     text NOT NULL REFERENCES tracked_links(short_id),
                clicked_at   timestamptz NOT NULL DEFAULT now(),
                target_url   text,
                user_agent   text
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_link_clicks_item
            ON link_clicks (feed_item_id, clicked_at)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_link_clicks_short
            ON link_clicks (short_id)
        """)


def down(conn) -> None:
    """Drop click-tracking tables."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS link_clicks")
        cur.execute("DROP TABLE IF EXISTS tracked_links")
