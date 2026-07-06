"""Summary parsing utility — DRY helper for jsonb summary field.

The `feed_items.summary` column is jsonb, but psycopg2 may return it as
dict (if jsonb) or str (if cast to text). This helper normalizes both.
"""
from __future__ import annotations

import json
from typing import Any


def parse_summary(summary: Any) -> dict:
    """Parse summary field from feed_items row.

    Handles:
        - dict (psycopg2 returns jsonb as dict by default)
        - str (valid JSON) → parse
        - str (invalid JSON) → return {}
        - None → return {}

    Args:
        summary: value from feed_items.summary column

    Returns:
        dict with summary data (possibly empty)
    """
    if summary is None:
        return {}
    if isinstance(summary, dict):
        return summary
    if isinstance(summary, str):
        stripped = summary.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    # Any other type → empty dict
    return {}
