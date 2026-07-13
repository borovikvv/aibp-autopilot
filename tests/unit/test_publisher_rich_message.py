"""Tests for rich-message publishing (issue #28)."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.generation.pipeline import resolve_post_image
from aibp.publishing import publisher

OK = {"ok": True, "result": {"message_id": 1}}
FAIL = {"ok": False, "description": "wrong file identifier/HTTP URL"}


def _run(coro):
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# resolve_post_image (generation side)
# ═══════════════════════════════════════════════════════════════════

def test_resolve_image_disabled_returns_none():
    assert resolve_post_image({"visual_policy": {"enabled": False,
                                                 "static_image_url": "https://x/i.png"}}) is None
    assert resolve_post_image({}) is None


def test_resolve_image_enabled_returns_url():
    policy = {"visual_policy": {"enabled": True, "static_image_url": "https://x/i.png"}}
    assert resolve_post_image(policy) == "https://x/i.png"


def test_resolve_image_rejects_non_http():
    policy = {"visual_policy": {"enabled": True, "static_image_url": "ftp://x/i.png"}}
    assert resolve_post_image(policy) is None


# ═══════════════════════════════════════════════════════════════════
# Publish path selection
# ═══════════════════════════════════════════════════════════════════

def test_no_media_sends_plain_text():
    item = {"id": 1, "need_image": False, "image_url": None}
    with patch.object(publisher, "send_message", new=AsyncMock(return_value=OK)) as sm, \
         patch.object(publisher, "send_photo", new=AsyncMock(return_value=OK)) as sp:
        result = _run(publisher._publish_post_message("T", "chat", item, "short text"))
    assert result == OK
    sp.assert_not_called()
    sm.assert_awaited_once()
    assert "link_preview_options" not in sm.await_args.kwargs


def test_short_post_with_media_uses_link_preview():
    """Short posts with media also go out as ONE rich message — never
    sendPhoto+caption (caption caps at 1024 and reads as an attachment)."""
    item = {"id": 1, "need_image": True, "image_url": "https://x/i.png"}
    with patch.object(publisher, "send_message", new=AsyncMock(return_value=OK)) as sm, \
         patch.object(publisher, "send_photo", new=AsyncMock(return_value=OK)) as sp:
        _run(publisher._publish_post_message("T", "chat", item, "x" * 500))
    sp.assert_not_called()
    sm.assert_awaited_once()
    lpo = sm.await_args.kwargs["link_preview_options"]
    assert lpo["url"] == "https://x/i.png"
    assert lpo["prefer_large_media"] is True
    assert lpo["show_above_text"] is True


def test_long_post_with_media_uses_link_preview():
    item = {"id": 1, "need_image": True, "image_url": "https://x/i.png"}
    long_text = "y" * (publisher.CAPTION_LIMIT + 100)
    with patch.object(publisher, "send_message", new=AsyncMock(return_value=OK)) as sm, \
         patch.object(publisher, "send_photo", new=AsyncMock(return_value=OK)) as sp:
        _run(publisher._publish_post_message("T", "chat", item, long_text))
    sp.assert_not_called()
    sm.assert_awaited_once()
    lpo = sm.await_args.kwargs["link_preview_options"]
    assert lpo["url"] == "https://x/i.png"
    assert lpo["prefer_large_media"] is True


def test_boundary_at_caption_limit_still_uses_link_preview():
    item = {"id": 1, "need_image": True, "image_url": "https://x/i.png"}
    with patch.object(publisher, "send_message", new=AsyncMock(return_value=OK)) as sm, \
         patch.object(publisher, "send_photo", new=AsyncMock(return_value=OK)) as sp:
        _run(publisher._publish_post_message("T", "chat", item, "z" * publisher.CAPTION_LIMIT))
    sp.assert_not_called()
    sm.assert_awaited_once()
    assert "link_preview_options" in sm.await_args.kwargs


def test_media_failure_falls_back_to_text():
    """A failed rich send must fall back to a plain text message (issue #28)."""
    item = {"id": 1, "need_image": True, "image_url": "https://x/broken.png"}
    sends = [FAIL, OK]
    sm = AsyncMock(side_effect=sends)
    with patch.object(publisher, "send_message", new=sm):
        result = _run(publisher._publish_post_message("T", "chat", item, "short"))
    assert sm.await_count == 2        # tried media, fell back to text
    assert "link_preview_options" in sm.await_args_list[0].kwargs   # first try: rich
    assert "link_preview_options" not in sm.await_args_list[1].kwargs  # fallback: plain
    assert result == OK


def test_link_preview_options_supersede_disable_preview():
    """send_message with link_preview_options must not also set disable_web_page_preview."""
    captured = {}

    class _Resp:
        def json(self):
            return OK

    async def fake_post(url, json):  # noqa: A002
        captured.update(json)
        return _Resp()

    with patch.object(publisher.httpx, "AsyncClient") as client_cls:
        client = client_cls.return_value.__aenter__.return_value
        client.post = AsyncMock(side_effect=fake_post)
        _run(publisher.send_message("T", "chat", "hi",
                                    link_preview_options={"url": "https://x/i.png"}))
    assert "link_preview_options" in captured
    assert "disable_web_page_preview" not in captured
