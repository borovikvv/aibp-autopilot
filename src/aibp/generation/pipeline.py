"""Post generation — LLM writes Telegram post, quality gate validates.

Cron: morning (10:00 MSK), evening (18:00 MSK), weekly (Sun 19:00 MSK).
For shadow testing: stage generation uses policy.stage.yaml and publishes to test channel.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from jinja2 import Template

from aibp.db.connection import execute, fetch_all
from aibp.enrichment.llm_client import OpenRouterClient
from aibp.generation.quality_gate import validate_cta_text, validate_post
from aibp.utils.config import get_settings, load_policy
from aibp.utils.summary import parse_summary

log = structlog.get_logger()

MSK = ZoneInfo("Europe/Moscow")

# ═══════════════════════════════════════════════════════════════════
# Prompt templates (Jinja2)
# ═══════════════════════════════════════════════════════════════════

POLICY_BLOCK_TEMPLATE = Template("""## Active Policy (auto-managed)

{% if rubric_weights %}
### Strategy rubric priorities (higher = more likely picked)
{% for rubric, weight in rubric_weights.items() %}
- {{ rubric }}: ×{{ weight }}{% endfor %}
{% endif %}

{% if post_params %}
### Post parameters for {{ slot }}
- Target length: {{ post_params[slot].target_chars[0] }}-{{ post_params[slot].target_chars[1] }} chars
- Paragraphs: {{ post_params[slot].paragraphs[0] }}-{{ post_params[slot].paragraphs[1] }}
- Max bold: {{ post_params[slot].max_bold }}
- Max emoji: {{ post_params[slot].max_emoji }}
- Scheduled hour (MSK): {{ post_params[slot].scheduled_hour_msk }}
{% endif %}

{% if regex_gates %}
### Additional editorial gates
{% for gate in regex_gates %}
- {{ gate.name }} ({{ gate.action }}): {{ gate.pattern }}{% endfor %}
{% endif %}

{% if source_scores %}
### Source scoring adjustments
{% for domain, score in source_scores.items() %}
{% if domain != "default" %}- {{ domain }}: {{ score }}{% endif %}{% endfor %}
{% endif %}
""")

GENERATION_PROMPT = Template("""You are the editor of @AI_Business_Pulse — a Russian-language Telegram channel about practical AI in business workflows.

The channel formula: NOT "what happened in AI", but "which work task can now be done differently — and where the limit is".

## Your task

Write ONE Russian Telegram post in HTML format for the {{ slot }} slot.

## Source material

TITLE: {{ title }}
SOURCE: {{ source }} ({{ source_domain }})
URL: {{ url }}
PUBLISHED: {{ published_at }}
EXCERPT: {{ text_excerpt }}

Editorial classification:
- strategy_rubric: {{ strategy_rubric }}
- topic_cluster: {{ topic_cluster }}
- source_fit_score: {{ source_fit_score }}
- one_sentence_angle: {{ one_sentence_angle }}

{{ policy_block }}

## Hard editorial rules

1. The post is NOT a source recap. The source is only raw material.
2. One thesis, one practical angle, one line of reasoning.
3. No source-centered attribution in body ("В материале X", "по данным Y", "как пишет Z").
4. Source link ONLY at the end: `<a href="{{ url }}">Источник</a>`
5. No forbidden terms: SMB, CEO, "для бизнеса", "владельцы бизнеса".
6. No AI clichés: "важно отметить", "в современном мире", "ключевой вывод".
7. No generic moral endings ("В итоге...", "Итог: ...").
8. Technical terms (RAG, token budget) allowed only with business translation.
9. Post must start with a bold headline: `<b>...</b>`, then 3-4 paragraphs.
10. Length: {{ target_chars_min }}-{{ target_chars_max }} chars, {{ paragraphs_min }}-{{ paragraphs_max }} paragraphs.
11. End with: `<a href="{{ url }}">Источник</a>` (exact format, nofollow not needed).

## Slot-specific guidance

{% if slot == "morning" %}
Morning = analytical pillar. Full implementation angle: process, metric, choice, risk.
{% elif slot == "evening" %}
Evening = boundary note. One limit, mistake, or distinction. Short and sharp.
{% elif slot == "weekly_digest" %}
Weekly digest = synthesis of 4-5 sources around one weekly signal.
{% endif %}

## Output

Return ONLY the post HTML. No explanation, no markdown fences.
""")


# ═══════════════════════════════════════════════════════════════════
# Candidate selection
# ═══════════════════════════════════════════════════════════════════

def select_candidate(slot: str, policy: dict, pipeline_env: str = "prod") -> dict | None:
    """Select one best candidate for the slot.

    For prod: pick from enriched, unused sources.
    For stage: pick from already-published prod sources (to compare same source
               with different policy), excluding those already re-published to stage.
    """
    rubric_weights = policy.get("rubric_weights", {})

    if pipeline_env == "stage":
        # Stage: select sources already published to prod, not yet re-published to stage.
        # This enables same-source comparison: prod post (prod policy) vs stage post (stage policy).
        candidates = fetch_all(
            """
            SELECT id, url, title, text, source, source_domain, source_lang,
                   source_published_at, summary, rank_score, relevance
            FROM feed_items
            WHERE status = 'published'
              AND pipeline_env = 'prod'
              AND target_channel = 'main'
              AND posted_at IS NOT NULL
              AND source_published_at > now() - interval '14 days'
              AND id NOT IN (
                  SELECT source_item_id FROM feed_items
                  WHERE source_item_id IS NOT NULL
                    AND pipeline_env = 'stage'
              )
            ORDER BY posted_at DESC
            LIMIT 20
            """
        )
    else:
        # Prod: fresh enriched candidates
        candidates = fetch_all(
            """
            SELECT id, url, title, text, source, source_domain, source_lang,
                   source_published_at, summary, rank_score, relevance
            FROM feed_items
            WHERE status = 'enriched'
              AND COALESCE(is_used, false) = false
              AND posted_at IS NULL
              AND post_draft IS NULL
              AND source_published_at > now() - interval '14 days'
              AND summary->'editorial'->>'publish_worthy' = 'true'
            ORDER BY rank_score DESC NULLS LAST, source_published_at DESC
            LIMIT 20
            """
        )

    if not candidates:
        log.info("no_candidates", slot=slot, pipeline_env=pipeline_env)
        return None

    # Filter by recommended_format matching slot
    slot_filtered = []
    for c in candidates:
        summary = parse_summary(c.get("summary"))
        editorial = summary.get("editorial", {})
        if editorial.get("recommended_format") == slot:
            slot_filtered.append(c)

    if not slot_filtered:
        slot_filtered = candidates

    # Thompson sampling multipliers (issue #18): the bandit nudges rubric
    # weights within [0.5x, 1.5x] per post, learning from every published
    # post instead of waiting for weekly experiments. Selection must not
    # fail if the bandit state is unavailable.
    bandit_multipliers: dict[str, float] = {}
    if pipeline_env == "prod" and rubric_weights:
        try:
            from aibp.self_learning.bandit import sample_rubric_multipliers
            bandit_multipliers = sample_rubric_multipliers(list(rubric_weights))
        except Exception as e:
            log.warning("bandit_unavailable", error=str(e))

    # Apply rubric weights
    def weighted_score(c: dict) -> float:
        summary = parse_summary(c.get("summary"))
        editorial = summary.get("editorial", {})
        rubric = editorial.get("strategy_rubric", "")
        weight = rubric_weights.get(rubric, 1.0) * bandit_multipliers.get(rubric, 1.0)
        base = float(c.get("rank_score", 50))
        return base * weight

    slot_filtered.sort(key=weighted_score, reverse=True)
    return slot_filtered[0] if slot_filtered else None


# ═══════════════════════════════════════════════════════════════════
# Post generation
# ═══════════════════════════════════════════════════════════════════

def generate_post(candidate: dict, slot: str, policy: dict, client: OpenRouterClient) -> str | None:
    """Generate post draft via LLM."""
    summary = parse_summary(candidate.get("summary"))
    editorial = summary.get("editorial", {})

    post_params = policy.get("post_params", {}).get(slot, {})
    policy_block = POLICY_BLOCK_TEMPLATE.render(
        rubric_weights=policy.get("rubric_weights", {}),
        post_params=policy.get("post_params", {}),
        slot=slot,
        regex_gates=policy.get("regex_gates", []),
        source_scores=policy.get("source_scores", {}),
    )

    prompt = GENERATION_PROMPT.render(
        slot=slot,
        title=candidate.get("title", ""),
        source=candidate.get("source", ""),
        source_domain=candidate.get("source_domain", ""),
        url=candidate.get("url", ""),
        published_at=candidate.get("source_published_at", ""),
        text_excerpt=(candidate.get("text") or "")[:2000],
        strategy_rubric=editorial.get("strategy_rubric"),
        topic_cluster=editorial.get("topic_cluster"),
        source_fit_score=editorial.get("source_fit_score"),
        one_sentence_angle=editorial.get("one_sentence_angle"),
        policy_block=policy_block,
        target_chars_min=post_params.get("target_chars", [800, 1400])[0],
        target_chars_max=post_params.get("target_chars", [800, 1400])[1],
        paragraphs_min=post_params.get("paragraphs", [4, 5])[0],
        paragraphs_max=post_params.get("paragraphs", [4, 5])[1],
    )

    try:
        post = client.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=2000,
        )
        return post.strip()
    except Exception as e:
        log.error("generation_failed", candidate=candidate["id"], error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════════
# CTA variants (issue #16)
# ═══════════════════════════════════════════════════════════════════

# CTA texts per variant. The variant name (not the text) is the policy
# dimension: pattern miner proposes weight changes, decision engine measures
# conversion per variant.
CTA_TEMPLATES = {
    "save_forward": "Сохраните пост — пригодится, когда дойдёте до внедрения. "
                    "И перешлите коллеге, который этим занимается.",
    "affiliate_link": "Детали, тарифы и ограничения — по ссылке ниже.",
    "comment_prompt": "Пробовали похожее у себя? Расскажите в комментариях, что сработало.",
}


def select_cta_variant(policy: dict) -> str | None:
    """Sample one CTA variant according to policy weights. None disables CTA."""
    weights = policy.get("cta_variants") or {}
    valid = {
        k: float(v) for k, v in weights.items()
        if k in CTA_TEMPLATES and isinstance(v, (int, float)) and v > 0
    }
    if not valid:
        return None
    import random
    return random.choices(list(valid.keys()), weights=list(valid.values()))[0]


def append_cta(post: str, variant: str) -> str:
    """Insert the CTA paragraph before the final source link."""
    text = CTA_TEMPLATES.get(variant)
    if not text:
        return post
    cta_html = f"<i>{text}</i>"
    lines = post.rstrip().rsplit("\n", 1)
    if len(lines) == 2 and "Источник" in lines[1]:
        return f"{lines[0]}\n{cta_html}\n\n{lines[1]}"
    return f"{post.rstrip()}\n\n{cta_html}"


# ═══════════════════════════════════════════════════════════════════
# Click tracking (issue #15)
# ═══════════════════════════════════════════════════════════════════

_HREF_RE = re.compile(r'<a href="([^"]+)">')


def wrap_tracked_links(post: str, feed_item_id: int) -> str:
    """Replace direct external URLs in the post with tracked short redirects.

    Runs AFTER the quality gate (which validates the original source URL).
    No-op when TRACKING_BASE_URL is not configured or on registry errors —
    a post with a direct link is better than no post.
    """
    if not get_settings().tracking_base_url:
        return post

    from aibp.tracking.redirect_service import register_link, short_url

    def _sub(match: re.Match) -> str:
        url = match.group(1)
        try:
            short_id = register_link(feed_item_id, url)
            return f'<a href="{short_url(short_id)}">'
        except Exception as e:
            log.warning("link_tracking_failed", feed_item_id=feed_item_id, url=url, error=str(e))
            return match.group(0)

    return _HREF_RE.sub(_sub, post)


# ═══════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════

def run(slot: str = "morning", pipeline_env: str = "prod") -> int:
    """Generate one post for the slot.

    Args:
        slot: morning | evening | weekly_digest
        pipeline_env: prod → publish to main channel with config/policy.yaml
                      stage → publish to test channel with config/policy.stage.yaml
    """
    policy = load_policy(pipeline_env=pipeline_env)
    if pipeline_env == "prod":
        # ADR-0007: an active interleave experiment alternates policies by day
        # in the main channel; on variant days the shadow policy is used.
        from aibp.self_learning.interleave import resolve_policy_for_today
        policy = resolve_policy_for_today(policy)
    client = OpenRouterClient()

    candidate = select_candidate(slot, policy, pipeline_env=pipeline_env)
    if candidate is None:
        log.info("no_candidate_available", slot=slot, pipeline_env=pipeline_env)
        return 0

    log.info(
        "generating",
        slot=slot,
        pipeline_env=pipeline_env,
        candidate_id=candidate["id"],
        title=candidate["title"][:60] if candidate["title"] else "",
    )

    # Generate with retry (max 3 attempts if quality gate fails)
    post = None
    for attempt in range(3):
        post = generate_post(candidate, slot, policy, client)
        if post is None:
            return 1

        validation = validate_post(
            post=post,
            expected_url=candidate["url"],
            slot=slot,
            extra_gates=policy.get("regex_gates", []),
        )

        if validation["ok"]:
            log.info("quality_gate_passed", attempt=attempt + 1, slot=slot, pipeline_env=pipeline_env)
            break
        else:
            log.warning(
                "quality_gate_failed",
                attempt=attempt + 1,
                pipeline_env=pipeline_env,
                hard_fails=validation["hard_fail_keys"],
            )
            if attempt == 2:
                log.error("quality_gate_failed_3_attempts", candidate=candidate["id"])
                if pipeline_env == "prod":
                    execute(
                        "UPDATE feed_items SET status = 'rejected' WHERE id = %s",
                        (candidate["id"],),
                    )
                return 1
    else:
        return 1

    if post is None:
        return 1

    # Calculate scheduled time
    post_params = policy.get("post_params", {}).get(slot, {})
    hour_msk = post_params.get("scheduled_hour_msk", 10 if slot == "morning" else 18)
    now_msk = datetime.now(MSK)
    scheduled = now_msk.replace(hour=hour_msk, minute=0, second=0, microsecond=0)
    if scheduled <= now_msk:
        scheduled += timedelta(hours=1)

    # CTA variant — prod only (that's where conversion is measured). The CTA is
    # appended after validate_post, so its text must pass the promotional-phrase
    # gate explicitly or it would bypass editorial tone checks (issue #26).
    cta_variant = None
    if pipeline_env == "prod":
        candidate_variant = select_cta_variant(policy)
        if candidate_variant:
            cta_check = validate_cta_text(CTA_TEMPLATES[candidate_variant])
            if cta_check["ok"]:
                post = append_cta(post, candidate_variant)
                cta_variant = candidate_variant
                log.info("cta_appended", variant=cta_variant, candidate_id=candidate["id"])
            else:
                log.warning("cta_rejected_by_gate", variant=candidate_variant,
                            hits=cta_check.get("hits"))

    summary = parse_summary(candidate.get("summary"))
    summary_patch = {
        "hermes": True,
        "mode": f"{slot}_generated_{pipeline_env}",
        "content_slot": slot,
        "strategy_rubric": summary.get("editorial", {}).get("strategy_rubric"),
        "policy_version": policy.get("version", "unknown"),
        "pipeline_env": pipeline_env,
        "cta_variant": cta_variant,
        "generated_at": datetime.now(UTC).isoformat(),
    }

    if pipeline_env == "stage":
        # INSERT new row for stage post (original prod row stays intact)
        dupe_key = f"stage:{candidate['id']}:{slot}"
        execute(
            """
            INSERT INTO feed_items (
                url, url_hash, title, text, source, source_domain, source_lang,
                source_published_at, status, category, dupe_key,
                summary, post_draft, review_status, scheduled_at,
                is_used, used_as, campaign_tag, need_image,
                pipeline_env, target_channel, source_item_id,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, 'stage_ready', %s, %s,
                COALESCE((SELECT summary FROM feed_items WHERE id = %s), '{}'::jsonb) || %s::jsonb,
                %s, 'approved', %s,
                false, %s, %s, false,
                'stage', 'test', %s,
                now(), now()
            )
            ON CONFLICT (dupe_key) DO NOTHING
            """,
            (
                candidate["url"],
                candidate["url"] and __import__("hashlib").sha256(candidate["url"].encode()).hexdigest(),
                candidate.get("title"),
                candidate.get("text"),
                candidate.get("source"),
                candidate.get("source_domain"),
                candidate.get("source_lang"),
                candidate.get("source_published_at"),
                candidate.get("category", "news"),
                dupe_key,
                candidate["id"],
                json.dumps(summary_patch, ensure_ascii=False),
                post,
                scheduled.astimezone(UTC),
                f"{slot}_stage_shadow",
                f"{slot}_stage",
                candidate["id"],
            ),
        )
        log.info(
            "stage_post_ready",
            source_candidate_id=candidate["id"],
            slot=slot,
            scheduled=scheduled.isoformat(),
        )
    else:
        # Prod: wrap source links into tracked redirects, then UPDATE in place
        post = wrap_tracked_links(post, candidate["id"])
        execute(
            """
            UPDATE feed_items
            SET
                post_draft = %s,
                status = 'approved',
                review_status = 'approved',
                pipeline_env = 'prod',
                target_channel = 'main',
                scheduled_at = %s,
                used_as = %s,
                campaign_tag = %s,
                cta_variant = %s,
                need_image = false,
                summary = COALESCE(summary, '{}'::jsonb) || %s::jsonb,
                updated_at = now()
            WHERE id = %s
            """,
            (
                post,
                scheduled.astimezone(UTC),
                f"{slot}_post",
                slot,
                cta_variant,
                json.dumps(summary_patch, ensure_ascii=False),
                candidate["id"],
            ),
        )
        log.info(
            "prod_post_ready",
            candidate_id=candidate["id"],
            slot=slot,
            scheduled=scheduled.isoformat(),
        )

    return 0


if __name__ == "__main__":
    import sys
    slot = sys.argv[1] if len(sys.argv) > 1 else "morning"
    env = sys.argv[2] if len(sys.argv) > 2 else "prod"
    raise SystemExit(run(slot=slot, pipeline_env=env))
