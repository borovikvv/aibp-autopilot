"""Migration 0003: Add source_item_id column to feed_items.

This column links stage/shadow posts back to their original prod source.
When a stage post is generated from a prod-published source, source_item_id
stores the feed_items.id of the original prod row. This enables:
  - Selecting sources for stage generation (find prod posts not yet re-published to stage)
  - Decision engine to compare same-source engagement across policies
"""
from __future__ import annotations


def up(conn) -> None:
    """Add source_item_id column."""
    with conn.cursor() as cur:
        # Check if column already exists (idempotent)
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'feed_items' AND column_name = 'source_item_id'
        """)
        if cur.fetchone():
            return  # already applied

        cur.execute("""
            ALTER TABLE feed_items
            ADD COLUMN source_item_id bigint
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_feed_items_source_item
            ON feed_items (source_item_id)
            WHERE source_item_id IS NOT NULL
        """)


def down(conn) -> None:
    """Remove column."""
    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_feed_items_source_item")
        cur.execute("ALTER TABLE feed_items DROP COLUMN IF EXISTS source_item_id")
