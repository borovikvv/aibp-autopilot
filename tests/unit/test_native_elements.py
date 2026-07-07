"""Tests for native Telegram elements: source button + evening poll (issue #33)."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.generation.pipeline import (
    POLL_VARIANT,
    build_evening_poll,
    select_cta_variant,
)
from aibp.publishing import publisher

OK = {"ok": True, "result": {"message_id": 1}}


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# Generation: poll eligibility + builder
# ═══════════════════════════════════════════════════════════════════

def test_poll_variant_only_eligible_in_evening():
    policy = {"cta_variants": {"poll": 1.0}}  # only poll has weight
    # evening → poll can be selected
    assert select_cta_variant(policy, slot="evening") == "poll"
    # other slots → poll not eligible → no CTA
    assert select_cta_variant(policy, slot="morning") is None
    assert select_cta_variant(policy, slot=None) is None


def test_text_cta_still_selectable_any_slot():
    policy = {"cta_variants": {"comment_prompt": 1.0}}
    assert select_cta_variant(policy, slot="morning") == "comment_prompt"


def test_build_evening_poll_shape():
    poll = build_evening_poll()
    assert poll["question"]
    assert 2 <= len(poll["options"]) <= 12
    assert all(isinstance(o, str) for o in poll["options"])


# ═══════════════════════════════════════════════════════════════════
# Publisher: source button markup + extras parsing
# ═══════════════════════════════════════════════════════════════════

def test_source_button_markup_shape():
    markup = publisher.source_button_markup("https://track/r/ab12cd34")
    button = markup["inline_keyboard"][0][0]
    assert button["url"] == "https://track/r/ab12cd34"
    assert "источник" in button["text"].lower()


def test_post_extras_reads_summary():
    item = {"summary": {"source_button_url": "https://track/r/x",
                        "poll": {"question": "Q?", "options": ["a", "b"]}}}
    markup, poll = publisher._post_extras(item)
    assert markup["inline_keyboard"][0][0]["url"] == "https://track/r/x"
    assert poll["question"] == "Q?"


def test_post_extras_absent_returns_none():
    markup, poll = publisher._post_extras({"summary": {}})
    assert markup is None and poll is None


def test_reply_markup_flows_into_send_path():
    item = {"id": 1, "need_image": False, "image_url": None}
    markup = publisher.source_button_markup("https://track/r/x")
    with patch.object(publisher, "send_message", new=AsyncMock(return_value=OK)) as sm:
        _run(publisher._publish_post_message("T", "chat", item, "text", reply_markup=markup))
    assert sm.await_args.kwargs["reply_markup"] == markup


# ═══════════════════════════════════════════════════════════════════
# Publisher: send_poll
# ═══════════════════════════════════════════════════════════════════

def test_send_poll_payload_and_limits():
    captured = {}

    class _Resp:
        def json(self):
            return OK

    async def fake_post(url, json):  # noqa: A002
        captured["url"] = url
        captured["json"] = json
        return _Resp()

    with patch.object(publisher.httpx, "AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=fake_post)
        _run(publisher.send_poll("T", "chat", "Q" * 400, ["opt" + str(i) for i in range(15)]))

    assert captured["url"].endswith("/sendPoll")
    assert len(captured["json"]["question"]) == 300          # question capped at 300
    assert len(captured["json"]["options"]) == 12            # options capped at 12
    assert captured["json"]["options"][0] == {"text": "opt0"}
