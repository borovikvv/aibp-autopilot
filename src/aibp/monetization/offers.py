"""Offers catalog + Thompson-sampling offer selection (issue #38).

The affiliate_link CTA used to be a bare phrase pointing at the source link —
revenue was not represented in the system at all. Now offers live in
PostgreSQL (partner/CPA links with topic tags and an estimated revenue per
click), and when the affiliate_link CTA is sampled at generation time, this
module picks the offer for the post's topic_cluster:

    score(offer) = θ_offer · revenue_per_click
    θ_offer ~ Beta(α, β)   # "post with this offer got ≥1 click in 48h"

The Beta posteriors reuse the existing bandit tables (dimension "offer",
arm_id = slug), updated by update_offer_outcomes() from the bandit cron.
While no offer has a known revenue_per_click, selection falls back to θ
alone; once revenues are known, zero-revenue offers only win against other
zero-revenue offers.

Every function that touches PostgreSQL degrades gracefully (None / no-op
with a warning): a post without an offer is better than no post.
"""
from __future__ import annotations

import structlog

from aibp.self_learning.bandit import ensure_arms, record_outcome, sample_thompson

log = structlog.get_logger()

OFFER_DIMENSION = "offer"


# ═══════════════════════════════════════════════════════════════════
# Catalog CRUD (used by CLI)
# ═══════════════════════════════════════════════════════════════════

def add_offer(slug: str, title: str, target_url: str, topics: list[str],
              revenue_per_click: float = 0, notes: str | None = None) -> None:
    from aibp.db.connection import execute
    execute(
        """
        INSERT INTO offers (slug, title, target_url, topics, revenue_per_click, notes)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (slug) DO UPDATE
        SET title = EXCLUDED.title, target_url = EXCLUDED.target_url,
            topics = EXCLUDED.topics, revenue_per_click = EXCLUDED.revenue_per_click,
            notes = EXCLUDED.notes, updated_at = now()
        """,
        (slug, title, target_url, topics, revenue_per_click, notes),
    )


def set_offer_status(slug: str, status: str) -> int:
    from aibp.db.connection import execute
    return execute(
        "UPDATE offers SET status = %s, updated_at = now() WHERE slug = %s",
        (status, slug),
    )


def list_offers(status: str | None = None) -> list[dict]:
    from aibp.db.connection import fetch_all
    if status:
        return fetch_all("SELECT * FROM offers WHERE status = %s ORDER BY slug", (status,))
    return fetch_all("SELECT * FROM offers ORDER BY slug")


# ═══════════════════════════════════════════════════════════════════
# Selection
# ═══════════════════════════════════════════════════════════════════

def eligible_offers(topic_cluster: str | None, offers: list[dict]) -> list[dict]:
    """Active offers matching the topic. Empty topics list = fits any topic;
    unknown post topic → only untagged offers are eligible."""
    result = []
    for offer in offers:
        if offer.get("status") != "active":
            continue
        topics = offer.get("topics") or []
        if not topics or (topic_cluster and topic_cluster in topics):
            result.append(offer)
    return result


def pick_offer(topic_cluster: str | None, offers: list[dict] | None = None,
               rng=None) -> dict | None:
    """Pick the offer with the highest expected revenue for this topic.

    offers=None fetches active offers from PostgreSQL; pass a list in tests.
    Returns None on empty catalog, no eligible offer, or PG failure —
    the caller falls back to non-affiliate CTA variants.
    """
    if offers is None:
        try:
            offers = list_offers(status="active")
        except Exception as e:
            log.warning("offers_unavailable", error=str(e))
            return None

    candidates = eligible_offers(topic_cluster, offers)
    if not candidates:
        return None

    slugs = [o["slug"] for o in candidates]
    ensure_arms(OFFER_DIMENSION, slugs)
    theta = sample_thompson(OFFER_DIMENSION, slugs, rng=rng)

    any_revenue = any(float(o.get("revenue_per_click") or 0) > 0 for o in candidates)
    best, best_score = None, -1.0
    for offer in candidates:
        rpc = float(offer.get("revenue_per_click") or 0)
        score = theta[offer["slug"]] * rpc if any_revenue else theta[offer["slug"]]
        if score > best_score:
            best, best_score = offer, score
    return best


# ═══════════════════════════════════════════════════════════════════
# Outcome collection (runs from the bandit cron)
# ═══════════════════════════════════════════════════════════════════

def score_offer_rows(rows: list[dict]) -> list[tuple[str, int, bool]]:
    """(slug, feed_item_id, success) per offer link; success = ≥1 click in 48h."""
    return [(r["slug"], r["feed_item_id"], (r["clicks"] or 0) > 0) for r in rows]


def update_offer_outcomes() -> int:
    """Score published offer links past the 48h horizon; update Beta posteriors.

    Idempotent via the bandit_observations PK — a post's offer outcome is
    recorded once. Returns the number of new observations.
    """
    try:
        from aibp.db.connection import fetch_all
        rows = fetch_all(
            """
            SELECT o.slug, tl.feed_item_id,
                   COUNT(lc.id) FILTER (
                       WHERE lc.clicked_at BETWEEN fi.posted_at
                                               AND fi.posted_at + interval '48 hours'
                   ) AS clicks
            FROM tracked_links tl
            JOIN offers o ON o.id = tl.offer_id
            JOIN feed_items fi ON fi.id = tl.feed_item_id
            LEFT JOIN link_clicks lc ON lc.short_id = tl.short_id
            WHERE fi.posted_at IS NOT NULL
              AND fi.posted_at <= now() - interval '48 hours'
            GROUP BY o.slug, tl.feed_item_id
            """
        )
    except Exception as e:
        log.warning("offer_outcomes_unavailable", error=str(e))
        return 0

    recorded = 0
    for slug, feed_item_id, success in score_offer_rows(rows):
        from aibp.self_learning.db import sqlite_conn
        with sqlite_conn() as conn:
            already = conn.execute(
                "SELECT 1 FROM bandit_observations WHERE feed_item_id = ? AND dimension = ?",
                (feed_item_id, OFFER_DIMENSION),
            ).fetchone()
        if already:
            continue
        record_outcome(OFFER_DIMENSION, slug, success, feed_item_id=feed_item_id)
        recorded += 1

    if recorded:
        log.info("offer_outcomes_recorded", observations=recorded)
    return recorded
