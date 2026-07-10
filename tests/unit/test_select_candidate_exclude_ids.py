"""Tests for select_candidate's exclude_ids param (issue #40, Task 3.9).

The competitor dedup loop calls select_candidate with exclude_ids — the IDs of
candidates already found to duplicate a competitor post, so the next pick skips
them. These tests pin that contract: an excluded candidate must not be returned.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.generation.pipeline import select_candidate


def test_excluded_candidate_not_returned():
    """The top candidate is excluded → the next one is returned."""
    with patch("aibp.generation.pipeline.fetch_all") as mock_fetch:
        mock_fetch.return_value = [
            {"id": 1, "url": "http://a", "title": "A", "text": "", "source": "s",
             "source_domain": "d", "source_lang": "en", "source_published_at": None,
             "summary": None, "rank_score": 80, "relevance": 1},
            {"id": 2, "url": "http://b", "title": "B", "text": "", "source": "s",
             "source_domain": "d", "source_lang": "en", "source_published_at": None,
             "summary": None, "rank_score": 70, "relevance": 1},
        ]
        policy = {"rubric_weights": {}}
        result = select_candidate("morning", policy, exclude_ids={1})
    assert result["id"] == 2


def test_returns_none_when_all_excluded():
    """Every candidate excluded → None (dedup loop then gives up)."""
    with patch("aibp.generation.pipeline.fetch_all") as mock_fetch:
        mock_fetch.return_value = [
            {"id": 1, "url": "http://a", "title": "A", "text": "", "source": "s",
             "source_domain": "d", "source_lang": "en", "source_published_at": None,
             "summary": None, "rank_score": 80, "relevance": 1},
            {"id": 2, "url": "http://b", "title": "B", "text": "", "source": "s",
             "source_domain": "d", "source_lang": "en", "source_published_at": None,
             "summary": None, "rank_score": 70, "relevance": 1},
        ]
        policy = {"rubric_weights": {}}
        result = select_candidate("morning", policy, exclude_ids={1, 2})
    assert result is None


def test_no_exclude_returns_top_candidate():
    """Sanity: without exclude_ids the top candidate is returned."""
    with patch("aibp.generation.pipeline.fetch_all") as mock_fetch:
        mock_fetch.return_value = [
            {"id": 1, "url": "http://a", "title": "A", "text": "", "source": "s",
             "source_domain": "d", "source_lang": "en", "source_published_at": None,
             "summary": None, "rank_score": 80, "relevance": 1},
            {"id": 2, "url": "http://b", "title": "B", "text": "", "source": "s",
             "source_domain": "d", "source_lang": "en", "source_published_at": None,
             "summary": None, "rank_score": 70, "relevance": 1},
        ]
        policy = {"rubric_weights": {}}
        result = select_candidate("morning", policy)
    assert result["id"] == 1
