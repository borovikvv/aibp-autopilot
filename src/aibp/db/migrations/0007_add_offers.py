"""Migration 0007: offers catalog + offer attribution on tracked links (issue #38).

offers holds partner/CPA offers with topic tags and an estimated revenue per
click; tracked_links.offer_id ties a click to the offer it monetizes, so
clicks → offer → estimated revenue becomes queryable per post and per offer.
"""
from __future__ import annotations


def up(conn) -> None:
    """Create offers; add offer_id to tracked_links."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                id                bigserial PRIMARY KEY,
                slug              text UNIQUE NOT NULL,
                title             text NOT NULL,
                target_url        text NOT NULL,
                topics            text[] NOT NULL DEFAULT '{}',
                revenue_per_click numeric NOT NULL DEFAULT 0,
                status            text NOT NULL DEFAULT 'active',
                notes             text,
                created_at        timestamptz DEFAULT now(),
                updated_at        timestamptz DEFAULT now()
            )
        """)
        cur.execute("""
            ALTER TABLE tracked_links
            ADD COLUMN IF NOT EXISTS offer_id bigint REFERENCES offers(id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_tracked_links_offer
            ON tracked_links (offer_id)
        """)


def down(conn) -> None:
    """Drop offer attribution and the catalog."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE tracked_links DROP COLUMN IF EXISTS offer_id")
        cur.execute("DROP TABLE IF EXISTS offers")
