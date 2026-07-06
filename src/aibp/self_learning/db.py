"""SQLite database for self-learning experiments.

Separate from PostgreSQL (which is the canonical content store).
SQLite is for: experiments, policies, prompt_changes, autopilot_events.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog

from aibp.utils.config import PROJECT_ROOT, get_settings

log = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_features (
    feed_item_id      INTEGER PRIMARY KEY,
    posted_at         TIMESTAMP NOT NULL,
    slot              TEXT NOT NULL,
    pipeline_env      TEXT NOT NULL,
    target_channel    TEXT NOT NULL,
    strategy_rubric   TEXT,
    topic_cluster     TEXT,
    source_domain     TEXT,
    source_url        TEXT,
    char_count        INTEGER,
    paragraph_count   INTEGER,
    bold_count        INTEGER,
    emoji_count       INTEGER,
    has_image         INTEGER,
    visual_kind       TEXT,
    scheduled_hour    INTEGER,
    cta_variant       TEXT,
    policy_version    TEXT NOT NULL,
    policy_blob       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS engagement_metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_item_id      INTEGER NOT NULL,
    measured_at       TIMESTAMP NOT NULL,
    views             INTEGER,
    forwards          INTEGER,
    replies           INTEGER,
    reactions_count   INTEGER,
    reactions_breakdown TEXT,
    subscribers_at    INTEGER,
    FOREIGN KEY (feed_item_id) REFERENCES post_features(feed_item_id)
);
CREATE INDEX IF NOT EXISTS idx_engagement_post ON engagement_metrics(feed_item_id, measured_at);

CREATE TABLE IF NOT EXISTS policies (
    version           TEXT PRIMARY KEY,
    created_at        TIMESTAMP NOT NULL,
    created_by        TEXT NOT NULL,
    parent_version    TEXT,
    yaml_content      TEXT NOT NULL,
    json_blob         TEXT NOT NULL,
    applies_to        TEXT NOT NULL,
    status            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP,
    experiment_type   TEXT NOT NULL,
    hypothesis        TEXT,
    policy_before     TEXT NOT NULL,
    policy_after      TEXT NOT NULL,
    applies_to        TEXT NOT NULL,
    status            TEXT NOT NULL,
    assignment_mode   TEXT NOT NULL DEFAULT 'interleave',
    control_posts     TEXT,
    shadow_posts      TEXT,
    control_engagement TEXT,
    shadow_engagement TEXT,
    effect_size       REAL,
    p_value           REAL,
    decision_reason   TEXT,
    rolled_back_at    TIMESTAMP,
    rollback_reason   TEXT
);

CREATE TABLE IF NOT EXISTS prompt_changes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    applied_at        TIMESTAMP NOT NULL,
    prompt_file       TEXT NOT NULL,
    diff_before       TEXT NOT NULL,
    diff_after        TEXT NOT NULL,
    experiment_id     INTEGER,
    reverted_at       TIMESTAMP,
    FOREIGN KEY (experiment_id) REFERENCES experiments_log(id)
);

CREATE TABLE IF NOT EXISTS bandit_arms (
    dimension   TEXT NOT NULL,
    arm_id      TEXT NOT NULL,
    alpha       REAL NOT NULL DEFAULT 1,
    beta        REAL NOT NULL DEFAULT 1,
    updated_at  TIMESTAMP,
    PRIMARY KEY (dimension, arm_id)
);

CREATE TABLE IF NOT EXISTS bandit_observations (
    feed_item_id INTEGER NOT NULL,
    dimension    TEXT NOT NULL,
    arm_id       TEXT NOT NULL,
    success      INTEGER NOT NULL,
    observed_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (feed_item_id, dimension)
);

CREATE TABLE IF NOT EXISTS autopilot_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at          TIMESTAMP NOT NULL,
    event_type        TEXT NOT NULL,
    experiment_id     INTEGER,
    details           TEXT,
    FOREIGN KEY (experiment_id) REFERENCES experiments_log(id)
);
"""


def get_db_path() -> Path:
    """Get SQLite DB path from settings."""
    return PROJECT_ROOT / get_settings().self_learning_db_path


@contextmanager
def sqlite_conn() -> Iterator[sqlite3.Connection]:
    """Context manager for SQLite connection."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after initial release. CREATE IF NOT EXISTS does not alter
# existing tables, so init_db() backfills them on already-deployed DBs.
_COLUMN_MIGRATIONS = [
    ("experiments_log", "assignment_mode", "TEXT NOT NULL DEFAULT 'cross_channel'"),
    ("post_features", "cta_variant", "TEXT"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _COLUMN_MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            log.info("sqlite_column_added", table=table, column=column)


def init_db() -> None:
    """Create all tables. Idempotent."""
    with sqlite_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
    log.info("sqlite_initialized", path=str(get_db_path()))


# Telegram views keep growing for ~72h after publishing. Comparing posts of
# different ages by MAX/last snapshot systematically favors older posts, so
# all engagement comparisons use the snapshot closest to this fixed horizon.
ENGAGEMENT_HORIZON_HOURS = 48


def get_snapshot_at_horizon(feed_item_id: int, hours: int = ENGAGEMENT_HORIZON_HOURS) -> dict | None:
    """Engagement snapshot closest to posted_at + hours.

    If no snapshot exists exactly at the horizon, the nearest one is used —
    before or after, whichever is closer in time. Returns None when the post
    has no snapshots at all.
    """
    with sqlite_conn() as conn:
        row = conn.execute(
            """
            SELECT em.views, em.forwards, em.replies, em.reactions_count,
                   em.subscribers_at, em.measured_at
            FROM engagement_metrics em
            JOIN post_features pf ON pf.feed_item_id = em.feed_item_id
            WHERE em.feed_item_id = ?
            ORDER BY ABS(julianday(em.measured_at) - julianday(pf.posted_at) - ? / 24.0)
            LIMIT 1
            """,
            (feed_item_id, float(hours)),
        ).fetchone()
        return dict(row) if row else None


def policy_version(policy_dict: dict) -> str:
    """Compute sha256 hash of policy dict (canonical JSON)."""
    canonical = json.dumps(policy_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def save_policy_version(
    policy_dict: dict,
    yaml_content: str,
    applies_to: str = "stage",
    created_by: str = "autopilot",
    parent_version: str | None = None,
) -> str:
    """Save policy version to SQLite. Returns version hash."""
    version = policy_version(policy_dict)
    with sqlite_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO policies
                (version, created_at, created_by, parent_version, yaml_content, json_blob, applies_to, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')
            """,
            (
                version,
                datetime.now(timezone.utc).isoformat(),
                created_by,
                parent_version,
                yaml_content,
                json.dumps(policy_dict, ensure_ascii=False),
                applies_to,
            ),
        )
    log.info("policy_saved", version=version, applies_to=applies_to)
    return version


def log_autopilot_event(event_type: str, experiment_id: int | None = None, details: dict | None = None) -> None:
    """Log an autopilot event (for kill switch, rate limiter, dashboard)."""
    with sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO autopilot_events (event_at, event_type, experiment_id, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                event_type,
                experiment_id,
                json.dumps(details, ensure_ascii=False) if details else None,
            ),
        )


if __name__ == "__main__":
    init_db()
    print(f"✅ SQLite initialized at {get_db_path()}")
