"""Cross-process advisory lock so only one getUpdates caller runs at a time.

getUpdates is exclusive per bot: two schedulers polling concurrently → 409
Conflict (issue #24). The engagement collector's getUpdates fallback and the
approval-gate callback poller both acquire this lock before calling getUpdates,
so they can never race on the same host. The lock is non-blocking — if it is
held elsewhere, the caller skips its getUpdates run and retries on the next
cron tick instead of risking a 409.

This removes the *concurrency* conflict. To also remove the residual risk of
the approval poller destructively advancing the update offset past the
collector's channel_post updates, set TELEGRAM_METRICS_CHAT_ID so the collector
uses copyMessage and never touches getUpdates at all (see docs/install.md).
"""
from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

from aibp.self_learning.db import get_db_path

log = structlog.get_logger()


def lock_path() -> Path:
    """Lock file lives next to the self-learning SQLite DB (a known writable dir)."""
    return get_db_path().parent / "telegram_getupdates.lock"


@contextmanager
def getupdates_lock() -> Iterator[bool]:
    """Yield True if the getUpdates lock was acquired, False if held elsewhere.

    Non-blocking: a busy lock means another process is already polling
    getUpdates, so the caller should skip and retry next tick.
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            log.info("getupdates_lock_busy", path=str(path))
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
