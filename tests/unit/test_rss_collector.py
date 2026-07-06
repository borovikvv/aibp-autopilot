"""Tests for RSS collector."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.collectors.rss_collector import _extract_domain, _extract_text, _parse_date


def test_extract_domain():
    assert _extract_domain("https://www.example.com/path") == "example.com"
    assert _extract_domain("https://blog.example.com/article") == "blog.example.com"
    assert _extract_domain("http://example.com") == "example.com"


def test_extract_text_strips_html():
    entry = {"summary": "<p>Hello <b>world</b></p>"}
    text = _extract_text(entry)
    assert "Hello" in text
    assert "world" in text
    assert "<p>" not in text
    assert "<b>" not in text


def test_extract_text_handles_content_list():
    entry = {
        "content": [
            {"value": "<div>First</div>"},
            {"value": "<span>Second</span>"},
        ]
    }
    text = _extract_text(entry)
    assert "First" in text
    assert "Second" in text


def test_extract_text_empty_entry():
    assert _extract_text({}) == ""


def test_parse_date_from_struct():
    """Test parsing date from struct_time-like tuple."""
    import time
    entry = {"published_parsed": time.struct_time((2026, 6, 30, 12, 0, 0, 0, 0, 0))}
    dt = _parse_date(entry)
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 6
