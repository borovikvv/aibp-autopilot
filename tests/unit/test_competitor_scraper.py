# tests/unit/test_competitor_scraper.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.growth.competitor_scraper import fetch_channel_posts, parse_channel_html


def test_parse_extracts_posts():
    html = (Path(__file__).resolve().parents[1] / "fixtures" / "tme_s_sample.html").read_text(encoding="utf-8")
    posts = parse_channel_html(html)
    assert len(posts) >= 1
    assert "text" in posts[0]
    assert "message_id" in posts[0]


def test_parse_empty_page():
    assert parse_channel_html("<html></html>") == []


def test_fetch_returns_empty_on_failure(monkeypatch):
    import httpx

    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx, "get", fake_get)
    posts = fetch_channel_posts("nonexistent_channel_xyz")
    assert posts == []
