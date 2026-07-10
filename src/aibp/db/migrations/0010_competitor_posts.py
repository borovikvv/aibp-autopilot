"""Migration 0010: competitor_posts table for dedup by embeddings (issue #40).

Stores recent posts scraped from competitor channels (t.me/s/ web preview) with
their OpenRouter text embeddings, so the generation pipeline can skip
near-duplicate topics before generating. Requires the pgvector extension.
"""
from __future__ import annotations


def up(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS competitor_posts (
                id            bigserial PRIMARY KEY,
                channel       text NOT NULL,
                message_id    text,
                posted_at     timestamptz,
                title         text,
                text_excerpt  text,
                embedding     vector(1536),
                fetched_at    timestamptz DEFAULT now(),
                UNIQUE (channel, message_id)
            )
        """)


def down(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS competitor_posts")
        # Do NOT drop the vector extension — other features may depend on it.
