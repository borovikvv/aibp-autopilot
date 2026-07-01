"""Migration 0002: Add source_fetched_at to feed_items.

Example migration — adds a column to track when RSS item was fetched
(not the same as source_published_at which is the article's own date).
"""
from __future__ import annotations


def up(conn) -> None:
    """Add source_fetched_at column."""
    with conn.cursor() as cur:
        # Check if column already exists (idempotent)
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'feed_items' AND column_name = 'source_fetched_at'
        """)
        if cur.fetchone():
            return  # already applied

        cur.execute("""
            ALTER TABLE feed_items
            ADD COLUMN source_fetched_at timestamptz DEFAULT now()
        """)
        cur.execute("""
            UPDATE feed_items SET source_fetched_at = created_at
            WHERE source_fetched_at IS NULL
        """)


def down(conn) -> None:
    """Remove column."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE feed_items DROP COLUMN IF EXISTS source_fetched_at")
