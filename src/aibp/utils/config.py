"""Configuration loader — reads .env and YAML configs."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    # Telegram
    telegram_bot_token: str
    telegram_channel_id_prod: str
    telegram_channel_id_test: str
    telegram_alert_chat_id: str

    # Database
    database_url: str

    # LLM
    openrouter_api_key: str
    openrouter_model: str
    openrouter_miner_model: str
    # Cheap high-volume tasks: RSS classification and same-story dedup checks.
    # These run hundreds of times a day, so they get a budget model; the
    # flagship model is reserved for post generation / mining / creatives.
    openrouter_enrichment_model: str
    openrouter_dedup_model: str
    openrouter_daily_budget_usd: float
    openrouter_embedding_model: str
    # OpenAI-compatible chat/completions gateway for flagship calls.
    openrouter_base_url: str
    # Second gateway for cheap high-volume tasks (opencode zen subscription).
    # When opencode_api_key is set, enrichment/dedup route here with
    # opencode_model; otherwise they stay on OpenRouter with the
    # openrouter_*_model settings above.
    opencode_api_key: str
    opencode_base_url: str
    opencode_model: str

    # Image gen (issue #34) — via OpenRouter /api/v1/images
    xai_api_key: str
    openrouter_image_model: str
    openrouter_image_cost_usd: float
    image_output_dir: Path
    image_public_base_url: str

    # Hermes
    hermes_api_url: str

    # App
    app_env: str
    app_timezone: str
    log_level: str
    policy_path: Path
    rss_feeds_path: Path

    # Dashboard
    dashboard_output_path: Path
    dashboard_base_url: str

    # Click tracking (issue #15). Empty base URL disables link wrapping.
    tracking_base_url: str
    tracking_port: int

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(PROJECT_ROOT / ".env")
        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_channel_id_prod=os.getenv("TELEGRAM_CHANNEL_ID_PROD", ""),
            telegram_channel_id_test=os.getenv("TELEGRAM_CHANNEL_ID_TEST", ""),
            telegram_alert_chat_id=os.getenv("TELEGRAM_ALERT_CHAT_ID", ""),
            database_url=os.getenv("DATABASE_URL", "postgresql://aibp:aibp@localhost:5432/aibp"),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5"),
            openrouter_miner_model=os.getenv("OPENROUTER_MINER_MODEL", "anthropic/claude-sonnet-5"),
            openrouter_enrichment_model=os.getenv("OPENROUTER_ENRICHMENT_MODEL", "deepseek/deepseek-v4-flash"),
            openrouter_dedup_model=os.getenv("OPENROUTER_DEDUP_MODEL", "deepseek/deepseek-v4-flash"),
            openrouter_daily_budget_usd=float(os.getenv("OPENROUTER_DAILY_BUDGET_USD", "5.0")),
            openrouter_embedding_model=os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small"),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            opencode_api_key=os.getenv("OPENCODE_API_KEY", ""),
            opencode_base_url=os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1"),
            opencode_model=os.getenv("OPENCODE_MODEL", "deepseek-v4-flash"),
            xai_api_key=os.getenv("XAI_API_KEY", ""),
            openrouter_image_model=os.getenv("OPENROUTER_IMAGE_MODEL", "google/gemini-2.5-flash-image"),
            openrouter_image_cost_usd=float(os.getenv("OPENROUTER_IMAGE_COST_USD", "0.04")),
            image_output_dir=Path(os.getenv("IMAGE_OUTPUT_DIR", "/srv/static/aibp/img")),
            image_public_base_url=os.getenv("IMAGE_PUBLIC_BASE_URL",
                                            "https://cockpit.borovikvv.ru/aibp/img"),
            hermes_api_url=os.getenv("HERMES_API_URL", "https://hermes-agent.nousresearch.com"),
            app_env=os.getenv("APP_ENV", "production"),
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            policy_path=Path(os.getenv("POLICY_PATH", "config/policy.yaml")),
            rss_feeds_path=Path(os.getenv("RSS_FEEDS_PATH", "config/rss_feeds.yaml")),
            dashboard_output_path=Path(os.getenv("DASHBOARD_OUTPUT_PATH", "/srv/static/aibp/dashboard.html")),
            dashboard_base_url=os.getenv("DASHBOARD_BASE_URL", "https://cockpit.borovikvv.ru/aibp"),
            tracking_base_url=os.getenv("TRACKING_BASE_URL", ""),
            tracking_port=int(os.getenv("TRACKING_PORT", "8091")),
        )


# ─── Policy loader ──────────────────────────────────────────────────
def load_policy(path: Path | None = None, pipeline_env: str = "prod") -> dict[str, Any]:
    """Load canonical policy.yaml.

    Args:
        path: Explicit path to policy file. If None, auto-detect by pipeline_env.
        pipeline_env: "prod" → config/policy.yaml, "stage" → config/policy.stage.yaml
                      (falls back to config/policy.yaml if stage file missing).

    Note (ADR-0007 / issue #22): `policy.stage.yaml` is the PREVIEW policy for
    the test channel (human QA) — NOT the interleave variant that drives
    statistics. The variant is loaded from SQLite by
    `self_learning.interleave.resolve_policy_for_today`, not here.
    """
    if path is not None:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    prod_path = PROJECT_ROOT / "config" / "policy.yaml"
    if pipeline_env == "stage":
        stage_path = PROJECT_ROOT / "config" / "policy.stage.yaml"
        if stage_path.exists():
            with open(stage_path, encoding="utf-8") as f:
                return yaml.safe_load(f)
        # Fall back to prod policy if stage policy not yet written
    with open(prod_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rss_feeds(path: Path | None = None) -> dict[str, Any]:
    """Load RSS feeds configuration."""
    if path is None:
        path = PROJECT_ROOT / "config" / "rss_feeds.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─── Singleton ──────────────────────────────────────────────────────
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
