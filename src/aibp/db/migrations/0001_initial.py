"""Migration 0001: Initial schema.

Creates all base tables. This is the same as config/schema.sql but
tracked as a migration so future changes are versioned.

For NEW installations: config/schema.sql is applied first (idempotent),
then this migration is marked as already applied.

For EXISTING installations (before migrations existed): this migration
is a no-op (everything already exists via IF NOT EXISTS).
"""
from __future__ import annotations


def up(conn) -> None:
    """Apply migration — no-op, schema.sql handles initial creation."""
    # All tables are created by config/schema.sql with CREATE IF NOT EXISTS.
    # This migration exists only to mark the baseline.
    pass


def down(conn) -> None:
    """Rollback — not supported for baseline."""
    raise NotImplementedError("Cannot rollback baseline migration")
