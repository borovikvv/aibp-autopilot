"""Migration 0004: Drop unused engagement_snapshots table.

This table was declared in schema.sql but never populated — all engagement
data is stored in SQLite (self_learning.db) by the engagement_collector.
The PG table only caused confusion for code readers.

See issue #2 in GitHub for details.
"""
from __future__ import annotations


def up(conn) -> None:
    """Drop engagement_snapshots table and its index."""
    with conn.cursor() as cur:
        # Drop index first (if exists)
        cur.execute("DROP INDEX IF EXISTS idx_engagement_feed")
        # Drop table
        cur.execute("DROP TABLE IF EXISTS engagement_snapshots")


def down(conn) -> None:
    """Recreate the table (for rollback)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS engagement_snapshots (
                id              bigserial PRIMARY KEY,
                feed_item_id    bigint NOT NULL REFERENCES feed_items(id),
                measured_at     timestamptz NOT NULL DEFAULT now(),
                views           integer,
                forwards        integer,
                replies         integer,
                reactions_count integer,
                reactions_breakdown jsonb,
                subscribers_at  integer,
                UNIQUE (feed_item_id, measured_at)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_engagement_feed
            ON engagement_snapshots (feed_item_id, measured_at DESC)
        """)
