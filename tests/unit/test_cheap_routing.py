# tests/unit/test_cheap_routing.py
"""Split routing for cheap tasks: opencode zen when configured, else OpenRouter."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.enrichment import llm_client  # noqa: E402


def _settings(**overrides):
    base = dict(
        openrouter_api_key="or-key",
        openrouter_model="anthropic/claude-sonnet-5",
        openrouter_miner_model="anthropic/claude-sonnet-5",
        openrouter_daily_budget_usd=5.0,
        openrouter_base_url="https://openrouter.ai/api/v1",
        opencode_api_key="",
        opencode_base_url="https://opencode.ai/zen/v1",
        opencode_model="deepseek-v4-flash",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_cheap_client_uses_openrouter_without_opencode_key(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client, "get_settings", lambda: _settings())
    monkeypatch.setattr(llm_client.OpenRouterClient, "_cost_log_dir", tmp_path, raising=False)

    client = llm_client.cheap_client("deepseek/deepseek-v4-flash")
    assert client.api_key == "or-key"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client.default_model == "deepseek/deepseek-v4-flash"


def test_cheap_client_routes_via_opencode_when_key_set(monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm_client, "get_settings", lambda: _settings(opencode_api_key="oc-key")
    )
    monkeypatch.setattr(llm_client.OpenRouterClient, "_cost_log_dir", tmp_path, raising=False)

    client = llm_client.cheap_client("deepseek/deepseek-v4-flash")
    assert client.api_key == "oc-key"
    assert client.base_url == "https://opencode.ai/zen/v1"
    assert client.default_model == "deepseek-v4-flash"


def test_flagship_client_stays_on_openrouter_even_with_opencode_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm_client, "get_settings", lambda: _settings(opencode_api_key="oc-key")
    )
    monkeypatch.setattr(llm_client.OpenRouterClient, "_cost_log_dir", tmp_path, raising=False)

    client = llm_client.OpenRouterClient()
    assert client.api_key == "or-key"
    assert client.base_url == "https://openrouter.ai/api/v1"
    assert client.default_model == "anthropic/claude-sonnet-5"


def test_base_url_trailing_slash_stripped(monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm_client, "get_settings",
        lambda: _settings(openrouter_base_url="https://example.com/v1/"),
    )
    monkeypatch.setattr(llm_client.OpenRouterClient, "_cost_log_dir", tmp_path, raising=False)

    client = llm_client.OpenRouterClient()
    assert client.base_url == "https://example.com/v1"
