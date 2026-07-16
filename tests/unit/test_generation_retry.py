"""Tests for informed retry after a quality-gate failure (issue #30)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.generation import pipeline

_CANDIDATE = {
    "id": 1, "url": "https://example.com/a", "title": "T",
    "source": "S", "source_domain": "example.com",
    "source_published_at": "2026-07-01", "text": "body",
    "summary": {"editorial": {"strategy_rubric": "anti_hype"}},
}

_MINIMAL_POLICY = {
    "rubric_weights": {"anti_hype": 1.0},
    "post_params": {"morning": {"target_chars": [800, 1400], "paragraphs": [4, 5]}},
}

_FORBIDDEN_POST = (
    "<b>Заголовок</b>\n\nПост для владельцев бизнеса и прочих читателей.\n\n"
    "Второй абзац с контекстом.\n\nТретий абзац про практику.\n\n"
    "Четвёртый абзац.\n\n"
    '<a href="https://example.com/a">Источник</a>'
)

# Mirrors the passing fixture in test_quality_gate.py, pointed at _CANDIDATE's url.
_CLEAN_POST = (
    "<b>AI-помощник нужно измерять по принятому результату</b>\n\n"
    "Первые недели внутреннего помощника часто выглядят успешными: запросов много, сотрудники пробуют сценарии.\n\n"
    "Но активность ещё не показывает пользу. Один человек закрывает рутинную сверку, другой гоняет дорогую модель ради черновика.\n\n"
    "Стоимость обработанной заявки — единственная метрика, которая отделяет пилот от хобби.\n\n"
    '<a href="https://example.com/a">Источник</a>'
)


# ═══════════════════════════════════════════════════════════════════
# extract_gate_failures
# ═══════════════════════════════════════════════════════════════════

def test_extract_gate_failures_maps_fail_and_warn():
    validation = {"verdicts": {
        "forbidden_terms": {"status": "fail", "hits": [{"text": "владельцев бизнеса"}]},
        "metric_presence": {"status": "warn", "note": "no numeric marker"},
        "source_link": {"status": "pass"},
    }}
    failures = pipeline.extract_gate_failures(validation)
    by_key = {f["key"]: f["hits"] for f in failures}
    assert set(by_key) == {"forbidden_terms", "metric_presence"}
    assert "владельцев бизнеса" in by_key["forbidden_terms"]
    assert "no numeric marker" in by_key["metric_presence"]


# ═══════════════════════════════════════════════════════════════════
# generate_post prompt injection + temperature
# ═══════════════════════════════════════════════════════════════════

class _CaptureClient:
    default_model = "x"

    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=0.4, max_tokens=2000):
        self.calls.append({"prompt": messages[0]["content"], "temperature": temperature})
        return "<b>ok</b>"


def test_generate_post_no_failures_no_retry_block():
    client = _CaptureClient()
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client)
    assert "Предыдущая попытка отклонена" not in client.calls[0]["prompt"]
    assert client.calls[0]["temperature"] == pytest.approx(0.4)


def test_generate_post_injects_failures_and_raises_temperature():
    client = _CaptureClient()
    failures = [{"key": "forbidden_terms", "hits": "владельцев бизнеса"}]
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client,
                           previous_failures=failures, attempt=2)
    prompt = client.calls[0]["prompt"]
    assert "Предыдущая попытка отклонена" in prompt
    assert "forbidden_terms" in prompt
    assert "владельцев бизнеса" in prompt
    assert client.calls[0]["temperature"] == pytest.approx(0.7)  # 0.4 + 0.15*2


def test_temperature_is_capped_at_0_7():
    client = _CaptureClient()
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client, attempt=5)
    assert client.calls[0]["temperature"] == pytest.approx(0.7)


# ═══════════════════════════════════════════════════════════════════
# End-to-end retry loop threads failures forward
# ═══════════════════════════════════════════════════════════════════

def test_retry_loop_feeds_failure_into_second_prompt(monkeypatch):
    """1st generation trips the gate → the 2nd prompt names the offending term."""
    prompts = []

    class FakeClient:
        default_model = "x"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages, temperature=0.4, max_tokens=2000):
            prompts.append(messages[0]["content"])
            return _FORBIDDEN_POST  # always fails → 3 informed attempts

    monkeypatch.setattr(pipeline, "OpenRouterClient", FakeClient)

    def fake_select(*a, **k):
        # Honour exclude_ids like the real selection: after the candidate is
        # rejected, the pool is empty — otherwise the fallback loop would
        # legitimately try 3 candidates × 3 attempts.
        if k.get("exclude_ids") and _CANDIDATE["id"] in k["exclude_ids"]:
            return None
        return dict(_CANDIDATE)

    monkeypatch.setattr(pipeline, "select_candidate", fake_select)
    # keep the test day-independent: on the weekly_case weekday the morning
    # slot would be skipped entirely and the test would fail spuriously
    monkeypatch.setattr(pipeline, "_should_skip_for_weekly_case", lambda *a, **k: False)

    # stage env: no PostgreSQL writes, no interleave/bandit/CTA (prod-only)
    rc = pipeline.run(slot="morning", pipeline_env="stage")

    assert rc == 1                                     # never cleared the gate
    assert len(prompts) == 3                           # 3 attempts for the one candidate
    assert "Предыдущая попытка отклонена" not in prompts[0]
    assert "Предыдущая попытка отклонена" in prompts[1]
    assert "владельц" in prompts[1]                    # the failing term is named


def test_failed_candidate_falls_back_to_next(monkeypatch):
    """A candidate that burns 3 attempts must not empty the slot — the next
    candidate is tried (2026-07-16 incident)."""
    calls = {"n": 0}

    class FakeClient:
        default_model = "x"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages, temperature=0.4, max_tokens=2000):
            calls["n"] += 1
            # candidate 1 always fails the gate; candidate 2 passes at once
            return _FORBIDDEN_POST if calls["n"] <= 3 else _CLEAN_POST

        def chat_json(self, *a, **k):
            return {"ok": True, "problems": []}

    good = dict(_CANDIDATE, id=_CANDIDATE["id"] + 1)

    def fake_select(*a, **k):
        excluded = k.get("exclude_ids") or set()
        if _CANDIDATE["id"] not in excluded:
            return dict(_CANDIDATE)
        if good["id"] not in excluded:
            return dict(good)
        return None

    monkeypatch.setattr(pipeline, "OpenRouterClient", FakeClient)
    monkeypatch.setattr(pipeline, "select_candidate", fake_select)
    monkeypatch.setattr(pipeline, "_should_skip_for_weekly_case", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "execute", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_resolve_slot_image", lambda *a, **k: None)

    rc = pipeline.run(slot="morning", pipeline_env="stage")

    assert rc == 0                    # slot filled by the fallback candidate
    assert calls["n"] == 4            # 3 failed attempts + 1 clean pass


# ═══════════════════════════════════════════════════════════════════
# Morning prompt: internal checklist, label ban, hook anti-repetition
# ═══════════════════════════════════════════════════════════════════

def test_morning_prompt_bans_section_labels_and_keeps_checklist():
    client = _CaptureClient()
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client)
    prompt = client.calls[0]["prompt"]
    assert "INTERNAL checklist" in prompt
    assert "FORBIDDEN: bold" in prompt
    assert "<b>Процесс.</b>" in prompt  # named as a negative example
    assert "MAY use up to 3 bold lead-in labels" not in prompt


def test_recent_openings_injected_into_morning_prompt():
    client = _CaptureClient()
    openings = ["Первые недели пилота выглядят успешными", "Бухгалтер за $400 записывает цифры"]
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client,
                           recent_openings=openings)
    prompt = client.calls[0]["prompt"]
    assert "Первые недели пилота выглядят успешными" in prompt
    assert "Бухгалтер за $400 записывает цифры" in prompt
    assert "Do NOT reuse their opening move" in prompt


def test_no_recent_openings_no_antirepetition_block():
    client = _CaptureClient()
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client)
    assert "Recent posts opened" not in client.calls[0]["prompt"]


def test_extract_opening_skips_headline_and_hashtag():
    draft = (
        "<b>Заголовок поста</b>\n\n"
        "Первое предложение тела, оно и есть зачин.\n\n"
        "#процесс\n"
        '<a href="https://example.com/a">Источник</a>'
    )
    assert pipeline.extract_opening(draft) == "Первое предложение тела, оно и есть зачин."


def test_extract_opening_empty_draft():
    assert pipeline.extract_opening(None) is None
    assert pipeline.extract_opening("") is None
    assert pipeline.extract_opening("<b>Только заголовок</b>") is None


def test_generation_prompt_carries_audience_context():
    """Foreign-jurisdiction stories must be angled at RU/CIS consequences."""
    client = _CaptureClient()
    pipeline.generate_post(_CANDIDATE, "morning", _MINIMAL_POLICY, client)
    prompt = client.calls[0]["prompt"]
    assert "Russia and CIS" in prompt
    assert "consequence for these readers" in prompt
