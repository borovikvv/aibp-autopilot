"""Competitor dedup by embeddings (issue #40).

Before generating a post, check whether a competitor channel already covered
the same story. Embeddings (OpenRouter text-embedding-3-small) are stored in
the competitor_posts table (pgvector). Cross-lingual pairs (RU competitor post
about an EN news story) fall into a grey zone resolved by a cheap LLM check.

Degrades to 'unique' (no block) on any infrastructure failure — publishing a
post is better than skipping it.
"""
from __future__ import annotations

import structlog

from aibp.db.connection import fetch_one

log = structlog.get_logger()

DEFAULT_DUP_THRESHOLD = 0.85
DEFAULT_GREY_THRESHOLD = 0.70


def classify_similarity(similarity: float, policy: dict) -> str:
    """'duplicate' | 'grey' | 'unique' based on cosine similarity thresholds."""
    cfg = policy.get("competitor_dedup", {})
    dup_t = cfg.get("dup_threshold", DEFAULT_DUP_THRESHOLD)
    grey_t = cfg.get("grey_threshold", DEFAULT_GREY_THRESHOLD)
    if similarity >= dup_t:
        return "duplicate"
    if similarity <= grey_t:
        return "unique"
    return "grey"


def _query_similarity(embedding: list[float]) -> tuple[float, str] | None:
    """Find the most similar competitor post. Returns (similarity, text) or None.

    similarity = 1 - cosine_distance (pgvector <=> returns distance).
    """
    # pgvector: <=> is cosine distance; similarity = 1 - distance.
    row = fetch_one(
        """
        SELECT 1 - (embedding <=> %s::vector) AS similarity, text_excerpt
        FROM competitor_posts
        WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %s::vector
        LIMIT 1
        """,
        (str(embedding), str(embedding)),
    )
    if row is None:
        return None
    return (float(row["similarity"]), row["text_excerpt"] or "")


def _llm_same_story(text_a: str, text_b: str) -> bool:
    """Cheap LLM check: are these the same news story? (grey-zone resolution)."""
    from aibp.enrichment.llm_client import cheap_client
    from aibp.utils.config import get_settings
    client = cheap_client(getattr(get_settings(), "openrouter_dedup_model", None))
    result = client.chat_json(
        messages=[{
            "role": "user",
            "content": (
                "Are these two texts about the SAME news story / event? "
                "Answer strictly JSON: {\"same\": true|false}\n\n"
                f"Text A: {text_a[:500]}\n\nText B: {text_b[:500]}"
            ),
        }],
        temperature=0.0,
        max_tokens=50,
    )
    return bool(result.get("same", False))


def check_duplicate(title: str, text: str, policy: dict) -> str:
    """'duplicate' | 'unique' | 'grey' for a candidate vs competitor_posts.

    Degrades to 'unique' (no block) on pgvector/OpenRouter unavailability.
    """
    query_text = f"{title}\n{text[:1000]}"
    try:
        from aibp.enrichment.llm_client import OpenRouterClient
        client = OpenRouterClient()
        embeddings = client.embed([query_text])
        if not embeddings:
            return "unique"
        embedding = embeddings[0]
    except Exception as e:
        log.warning("dedup_embedding_failed", error=str(e))
        return "unique"

    try:
        result = _query_similarity(embedding)
    except Exception as e:
        log.warning("dedup_query_failed", error=str(e))
        return "unique"

    if result is None:
        return "unique"

    similarity, comp_text = result
    log.info("dedup_similarity", similarity=round(similarity, 3))
    classification = classify_similarity(similarity, policy)

    if classification == "grey":
        try:
            if _llm_same_story(query_text, comp_text):
                return "duplicate"
            return "unique"
        except Exception as e:
            log.warning("dedup_llm_check_failed", error=str(e))
            return "unique"

    return classification
