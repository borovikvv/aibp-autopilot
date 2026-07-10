# tests/unit/test_competitor_dedup.py
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.self_learning.competitor_dedup import check_duplicate, classify_similarity

POLICY = {"competitor_dedup": {"dup_threshold": 0.85, "grey_threshold": 0.70}}


def test_classify_duplicate():
    assert classify_similarity(0.90, POLICY) == "duplicate"

def test_classify_unique():
    assert classify_similarity(0.50, POLICY) == "unique"

def test_classify_grey():
    assert classify_similarity(0.78, POLICY) == "grey"

def test_check_duplicate_degrades_to_unique_on_error():
    """When the embedding API / pgvector is unavailable, return 'unique' (no block)."""
    with patch("aibp.self_learning.competitor_dedup._query_similarity", side_effect=Exception("pgvector unavailable")):
        result = check_duplicate("Some title", "Some text", POLICY)
    assert result == "unique"
