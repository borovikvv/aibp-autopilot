"""Tests for engagement_collector — parsing, 409 handling, method selection."""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.self_learning.engagement_collector import (
    _parse_chat_id,
    collect_engagement_for_post,
    extract_features_for_post,
    get_chat_members_count,
    get_views_via_copy,
    get_views_via_updates,
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
# get_views_via_updates — 409 conflict handling
# ═══════════════════════════════════════════════════════════════════

class TestGetViewsViaUpdates:
    @pytest.mark.asyncio
    async def test_409_conflict_triggers_alert(self):
        """When getUpdates returns 409, should send alert if alert_chat_id set."""
        mock_response = {
            "ok": False,
            "error_code": 409,
            "description": "Conflict: terminated by other getUpdates request"
        }
        alert_sent = []

        async def mock_send_alert(token, chat_id, msg):
            alert_sent.append((chat_id, msg))

        with patch("httpx.AsyncClient") as mock_client, \
             patch("aibp.self_learning.engagement_collector._send_alert", side_effect=mock_send_alert):
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=mock_response)))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_views_via_updates(
                "fake_token", -1003300906776, 123,
                alert_chat_id="999999",
            )
            assert result is None
            assert len(alert_sent) == 1
            assert "409" in alert_sent[0][1] or "Conflict" in alert_sent[0][1]

    @pytest.mark.asyncio
    async def test_409_without_alert_chat_silent(self):
        """409 without alert_chat_id should just log, not crash."""
        mock_response = {
            "ok": False,
            "error_code": 409,
            "description": "Conflict"
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=mock_response)))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_views_via_updates("fake_token", -1003300906776, 123)
            assert result is None  # no crash, no alert

    @pytest.mark.asyncio
    async def test_finds_matching_post(self):
        """Should extract views when matching post found in updates."""
        mock_response = {
            "ok": True,
            "result": [
                {
                    "channel_post": {
                        "chat": {"id": -1003300906776},
                        "message_id": 123,
                        "views": 456,
                        "forwards": 5,
                        "reactions": [{"type": {"emoji": "👍"}, "total_count": 10}],
                    }
                }
            ],
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=mock_response)))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_views_via_updates("fake_token", -1003300906776, 123)
            assert result is not None
            assert result["views"] == 456
            assert result["forwards"] == 5
            assert result["reactions_count"] == 10

    @pytest.mark.asyncio
    async def test_no_matching_post(self):
        """Should return None if post not in updates."""
        mock_response = {
            "ok": True,
            "result": [
                {
                    "channel_post": {
                        "chat": {"id": -1003300906776},
                        "message_id": 999,  # different message
                        "views": 100,
                    }
                }
            ],
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value=mock_response)))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_views_via_updates("fake_token", -1003300906776, 123)
            assert result is None


# ═══════════════════════════════════════════════════════════════════
# get_views_via_copy
# ═══════════════════════════════════════════════════════════════════

class TestGetViewsViaCopy:
    @pytest.mark.asyncio
    async def test_successful_copy_and_extract(self):
        """copyMessage returns message with views, then deletes the copy."""
        copy_response = {
            "ok": True,
            "result": {
                "message_id": 789,  # ID of the copy in metrics chat
                "views": 500,
                "forwards": 3,
                "reactions": [{"type": {"emoji": "🔥"}, "total_count": 7}],
            },
        }
        delete_response = {"ok": True, "result": True}

        call_count = 0
        async def mock_post(url, json=None):
            nonlocal call_count
            call_count += 1
            if "copyMessage" in url:
                return MagicMock(json=MagicMock(return_value=copy_response))
            elif "deleteMessage" in url:
                return MagicMock(json=MagicMock(return_value=delete_response))

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(side_effect=mock_post)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_views_via_copy("fake_token", -1003300906776, 123, 999999)
            assert result is not None
            assert result["views"] == 500
            assert result["forwards"] == 3
            assert result["reactions_count"] == 7
            assert call_count == 2  # copy + delete

    @pytest.mark.asyncio
    async def test_copy_fails_message_not_found(self):
        """If message was deleted from channel, copyMessage returns 400."""
        copy_response = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message to copy not found"
        }
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = MagicMock()
            mock_instance.post = AsyncMock(
                return_value=MagicMock(json=MagicMock(return_value=copy_response))
            )
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await get_views_via_copy("fake_token", -1003300906776, 123, 999999)
            assert result is None


# ═══════════════════════════════════════════════════════════════════
# collect_engagement_for_post — method selection
# ═══════════════════════════════════════════════════════════════════

class TestCollectEngagementMethodSelection:
    @pytest.mark.asyncio
    async def test_uses_copyMessage_when_configured(self):
        """When metrics_chat_id is set, should try copyMessage first."""
        with patch("aibp.self_learning.engagement_collector.get_views_via_copy", new_callable=AsyncMock) as mock_copy, \
             patch("aibp.self_learning.engagement_collector.get_views_via_updates", new_callable=AsyncMock) as mock_updates:

            mock_copy.return_value = {"views": 100, "forwards": 0, "replies": 0,
                                       "reactions_count": 0, "reactions_breakdown": "[]"}

            result = await collect_engagement_for_post(
                "fake_token",
                {"target_channel_id": "-1003300906776", "published_message_id": "123"},
                subscribers_at=1000,
                metrics_chat_id="999999",
            )
            assert result is not None
            assert result["views"] == 100
            assert result["method"] == "copyMessage"
            mock_updates.assert_not_called()  # fallback not used

    @pytest.mark.asyncio
    async def test_falls_back_to_updates_when_copy_fails(self):
        """If copyMessage returns None, should try getUpdates."""
        with patch("aibp.self_learning.engagement_collector.get_views_via_copy", new_callable=AsyncMock) as mock_copy, \
             patch("aibp.self_learning.engagement_collector.get_views_via_updates", new_callable=AsyncMock) as mock_updates:

            mock_copy.return_value = None  # copy failed
            mock_updates.return_value = {"views": 50, "forwards": 0, "replies": 0,
                                          "reactions_count": 0, "reactions_breakdown": "[]"}

            result = await collect_engagement_for_post(
                "fake_token",
                {"target_channel_id": "-1003300906776", "published_message_id": "123"},
                subscribers_at=1000,
                metrics_chat_id="999999",
            )
            assert result is not None
            assert result["views"] == 50
            assert result["method"] == "getUpdates"

    @pytest.mark.asyncio
    async def test_uses_updates_when_no_metrics_chat(self):
        """Without metrics_chat_id, should use getUpdates directly."""
        with patch("aibp.self_learning.engagement_collector.get_views_via_copy", new_callable=AsyncMock) as mock_copy, \
             patch("aibp.self_learning.engagement_collector.get_views_via_updates", new_callable=AsyncMock) as mock_updates:

            mock_updates.return_value = {"views": 50, "forwards": 0, "replies": 0,
                                          "reactions_count": 0, "reactions_breakdown": "[]"}

            result = await collect_engagement_for_post(
                "fake_token",
                {"target_channel_id": "-1003300906776", "published_message_id": "123"},
                subscribers_at=1000,
                metrics_chat_id="",  # not configured
            )
            assert result is not None
            mock_copy.assert_not_called()
            mock_updates.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_chat_id_returns_none(self):
        """Invalid chat_id should return None without API calls."""
        with patch("aibp.self_learning.engagement_collector.get_views_via_copy", new_callable=AsyncMock) as mock_copy, \
             patch("aibp.self_learning.engagement_collector.get_views_via_updates", new_callable=AsyncMock) as mock_updates:

            result = await collect_engagement_for_post(
                "fake_token",
                {"target_channel_id": "not-a-number", "published_message_id": "123"},
                subscribers_at=1000,
            )
            assert result is None
            mock_copy.assert_not_called()
            mock_updates.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_message_id_returns_none(self):
        """Missing published_message_id should return None."""
        result = await collect_engagement_for_post(
            "fake_token",
            {"target_channel_id": "-1003300906776", "published_message_id": None},
            subscribers_at=1000,
        )
        assert result is None


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
