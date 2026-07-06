"""Uptime check for the redirect service (issue #21).

Cron-able: curls /healthz and, after N consecutive failures, sends a Telegram
alert to the owner. Consecutive-failure state persists in a small file so a
single blip does not page, but a real outage does.

    python -m aibp.tracking.healthcheck
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import httpx
import structlog

from aibp.self_learning.db import get_db_path
from aibp.utils.config import get_settings

log = structlog.get_logger()

ALERT_AFTER_FAILURES = 3
TELEGRAM_API = "https://api.telegram.org"


def _state_path() -> Path:
    """Consecutive-failure counter lives next to the self-learning SQLite DB."""
    return get_db_path().parent / "redirect_health.state"


def _read_failures() -> int:
    try:
        return int(_state_path().read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_failures(n: int) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(n))


def check_once(timeout: float = 5.0) -> bool:
    """Return True if /healthz answers 200."""
    port = get_settings().tracking_port
    url = f"http://localhost:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return resp.status == 200
    except Exception as e:
        log.warning("redirect_healthz_failed", url=url, error=str(e))
        return False


def _send_alert(message: str) -> None:
    """Best-effort Telegram alert (never raises)."""
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_alert_chat_id:
        return
    try:
        httpx.post(
            f"{TELEGRAM_API}/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": s.telegram_alert_chat_id,
                  "text": f"⚠️ AIBP Redirect Service\n\n{message}"},
            timeout=10,
        )
    except Exception as e:
        log.error("redirect_alert_failed", error=str(e))


def run() -> int:
    """One health check. Returns 0 if healthy, 1 otherwise."""
    if check_once():
        if _read_failures():
            log.info("redirect_recovered")
        _write_failures(0)
        return 0

    failures = _read_failures() + 1
    _write_failures(failures)
    log.warning("redirect_healthcheck_failure", consecutive=failures)

    # Alert once, exactly when we cross the threshold, to avoid paging every tick.
    if failures == ALERT_AFTER_FAILURES:
        _send_alert(
            f"Health check failed {failures} times in a row — tracked links may be down.\n"
            "Check the redirect service: systemctl status aibp-redirect "
            "(or docker compose ps redirect)."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(run())
