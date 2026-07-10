"""Self-learning state storage (PostgreSQL, issue #43).

Previously a separate SQLite DB (data/self_learning.db); now consolidated
into the main PostgreSQL store so engagement, post_features and link_clicks
live in one database. Tables are created by migration 0009.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import structlog
from psycopg2.extras import Json

from aibp.db.connection import execute, fetch_one

log = structlog.get_logger()

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
    row = fetch_one(
        """
        SELECT em.views, em.forwards, em.replies, em.reactions_count,
               em.subscribers_at, em.measured_at
        FROM engagement_metrics em
        JOIN post_features pf ON pf.feed_item_id = em.feed_item_id
        WHERE em.feed_item_id = %s
        ORDER BY ABS(EXTRACT(EPOCH FROM (em.measured_at - pf.posted_at)) / 3600.0 - %s)
        LIMIT 1
        """,
        (feed_item_id, float(hours)),
    )
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
    """Save policy version to PostgreSQL. Returns version hash."""
    version = policy_version(policy_dict)
    execute(
        """
        INSERT INTO policies
            (version, created_at, created_by, parent_version, yaml_content, json_blob, applies_to, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft')
        ON CONFLICT (version) DO UPDATE SET
            created_at = EXCLUDED.created_at,
            created_by = EXCLUDED.created_by,
            parent_version = EXCLUDED.parent_version,
            yaml_content = EXCLUDED.yaml_content,
            json_blob = EXCLUDED.json_blob,
            applies_to = EXCLUDED.applies_to,
            status = EXCLUDED.status
        """,
        (
            version,
            datetime.now(UTC),
            created_by,
            parent_version,
            yaml_content,
            Json(policy_dict),
            applies_to,
        ),
    )
    log.info("policy_saved", version=version, applies_to=applies_to)
    return version


def log_autopilot_event(event_type: str, experiment_id: int | None = None, details: dict | None = None) -> None:
    """Log an autopilot event (for kill switch, rate limiter, dashboard)."""
    execute(
        """
        INSERT INTO autopilot_events (event_at, event_type, experiment_id, details)
        VALUES (%s, %s, %s, %s)
        """,
        (
            datetime.now(UTC),
            event_type,
            experiment_id,
            Json(details) if details else None,
        ),
    )
