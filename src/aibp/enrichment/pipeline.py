"""Enrichment pipeline — LLM classification of new feed items.

Cron: every 2h via Hermes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import structlog

from aibp.db.connection import execute, fetch_all
from aibp.enrichment.llm_client import OpenRouterClient
from aibp.utils.config import get_settings, load_policy

log = structlog.get_logger()


ENRICHMENT_PROMPT = """You are an editorial assistant for a Russian-language Telegram channel about practical AI in business workflows (@AI_Business_Pulse).

For each news item, classify it according to the channel's editorial strategy. The channel focuses on: "which work task can now be done differently with AI — and where the limit is".

Classify the following article:

TITLE: {title}
SOURCE: {source} ({source_domain})
PUBLISHED: {published_at}
LANGUAGE: {lang}
EXCERPT: {text_excerpt}

Return JSON with this exact schema:
{{
  "publish_worthy": true | false,
  "strategy_rubric": "process_under_ai" | "pilot_without_chaos" | "implementation_metric" | "ai_regulation" | "tool_through_scenario" | "anti_hype" | null,
  "topic_cluster": "sales_revenue" | "support_service" | "documents_backoffice" | "operations_management" | "marketing_content_ops" | "hr_training" | "risk_data_governance" | "ai_tools" | null,
  "recommended_format": "morning" | "evening" | "weekly_digest" | "weekly_case" | null,
  "source_fit_score": 1-5 (5 = perfect fit, 1 = irrelevant),
  "importance_hint": "A" | "B" | "C",
  "rank_score": 0-100 (editorial priority),
  "one_sentence_angle": "one-sentence editorial thesis in Russian",
  "why_not_fit": "if publish_worthy=false, explain why (in Russian)"
}}

Rules:
- "publish_worthy" = false if the item is: pure product release without process angle, generic "AI is important" claim, ad-like tool mention, technical curiosity without business angle.
- "source_fit_score" >= 4 means the source supports a strong editorial post.
- "recommended_format": "weekly_case" when the item is a real implementation case (cases/regulation/research) with concrete before/after figures — a source that supports a deep "process → tool → metric → limit" breakdown.
- "one_sentence_angle" must be in Russian and reflect the practical/business angle, not the news itself.
- Return ONLY the JSON, no other text.
"""


def enrich_item(item: dict, client: OpenRouterClient, policy: dict) -> dict | None:
    """Enrich one feed_items row via LLM."""
    prompt = ENRICHMENT_PROMPT.format(
        title=item.get("title", "")[:500],
        source=item.get("source", ""),
        source_domain=item.get("source_domain", ""),
        published_at=item.get("source_published_at", ""),
        lang=item.get("source_lang", "en"),
        text_excerpt=(item.get("text") or "")[:2000],
    )

    try:
        result = client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=800,
        )
    except Exception as e:
        log.error("enrichment_llm_failed", item_id=item["id"], error=str(e))
        return None

    # Apply source scoring from policy
    source_scores = policy.get("source_scores", {})
    domain = item.get("source_domain", "")
    score_adjustment = source_scores.get(domain, source_scores.get("default", 0.0))
    base_score = float(result.get("source_fit_score", 3))
    adjusted_score = max(0, min(5, base_score + score_adjustment))
    result["source_fit_score_adjusted"] = adjusted_score

    return result


def run() -> int:
    """Main entry point — enrich all 'new' items."""
    policy = load_policy()
    # Classification is high-volume and schema-bound — runs on the cheap model.
    client = OpenRouterClient(
        default_model=getattr(get_settings(), "openrouter_enrichment_model", None)
    )

    # Get unenriched items (status='new')
    candidates = fetch_all(
        """
        SELECT id, url, title, text, source, source_domain, source_lang, source_published_at
        FROM feed_items
        WHERE status = 'new'
          AND source_published_at > now() - interval '14 days'
        ORDER BY source_published_at DESC
        LIMIT 30
        """
    )

    if not candidates:
        log.info("no_candidates")
        return 0

    log.info("enrichment_start", candidates=len(candidates))
    enriched_count = 0

    for item in candidates:
        log.info("enriching", item_id=item["id"], title=item["title"][:60] if item["title"] else "")
        result = enrich_item(item, client, policy)

        if result is None:
            # Mark as failed so we don't retry every cycle
            execute(
                "UPDATE feed_items SET status = 'failed' WHERE id = %s",
                (item["id"],),
            )
            continue

        summary = {
            "editorial": result,
            "enriched_at": datetime.now(UTC).isoformat(),
            "enrichment_version": "v1",
        }

        execute(
            """
            UPDATE feed_items
            SET
                status = 'enriched',
                summary = %s::jsonb,
                rank_score = %s,
                importance_hint = %s,
                relevance = %s,
                rubric = COALESCE(rubric, 'operator_note'),
                topic = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (
                json.dumps(summary, ensure_ascii=False),
                result.get("rank_score", 50),
                result.get("importance_hint", "C"),
                result.get("source_fit_score_adjusted", 3.0),
                result.get("topic_cluster"),
                item["id"],
            ),
        )
        enriched_count += 1

    log.info("enrichment_complete", enriched=enriched_count, total=len(candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
