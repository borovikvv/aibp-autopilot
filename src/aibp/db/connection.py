"""PostgreSQL connection management.

Direct psycopg2 with parameterized queries. No n8n DB gateway.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from aibp.utils.config import get_settings

# Module-level pool (initialized lazily)
_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    """Get or create the connection pool."""
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=s.database_url,
        )
    return _pool


@contextmanager
def db_conn() -> Iterator[psycopg2.extras.DictCursor]:
    """Context manager yielding a DictCursor.

    Usage:
        with db_conn() as cur:
            cur.execute("SELECT * FROM feed_items WHERE id = %s", (item_id,))
            row = cur.fetchone()

    Commits on success, rolls back on exception, always returns conn to pool.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ─── Helpers ────────────────────────────────────────────────────────
def url_hash(url: str) -> str:
    """SHA256 hash of URL for dedup."""
    return hashlib.sha256(url.encode()).hexdigest()


def fetch_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Fetch single row as dict."""
    with db_conn() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def fetch_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Fetch all rows as list of dicts."""
    with db_conn() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    """Execute INSERT/UPDATE/DELETE, return affected rows count."""
    with db_conn() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Execute with RETURNING clause, return first row."""
    with db_conn() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def close_pool() -> None:
    """Close all connections in the pool (call on shutdown)."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
