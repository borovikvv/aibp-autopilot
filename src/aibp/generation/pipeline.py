"""Post generation — LLM writes Telegram post, quality gate validates.

Cron: morning (10:00 MSK), evening (18:00 MSK), weekly (Sun 19:00 MSK).
For shadow testing: stage generation uses policy.stage.yaml and publishes to test channel.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
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
9. Post must start with a bold headline: `<b>...</b>`, then 3-4 paragraphs. Bold is capped at {{ max_bold }} uses total.
10. Length: {{ target_chars_min }}-{{ target_chars_max }} chars, {{ paragraphs_min }}-{{ paragraphs_max }} paragraphs.
11. End with: `<a href="{{ url }}">Источник</a>` (exact format, nofollow not needed).

## Slot-specific guidance

{% if slot == "morning" %}
Morning = analytical pillar. The post must ANSWER four questions — this is an
INTERNAL checklist for you, invisible to the reader:
1. Which work task changes, and how it was done before.
2. Which tool or approach makes the change possible.
3. What the measurable effect is (time, money, %, count).
4. Where the boundary is — when this stops working.

Weave the answers into connected prose: one line of reasoning from the first
sentence to the last, paragraphs flowing into each other. FORBIDDEN: bold
section labels ("<b>Процесс.</b>", "<b>Метрика.</b>", "<b>Граница.</b>" or any
similar one-word lead-in). The reader must never see the skeleton. No bullet
symbols, no emoji, no lists. The only bold element is the headline.

Vary the hook. Pick ONE opening move that fits this material best:
- a contrast of two numbers or prices;
- a question the reader has already asked themselves;
- an unexpected number with a turn ("...and that's not the interesting part");
- a one-sentence scene from someone's working day.
{% if recent_openings %}
Recent posts opened with the lines below. Do NOT reuse their opening move or
sentence shape:
{% for opening in recent_openings %}- {{ opening }}
{% endfor %}{% endif %}
Do NOT add a hashtag yourself — one is appended automatically.
{% elif slot == "evening" %}
Evening = boundary note. One limit, mistake, or distinction. Short and sharp.
{% elif slot == "weekly_digest" %}
Weekly digest = synthesis of 4-5 sources around one weekly signal.
{% elif slot == "weekly_case" %}
Weekly case = a deep "case study" breakdown of ONE implementation: process → tool → before/after numbers → stop condition. Not a recap.
{% endif %}

{% if previous_failures %}
## Предыдущая попытка отклонена редактурой

Твой прошлый вариант не прошёл. Исправь ТОЛЬКО перечисленное, не ломая остального:
{% for f in previous_failures %}- {{ f.key }}{% if f.hits %}: {{ f.hits }}{% endif %}
{% endfor %}
{% endif %}

## Output

Return ONLY the post HTML. No explanation, no markdown fences.
""")


WEEKLY_CASE_PROMPT = Template("""You are the editor of @AI_Business_Pulse.

Write ONE Russian Telegram post — a weekly "case study" breakdown for the weekly_case slot.

## Source material
TITLE: {{ title }}
SOURCE: {{ source }} ({{ source_domain }})
URL: {{ url }}
EXCERPT: {{ text_excerpt }}

## Format — "разбор одного внедрения с ROI"
The post must follow this structure:
1. <b>Заголовок</b> — one-line case summary.
2. <b>Процесс.</b> What work task changed and how (the before-state).
3. <b>Инструмент.</b> Which AI tool/approach was used and why.
4. <b>Метрика.</b> Before/after numbers — time, cost, quality, count. At least one concrete number is REQUIRED.
5. <b>Граница.</b> Where this stops working / stop condition / limitation.
End with: <a href="{{ url }}">Источник</a>

## Rules
- Russian language, HTML format.
- One source, one deep breakdown — NOT a news recap.
- At least one numeric measurement (%, time, cost, count) in the body.
- No forbidden terms: SMB, CEO, "для бизнеса".
- No AI clichés. No generic moral endings.
- Length: {{ target_chars_min }}-{{ target_chars_max }} chars, {{ paragraphs_min }}-{{ paragraphs_max }} paragraphs.
- Max bold: {{ max_bold }}.

Return ONLY the post HTML. No explanation, no markdown fences.
""")


# ═══════════════════════════════════════════════════════════════════
# Weekly case day mechanics (issue #40)
# ═══════════════════════════════════════════════════════════════════

def _should_skip_for_weekly_case(slot: str, policy: dict, today: date | None = None) -> bool:
    """True when this slot should NOT run today due to the weekly_case schedule.

    Single source of truth for the weekly_case day logic:
    - On the case weekday (today.weekday() == weekly_case.weekday): the morning
      slot is replaced by weekly_case — morning skips, weekly_case fires.
    - On any other day: weekly_case does not run.
    - Slots other than morning/weekly_case are never affected.

    When `today` is None it defaults to the current MSK date.
    """
    if today is None:
        today = datetime.now(MSK).date()
    wc = policy.get("weekly_case") or {}
    if not wc.get("enabled", False):
        return False
    is_case_day = today.weekday() == wc.get("weekday", 2)
    if slot == "morning" and is_case_day:
        return True
    if slot == "weekly_case" and not is_case_day:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# Candidate selection
# ═══════════════════════════════════════════════════════════════════

def select_candidate(
    slot: str,
    policy: dict,
    pipeline_env: str = "prod",
    exclude_ids: set[int] | None = None,
) -> dict | None:
    """Select one best candidate for the slot.

    For prod: pick from enriched, unused sources.
    For stage: pick from already-published prod sources (to compare same source
               with different policy), excluding those already re-published to stage.

    For weekly_case (issue #40): prefer candidates whose editorial category is
    'cases', 'regulation' or 'research' with source_fit_score >= 4 — a deep
    implementation breakdown needs a source that supports one.

    exclude_ids: candidate IDs to ignore (dedup loop, Task 3.7). Applied to all
                 slots before returning.
    """
    rubric_weights = policy.get("rubric_weights", {})
    exclude_ids = exclude_ids or set()

    # ── weekly_case: prefer cases/regulation/research with high source_fit ──
    if slot == "weekly_case" and pipeline_env != "stage":
        case_candidates = fetch_all(
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
              AND category IN ('cases', 'regulation', 'research')
            ORDER BY rank_score DESC NULLS LAST
            LIMIT 20
            """
        )
        if exclude_ids:
            case_candidates = [c for c in case_candidates if c["id"] not in exclude_ids]
        if not case_candidates:
            log.info("no_candidates", slot=slot, pipeline_env=pipeline_env)
            return None
        # Prefer source_fit_score >= 4; fall back to the whole case pool.
        slot_filtered = [
            c for c in case_candidates
            if (parse_summary(c.get("summary")).get("editorial", {}).get("source_fit_score", 0) or 0) >= 4
        ]
        if not slot_filtered:
            slot_filtered = case_candidates
        slot_filtered.sort(key=lambda c: float(c.get("rank_score", 50)), reverse=True)
        return slot_filtered[0]

    fresh_candidates_sql = """
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
        if not candidates:
            # Cold start: no prod-published sources yet (fresh install / prod not
            # cut over). Fall back to fresh enriched items so the test channel
            # can run standalone before the prod pipeline exists. Items already
            # turned into a stage post are excluded — the shadow flow never marks
            # its fresh sources is_used, so without this every slot would keep
            # picking the same top-ranked item (and silently no-op on dupe_key).
            log.info("stage_fallback_fresh_candidates", slot=slot)
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
                  AND id NOT IN (
                      SELECT source_item_id FROM feed_items
                      WHERE source_item_id IS NOT NULL
                        AND pipeline_env = 'stage'
                  )
                ORDER BY rank_score DESC NULLS LAST, source_published_at DESC
                LIMIT 20
                """
            )
    else:
        # Prod: fresh enriched candidates
        candidates = fetch_all(fresh_candidates_sql)

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
    if exclude_ids:
        slot_filtered = [c for c in slot_filtered if c["id"] not in exclude_ids]
    return slot_filtered[0] if slot_filtered else None


# ═══════════════════════════════════════════════════════════════════
# Post generation
# ═══════════════════════════════════════════════════════════════════

_OPENING_TAG_RE = re.compile(r"<[^>]+>")


def extract_opening(post_draft: str | None, max_chars: int = 120) -> str | None:
    """First body sentence of a post, plain text — the post's 'opening move'.

    Skips the bold headline line: hook repetition lives in the first body
    sentence, and headlines are already distinct per source.
    """
    if not post_draft:
        return None
    lines = [ln.strip() for ln in post_draft.splitlines() if ln.strip()]
    if lines and lines[0].startswith("<b>"):
        lines = lines[1:]
    for line in lines:
        plain = _OPENING_TAG_RE.sub("", line).strip()
        if plain and "Источник" not in plain and not plain.startswith("#"):
            return plain[:max_chars]
    return None


def fetch_recent_openings(limit: int = 5) -> list[str]:
    """Openings of the last published main-channel posts (anti-repetition).

    Injected into the morning prompt so consecutive posts don't reuse the
    same hook shape. Degrades to [] on any DB problem — variety guidance is
    never worth blocking generation.
    """
    try:
        rows = fetch_all(
            """
            SELECT post_draft FROM feed_items
            WHERE status = 'published'
              AND pipeline_env = 'prod'
              AND target_channel = 'main'
              AND post_draft IS NOT NULL
            ORDER BY posted_at DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
    except Exception as e:
        log.warning("recent_openings_unavailable", error=str(e))
        return []
    openings = [extract_opening(r.get("post_draft")) for r in rows]
    return [o for o in openings if o]


def extract_gate_failures(validation: dict) -> list[dict]:
    """Turn a validate_post result into retry hints (issue #30).

    Includes every failed verdict plus warnings (e.g. metric_presence), each
    as {"key": ..., "hits": "..."} so the next prompt can name what to fix.
    """
    failures = []
    for key, verdict in validation.get("verdicts", {}).items():
        if verdict.get("status") not in ("fail", "warn"):
            continue
        hits = verdict.get("hits") or []
        detail = ", ".join(h.get("text", "") for h in hits if h.get("text"))
        if not detail:
            detail = verdict.get("note") or ""
        failures.append({"key": key, "hits": detail})
    return failures


def generate_post(candidate: dict, slot: str, policy: dict, client: OpenRouterClient,
                  previous_failures: list[dict] | None = None, attempt: int = 0,
                  recent_openings: list[str] | None = None) -> str | None:
    """Generate post draft via LLM.

    previous_failures: gate feedback from the prior attempt, injected into the
    prompt so the model fixes the specific problem instead of re-rolling blind
    (issue #30). temperature rises slightly on later attempts to escape a stuck
    formulation.

    recent_openings: first sentences of the latest published posts, injected
    into the morning prompt so the hook doesn't repeat day to day.
    """
    summary = parse_summary(candidate.get("summary"))
    editorial = summary.get("editorial", {})

    post_params = policy.get("post_params", {}).get(slot, {})

    if slot == "weekly_case":
        # Issue #40: deep "case study" breakdown uses its own dedicated template.
        prompt = WEEKLY_CASE_PROMPT.render(
            title=candidate.get("title", ""),
            source=candidate.get("source", ""),
            source_domain=candidate.get("source_domain", ""),
            url=candidate.get("url", ""),
            text_excerpt=(candidate.get("text") or "")[:2000],
            target_chars_min=post_params.get("target_chars", [1200, 1800])[0],
            target_chars_max=post_params.get("target_chars", [1200, 1800])[1],
            paragraphs_min=post_params.get("paragraphs", [4, 6])[0],
            paragraphs_max=post_params.get("paragraphs", [4, 6])[1],
            max_bold=post_params.get("max_bold", 4),
        )
    else:
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
            max_bold=post_params.get("max_bold", 1),
            previous_failures=previous_failures or [],
            recent_openings=recent_openings or [],
        )

    # Nudge temperature up on retries to escape a stuck formulation.
    temperature = min(0.4 + 0.15 * attempt, 0.7)

    try:
        post = client.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            # Reasoning models (sonnet-5) spend tokens on the reasoning channel
            # before the visible answer — 2000 starved the content entirely.
            max_tokens=4000,
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


# A native poll is an alternative CTA, eligible only in the evening slot
# (issue #33) — a boundary note invites "would this work for you?".
POLL_VARIANT = "poll"

EVENING_POLL = {
    "question": "Сработало бы это у вас?",
    "options": ["Да, попробуем", "Уже используем", "Нет, не наш случай"],
}


def select_cta_variant(policy: dict, slot: str | None = None) -> str | None:
    """Sample one CTA variant according to policy weights. None disables CTA.

    The 'poll' variant (issue #33) is eligible only for the evening slot.
    """
    weights = policy.get("cta_variants") or {}
    eligible = set(CTA_TEMPLATES)
    if slot == "evening":
        eligible.add(POLL_VARIANT)
    valid = {
        k: float(v) for k, v in weights.items()
        if k in eligible and isinstance(v, (int, float)) and v > 0
    }
    if not valid:
        return None
    import random
    return random.choices(list(valid.keys()), weights=list(valid.values()))[0]


def build_evening_poll() -> dict:
    """A native poll for the evening slot (issue #33)."""
    return {"question": EVENING_POLL["question"], "options": list(EVENING_POLL["options"])}


# Deterministic rubric → hashtag map (issue #31). The rubric is assigned at
# enrichment, so this needs no LLM — just a lookup for channel navigation.
RUBRIC_HASHTAGS = {
    "process_under_ai": "#процесс",
    "pilot_without_chaos": "#пилот",
    "implementation_metric": "#метрика",
    "ai_regulation": "#контур",
    "tool_through_scenario": "#инструмент",
    "anti_hype": "#безхайпа",
}


def rubric_hashtag(rubric: str | None) -> str | None:
    """Hashtag for a strategy rubric, or None for an unknown/empty rubric."""
    return RUBRIC_HASHTAGS.get(rubric or "")


def append_hashtag(post: str, rubric: str | None) -> str:
    """Insert the rubric hashtag on its own line before the final source link."""
    tag = rubric_hashtag(rubric)
    if not tag:
        return post
    lines = post.rstrip().rsplit("\n", 1)
    if len(lines) == 2 and "Источник" in lines[1]:
        return f"{lines[0]}\n\n{tag}\n{lines[1]}"
    return f"{post.rstrip()}\n\n{tag}"


def _insert_before_source(post: str, cta_html: str) -> str:
    """Insert a CTA paragraph before the final source link."""
    lines = post.rstrip().rsplit("\n", 1)
    if len(lines) == 2 and "Источник" in lines[1]:
        return f"{lines[0]}\n{cta_html}\n\n{lines[1]}"
    return f"{post.rstrip()}\n\n{cta_html}"


def append_cta(post: str, variant: str) -> str:
    """Insert the CTA paragraph before the final source link."""
    text = CTA_TEMPLATES.get(variant)
    if not text:
        return post
    return _insert_before_source(post, f"<i>{text}</i>")


AFFILIATE_VARIANT = "affiliate_link"


def attach_affiliate_offer(post: str, candidate: dict) -> tuple[str, str] | None:
    """Pick an offer for the post's topic and append a tracked offer CTA.

    Returns (post_with_cta, offer_slug), or None when no offer can be
    attached — empty/unreachable catalog, no eligible offer for the topic,
    tracking not configured, or the offer title fails the promotional gate.
    The caller then falls back to non-affiliate CTA variants (issue #38).
    """
    if not get_settings().tracking_base_url:
        return None
    try:
        from aibp.monetization.offers import pick_offer
        from aibp.tracking.redirect_service import register_link, short_url

        summary = parse_summary(candidate.get("summary"))
        topic = (summary.get("editorial") or {}).get("topic_cluster")
        offer = pick_offer(topic)
        if offer is None:
            return None
        if not validate_cta_text(offer["title"])["ok"]:
            log.warning("offer_title_rejected_by_gate", offer=offer["slug"])
            return None
        url = short_url(register_link(candidate["id"], offer["target_url"],
                                      offer_id=offer["id"]))
        cta_html = (f'<i>По теме поста: <a href="{url}">{offer["title"]}</a> — '
                    f'детали, тарифы и ограничения по ссылке.</i>')
        return _insert_before_source(post, cta_html), offer["slug"]
    except Exception as e:
        log.warning("offer_attach_failed", candidate_id=candidate.get("id"), error=str(e))
        return None


# ═══════════════════════════════════════════════════════════════════
# Post media (issue #28)
# ═══════════════════════════════════════════════════════════════════

def resolve_post_image(policy: dict) -> str | None:
    """Operator-set static image URL for a post, or None.

    This is the static override (visual_policy.static_image_url, gated by
    visual_policy.enabled). Per-post OpenRouter generation
    (visual_policy.generate) is handled separately in run() via
    aibp.generation.image_gen (issue #34).
    """
    vp = policy.get("visual_policy") or {}
    if not vp.get("enabled"):
        return None
    url = (vp.get("static_image_url") or "").strip()
    return url if url.startswith(("http://", "https://")) else None


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
        slot: morning | evening | weekly_digest | weekly_case
        pipeline_env: prod → publish to main channel with config/policy.yaml
                      stage → publish to test channel with config/policy.stage.yaml
    """
    policy = load_policy(pipeline_env=pipeline_env)
    if pipeline_env == "prod":
        # ADR-0007: an active interleave experiment alternates policies by day
        # in the main channel; on variant days the shadow policy is used.
        from aibp.self_learning.interleave import resolve_policy_for_today
        policy = resolve_policy_for_today(policy)

    # Weekly case day mechanics (issue #40): on the configured weekday the
    # morning slot is replaced by weekly_case; on other days weekly_case does
    # not run. Exactly one of them fires.
    if _should_skip_for_weekly_case(slot, policy):
        log.info("slot_skipped_weekly_case", slot=slot)
        return 0

    client = OpenRouterClient()

    # Competitor dedup (issue #40): skip candidates already covered by competitors.
    # Prod only — stage re-publishes existing prod sources for shadow comparison,
    # and skipping a stage post would lose a paired data point for the experiment.
    # Up to 3 attempts — each duplicate hit is added to exclude_ids so the next
    # pick skips it. Degrades to 'unique' (no block) on any infra failure, so a
    # downed embeddings service never blocks publishing.
    exclude_ids: set[int] = set()
    candidate = None
    if pipeline_env == "prod":
        for _dedup_attempt in range(3):
            candidate = select_candidate(slot, policy, pipeline_env=pipeline_env, exclude_ids=exclude_ids or None)
            if candidate is None:
                break
            dedup_result = "unique"
            try:
                from aibp.self_learning.competitor_dedup import check_duplicate
                dedup_result = check_duplicate(
                    candidate.get("title", ""),
                    candidate.get("text", ""),
                    policy,
                )
            except Exception as e:
                log.warning("competitor_dedup_failed", error=str(e))
            if dedup_result != "duplicate":
                break
            log.info("candidate_skipped_competitor_dup", candidate_id=candidate["id"])
            exclude_ids.add(candidate["id"])
        else:
            candidate = None
    else:
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

    # Generate with retry (max 3 attempts if a check fails). Each retry is
    # "informed": the prior attempt's feedback goes into the prompt (issue #30)
    # instead of re-rolling the same prompt blind. Two checks run in order:
    # the deterministic regex gate (safety layer), then the LLM editor
    # (quality layer — reads the post as one whole text). Editor problems feed
    # the retry prompt the same way gate failures do.
    editor_cfg = policy.get("llm_editor") or {}
    editor_enabled = editor_cfg.get("enabled", True)

    post = None
    previous_failures: list[dict] | None = None
    recent_openings = fetch_recent_openings() if slot == "morning" else []
    for attempt in range(3):
        post = generate_post(candidate, slot, policy, client,
                             previous_failures=previous_failures, attempt=attempt,
                             recent_openings=recent_openings)
        if post is None:
            # LLM-level failure (transient API error, empty completion) — worth
            # another attempt, unlike a permanent config problem. Bail out only
            # when every attempt failed.
            if attempt == 2:
                return 1
            continue

        validation = validate_post(
            post=post,
            expected_url=candidate["url"],
            slot=slot,
            extra_gates=policy.get("regex_gates", []),
        )

        if validation["ok"]:
            review = {"ok": True, "problems": [], "skipped": True}
            if editor_enabled:
                from aibp.generation.llm_editor import editor_failures, review_post
                review = review_post(
                    post, slot, client,
                    source_title=candidate.get("title", ""),
                    source_excerpt=candidate.get("text") or "",
                    model=editor_cfg.get("model"),
                )
            if review["ok"]:
                log.info(
                    "quality_gate_passed",
                    attempt=attempt + 1, slot=slot, pipeline_env=pipeline_env,
                    llm_editor="skipped" if review.get("skipped") else "approved",
                )
                break
            previous_failures = editor_failures(review)
            log.warning(
                "llm_editor_rejected",
                attempt=attempt + 1,
                pipeline_env=pipeline_env,
                problems=[p["key"] for p in review["problems"]],
                informed_retry=attempt < 2,
            )
        else:
            previous_failures = extract_gate_failures(validation)
            log.warning(
                "quality_gate_failed",
                attempt=attempt + 1,
                pipeline_env=pipeline_env,
                hard_fails=validation["hard_fail_keys"],
                informed_retry=attempt < 2,
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

    # CTA variant — prod only (that's where conversion is measured). Text CTAs
    # are appended after validate_post, so their text must pass the
    # promotional-phrase gate (issue #26). The 'poll' variant (issue #33) is a
    # native evening poll instead of appended text.
    cta_variant = None
    poll = None
    offer_slug = None
    if pipeline_env == "prod":
        candidate_variant = select_cta_variant(policy, slot=slot)

        # affiliate_link now means a real offer from the catalog (issue #38).
        # No attachable offer → resample among the remaining variants.
        if candidate_variant == AFFILIATE_VARIANT:
            attached = attach_affiliate_offer(post, candidate)
            if attached is not None:
                post, offer_slug = attached
                cta_variant = AFFILIATE_VARIANT
                candidate_variant = None
                log.info("offer_cta_appended", offer=offer_slug, candidate_id=candidate["id"])
            else:
                fallback = {k: v for k, v in (policy.get("cta_variants") or {}).items()
                            if k != AFFILIATE_VARIANT}
                candidate_variant = select_cta_variant({"cta_variants": fallback}, slot=slot)
                log.info("offer_unavailable_cta_fallback",
                         fallback_variant=candidate_variant, candidate_id=candidate["id"])

        if candidate_variant == POLL_VARIANT:
            poll = build_evening_poll()
            cta_variant = POLL_VARIANT
            log.info("evening_poll_attached", candidate_id=candidate["id"])
        elif candidate_variant:
            cta_check = validate_cta_text(CTA_TEMPLATES[candidate_variant])
            if cta_check["ok"]:
                post = append_cta(post, candidate_variant)
                cta_variant = candidate_variant
                log.info("cta_appended", variant=cta_variant, candidate_id=candidate["id"])
            else:
                log.warning("cta_rejected_by_gate", variant=candidate_variant,
                            hits=cta_check.get("hits"))

    # Tracked inline source button (issue #33): opt-in via policy.telegram.
    source_button_url = None
    if pipeline_env == "prod" and (policy.get("telegram") or {}).get("source_button"):
        if get_settings().tracking_base_url and candidate.get("url"):
            try:
                from aibp.tracking.redirect_service import register_link, short_url
                source_button_url = short_url(register_link(candidate["id"], candidate["url"]))
            except Exception as e:
                log.warning("source_button_link_failed", candidate_id=candidate["id"], error=str(e))

    summary = parse_summary(candidate.get("summary"))
    summary_patch = {
        "hermes": True,
        "mode": f"{slot}_generated_{pipeline_env}",
        "content_slot": slot,
        "strategy_rubric": summary.get("editorial", {}).get("strategy_rubric"),
        "policy_version": policy.get("version", "unknown"),
        "pipeline_env": pipeline_env,
        "cta_variant": cta_variant,
        "offer_slug": offer_slug,
        "poll": poll,
        "source_button_url": source_button_url,
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
                # url_hash stays NULL: it is the collector's dedup key and the
                # source row already holds sha256(url) — a stage copy with the
                # same hash would violate the UNIQUE constraint. Stage dedup is
                # handled by dupe_key below.
                None,
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
        # Prod: rubric hashtag (issue #31), then wrap source links into tracked
        # redirects, then UPDATE in place.
        post = append_hashtag(post, summary_patch["strategy_rubric"])
        post = wrap_tracked_links(post, candidate["id"])

        # Post image: operator static override, else OpenRouter generation
        # when enabled (issue #28/#34). Failure → text-only post.
        image_url = resolve_post_image(policy)
        image_status = None
        vp = policy.get("visual_policy") or {}
        if image_url is None and vp.get("enabled") and vp.get("generate"):
            from aibp.generation.image_gen import generate_post_image
            image_url = generate_post_image(candidate["id"], candidate, policy, client)
            image_status = "generated" if image_url else "failed"

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
                need_image = %s,
                image_url = %s,
                image_status = %s,
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
                bool(image_url),
                image_url,
                image_status,
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
