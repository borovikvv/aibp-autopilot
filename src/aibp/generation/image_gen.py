"""Post image generation via OpenRouter (issue #34).

Generates a scheme/illustration for a post from its editorial angle and
visual_policy, saves the PNG to the static dir, and returns a public URL the
publisher attaches as sendPhoto media or a large link preview (ADR-0009).
Text-to-image only — video is out of scope (#34).

The static dir (IMAGE_OUTPUT_DIR) must be web-served at IMAGE_PUBLIC_BASE_URL.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from aibp.enrichment.llm_client import OpenRouterClient
from aibp.utils.config import get_settings
from aibp.utils.summary import parse_summary

log = structlog.get_logger()

# Visual style per visual_policy.kind — kept sober to match the channel voice.
# Styles must never ask for labels/words: image models render text unreliably
# (misspelled pseudo-words), so every kind is strictly text-free.
_KIND_STYLE = {
    "process_scheme": (
        "a clean minimal abstract process visual — unlabeled geometric shapes "
        "connected by arrows showing flow and transformation, flat editorial style"
    ),
    "editorial_metaphor": "a restrained editorial illustration with a single visual metaphor, flat style",
}


def build_image_prompt(candidate: dict, policy: dict) -> str:
    """Compose the image prompt from the post angle + visual_policy."""
    vp = policy.get("visual_policy") or {}
    editorial = parse_summary(candidate.get("summary")).get("editorial", {})
    angle = editorial.get("one_sentence_angle") or candidate.get("title") or ""
    style = _KIND_STYLE.get(vp.get("kind", "process_scheme"), _KIND_STYLE["process_scheme"])
    palette = vp.get("palette", "editorial_light")
    return (
        f"{style}. Palette: {palette}. "
        f"STRICTLY NO TEXT of any kind: no letters, no words, no numbers, no labels, "
        f"no captions, no logos, no watermarks, no typography, no writing on objects. "
        f"Convey the subject purely through imagery — never write it out. "
        f"Subject: {angle}. Practical business-AI context, sober tone, no hype."
    )


def generate_post_image(feed_item_id: int, candidate: dict, policy: dict,
                        client: OpenRouterClient | None = None) -> str | None:
    """Generate + store a post image, returning its public URL (None on failure)."""
    s = get_settings()
    prompt = build_image_prompt(candidate, policy)

    try:
        client = client or OpenRouterClient()
        image_bytes = client.generate_image(prompt)
    except Exception as e:
        log.error("post_image_gen_error", feed_item_id=feed_item_id, error=str(e))
        return None
    if not image_bytes:
        return None

    try:
        out_dir = Path(s.image_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{feed_item_id}.png").write_bytes(image_bytes)
    except OSError as e:
        log.error("post_image_write_failed", feed_item_id=feed_item_id, error=str(e))
        return None

    url = f"{s.image_public_base_url.rstrip('/')}/{feed_item_id}.png"
    log.info("post_image_generated", feed_item_id=feed_item_id, url=url)
    return url
