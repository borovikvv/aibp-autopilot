"""Tests for OpenRouter post image generation (issue #34)."""
import base64
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.generation import image_gen

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"
CANDIDATE = {
    "id": 42, "title": "Как переложить проверку на модель",
    "summary": {"editorial": {"one_sentence_angle": "AI проверяет заявки, человек — спорные",
                              "strategy_rubric": "implementation_metric"}},
}
POLICY = {"visual_policy": {"enabled": True, "generate": True,
                            "kind": "process_scheme", "palette": "editorial_light"}}


@pytest.fixture()
def img_settings(tmp_path):
    s = SimpleNamespace(
        image_output_dir=tmp_path / "img",
        image_public_base_url="https://cdn.example/aibp/img",
        openrouter_image_model="google/gemini-2.5-flash-image",
    )
    with patch.object(image_gen, "get_settings", return_value=s):
        yield tmp_path


# ═══════════════════════════════════════════════════════════════════
# build_image_prompt
# ═══════════════════════════════════════════════════════════════════

def test_prompt_includes_angle_kind_palette():
    prompt = image_gen.build_image_prompt(CANDIDATE, POLICY)
    assert "AI проверяет заявки" in prompt
    assert "abstract process visual" in prompt  # process_scheme style, text-free
    assert "unlabeled" in prompt                # never ask the model for labels
    assert "editorial_light" in prompt
    assert "no text" in prompt.lower()          # avoid baked-in captions
    assert "no letters" in prompt.lower()       # hardened: models misspell words


def test_prompt_falls_back_to_title_without_angle():
    cand = {"id": 1, "title": "Заголовок", "summary": {}}
    assert "Заголовок" in image_gen.build_image_prompt(cand, POLICY)


# ═══════════════════════════════════════════════════════════════════
# generate_post_image
# ═══════════════════════════════════════════════════════════════════

def test_generates_saves_and_returns_public_url(img_settings):
    client = MagicMock()
    client.generate_image.return_value = PNG

    url = image_gen.generate_post_image(42, CANDIDATE, POLICY, client=client)

    assert url == "https://cdn.example/aibp/img/42.png"
    saved = img_settings / "img" / "42.png"
    assert saved.exists()
    assert saved.read_bytes() == PNG
    # the prompt actually reached the client
    client.generate_image.assert_called_once()


def test_returns_none_when_generation_fails(img_settings):
    client = MagicMock()
    client.generate_image.return_value = None
    assert image_gen.generate_post_image(42, CANDIDATE, POLICY, client=client) is None
    assert not (img_settings / "img" / "42.png").exists()


def test_returns_none_on_client_exception(img_settings):
    client = MagicMock()
    client.generate_image.side_effect = RuntimeError("api down")
    assert image_gen.generate_post_image(42, CANDIDATE, POLICY, client=client) is None


# ═══════════════════════════════════════════════════════════════════
# OpenRouterClient.generate_image (parsing)
# ═══════════════════════════════════════════════════════════════════

def test_client_generate_image_parses_b64(monkeypatch, tmp_path):
    from aibp.enrichment import llm_client

    s = SimpleNamespace(
        openrouter_api_key="k", openrouter_model="m", openrouter_miner_model="m",
        openrouter_daily_budget_usd=0.0, openrouter_image_model="img-model",
        openrouter_image_cost_usd=0.04,
    )
    monkeypatch.setattr(llm_client, "get_settings", lambda: s)
    monkeypatch.setattr(llm_client.OpenRouterClient, "_cost_log_dir", tmp_path, raising=False)

    client = llm_client.OpenRouterClient(api_key="k")
    client.cost_log = tmp_path / "cost.jsonl"
    monkeypatch.setattr(client, "_check_budget", lambda: None)
    monkeypatch.setattr(client, "_log_cost", lambda *a, **k: None)

    payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": {"cost": 0.03}}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json, headers):
            assert url.endswith("/images")
            assert json["model"] == "img-model"
            return _Resp()

    monkeypatch.setattr(llm_client.httpx, "Client", lambda *a, **k: _Client())

    assert client.generate_image("a prompt") == PNG


def test_client_generate_image_none_on_empty_data(monkeypatch, tmp_path):
    from aibp.enrichment import llm_client

    s = SimpleNamespace(
        openrouter_api_key="k", openrouter_model="m", openrouter_miner_model="m",
        openrouter_daily_budget_usd=0.0, openrouter_image_model="img-model",
        openrouter_image_cost_usd=0.04,
    )
    monkeypatch.setattr(llm_client, "get_settings", lambda: s)
    client = llm_client.OpenRouterClient(api_key="k")
    client.cost_log = tmp_path / "cost.jsonl"
    monkeypatch.setattr(client, "_check_budget", lambda: None)
    monkeypatch.setattr(client, "_log_cost", lambda *a, **k: None)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": []}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json, headers):
            return _Resp()

    monkeypatch.setattr(llm_client.httpx, "Client", lambda *a, **k: _Client())
    assert client.generate_image("p") is None


# ═══════════════════════════════════════════════════════════════════
# Per-slot image gating (visual_policy.slots)
# ═══════════════════════════════════════════════════════════════════

def test_slot_image_gating():
    from aibp.generation.pipeline import _resolve_slot_image
    policy = {"visual_policy": {"enabled": True, "generate": False,
                                "static_image_url": "https://x/i.png",
                                "slots": ["morning", "weekly_case"]}}
    cand = {"id": 1, "title": "t", "summary": {}}
    assert _resolve_slot_image(cand, "morning", policy, None) == "https://x/i.png"
    assert _resolve_slot_image(cand, "weekly_case", policy, None) == "https://x/i.png"
    assert _resolve_slot_image(cand, "evening", policy, None) is None


def test_slot_image_no_slots_list_means_all():
    from aibp.generation.pipeline import _resolve_slot_image
    policy = {"visual_policy": {"enabled": True, "generate": False,
                                "static_image_url": "https://x/i.png"}}
    cand = {"id": 1, "title": "t", "summary": {}}
    assert _resolve_slot_image(cand, "evening", policy, None) == "https://x/i.png"
