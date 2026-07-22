"""Tests for engagement_collector — web-preview parsing, counts, features."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.self_learning.engagement_collector import (
    _parse_chat_id,
    _parse_count,
    build_metrics,
    extract_features_for_post,
    fetch_views_map,
    get_chat_members_count,
    parse_views_from_html,
)

# ═══════════════════════════════════════════════════════════════════
# _parse_chat_id
# ═══════════════════════════════════════════════════════════════════

class TestParseChatId:
    def test_valid_negative_int(self):
        """Channel IDs are negative (e.g., -1003300906776)."""
        assert _parse_chat_id("-1003300906776") == -1003300906776

    def test_valid_negative_string(self):
        assert _parse_chat_id("-1003825827505") == -1003825827505

    def test_valid_positive_int(self):
        """Personal chat IDs are positive."""
        assert _parse_chat_id("123456789") == 123456789

    def test_already_int(self):
        assert _parse_chat_id(-1003300906776) == -1003300906776
        assert _parse_chat_id(12345) == 12345

    def test_none(self):
        assert _parse_chat_id(None) is None

    def test_empty_string(self):
        assert _parse_chat_id("") is None

    def test_whitespace_only(self):
        assert _parse_chat_id("   ") is None

    def test_non_numeric(self):
        assert _parse_chat_id("abc") is None
        assert _parse_chat_id("@AI_Business_Pulse") is None

    def test_float_string(self):
        """Float-like strings should be rejected (chat_id is integer)."""
        assert _parse_chat_id("123.45") is None

    def test_very_large_number(self):
        """Telegram channel IDs can be very large (>-10^13)."""
        result = _parse_chat_id("-100330090677699")
        assert result == -100330090677699


# ═══════════════════════════════════════════════════════════════════
# get_chat_members_count
# ═══════════════════════════════════════════════════════════════════

class TestGetChatMembersCount:
    @pytest.mark.asyncio
    async def test_valid_response(self):
        mock_response = {"ok": True, "result": 1234}
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=mock_response)))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_chat_members_count("fake_token", "-1003300906776")
            assert result == 1234

    @pytest.mark.asyncio
    async def test_invalid_chat_id(self):
        result = await get_chat_members_count("fake_token", "not-a-number")
        assert result is None

    @pytest.mark.asyncio
    async def test_none_chat_id(self):
        result = await get_chat_members_count("fake_token", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_api_error(self):
        mock_response = {"ok": False, "description": "chat not found"}
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=mock_response)))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_chat_members_count("fake_token", "-1003300906776")
            assert result is None


# ═══════════════════════════════════════════════════════════════════
# _parse_count — Telegram abbreviated view counters
# ═══════════════════════════════════════════════════════════════════

class TestParseCount:
    def test_plain(self):
        assert _parse_count("8") == 8
        assert _parse_count("123") == 123

    def test_thousands(self):
        assert _parse_count("1.2K") == 1200
        assert _parse_count("1K") == 1000
        assert _parse_count("12.3K") == 12300

    def test_millions(self):
        assert _parse_count("3M") == 3_000_000
        assert _parse_count("1.5M") == 1_500_000

    def test_comma_and_spaces(self):
        assert _parse_count("1,234") == 1234
        assert _parse_count(" 42 ") == 42

    def test_empty_or_garbage(self):
        assert _parse_count("") == 0
        assert _parse_count("abc") == 0


# ═══════════════════════════════════════════════════════════════════
# parse_views_from_html — t.me/s preview page
# ═══════════════════════════════════════════════════════════════════

# Minimal shape mirroring the real t.me/s markup: a data-post anchor per
# message, each followed by a views span before the next anchor.
_PREVIEW_HTML = """
<div class="tgme_widget_message" data-post="AI_Business_Pulse/663">
  <div class="tgme_widget_message_views">7</div>
</div>
<div class="tgme_widget_message" data-post="AI_Business_Pulse/664">
  <div class="tgme_widget_message_views">1.2K</div>
</div>
<div class="tgme_widget_message" data-post="AI_Business_Pulse/665">
  <div class="tgme_widget_message_views">9</div>
</div>
"""


class TestParseViewsFromHtml:
    def test_maps_message_id_to_views(self):
        result = parse_views_from_html(_PREVIEW_HTML)
        assert result == {663: 7, 664: 1200, 665: 9}

    def test_post_without_views_span_is_zero(self):
        html = '<div data-post="chan/700"></div><div data-post="chan/701"><span class="tgme_widget_message_views">5</span></div>'
        result = parse_views_from_html(html)
        assert result[700] == 0  # seen but no views yet, not absent
        assert result[701] == 5

    def test_empty_page(self):
        assert parse_views_from_html("<html></html>") == {}

    def test_does_not_leak_views_across_messages(self):
        # Post 800 has no span; the span belongs to 801 and must not bleed up.
        html = ('<a data-post="chan/800"></a>'
                '<a data-post="chan/801"></a><i class="tgme_widget_message_views">99</i>')
        result = parse_views_from_html(html)
        assert result == {800: 0, 801: 99}


# ═══════════════════════════════════════════════════════════════════
# fetch_views_map — pagination
# ═══════════════════════════════════════════════════════════════════

class TestFetchViewsMap:
    @pytest.mark.asyncio
    async def test_paginates_until_min_id(self):
        # Page 1 (newest): 665,664; page 2: 663,662. min_message_id=663 stops after page 2.
        pages = [
            '<a data-post="c/665"></a><b class="tgme_widget_message_views">9</b>'
            '<a data-post="c/664"></a><b class="tgme_widget_message_views">8</b>',
            '<a data-post="c/663"></a><b class="tgme_widget_message_views">7</b>'
            '<a data-post="c/662"></a><b class="tgme_widget_message_views">6</b>',
        ]
        calls = []

        async def mock_get(url, params=None):
            calls.append(params)
            idx = len(calls) - 1
            return MagicMock(text=pages[idx], raise_for_status=MagicMock())

        with patch("httpx.AsyncClient") as mock_client:
            inst = MagicMock()
            inst.get = AsyncMock(side_effect=mock_get)
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = inst

            result = await fetch_views_map("c", min_message_id=663)
            assert result == {665: 9, 664: 8, 663: 7, 662: 6}
            assert calls[0] == {}                 # first page: no before
            assert calls[1] == {"before": 664}    # paged from oldest of page 1

    @pytest.mark.asyncio
    async def test_stops_when_pagination_repeats(self):
        page = '<a data-post="c/665"></a><b class="tgme_widget_message_views">9</b>'

        async def mock_get(url, params=None):
            return MagicMock(text=page, raise_for_status=MagicMock())

        with patch("httpx.AsyncClient") as mock_client:
            inst = MagicMock()
            inst.get = AsyncMock(side_effect=mock_get)
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = inst

            result = await fetch_views_map("c", min_message_id=0, max_pages=10)
            assert result == {665: 9}  # no new ids on page 2 → stop, no infinite loop

    @pytest.mark.asyncio
    async def test_http_error_returns_partial(self):
        with patch("httpx.AsyncClient") as mock_client:
            inst = MagicMock()
            inst.get = AsyncMock(side_effect=__import__("httpx").HTTPError("boom"))
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = inst

            result = await fetch_views_map("c")
            assert result == {}  # degrades to empty, no crash


# ═══════════════════════════════════════════════════════════════════
# build_metrics
# ═══════════════════════════════════════════════════════════════════

class TestBuildMetrics:
    def test_views_and_subs_set_rest_zero(self):
        m = build_metrics(42, 317)
        assert m["views"] == 42
        assert m["subscribers_at"] == 317
        assert m["forwards"] == 0
        assert m["reactions_count"] == 0
        assert m["reactions_breakdown"] == "[]"


# ═══════════════════════════════════════════════════════════════════
# extract_features_for_post
# ═══════════════════════════════════════════════════════════════════

class TestExtractFeatures:
    def test_extracts_basic_features(self):
        item = {
            "id": 42,
            "post_draft": "<b>Test</b>\n\nParagraph 1\n\nParagraph 2",
            "summary": {"strategy_rubric": "anti_hype", "policy_version": "v1"},
            "source_domain": "example.com",
            "url": "https://example.com/post",
            "pipeline_env": "prod",
            "target_channel": "main",
            "used_as": "morning_post",
            "image_url": None,
            "posted_at": None,
        }
        features = extract_features_for_post(item)
        assert features["feed_item_id"] == 42
        assert features["char_count"] > 0
        # 3 paragraphs: <b>Test</b>, Paragraph 1, Paragraph 2 (all separated by \n\n)
        assert features["paragraph_count"] == 3
        assert features["bold_count"] == 1
        assert features["slot"] == "morning"
        assert features["pipeline_env"] == "prod"
        assert features["strategy_rubric"] == "anti_hype"

    def test_handles_missing_summary(self):
        item = {
            "id": 1,
            "post_draft": "text",
            "summary": None,
            "used_as": "evening_post",
            "posted_at": None,
        }
        features = extract_features_for_post(item)
        assert features["strategy_rubric"] is None
        assert features["slot"] == "evening"

    def test_handles_string_summary(self):
        """Summary may come as JSON string from some DB drivers."""
        import json
        item = {
            "id": 1,
            "post_draft": "text",
            "summary": json.dumps({"strategy_rubric": "process_under_ai"}),
            "used_as": "morning_post",
            "posted_at": None,
        }
        features = extract_features_for_post(item)
        assert features["strategy_rubric"] == "process_under_ai"

    def test_slot_from_weekly_digest(self):
        item = {
            "id": 1,
            "post_draft": "text",
            "summary": {},
            "used_as": "weekly_digest_prod",
            "posted_at": None,
        }
        features = extract_features_for_post(item)
        assert features["slot"] == "weekly_digest"

    def test_slot_unknown_when_not_matching(self):
        item = {
            "id": 1,
            "post_draft": "text",
            "summary": {},
            "used_as": "something_else",
            "posted_at": None,
        }
        features = extract_features_for_post(item)
        assert features["slot"] == "unknown"
