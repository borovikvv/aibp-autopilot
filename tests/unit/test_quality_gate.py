"""Tests for editorial quality gate."""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.generation.quality_gate import (
    CLICHE_RE,
    FORBIDDEN_RE,
    METRIC_PRESENCE_RE,
    MIXED_SCRIPT_RE,
    SOURCE_FRAMING_RE,
    validate_cta_text,
    validate_post,
)


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


def test_forbidden_re_allows_neutral_lexicon():
    """Issue #29: neutral words sharing a root with jargon must NOT fail."""
    for phrase in (
        "руководитель проекта собрал команду",
        "собственник процесса определяет stop-условие",
        "инструмент для бизнес-процесса",
        "шаблон для бизнес-задачи",
    ):
        assert FORBIDDEN_RE.search(phrase) is None, phrase


def test_forbidden_re_still_catches_audience_labels():
    """Issue #29: genuine audience jargon must still fail."""
    for phrase in (
        "статья для владельцев бизнеса",
        "решение для малого и среднего бизнеса",
        "гайд для SMB",
        "полезно для бизнеса в целом",
        "продукт для собственников бизнеса",
    ):
        assert FORBIDDEN_RE.search(phrase) is not None, phrase


def _morning_post(body_middle: str) -> str:
    return (
        "<b>Заголовок про процесс</b>\n\n"
        f"{body_middle}\n\n"
        "Второй абзац про выбор инструмента под задачу.\n\n"
        "Третий абзац про ограничение подхода.\n\n"
        "Четвёртый абзац про следующий шаг.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )


def test_metric_presence_warns_without_number():
    """Issue #32: a morning post with no measurable fact → warn (not fail)."""
    post = _morning_post("Команда переосмыслила подход и стала работать иначе.")
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["verdicts"]["metric_presence"]["status"] == "warn"
    assert "metric_presence" not in result["hard_fail_keys"]  # never blocks


def test_metric_presence_passes_with_number():
    post = _morning_post("Проверку сократили с 40 до 12 минут на заявку.")
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["verdicts"]["metric_presence"]["status"] == "pass"


def test_metric_presence_regex_markers():
    for phrase in ("выросло на 30%", "сэкономили 500 ₽", "заняло 3 дня",
                   "стало вдвое быстрее", "ускорилось в несколько раз"):
        assert METRIC_PRESENCE_RE.search(phrase) is not None, phrase
    assert METRIC_PRESENCE_RE.search("просто стало лучше и удобнее") is None


def test_metric_presence_only_for_morning():
    """Evening/weekly slots must not get a metric_presence verdict."""
    evening = (
        "<b>Граница</b>\n\n"
        "Короткая заметка про один предел без цифр.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(evening, expected_url="https://example.com/article", slot="evening")
    assert "metric_presence" not in result["verdicts"]


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


def test_section_labels_fail_in_morning():
    """Bold lead-in labels are banned in daily slots — structure must be prose."""
    post = (
        "<b>Заголовок про внедрение</b>\n\n"
        "<b>Процесс.</b> Компания перевела сверку счетов на автоматическую категоризацию.\n\n"
        "<b>Метрика.</b> Время закрытия месяца сократилось вдвое.\n\n"
        "<b>Граница.</b> На нестандартных транзакциях модель ошибается.\n\n"
        "Четвёртый абзац с выводом.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["ok"] is False
    assert "section_labels" in result["hard_fail_keys"]
    assert len(result["verdicts"]["section_labels"]["hits"]) == 3


def test_section_labels_fail_in_evening():
    post = (
        "Короткая мысль о границе применимости.\n\n"
        "<b>Вывод:</b> не каждый процесс стоит автоматизировать.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="evening")
    assert result["ok"] is False
    assert "section_labels" in result["hard_fail_keys"]


def test_section_labels_allowed_in_weekly_case():
    """weekly_case keeps labels deliberately — the branded weekly format."""
    post = (
        "<b>Кейс: сверка счетов за часы вместо дней</b>\n\n"
        "<b>Процесс.</b> Раньше бухгалтер категоризировал транзакции вручную три дня.\n\n"
        "<b>Инструмент.</b> Автокатегоризация через LLM с ручной досверкой расхождений.\n\n"
        "<b>Метрика.</b> Закрытие месяца — 4 часа вместо 3 дней на клиента.\n\n"
        "<b>Граница.</b> Нестандартные транзакции по-прежнему требуют человека.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="weekly_case")
    assert "section_labels" not in result["verdicts"]
    assert result["ok"] is True


def test_multiword_bold_headline_is_not_a_label():
    """A normal multi-word headline must not trip the section-label gate."""
    post = (
        "<b>AI-помощник нужно измерять по принятому результату</b>\n\n"
        "Первый абзац с цифрой: 30 заявок в день.\n\n"
        "Второй абзац развивает мысль.\n\n"
        "Третий абзац про границу применимости.\n\n"
        "Четвёртый абзац.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["verdicts"]["section_labels"]["status"] == "pass"
    assert result["ok"] is True


def test_mixed_script_re_catches_transliteration_bleed():
    """A word gluing Latin and Cyrillic together must be flagged."""
    for word in (
        "juridически",   # Latin "jur" + Cyrillic "идически"
        "юридicheski",   # Cyrillic head + Latin tail
        "сbербанк",      # Latin "c" homoglyph inside Cyrillic
        "GPТ",           # Cyrillic "Т" homoglyph inside Latin
    ):
        assert MIXED_SCRIPT_RE.search(word) is not None, word


def test_mixed_script_re_allows_clean_tokens():
    """Hyphenated compounds and pure-script tokens must pass — a hyphen,
    space, or digit breaks the letter-run so the scripts stay separate."""
    for phrase in (
        "AI-помощник разбирает заявки",
        "ML-модель и IT-отдел",
        "чистый русский текст без латиницы",
        "pure english RAG pipeline",
        "GPT-4 и Claude 5 обрабатывают 30 заявок",
        "рост в 2х по выручке",  # digit breaks the run
    ):
        assert MIXED_SCRIPT_RE.search(phrase) is None, phrase


def test_mixed_script_hard_fails_in_post():
    post = (
        "<b>Заголовок про заявки</b>\n\n"
        "Модель обрабатывает заявки и juridически проверяет договоры.\n\n"
        "Второй абзац развивает мысль про процесс.\n\n"
        "Третий абзац про границу применимости за 30 минут.\n\n"
        "Четвёртый абзац с выводом.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="evening")
    assert result["ok"] is False
    assert "mixed_script" in result["hard_fail_keys"]
    assert result["verdicts"]["mixed_script"]["hits"][0]["text"] == "juridически"


def test_mixed_script_hard_fails_across_all_slots():
    post = (
        "<b>Заголовок</b>\n\n"
        "Текст со словом сbербанк внутри абзаца.\n\n"
        "Второй абзац.\n\n"
        "Третий абзац про метрику 30 заявок.\n\n"
        "Четвёртый абзац.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    for slot in ("morning", "evening", "weekly_case"):
        result = validate_post(post, expected_url="https://example.com/article", slot=slot)
        assert "mixed_script" in result["hard_fail_keys"], slot


def test_clean_post_passes_mixed_script_gate():
    post = (
        "<b>AI-помощник нужно измерять по принятому результату</b>\n\n"
        "Первые недели помощник обрабатывает 30 заявок в день, GPT-4 гоняет черновики.\n\n"
        "Но активность ещё не показывает пользу для процесса.\n\n"
        "Стоимость обработанной заявки отделяет пилот от хобби.\n\n"
        '<a href="https://example.com/article">Источник</a>'
    )
    result = validate_post(post, expected_url="https://example.com/article", slot="morning")
    assert result["verdicts"]["mixed_script"]["status"] == "pass"


def test_cta_mixed_script_fails():
    assert validate_cta_text("juridически чистый совет")["ok"] is False
    assert validate_cta_text("Разбор внутри — 5 минут чтения")["ok"] is True
