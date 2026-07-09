# tests/unit/test_tiers.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.self_learning.tiers import load_tier_config

POLICY = {
    "safety": {
        "experiment_tiers": {
            "low_risk": {
                "experiment_types": ["rubric_weight", "cta", "source_score", "visual"],
                "promote_probability": 0.90,
                "min_effect_pct": 5,
                "experiment_window_days": 14,
            },
            "high_risk": {
                "experiment_types": ["post_param", "regex_gate"],
                "promote_probability": 0.95,
                "min_effect_pct": 5,
                "experiment_window_days": 28,
            },
        }
    }
}

def test_low_risk_tier():
    cfg = load_tier_config("rubric_weight", POLICY)
    assert cfg["promote_probability"] == 0.90
    assert cfg["experiment_window_days"] == 14

def test_high_risk_tier():
    cfg = load_tier_config("post_param", POLICY)
    assert cfg["promote_probability"] == 0.95
    assert cfg["experiment_window_days"] == 28

def test_unknown_type_defaults_to_high_risk():
    cfg = load_tier_config("unknown_type", POLICY)
    assert cfg["promote_probability"] == 0.95
    assert cfg["experiment_window_days"] == 28

def test_flat_field_fallback_for_old_policy():
    old_policy = {"safety": {"promote_probability": 0.95, "min_effect_pct": 5, "experiment_window_days": 14}}
    cfg = load_tier_config("rubric_weight", old_policy)
    assert cfg["promote_probability"] == 0.95
    assert cfg["experiment_window_days"] == 14
