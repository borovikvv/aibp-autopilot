"""Tests for weekly_case slot day mechanics (issue #40)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.generation import pipeline


def test_morning_skipped_on_case_day():
    """run(slot='morning') is skipped when today is the case weekday."""
    policy = {"weekly_case": {"enabled": True, "weekday": 2}}
    # Wednesday 2026-07-08 = weekday 2 → morning should skip
    assert pipeline._should_skip_for_weekly_case("morning", policy, date(2026, 7, 8)) is True


def test_weekly_case_skipped_on_non_case_day():
    policy = {"weekly_case": {"enabled": True, "weekday": 2}}
    # Monday 2026-07-06 = weekday 0 → weekly_case should skip
    assert pipeline._should_skip_for_weekly_case("weekly_case", policy, date(2026, 7, 6)) is True


def test_weekly_case_fires_on_case_day():
    policy = {"weekly_case": {"enabled": True, "weekday": 2}}
    # Wednesday → weekly_case fires
    assert pipeline._should_skip_for_weekly_case("weekly_case", policy, date(2026, 7, 8)) is False


def test_morning_fires_on_non_case_day():
    policy = {"weekly_case": {"enabled": True, "weekday": 2}}
    # Monday → morning fires
    assert pipeline._should_skip_for_weekly_case("morning", policy, date(2026, 7, 6)) is False


def test_exactly_one_fires_per_day():
    """On every weekday exactly one of (morning, weekly_case) fires."""
    policy = {"weekly_case": {"enabled": True, "weekday": 2}}
    # Wednesday: morning skips, weekly_case fires
    d = date(2026, 7, 8)
    assert pipeline._should_skip_for_weekly_case("morning", policy, d) is True
    assert pipeline._should_skip_for_weekly_case("weekly_case", policy, d) is False
    # Monday: morning fires, weekly_case skips
    d = date(2026, 7, 6)
    assert pipeline._should_skip_for_weekly_case("morning", policy, d) is False
    assert pipeline._should_skip_for_weekly_case("weekly_case", policy, d) is True


def test_disabled_means_never_skips():
    """When weekly_case is disabled, neither slot is skipped."""
    policy = {"weekly_case": {"enabled": False, "weekday": 2}}
    d = date(2026, 7, 8)  # Wednesday
    assert pipeline._should_skip_for_weekly_case("morning", policy, d) is False
    assert pipeline._should_skip_for_weekly_case("weekly_case", policy, d) is False


def test_no_weekly_case_config_means_never_skips():
    """No weekly_case block at all → nothing skipped."""
    policy = {}
    d = date(2026, 7, 8)
    assert pipeline._should_skip_for_weekly_case("morning", policy, d) is False
    assert pipeline._should_skip_for_weekly_case("weekly_case", policy, d) is False


def test_other_slots_unaffected_by_case_day():
    """evening / weekly_digest must never be skipped by weekly_case logic."""
    policy = {"weekly_case": {"enabled": True, "weekday": 2}}
    d = date(2026, 7, 8)  # Wednesday
    assert pipeline._should_skip_for_weekly_case("evening", policy, d) is False
    assert pipeline._should_skip_for_weekly_case("weekly_digest", policy, d) is False
