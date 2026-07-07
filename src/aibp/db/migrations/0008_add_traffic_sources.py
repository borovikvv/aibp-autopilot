"""Migration 0008: traffic sources + invite-link join attribution (issue #39).

Each paid placement / cross-promo / external platform gets its own Telegram
invite link (traffic_sources); chat_member updates map every join to the
source it came from (invite_joins). cost_rub / COUNT(joins) = actual CPS,
replacing the assumed_conversion_pct guess in the growth report.
"""
from __future__ import annotations


def up(conn) -> None:
    """Create traffic_sources and invite_joins."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS traffic_sources (
                id               bigserial PRIMARY KEY,
                slug             text UNIQUE NOT NULL,
                kind             text NOT NULL DEFAULT 'ad_buy',
                channel_username text,
                invite_link      text UNIQUE,
                cost_rub         numeric NOT NULL DEFAULT 0,
                expected_subscribers numeric,
                expected_cps_rub numeric,
                ad_posted_at     timestamptz,
                status           text NOT NULL DEFAULT 'draft',
                notes            text,
                created_at       timestamptz DEFAULT now(),
                updated_at       timestamptz DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS invite_joins (
                id         bigserial PRIMARY KEY,
                source_id  bigint NOT NULL REFERENCES traffic_sources(id),
                user_id    bigint NOT NULL,
                joined_at  timestamptz NOT NULL DEFAULT now(),
                UNIQUE (source_id, user_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_invite_joins_source
            ON invite_joins (source_id, joined_at)
        """)


def down(conn) -> None:
    """Drop join attribution tables."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS invite_joins")
        cur.execute("DROP TABLE IF EXISTS traffic_sources")
