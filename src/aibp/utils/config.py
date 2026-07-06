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
    openrouter_daily_budget_usd: float

    # Image gen
    xai_api_key: str

    # Hermes
    hermes_api_url: str

    # App
    app_env: str
    app_timezone: str
    log_level: str
    policy_path: Path
    self_learning_db_path: Path
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
            openrouter_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"),
            openrouter_miner_model=os.getenv("OPENROUTER_MINER_MODEL", "anthropic/claude-sonnet-4"),
            openrouter_daily_budget_usd=float(os.getenv("OPENROUTER_DAILY_BUDGET_USD", "5.0")),
            xai_api_key=os.getenv("XAI_API_KEY", ""),
            hermes_api_url=os.getenv("HERMES_API_URL", "https://hermes-agent.nousresearch.com"),
            app_env=os.getenv("APP_ENV", "production"),
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            policy_path=Path(os.getenv("POLICY_PATH", "config/policy.yaml")),
            self_learning_db_path=Path(os.getenv("SELF_LEARNING_DB_PATH", "data/self_learning.db")),
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
