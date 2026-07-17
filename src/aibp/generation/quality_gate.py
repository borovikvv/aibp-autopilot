"""Editorial quality gate — deterministic regex validation of LLM output.

This is the SAFETY LAYER between LLM generation and publishing.
LLM output is NEVER trusted directly — every post must pass this gate.

Core patterns (FORBIDDEN_RE, CLICHE_RE, SOURCE_FRAMING_RE) are HARDCODED
and cannot be modified by self-learning. Only ADDED patterns from policy.yaml
are dynamic.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

# Add project root for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


# ═══════════════════════════════════════════════════════════════════
# CORE PATTERNS (hardcoded, never modified by autopilot)
# ═══════════════════════════════════════════════════════════════════

# Forbidden AUDIENCE LABELS — channel tone rejects business-school jargon.
# Issue #29: match audience labels, not neutral word roots. "руководитель
# проекта", "собственник процесса" (process owner — the channel's own
# vocabulary for pilot design), "для бизнес-процесса" must pass; only the
# audience framing ("владельцы бизнеса", "для малого бизнеса", SMB) fails.
FORBIDDEN_RE = re.compile(
    r"(\bSMB\b|\bSME\b|\bМСБ\b|"
    r"мал(ый|ого|ому|ым|ом|ые|ых|ыми)?\s+бизнес|"
    r"средн(ий|его|ему|им|ем|ие|их|ими)?\s+бизнес|"
    r"мал(ый|ого|ому|ым|ом|ые|ых|ыми)?\s+и\s+средн(ий|его|ему|им|ем|ие|их|ими)?\s+бизнес|"
    r"владельц\w*\s+бизнес\w*|владелец\s+бизнес\w*|"
    r"собственник\w*\s+бизнес\w*|\bCEO\b|\bСЕО\b|"
    r"для\s+предпринимател\w*|для\s+управленц\w*|для\s+таких\s+компан\w+|"
    # "для бизнеса" (audience) fails, but "для бизнес-процесса/задачи" passes:
    # require a noun case ending so the hyphenated compound is not matched.
    r"для\s+бизнес(?:а|у|е|ом)\b|"
    r"управленческ\w+\s+вопрос|управленческ\w+\s+вывод|операторск\w+\s+вывод)",
    re.IGNORECASE,
)

# AI template phrases / clichés — banned "essay-like" language
CLICHE_RE = re.compile(
    r"(важно\s+отметить|в\s+современном\s+мире|данный\s+материал|"
    r"это\s+подчеркивает|ключевой\s+вывод\s+заключается|"
    r"практический\s+вывод|суммаризирует\s+локальной\s+моделью|"
    r"полезен\s+управленческий\s+вопрос|"
    r"разборчив\w*\s+черновик\w*)",
    re.IGNORECASE,
)

# Promotional / clickbait CTA phrases — banned everywhere, including appended
# CTAs (issue #26). The channel's tone is anti-hype; a CTA must invite, not sell.
PROMOTIONAL_CTA_RE = re.compile(
    r"(подпишите?сь|подписывайте?сь|не\s+пропуст\w+|жм[иё]те|"
    r"ставьте\s+лайк\w*|переходите\s+по\s+ссылк\w+|регистрируйте?сь|"
    r"покупайте?|заказывайте?|пишите\s+в\s+директ|"
    r"успей\w*\s+купить|только\s+сегодня|перейди\w*\s+по\s+ссылк\w+)",
    re.IGNORECASE,
)

# Measurable marker — a morning post should carry at least one number/metric
# (the implementation_metric rubric). Digit, currency, time/count unit, or a
# spelled-out multiplier. Warn-only (issue #32), never a hard fail.
METRIC_PRESENCE_RE = re.compile(
    r"(\d|[₽$€]|\bруб\b|"
    r"вдво[еёя]|втро[еёя]|вчетверо|вполовину|"
    r"в\s+(?:нескольк|десятк|сотн|тысяч)\w*\s+раз)",
    re.IGNORECASE,
)

# Bold section labels — "<b>Процесс.</b> …", "<b>Метрика.</b> …" and the like.
# Daily posts must weave structure into prose (structure is an internal
# checklist, not visible markup); repeating labels every day reads as
# template-filling and triggers banner blindness. Hard fail for morning and
# evening; weekly_case keeps them deliberately — a once-a-week branded format.
# Matches any single capitalized word in bold ending with "." or ":" — the
# label vocabulary drifts ("Контекст.", "Риск."), so no fixed word list.
SECTION_LABEL_RE = re.compile(
    r"<b>\s*[А-ЯЁA-Z][\w-]*[.:]\s*</b>",
)

# Source framing — post must NOT refer to source in body (only at end)
SOURCE_FRAMING_RE = re.compile(
    r"(В\s+материал[еа](?:\s+[A-ZА-ЯЁA-Za-zА-Яа-яЁё0-9 ._—–-]{0,80})?|"
    r"В\s+стать[еия](?:\s+[A-ZА-ЯЁA-Za-zА-Яа-яЁё0-9 ._—–-]{0,80})?|"
    r"как\s+пишет(?:\s+[A-ZА-ЯЁA-Za-zА-Яа-яЁё0-9 ._—–-]{0,80})?|"
    r"по\s+данным\s+(?-i:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9 ._—–-]{1,80})|"
    r"(?:сообщает|пишет)\s+(?-i:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9 ._—–-]{1,80})|"
    r"со\s+ссылкой\s+на\s+(?-i:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё0-9 ._—–-]{1,80})|"
    r"автор\s+пишет|источник\s+рассказывает|"
    r"детал[еий]\s+там\s+много|материал\s+показывает|разбор\s+показывает)",
    re.IGNORECASE,
)

# Opening source reference ban — post must NOT start with source attribution
OPENING_SOURCE_RE = re.compile(
    r"^\s*(?:<b>.*?</b>\s*)?(Материал|Статья|В\s+статье|В\s+материале|Автор\s+пишет|Источник\s+рассказывает|Разбор)\b",
    re.IGNORECASE | re.DOTALL,
)

# Technical density — too many tech terms without business translation
TECHNICAL_DENSITY_RE = re.compile(
    r"(HTTP\s*200|\bRAG\b|tool\s+manifest|token\s+budget|iteration\s+ceiling|"
    r"retrieval\s+poisoning|sub-agent|subagent|\bтокен\w*\b|"
    r"\b30-й\b|\b50-й\b|\b100-й\b|200\s+повтор\w*)",
    re.IGNORECASE,
)

# Mixed-script word — a single contiguous letter-run that glues Latin and
# Cyrillic together ("juridически" = Latin "jur" + Cyrillic "идически"). This
# is transliteration bleed / homoglyph corruption from the LLM and must never
# reach the channel. Hard fail in every slot.
#
# The run is LETTERS ONLY: a hyphen, space, or digit breaks it, so legitimate
# compounds ("AI-помощник", "ML-модель", "IT-отдел") and pure-Latin tokens
# ("RAG", "GPT-4", "OpenAI") pass — their scripts sit in separate runs. A match
# requires at least one point where a Latin letter is directly adjacent to a
# Cyrillic one; the run is then captured greedily on both sides to report the
# whole broken word. Cyrillic class covers А-я plus Ё/ё (outside that range).
_LAT = r"A-Za-z"
_CYR = r"А-Яа-яЁё"
MIXED_SCRIPT_RE = re.compile(
    rf"[{_LAT}{_CYR}]*(?:[{_LAT}][{_CYR}]|[{_CYR}][{_LAT}])[{_LAT}{_CYR}]*"
)

# Generic final moral — banned "moral of the story" endings
GENERIC_FINAL_MORAL_RE = re.compile(
    r"^(В\s+итоге|Итог|Главн(ый|ое)\s+вывод|Граница\s+получается|"
    r"Именно\s+поэтому|Поэтому\s+важно|Такой\s+подход|Это\s+и\s+есть)\b",
    re.IGNORECASE,
)

# Utility patterns
SOURCE_LINK_RE = re.compile(r'<a\s+href="([^"]+)">Источник</a>', re.IGNORECASE)
HREF_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
BOLD_RE = re.compile(r"<b>.*?</b>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
REGULAR_EMOJI_RE = re.compile(r"[🗞🚀🔥💡✅⚡🤖]")

# Translation hint — if technical terms appear, post must translate to business
WORKFLOW_TRANSLATION_RE = re.compile(
    r"(процесс|заявк|документ|письм|карточк|отч[её]т|провер|ошибк|лимит|"
    r"человек|сотрудник|клиент|стоимост|качество|исправлен|останов|результат|действи)",
    re.IGNORECASE,
)


# ═══════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════

def _strip_urls(text: str) -> str:
    return URL_RE.sub("", text)


def _plain(text: str) -> str:
    return TAG_RE.sub("", text)


def _body_without_final_source(post: str) -> str:
    return SOURCE_LINK_RE.sub("", post).strip()


def _hits(pattern: re.Pattern, text: str) -> list[dict[str, Any]]:
    found = []
    for match in pattern.finditer(text):
        found.append({
            "text": match.group(0),
            "pos": match.start(),
        })
    return found


def _verdict(status: str, hits: list | None = None, note: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if hits is not None:
        result["hits"] = hits
    if note:
        result["note"] = note
    return result


def validate_cta_text(text: str) -> dict[str, Any]:
    """Check a CTA snippet against the promotional-phrase banlist (issue #26).

    CTA text is appended AFTER validate_post, so it would otherwise bypass the
    editorial gate. Returns {"ok": bool, "status": ..., "hits": [...]}.
    """
    plain = _plain(text)
    hits = _hits(PROMOTIONAL_CTA_RE, plain)
    note = "promotional CTA phrase" if hits else None
    if not hits:
        hits = _hits(MIXED_SCRIPT_RE, plain)
        note = "word mixes Latin and Cyrillic letters" if hits else None
    status = "fail" if hits else "pass"
    return {"ok": not hits, **_verdict(status, hits, note=note)}


def validate_post(
    post: str,
    expected_url: str | None = None,
    slot: str = "morning",
    extra_gates: list[dict] | None = None,
) -> dict[str, Any]:
    """Validate final Telegram HTML post_draft.

    Args:
        post: HTML post text
        expected_url: source URL that must appear in the final link
        slot: morning | evening | weekly_digest
        extra_gates: additional regex gates from policy.yaml (auto-populated)

    Returns:
        {"ok": bool, "slot": str, "verdicts": {...}, "hard_fail_keys": [...]}
    """
    body_html = _body_without_final_source(post)
    body_plain = _plain(_strip_urls(body_html))
    full_plain_no_urls = _plain(_strip_urls(post))
    hrefs = SOURCE_LINK_RE.findall(post)
    verdicts: dict[str, dict[str, Any]] = {}

    # Source link check
    source_errors: list[dict[str, Any]] = []
    if slot == "weekly_digest":
        visible_hrefs = HREF_RE.findall(post)
        if visible_hrefs:
            source_errors.append({"text": "weekly_digest must have zero visible links"})
    else:
        if len(hrefs) != 1:
            source_errors.append({"text": f"source_link_count={len(hrefs)} (must be 1)"})
        elif expected_url and hrefs[0] != expected_url:
            source_errors.append({"text": f"source_url_mismatch: {hrefs[0]}"})
        if expected_url and not post.rstrip().endswith(f'<a href="{expected_url}">Источник</a>'):
            source_errors.append({"text": "post must end with exact source link"})
    verdicts["source_link"] = _verdict("fail" if source_errors else "pass", source_errors)

    # Core gates (hardcoded)
    forbidden_hits = _hits(FORBIDDEN_RE, full_plain_no_urls)
    verdicts["forbidden_terms"] = _verdict("fail" if forbidden_hits else "pass", forbidden_hits)

    cliche_hits = _hits(CLICHE_RE, full_plain_no_urls)
    verdicts["ai_template_phrases"] = _verdict("fail" if cliche_hits else "pass", cliche_hits)

    mixed_hits = _hits(MIXED_SCRIPT_RE, full_plain_no_urls)
    verdicts["mixed_script"] = _verdict(
        "fail" if mixed_hits else "pass",
        mixed_hits,
        "word mixes Latin and Cyrillic letters (transliteration bleed)" if mixed_hits else None,
    )

    source_hits = _hits(SOURCE_FRAMING_RE, body_plain)
    verdicts["source_framing"] = _verdict("fail" if source_hits else "pass", source_hits)

    opening_fail = bool(OPENING_SOURCE_RE.search(post.strip()))
    verdicts["opening"] = _verdict(
        "fail" if opening_fail else "pass",
        [{"text": "opening refers to source"}] if opening_fail else [],
    )

    # Visible section labels — banned in daily slots, allowed in weekly_case
    # (branded weekly format) and weekly_digest.
    if slot in ("morning", "evening"):
        label_hits = _hits(SECTION_LABEL_RE, body_html)
        verdicts["section_labels"] = _verdict(
            "fail" if label_hits else "pass",
            label_hits,
            "structure must be woven into prose, not marked with bold labels" if label_hits else None,
        )

    tech_hits = _hits(TECHNICAL_DENSITY_RE, body_plain)
    has_translation = bool(WORKFLOW_TRANSLATION_RE.search(body_plain))
    tech_fail = bool(tech_hits) and (len(tech_hits) >= 2 or not has_translation)
    verdicts["technical_density"] = _verdict(
        "fail" if tech_fail else "pass",
        tech_hits,
        "translate tech terms to business meaning" if tech_fail else None,
    )

    # Structure check
    paragraph_count = len([p for p in re.split(r"\n\s*\n", body_plain.strip()) if p.strip()])
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body_plain.strip()) if p.strip()]
    length_note = f"paragraphs={paragraph_count}; chars={len(post)}"

    if slot == "morning":
        structure_issues = []
        if paragraph_count < 4 or paragraph_count > 5:
            structure_issues.append(f"target_paragraphs=4-5, got {paragraph_count}")
        body_paragraphs = paragraphs[1:] if paragraphs and paragraphs[0].startswith("<b>") else paragraphs
        if body_paragraphs and GENERIC_FINAL_MORAL_RE.search(body_paragraphs[-1]):
            structure_issues.append("generic_final_moral")
        verdicts["morning_structure"] = _verdict(
            "warn" if structure_issues else "pass",
            note="; ".join([length_note] + structure_issues),
        )
        # Soft gate: morning posts should carry a measurable fact (issue #32).
        # warn only — legitimate exceptions exist, but the signal feeds
        # observability and the informed-retry hint (issue #30).
        has_metric = bool(METRIC_PRESENCE_RE.search(body_plain))
        verdicts["metric_presence"] = _verdict(
            "pass" if has_metric else "warn",
            note=None if has_metric else "no numeric/measurable marker in body",
        )
    elif slot == "weekly_case":
        # Issue #40: deep case-study breakdown. Same structural checks as the
        # morning post (paragraph range + no generic moral), plus a soft gate
        # that the post carries at least one before/after number — the whole
        # point of a case study. Warn-only, never blocks.
        structure_issues = []
        if paragraph_count < 4 or paragraph_count > 6:
            structure_issues.append(f"target_paragraphs=4-6, got {paragraph_count}")
        body_paragraphs = paragraphs[1:] if paragraphs and paragraphs[0].startswith("<b>") else paragraphs
        if body_paragraphs and GENERIC_FINAL_MORAL_RE.search(body_paragraphs[-1]):
            structure_issues.append("generic_final_moral")
        verdicts["weekly_case_structure"] = _verdict(
            "warn" if structure_issues else "pass",
            note="; ".join([length_note] + structure_issues),
        )
        has_metric = bool(METRIC_PRESENCE_RE.search(body_plain))
        verdicts["case_metrics_presence"] = _verdict(
            "pass" if has_metric else "warn",
            note=None if has_metric else "weekly_case requires at least one numeric measurement",
        )
    elif slot == "weekly_digest":
        bullet_lines = re.findall(r"(?m)^\s*[•-]\s+(.+)$", post)
        if len(bullet_lines) < 4:
            verdicts["weekly_structure"] = _verdict(
                "warn",
                note=f"bullets={len(bullet_lines)} (min 4); {length_note}",
            )
        else:
            verdicts["weekly_structure"] = _verdict("pass", note=length_note)
    else:
        verdicts["length"] = _verdict("pass", note=length_note)

    # Extra gates from policy.yaml (dynamic, added by self-learning)
    if extra_gates:
        for gate in extra_gates:
            name = gate.get("name", "unnamed_gate")
            pattern_str = gate.get("pattern", "")
            action = gate.get("action", "warn")
            try:
                pattern = re.compile(pattern_str)
                gate_hits = _hits(pattern, full_plain_no_urls)
                if gate_hits:
                    verdicts[name] = _verdict(action, gate_hits)
            except re.error as e:
                verdicts[name] = _verdict("warn", note=f"invalid_regex: {e}")

    # Hard fail keys
    hard_keys = [
        "source_link", "forbidden_terms", "ai_template_phrases",
        "mixed_script", "source_framing", "opening", "technical_density",
    ]
    if "section_labels" in verdicts:
        hard_keys.append("section_labels")
    # Add dynamic gates with action=fail
    if extra_gates:
        for gate in extra_gates:
            if gate.get("action") == "fail":
                hard_keys.append(gate.get("name", "unnamed_gate"))

    ok = all(verdicts.get(k, {}).get("status") == "pass" for k in hard_keys)
    return {
        "ok": ok,
        "slot": slot,
        "verdicts": verdicts,
        "hard_fail_keys": [k for k in hard_keys if verdicts.get(k, {}).get("status") == "fail"],
    }
