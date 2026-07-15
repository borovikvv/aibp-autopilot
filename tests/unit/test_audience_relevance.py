"""Tests for RU/CIS audience relevance in enrichment ranking."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.enrichment.pipeline import ENRICHMENT_PROMPT, enrich_item

_ITEM = {"id": 1, "title": "T", "source": "S", "source_domain": "example.com",
         "source_published_at": "2026-07-01", "source_lang": "en", "text": "body"}

_MULTIPLIERS = {1: 0.5, 2: 0.7, 3: 1.0, 4: 1.15, 5: 1.3}


def _policy(multipliers=_MULTIPLIERS):
    policy = {"source_scores": {"default": 0.0}}
    if multipliers is not None:
        policy["audience"] = {"relevance_multipliers": multipliers}
    return policy


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, messages, temperature=0.2, max_tokens=800):
        return dict(self.payload)


def test_low_relevance_scales_rank_down():
    """EU-only regulation: rank 80 × 0.5 → 40."""
    client = _FakeClient({"rank_score": 80, "audience_relevance": 1, "source_fit_score": 4})
    result = enrich_item(dict(_ITEM), client, _policy())
    assert result["rank_score"] == 40
    assert result["audience_relevance"] == 1


def test_high_relevance_scales_rank_up_and_clamps_at_100():
    client = _FakeClient({"rank_score": 90, "audience_relevance": 5, "source_fit_score": 4})
    result = enrich_item(dict(_ITEM), client, _policy())
    assert result["rank_score"] == 100  # 90 × 1.3 = 117 → clamp


def test_neutral_relevance_keeps_rank():
    client = _FakeClient({"rank_score": 72, "audience_relevance": 3, "source_fit_score": 4})
    result = enrich_item(dict(_ITEM), client, _policy())
    assert result["rank_score"] == 72


def test_missing_or_garbage_relevance_defaults_to_neutral():
    for bad in (None, "unknown", {}):
        client = _FakeClient({"rank_score": 60, "audience_relevance": bad, "source_fit_score": 4})
        result = enrich_item(dict(_ITEM), client, _policy())
        assert result["rank_score"] == 60, bad
        assert result["audience_relevance"] == 3, bad


def test_out_of_range_relevance_is_clamped():
    client = _FakeClient({"rank_score": 50, "audience_relevance": 9, "source_fit_score": 4})
    result = enrich_item(dict(_ITEM), client, _policy())
    assert result["audience_relevance"] == 5
    assert result["rank_score"] == 65  # 50 × 1.3


def test_no_audience_section_disables_adjustment():
    client = _FakeClient({"rank_score": 80, "audience_relevance": 1, "source_fit_score": 4})
    result = enrich_item(dict(_ITEM), client, _policy(multipliers=None))
    assert result["rank_score"] == 80  # untouched — backward compatible


def test_enrichment_prompt_asks_for_audience_relevance():
    assert "audience_relevance" in ENRICHMENT_PROMPT
    assert "Russia and CIS" in ENRICHMENT_PROMPT
    # foreign regulation must default low unless a local consequence is named
    assert "foreign regulation" in ENRICHMENT_PROMPT
