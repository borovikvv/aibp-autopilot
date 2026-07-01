"""Logging configuration — structlog with JSON output."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from aibp.utils.config import PROJECT_ROOT, get_settings


def configure_logging(service_name: str = "aibp", level: str | None = None) -> None:
    """Configure structlog with JSON output to stderr.

    Args:
        service_name: logical service name (appears in every log line)
        level: log level (DEBUG|INFO|WARNING|ERROR). Defaults to LOG_LEVEL env.
    """
    s = get_settings()
    level = level or s.log_level

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Bind service name
    structlog.contextvars.bind_contextvars(service=service_name)

    # Also write to daily log file
    log_dir = PROJECT_ROOT / "reports" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d')}.jsonl"

    file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    root = logging.getLogger()
    root.addHandler(file_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
