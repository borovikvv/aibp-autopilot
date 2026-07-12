"""Tests for the LLM editor — holistic pre-publish review."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.generation import pipeline
from aibp.generation.llm_editor import editor_failures, review_post

_REJECT_VERDICT = {
    "ok": False,
    "problems": [
        {"key": "single_thread", "detail": "Третий абзац не связан с тезисом — убрать или связать."},
        {"key": "hook", "detail": "Первое предложение — общее место, нужен контраст или цифра."},
    ],
}


class _JsonClient:
    """Captures editor prompts; returns a fixed verdict or raises."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.prompts = []
        self.models = []

    def chat_json(self, messages, model=None, temperature=0.0, max_tokens=1000):
        self.prompts.append(messages[0]["content"])
        self.models.append(model)
        if self.error:
            raise self.error
        return self.payload


# ═══════════════════════════════════════════════════════════════════
# review_post verdicts
# ═══════════════════════════════════════════════════════════════════

def test_review_post_approves_clean_post():
    client = _JsonClient(payload={"ok": True, "problems": []})
    review = review_post("<b>Пост</b>\n\nТело.", "morning", client)
    assert review == {"ok": True, "problems": [], "skipped": False}


def test_review_post_rejects_and_maps_problems():
    client = _JsonClient(payload=_REJECT_VERDICT)
    review = review_post("<b>Пост</b>\n\nТело.", "morning", client)
    assert review["ok"] is False
    assert review["skipped"] is False
    assert [p["key"] for p in review["problems"]] == ["single_thread", "hook"]


def test_review_post_degrades_open_on_infra_error():
    """Editor is a quality layer, not a safety layer: infra failure must not block."""
    client = _JsonClient(error=RuntimeError("budget exceeded"))
    review = review_post("<b>Пост</b>\n\nТело.", "morning", client)
    assert review["ok"] is True
    assert review["skipped"] is True


def test_review_post_degrades_open_on_malformed_verdict():
    client = _JsonClient(payload={"verdict": "выглядит нормально"})
    review = review_post("<b>Пост</b>\n\nТело.", "morning", client)
    assert review["ok"] is True
    assert review["skipped"] is True


def test_review_prompt_contains_post_source_and_slot():
    client = _JsonClient(payload={"ok": True, "problems": []})
    review_post("<b>Пост</b>\n\nУникальное тело поста.", "evening", client,
                source_title="Заголовок источника",
                source_excerpt="Выдержка из источника с цифрой 42.",
                model="anthropic/claude-haiku-4.5")
    prompt = client.prompts[0]
    assert "Уникальное тело поста." in prompt
    assert "Заголовок источника" in prompt
    assert "цифрой 42" in prompt
    assert "slot: evening" in prompt
    assert client.models == ["anthropic/claude-haiku-4.5"]


def test_editor_failures_use_retry_hint_format():
    failures = editor_failures({"problems": _REJECT_VERDICT["problems"]})
    assert failures[0] == {
        "key": "editor_single_thread",
        "hits": "Третий абзац не связан с тезисом — убрать или связать.",
    }


# ═══════════════════════════════════════════════════════════════════
# run() loop: editor rejection feeds the next generation prompt
# ═══════════════════════════════════════════════════════════════════

_CANDIDATE = {
    "id": 7, "url": "https://example.com/a", "title": "T",
    "source": "S", "source_domain": "example.com",
    "source_published_at": "2026-07-01", "text": "source body",
    "summary": {"editorial": {"strategy_rubric": "anti_hype"}},
    "category": "cases",
}

# Passes every regex gate for slot=morning.
_CLEAN_POST = (
    "<b>AI-помощник нужно измерять по принятому результату</b>\n\n"
    "Первые недели внутреннего помощника выглядят успешными: 30 запросов в день.\n\n"
    "Но активность ещё не показывает пользу для конкретного процесса.\n\n"
    "Стоимость обработанной заявки отделяет пилот от хобби.\n\n"
    '<a href="https://example.com/a">Источник</a>'
)


def test_run_loop_feeds_editor_rejection_into_second_prompt(monkeypatch):
    """Regex gate passes, editor rejects once → 2nd prompt names the problem."""
    chat_prompts = []
    editor_verdicts = [dict(_REJECT_VERDICT), {"ok": True, "problems": []}]

    class FakeClient:
        default_model = "x"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages, temperature=0.4, max_tokens=2000):
            chat_prompts.append(messages[0]["content"])
            return _CLEAN_POST

        def chat_json(self, messages, model=None, temperature=0.0, max_tokens=1000):
            return editor_verdicts.pop(0)

    monkeypatch.setattr(pipeline, "OpenRouterClient", FakeClient)
    monkeypatch.setattr(pipeline, "select_candidate", lambda *a, **k: dict(_CANDIDATE))
    monkeypatch.setattr(pipeline, "fetch_recent_openings", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_should_skip_for_weekly_case", lambda *a, **k: False)
    executed = []
    monkeypatch.setattr(pipeline, "execute", lambda sql, params=(): executed.append(sql) or 1)

    rc = pipeline.run(slot="morning", pipeline_env="stage")

    assert rc == 0
    assert len(chat_prompts) == 2
    assert "Предыдущая попытка отклонена" in chat_prompts[1]
    assert "editor_single_thread" in chat_prompts[1]
    assert "Третий абзац не связан с тезисом" in chat_prompts[1]
    assert executed  # approved post reached the stage INSERT


def test_run_loop_editor_rejects_three_times_gives_up(monkeypatch):
    class FakeClient:
        default_model = "x"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages, temperature=0.4, max_tokens=2000):
            return _CLEAN_POST

        def chat_json(self, messages, model=None, temperature=0.0, max_tokens=1000):
            return dict(_REJECT_VERDICT)

    monkeypatch.setattr(pipeline, "OpenRouterClient", FakeClient)
    monkeypatch.setattr(pipeline, "select_candidate", lambda *a, **k: dict(_CANDIDATE))
    monkeypatch.setattr(pipeline, "fetch_recent_openings", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_should_skip_for_weekly_case", lambda *a, **k: False)
    executed = []
    monkeypatch.setattr(pipeline, "execute", lambda sql, params=(): executed.append(sql) or 1)

    rc = pipeline.run(slot="morning", pipeline_env="stage")

    assert rc == 1
    assert not executed  # stage env never rejects in DB, and nothing published


def test_run_loop_editor_disabled_skips_review(monkeypatch):
    """llm_editor.enabled: false → chat_json is never called."""
    json_calls = []

    class FakeClient:
        default_model = "x"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages, temperature=0.4, max_tokens=2000):
            return _CLEAN_POST

        def chat_json(self, *a, **k):
            json_calls.append(1)
            return {"ok": True, "problems": []}

    monkeypatch.setattr(pipeline, "OpenRouterClient", FakeClient)
    monkeypatch.setattr(pipeline, "select_candidate", lambda *a, **k: dict(_CANDIDATE))
    monkeypatch.setattr(pipeline, "fetch_recent_openings", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_should_skip_for_weekly_case", lambda *a, **k: False)
    monkeypatch.setattr(pipeline, "execute", lambda sql, params=(): 1)

    policy = pipeline.load_policy(pipeline_env="stage")
    policy["llm_editor"] = {"enabled": False}
    monkeypatch.setattr(pipeline, "load_policy", lambda *a, **k: policy)

    rc = pipeline.run(slot="morning", pipeline_env="stage")

    assert rc == 0
    assert json_calls == []
