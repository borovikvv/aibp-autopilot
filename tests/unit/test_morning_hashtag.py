"""Tests for rubric hashtag + morning structure (issue #31)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.generation.pipeline import (
    RUBRIC_HASHTAGS,
    append_hashtag,
    rubric_hashtag,
)

SOURCE = '<a href="https://example.com/a">Источник</a>'


# ═══════════════════════════════════════════════════════════════════
# rubric → hashtag map
# ═══════════════════════════════════════════════════════════════════

def test_map_covers_all_six_rubrics():
    expected = {"process_under_ai", "pilot_without_chaos", "implementation_metric",
                "ai_regulation", "tool_through_scenario", "anti_hype"}
    assert set(RUBRIC_HASHTAGS) == expected
    assert all(tag.startswith("#") for tag in RUBRIC_HASHTAGS.values())


def test_rubric_hashtag_known_and_unknown():
    assert rubric_hashtag("implementation_metric") == "#метрика"
    assert rubric_hashtag("unknown_rubric") is None
    assert rubric_hashtag(None) is None
    assert rubric_hashtag("") is None


# ═══════════════════════════════════════════════════════════════════
# append_hashtag placement
# ═══════════════════════════════════════════════════════════════════

def test_hashtag_inserted_before_source_link():
    post = f"<b>Заголовок</b>\n\nТекст поста.\n\n{SOURCE}"
    result = append_hashtag(post, "implementation_metric")
    assert "#метрика" in result
    # hashtag sits before the source link, post still ends with the link
    assert result.rstrip().endswith(SOURCE)
    assert result.index("#метрика") < result.index("Источник")
    assert result.count("Источник") == 1


def test_hashtag_without_source_appends_to_end():
    post = "<b>Заголовок</b>\n\nТекст."
    result = append_hashtag(post, "anti_hype")
    assert result.rstrip().endswith("#безхайпа")


def test_hashtag_noop_for_unknown_rubric():
    post = f"<b>Заголовок</b>\n\nТекст.\n\n{SOURCE}"
    assert append_hashtag(post, "unknown") == post
    assert append_hashtag(post, None) == post


def test_hashtag_does_not_add_second_link():
    """The hashtag has no href, so the source_link gate (1 link) still holds."""
    post = f"<b>Заголовок</b>\n\nТекст.\n\n{SOURCE}"
    result = append_hashtag(post, "process_under_ai")
    assert result.count('<a href=') == 1


# ═══════════════════════════════════════════════════════════════════
# variant C (bold lead-ins) passes the quality gate at max_bold=4
# ═══════════════════════════════════════════════════════════════════

def test_variant_c_morning_post_passes_gate():
    from aibp.generation.quality_gate import validate_post

    post = (
        "<b>Как переложили проверку заявок на модель</b>\n\n"
        "<b>Процесс.</b> Заявки теперь классифицирует модель, а не оператор вручную.\n"
        "<b>Метрика.</b> Время проверки упало с 40 до 12 минут на заявку.\n"
        "<b>Граница.</b> Спорные кейсы всё равно уходят человеку.\n\n"
        "Что это меняет для очереди обработки в ближайший месяц.\n\n"
        "Отдельный абзац про следующий шаг и ограничение подхода.\n\n"
        f"#метрика\n{SOURCE}"
    )
    result = validate_post(post, expected_url="https://example.com/a", slot="morning")
    # 4 bold uses, a metric present, one source link → no hard fail
    assert result["hard_fail_keys"] == []
    assert result["verdicts"]["metric_presence"]["status"] == "pass"
    assert result["verdicts"]["source_link"]["status"] == "pass"
