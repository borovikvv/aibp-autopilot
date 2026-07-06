"""Tests for select_candidate (generation) and apply_change_to_policy (self-learning).

Issue #9: these functions were untested.
  - select_candidate: filters candidates by slot, applies rubric weights for sorting
  - apply_change_to_policy: applies change_spec to policy dict for each experiment_type
"""
import copy
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.self_learning.policy_updater import apply_change_to_policy, validate_change_spec
from aibp.utils.summary import parse_summary

# ═══════════════════════════════════════════════════════════════════
# Test policy fixture
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def base_policy():
    return {
        "version": "v_test",
        "autopilot_paused": False,
        "rubric_weights": {
            "process_under_ai": 1.0,
            "pilot_without_chaos": 1.0,
            "implementation_metric": 1.0,
            "ai_regulation": 1.0,
            "tool_through_scenario": 1.0,
            "anti_hype": 1.0,
        },
        "post_params": {
            "morning": {"target_chars": [800, 1400], "paragraphs": [4, 5], "max_bold": 1, "max_emoji": 0, "scheduled_hour_msk": 10},
            "evening": {"target_chars": [400, 700], "paragraphs": [2, 3], "max_bold": 1, "max_emoji": 0, "scheduled_hour_msk": 18},
        },
        "regex_gates": [],
        "source_scores": {"default": 0.0, "openai.com": 0.3},
        "visual_policy": {"title_max_chars": 78, "blocks_range": [3, 5], "palette": "editorial_light"},
    }


# ═══════════════════════════════════════════════════════════════════
# apply_change_to_policy — rubric_weight
# ═══════════════════════════════════════════════════════════════════

class TestApplyChangeRubricWeight:
    def test_updates_rubric_weight(self, base_policy):
        hypothesis = {
            "experiment_type": "rubric_weight",
            "change_spec": {"rubric": "anti_hype", "new_weight": 1.5},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["rubric_weights"]["anti_hype"] == 1.5
        # Other rubrics unchanged
        assert new_policy["rubric_weights"]["process_under_ai"] == 1.0

    def test_does_not_mutate_original(self, base_policy):
        """apply_change_to_policy must not mutate the input policy."""
        original = copy.deepcopy(base_policy)
        hypothesis = {
            "experiment_type": "rubric_weight",
            "change_spec": {"rubric": "anti_hype", "new_weight": 2.0},
        }
        _ = apply_change_to_policy(base_policy, hypothesis)
        assert base_policy == original  # unchanged

    def test_bumps_version(self, base_policy):
        hypothesis = {
            "experiment_type": "rubric_weight",
            "change_spec": {"rubric": "anti_hype", "new_weight": 1.3},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["version"] != base_policy["version"]


# ═══════════════════════════════════════════════════════════════════
# apply_change_to_policy — post_param
# ═══════════════════════════════════════════════════════════════════

class TestApplyChangePostParam:
    def test_updates_post_param(self, base_policy):
        hypothesis = {
            "experiment_type": "post_param",
            "change_spec": {
                "slot": "morning",
                "param": "target_chars",
                "new_value": [900, 1300],
            },
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["post_params"]["morning"]["target_chars"] == [900, 1300]
        # Other params in same slot unchanged
        assert new_policy["post_params"]["morning"]["paragraphs"] == [4, 5]
        # Other slots unchanged
        assert new_policy["post_params"]["evening"]["target_chars"] == [400, 700]


# ═══════════════════════════════════════════════════════════════════
# apply_change_to_policy — source_score
# ═══════════════════════════════════════════════════════════════════

class TestApplyChangeSourceScore:
    def test_adds_new_domain(self, base_policy):
        hypothesis = {
            "experiment_type": "source_score",
            "change_spec": {"domain": "anthropic.com", "new_score": 0.4},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["source_scores"]["anthropic.com"] == 0.4

    def test_updates_existing_domain(self, base_policy):
        hypothesis = {
            "experiment_type": "source_score",
            "change_spec": {"domain": "openai.com", "new_score": -0.2},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["source_scores"]["openai.com"] == -0.2

    def test_preserves_other_domains(self, base_policy):
        hypothesis = {
            "experiment_type": "source_score",
            "change_spec": {"domain": "new.com", "new_score": 0.1},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["source_scores"]["default"] == 0.0
        assert new_policy["source_scores"]["openai.com"] == 0.3


# ═══════════════════════════════════════════════════════════════════
# apply_change_to_policy — regex_gate
# ═══════════════════════════════════════════════════════════════════

class TestApplyChangeRegexGate:
    def test_adds_new_regex_gate(self, base_policy):
        hypothesis = {
            "experiment_type": "regex_gate",
            "change_spec": {
                "name": "BANNED_PHRASE_RE",
                "pattern": r"суперскидка",
                "action": "fail",
            },
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert len(new_policy["regex_gates"]) == 1
        gate = new_policy["regex_gates"][0]
        assert gate["name"] == "BANNED_PHRASE_RE"
        assert gate["pattern"] == r"суперскидка"
        assert gate["action"] == "fail"

    def test_adds_multiple_gates(self, base_policy):
        # Add first gate
        h1 = {
            "experiment_type": "regex_gate",
            "change_spec": {"name": "GATE1", "pattern": r"foo", "action": "warn"},
        }
        policy_after_1 = apply_change_to_policy(base_policy, h1)
        # Add second gate on top
        h2 = {
            "experiment_type": "regex_gate",
            "change_spec": {"name": "GATE2", "pattern": r"bar", "action": "fail"},
        }
        policy_after_2 = apply_change_to_policy(policy_after_1, h2)
        assert len(policy_after_2["regex_gates"]) == 2


# ═══════════════════════════════════════════════════════════════════
# apply_change_to_policy — visual
# ═══════════════════════════════════════════════════════════════════

class TestApplyChangeVisual:
    def test_updates_visual_param(self, base_policy):
        hypothesis = {
            "experiment_type": "visual",
            "change_spec": {"param": "palette", "new_value": "editorial_dark"},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["visual_policy"]["palette"] == "editorial_dark"

    def test_updates_blocks_range(self, base_policy):
        hypothesis = {
            "experiment_type": "visual",
            "change_spec": {"param": "blocks_range", "new_value": [2, 4]},
        }
        new_policy = apply_change_to_policy(base_policy, hypothesis)
        assert new_policy["visual_policy"]["blocks_range"] == [2, 4]


# ═══════════════════════════════════════════════════════════════════
# apply_change_to_policy — error cases
# ═══════════════════════════════════════════════════════════════════

class TestApplyChangeErrorCases:
    def test_unknown_experiment_type_raises(self, base_policy):
        """Unknown experiment_type should raise ValueError (fail fast)."""
        hypothesis = {
            "experiment_type": "unknown_type",
            "change_spec": {},
        }
        with pytest.raises(ValueError, match="Unknown experiment_type"):
            apply_change_to_policy(base_policy, hypothesis)


# ═══════════════════════════════════════════════════════════════════
# validate_change_spec — comprehensive coverage
# ═══════════════════════════════════════════════════════════════════

class TestValidateChangeSpec:
    def test_rubric_weight_valid(self, base_policy):
        hyp = {"experiment_type": "rubric_weight", "change_spec": {"rubric": "anti_hype", "new_weight": 1.3}}
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is True

    def test_rubric_weight_unknown_rubric(self, base_policy):
        hyp = {"experiment_type": "rubric_weight", "change_spec": {"rubric": "nonexistent", "new_weight": 1.3}}
        valid, reason = validate_change_spec(hyp, base_policy)
        assert valid is False
        assert "unknown rubric" in reason

    def test_rubric_weight_out_of_range_high(self, base_policy):
        hyp = {"experiment_type": "rubric_weight", "change_spec": {"rubric": "anti_hype", "new_weight": 5.0}}
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is False

    def test_rubric_weight_negative(self, base_policy):
        hyp = {"experiment_type": "rubric_weight", "change_spec": {"rubric": "anti_hype", "new_weight": -0.5}}
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is False

    def test_post_param_valid(self, base_policy):
        hyp = {
            "experiment_type": "post_param",
            "change_spec": {"slot": "morning", "param": "target_chars", "new_value": [900, 1300]},
        }
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is True

    def test_post_param_unknown_slot(self, base_policy):
        hyp = {
            "experiment_type": "post_param",
            "change_spec": {"slot": "nonexistent", "param": "x", "new_value": 1},
        }
        valid, reason = validate_change_spec(hyp, base_policy)
        assert valid is False
        assert "unknown slot" in reason

    def test_source_score_valid(self, base_policy):
        hyp = {"experiment_type": "source_score", "change_spec": {"domain": "example.com", "new_score": -0.3}}
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is True

    def test_source_score_missing_domain(self, base_policy):
        hyp = {"experiment_type": "source_score", "change_spec": {"new_score": 0.5}}
        valid, reason = validate_change_spec(hyp, base_policy)
        assert valid is False
        assert "missing domain" in reason

    def test_source_score_out_of_range(self, base_policy):
        hyp = {"experiment_type": "source_score", "change_spec": {"domain": "x.com", "new_score": 2.0}}
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is False

    def test_regex_gate_valid(self, base_policy):
        hyp = {
            "experiment_type": "regex_gate",
            "change_spec": {"name": "TEST_RE", "pattern": r"test", "action": "fail"},
        }
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is True

    def test_regex_gate_invalid_pattern(self, base_policy):
        """Invalid regex should be rejected."""
        hyp = {
            "experiment_type": "regex_gate",
            "change_spec": {"name": "BAD_RE", "pattern": r"[unclosed", "action": "fail"},
        }
        valid, reason = validate_change_spec(hyp, base_policy)
        assert valid is False
        assert "invalid regex" in reason.lower()

    def test_regex_gate_invalid_action(self, base_policy):
        hyp = {
            "experiment_type": "regex_gate",
            "change_spec": {"name": "TEST_RE", "pattern": r"test", "action": "block"},
        }
        valid, _ = validate_change_spec(hyp, base_policy)
        assert valid is False

    def test_missing_experiment_type(self, base_policy):
        hyp = {"change_spec": {}}
        valid, reason = validate_change_spec(hyp, base_policy)
        assert valid is False

    def test_missing_change_spec(self, base_policy):
        hyp = {"experiment_type": "rubric_weight"}
        valid, reason = validate_change_spec(hyp, base_policy)
        assert valid is False


# ═══════════════════════════════════════════════════════════════════
# select_candidate — tested via mocking DB calls
# ═══════════════════════════════════════════════════════════════════

class TestSelectCandidate:
    """select_candidate reads from DB, so we mock fetch_all."""

    def _make_candidate(self, id_, rubric, score, fmt="morning", publish_worthy=True):
        """Build a candidate dict as it would come from feed_items."""
        import json
        return {
            "id": id_,
            "url": f"https://example.com/{id_}",
            "title": f"Post {id_}",
            "text": "excerpt",
            "source": "test",
            "source_domain": "example.com",
            "source_lang": "en",
            "source_published_at": "2026-06-30T10:00:00Z",
            "summary": json.dumps({
                "editorial": {
                    "publish_worthy": publish_worthy,
                    "recommended_format": fmt,
                    "strategy_rubric": rubric,
                    "source_fit_score": 4,
                }
            }),
            "rank_score": score,
            "relevance": 4.0,
        }

    def test_returns_none_when_no_candidates(self, base_policy):
        from aibp.generation import pipeline
        with patch.object(pipeline, "fetch_all", return_value=[]):
            result = pipeline.select_candidate("morning", base_policy, pipeline_env="prod")
        assert result is None

    def test_selects_highest_weighted_score(self, base_policy):
        """Candidate with higher rubric weight * rank_score should win."""
        from aibp.generation import pipeline
        candidates = [
            self._make_candidate(1, "anti_hype", 80),
            self._make_candidate(2, "process_under_ai", 100),
        ]
        with patch.object(pipeline, "fetch_all", return_value=candidates):
            result = pipeline.select_candidate("morning", base_policy, pipeline_env="prod")
        assert result["id"] == 2  # rank 100 > 80

    def test_rubric_weight_affects_selection(self, base_policy):
        """With anti_hype weight=2.0, anti_hype (rank 80) should beat process_under_ai (rank 100)."""
        from aibp.generation import pipeline
        candidates = [
            self._make_candidate(1, "anti_hype", 80),
            self._make_candidate(2, "process_under_ai", 100),
        ]
        policy = copy.deepcopy(base_policy)
        policy["rubric_weights"]["anti_hype"] = 2.0
        with patch.object(pipeline, "fetch_all", return_value=candidates):
            result = pipeline.select_candidate("morning", policy, pipeline_env="prod")
        assert result["id"] == 1  # 80*2=160 > 100*1=100

    def test_filters_by_slot(self, base_policy):
        """When slot=morning, candidates with recommended_format='morning' preferred."""
        from aibp.generation import pipeline
        candidates = [
            self._make_candidate(1, "anti_hype", 100, fmt="evening"),
            self._make_candidate(2, "anti_hype", 80, fmt="morning"),
        ]
        with patch.object(pipeline, "fetch_all", return_value=candidates):
            result = pipeline.select_candidate("morning", base_policy, pipeline_env="prod")
        assert result["id"] == 2  # matches slot despite lower rank

    def test_falls_back_to_any_when_no_slot_match(self, base_policy):
        """If no candidate matches the slot, fall back to any candidate."""
        from aibp.generation import pipeline
        candidates = [
            self._make_candidate(1, "anti_hype", 90, fmt="evening"),
            self._make_candidate(2, "anti_hype", 100, fmt="weekly_digest"),
        ]
        with patch.object(pipeline, "fetch_all", return_value=candidates):
            result = pipeline.select_candidate("morning", base_policy, pipeline_env="prod")
        assert result is not None
        assert result["id"] == 2  # highest rank wins when no slot match


# ═══════════════════════════════════════════════════════════════════
# parse_summary — additional edge cases for DRY coverage (issue #7)
# ═══════════════════════════════════════════════════════════════════

class TestParseSummary:
    def test_dict_input(self):
        summary = {"editorial": {"rubric": "anti_hype"}}
        assert parse_summary(summary) == summary

    def test_valid_json_string(self):
        import json
        d = {"editorial": {"rubric": "anti_hype"}}
        assert parse_summary(json.dumps(d)) == d

    def test_invalid_json_string(self):
        assert parse_summary("not json") == {}

    def test_none(self):
        assert parse_summary(None) == {}

    def test_empty_string(self):
        assert parse_summary("") == {}

    def test_json_array_returns_empty(self):
        """parse_summary should return {} for non-dict JSON (e.g., array)."""
        assert parse_summary("[1, 2, 3]") == {}

    def test_json_number_returns_empty(self):
        assert parse_summary("42") == {}

    def test_whitespace_string(self):
        assert parse_summary("   ") == {}

    def test_int_input(self):
        """Non-string, non-dict input returns {}."""
        assert parse_summary(42) == {}

    def test_list_input(self):
        assert parse_summary([1, 2]) == {}
