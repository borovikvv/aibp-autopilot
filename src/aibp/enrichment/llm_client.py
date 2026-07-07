"""OpenRouter LLM client — abstraction over Claude/GPT.

Supports: chat, chat_json, with retry and cost tracking.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from aibp.utils.config import PROJECT_ROOT, get_settings

log = structlog.get_logger()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"

# Cost per 1M tokens (USD). Update from https://openrouter.ai/models when needed.
COST_TABLE: dict[str, dict[str, float]] = {
    "anthropic/claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "anthropic/claude-opus-4": {"input": 15.0, "output": 75.0},
    "openai/gpt-4o": {"input": 2.5, "output": 10.0},
    "openai/gpt-4o-mini": {"input": 0.15, "output": 0.6},
}


class BudgetExceededError(Exception):
    """Raised when daily LLM budget is exceeded."""


class LLMError(Exception):
    """Raised when LLM call fails permanently."""


class OpenRouterClient:
    """HTTP client for OpenRouter API with retry, cost tracking, budget guard."""

    def __init__(
        self,
        api_key: str | None = None,
        default_model: str | None = None,
        daily_budget_usd: float | None = None,
    ) -> None:
        s = get_settings()
        self.api_key = api_key or s.openrouter_api_key
        self.default_model = default_model or s.openrouter_model
        self.daily_budget = daily_budget_usd or s.openrouter_daily_budget_usd
        # Daily-rotated cost log: llm_cost_YYYYMMDD.jsonl
        self._cost_log_dir = PROJECT_ROOT / "reports"
        self._cost_log_dir.mkdir(parents=True, exist_ok=True)
        self._today_str = datetime.now(UTC).strftime("%Y%m%d")
        self.cost_log = self._cost_log_dir / f"llm_cost_{self._today_str}.jsonl"

        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY not set")

    def _get_today_cost_log(self) -> Path:
        """Get today's cost log path, rotating if date changed."""
        today = datetime.now(UTC).strftime("%Y%m%d")
        if today != self._today_str:
            self._today_str = today
            self.cost_log = self._cost_log_dir / f"llm_cost_{today}.jsonl"
        return self.cost_log

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=16),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout)),
        reraise=True,
    )
    def _call_api(self, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
        """Make raw API call with retry."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost for a call."""
        rates = COST_TABLE.get(model, COST_TABLE["anthropic/claude-sonnet-4"])
        return (input_tokens / 1_000_000) * rates["input"] + (output_tokens / 1_000_000) * rates["output"]

    def _log_cost(self, model: str, input_tokens: int, output_tokens: int, cost: float) -> None:
        """Append cost record to today's JSONL log."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        }
        log_path = self._get_today_cost_log()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def _check_budget(self) -> None:
        """Check if today's cumulative cost exceeds budget.

        Reads only today's file (llm_cost_YYYYMMDD.jsonl), not the entire history.
        O(n) where n = calls today, not n = calls since system started.
        """
        if self.daily_budget <= 0:
            return
        log_path = self._get_today_cost_log()
        if not log_path.exists():
            return  # no calls today yet

        total = 0.0
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    total += rec.get("cost_usd", 0)
                except json.JSONDecodeError:
                    continue
        if total >= self.daily_budget:
            raise BudgetExceededError(
                f"Daily budget ${self.daily_budget} exceeded (${total:.2f} used today)"
            )

    def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> str:
        """Send chat messages, return assistant text."""
        self._check_budget()
        model = model or self.default_model
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            result = self._call_api(payload)
        except Exception as e:
            log.error("llm_call_failed", model=model, error=str(e))
            raise LLMError(f"LLM call failed: {e}") from e

        usage = result.get("usage", {})
        input_t = usage.get("prompt_tokens", 0)
        output_t = usage.get("completion_tokens", 0)
        cost = self._estimate_cost(model, input_t, output_t)
        self._log_cost(model, input_t, output_t, cost)

        text = result["choices"][0]["message"]["content"]
        log.info("llm_call_ok", model=model, input_tokens=input_t, output_tokens=output_t, cost_usd=round(cost, 4))
        return text

    def generate_image(self, prompt: str, model: str | None = None) -> bytes | None:
        """Generate an image via OpenRouter /api/v1/images (issue #34).

        Returns raw image bytes, or None on any failure (the caller falls back
        to a text-only post). Cost is charged against the daily budget: the
        response usage.cost if present, else a flat estimate from settings.
        """
        self._check_budget()
        s = get_settings()
        model = model or s.openrouter_image_model
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(OPENROUTER_IMAGES_URL,
                                   json={"model": model, "prompt": prompt}, headers=headers)
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            log.error("image_gen_failed", model=model, error=str(e))
            return None

        cost = float(result.get("usage", {}).get("cost") or s.openrouter_image_cost_usd)
        self._log_cost(model, 0, 0, cost)

        data = result.get("data") or []
        b64 = data[0].get("b64_json") if data else None
        if not b64:
            log.error("image_gen_no_data", model=model, keys=list(result.keys()))
            return None
        try:
            import base64
            image_bytes = base64.b64decode(b64)
        except Exception as e:
            log.error("image_gen_decode_failed", error=str(e))
            return None
        log.info("image_gen_ok", model=model, bytes=len(image_bytes), cost_usd=round(cost, 4))
        return image_bytes

    def chat_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4000,
    ) -> dict[str, Any]:
        """Send chat messages, parse JSON response.

        Appends system instruction to return strict JSON.
        """
        messages = list(messages)
        messages.append({
            "role": "system",
            "content": "Return ONLY valid JSON. No markdown, no explanation, no code fences. Just the JSON object.",
        })
        text = self.chat(messages, model=model, temperature=temperature, max_tokens=max_tokens)

        # Strip markdown code fences if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            log.error("llm_json_parse_failed", error=str(e), text_preview=text[:200])
            raise LLMError(f"Failed to parse JSON: {e}") from e
