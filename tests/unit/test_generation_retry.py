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
    monkeypatch.setattr(pipeline, "select_candidate", lambda *a, **k: dict(_CANDIDATE))

    # stage env: no PostgreSQL writes, no interleave/bandit/CTA (prod-only)
    rc = pipeline.run(slot="morning", pipeline_env="stage")

    assert rc == 1                                     # never cleared the gate
    assert len(prompts) == 3                           # 3 attempts
    assert "Предыдущая попытка отклонена" not in prompts[0]
    assert "Предыдущая попытка отклонена" in prompts[1]
    assert "владельц" in prompts[1]                    # the failing term is named
