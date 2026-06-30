"""Tests for editorial quality gate."""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.generation.quality_gate import validate_post, FORBIDDEN_RE, CLICHE_RE, SOURCE_FRAMING_RE


def test_valid_morning_post_passes():
    post = (
        "<b>AI-помощник нужно измерять по принятому результату</b>\n\n"
        "Первые недели внутреннего помощника часто выглядят успешными: запросов много, сотрудники пробуют сценарии.\n\n"
        "Но активность ещё не показывает пользу. Один человек закрывает рутинную сверку, другой гоняет дорогую модель ради черновика.\n\n"
        "Стоимость обработанной заявки — единственная метрика, которая отделяет пилот от хобби.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is True
    assert result["hard_fail_keys"] == []


def test_forbidden_terms_fail():
    post = (
        "<b>Заголовок</b>\n\n"
        "Этот инструмент полезен для малого бизнеса и руководителей.\n\n"
        "Второй абзац с дополнительным контекстом.\n\n"
        "Третий абзац про практическое применение.\n\n"
        "Четвёртый абзац с выводом.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "forbidden_terms" in result["hard_fail_keys"]


def test_source_framing_fails():
    post = (
        "<b>Заголовок</b>\n\n"
        "В материале Towards AI эта логика разобрана через агента и sandbox.\n\n"
        "Деталей там много, но главный вывод простой.\n\n"
        "Третий абзац про практику.\n\n"
        "Четвёртый про метрики.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "source_framing" in result["hard_fail_keys"]


def test_ai_cliche_fails():
    post = (
        "<b>Заголовок</b>\n\n"
        "Важно отметить, что в современном мире AI играет ключевую роль.\n\n"
        "Второй абзац.\n\n"
        "Третий абзац.\n\n"
        "Четвёртый абзац.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "ai_template_phrases" in result["hard_fail_keys"]


def test_missing_source_link_fails():
    post = (
        "<b>Заголовок</b>\n\n"
        "Первый абзац.\n\n"
        "Второй абзац.\n\n"
        "Третий абзац.\n\n"
        "Четвёртый абзац без ссылки на источник."
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "source_link" in result["hard_fail_keys"]


def test_wrong_source_url_fails():
    post = (
        "<b>Заголовок</b>\n\n"
        "Первый абзац.\n\n"
        "Второй абзац.\n\n"
        "Третий абзац.\n\n"
        "Четвёртый абзац.\n\n"
        '<a href="https://wrong.com">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "source_link" in result["hard_fail_keys"]


def test_extra_gates_from_policy():
    """Test that dynamic regex gates from policy.yaml are applied."""
    extra_gates = [
        {
            "name": "custom_banned_word",
            "pattern": r"суперскидка",
            "action": "fail",
        }
    ]
    post = (
        "<b>Заголовок</b>\n\n"
        "Первый абзац про суперскидка.\n\n"
        "Второй абзац.\n\n"
        "Третий абзац.\n\n"
        "Четвёртый абзац.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning", extra_gates=extra_gates)
    assert result["ok"] is False
    assert "custom_banned_word" in result["hard_fail_keys"]


def test_opening_source_reference_fails():
    post = (
        "В статье Bloomberg разбирается новый закон.\n\n"
        "Второй абзац.\n\n"
        "Третий абзац.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "opening" in result["hard_fail_keys"]
