"""LLM editor — second-pass holistic review of a generated post.

The regex quality gate (quality_gate.py) checks LOCAL patterns: forbidden
terms, clichés, link format. It cannot see whether the post works as ONE
text — whether the reasoning holds together, whether it reads like a filled
template, whether a number was invented. This editor reads the finished post
as a whole and returns a structured verdict.

Placement in the pipeline: runs AFTER the regex gate passes, inside the same
3-attempt retry loop. Editor problems are fed back into the next generation
prompt exactly like gate failures (informed retry, issue #30).

Failure philosophy: the editor is a QUALITY layer, not a safety layer. Any
infra problem (budget, network, unparseable JSON) degrades OPEN — the post
already passed the deterministic gate, and a regex-validated post is better
than no post. Only an explicit "ok": false verdict blocks.
"""
from __future__ import annotations

from typing import Any

import structlog
from jinja2 import Template

log = structlog.get_logger()

EDITOR_PROMPT = Template("""You are the senior editor of @AI_Business_Pulse — a Russian-language Telegram channel about practical AI in business workflows. A finished post is on your desk, seconds before publication. Automated checks (banned phrases, link format) have already passed. Your job is what automation cannot do: judge the post AS A WHOLE — a single text read top to bottom — not as isolated paragraphs.

## Post (slot: {{ slot }})

{{ post }}

## Source material the post was written from

TITLE: {{ source_title }}
EXCERPT: {{ source_excerpt }}

## Review checklist — the post as a whole

1. single_thread: the post develops ONE line of reasoning from the hook to the last sentence. Paragraphs follow from each other; removing any one of them should leave a visible hole. Flag: a paragraph that could be deleted without loss, a sideways jump, two competing theses.
2. template_feel: the post must read as written by a person with a point, not as a filled form. Flag: visible section labels, paragraphs of identical shape ("statement — example — conclusion" three times in a row), mechanical transitions.
3. coverage: taken as a whole, the post answers: which work task changes, which tool/approach enables it, what the measurable effect is, and where the boundary of applicability lies. The answers may be woven anywhere in the text — but a missing answer is a flag.
4. hook: the first two sentences give a specific reason to keep reading (a contrast, a tension, an unexpected number). Flag: a generic true-but-boring opening.
5. no_leaked_meta: no editorial notes or summaries leaked into the post — nothing after the final "Источник" link, no parenthetical recaps, no instructions-speak.
6. factual_support: every specific number and claim is supported by the source excerpt (or is clearly the author's own arithmetic on those numbers). Flag invented specifics. The excerpt is truncated — flag only what CONTRADICTS it or clearly comes from nowhere, not what is merely absent.

## Verdict

Return JSON:
{"ok": <bool>, "problems": [{"key": "<checklist item>", "detail": "<одно предложение по-русски: что не так и как исправить>"}]}

"ok": false ONLY for defects a subscriber would actually notice — a broken line of reasoning, a template smell, a missing checklist answer, leaked meta-text, an invented number. Style preferences and minor wording are NOT blocking: mention nothing or put them in problems while keeping "ok": true. An empty problems list means the post is clean.
""")

# Verdict keys the retry prompt understands; anything else from the model is
# passed through untouched — the next generation attempt reads the detail text.
_REVIEW_SLOT_DEFAULT = "morning"


def review_post(
    post: str,
    slot: str,
    client: Any,
    source_title: str = "",
    source_excerpt: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """Holistic editorial review of one finished post.

    Returns {"ok": bool, "problems": [{"key", "detail"}], "skipped": bool}.
    skipped=True means the review could not run (infra) and the post is
    accepted on the regex gate alone.
    """
    prompt = EDITOR_PROMPT.render(
        post=post,
        slot=slot or _REVIEW_SLOT_DEFAULT,
        source_title=source_title,
        source_excerpt=(source_excerpt or "")[:2000],
    )
    try:
        verdict = client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.0,
            max_tokens=1000,
        )
    except Exception as e:
        log.warning("llm_editor_unavailable", error=str(e))
        return {"ok": True, "problems": [], "skipped": True}

    if not isinstance(verdict, dict) or not isinstance(verdict.get("ok"), bool):
        log.warning("llm_editor_malformed_verdict", verdict_preview=str(verdict)[:200])
        return {"ok": True, "problems": [], "skipped": True}

    problems = []
    for p in verdict.get("problems") or []:
        if isinstance(p, dict) and p.get("detail"):
            problems.append({"key": str(p.get("key", "editor")), "detail": str(p["detail"])})

    result = {"ok": verdict["ok"], "problems": problems, "skipped": False}
    log.info(
        "llm_editor_verdict",
        ok=result["ok"],
        problems=[p["key"] for p in problems],
        slot=slot,
    )
    return result


def editor_failures(review: dict[str, Any]) -> list[dict]:
    """Map an editor verdict to the informed-retry hint format of
    extract_gate_failures: [{"key": ..., "hits": ...}]."""
    return [
        {"key": f"editor_{p['key']}", "hits": p["detail"]}
        for p in review.get("problems", [])
    ]
