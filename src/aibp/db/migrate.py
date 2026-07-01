"""Migration runner — applies pending migrations to PostgreSQL.

Each migration is a Python module in src/aibp/db/migrations/ named NNNN_*.py
with up(conn) and optionally down(conn) functions.

Migrations are tracked in `_migrations` table:
    - id (serial)
    - name (text, e.g. "0001_initial")
    - applied_at (timestamptz)

Usage:
    python3 -m aibp.db.migrate           # apply all pending
    python3 -m aibp.db.migrate --status  # show applied/pending
    python3 -m aibp.db.migrate --rollback NNNN  # rollback one migration
"""
from __future__ import annotations

import importlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

from aibp.db.connection import db_conn
from aibp.db.init_db import init_db

log = structlog.get_logger()

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _ensure_migrations_table() -> None:
    """Create _migrations table if not exists."""
    with db_conn() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                id          serial PRIMARY KEY,
                name        text UNIQUE NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
        """)


def _get_applied_migrations() -> set[str]:
    """Get set of already-applied migration names."""
    with db_conn() as cur:
        cur.execute("SELECT name FROM _migrations ORDER BY name")
        return {row["name"] for row in cur.fetchall()}


def _discover_migrations() -> list[str]:
    """Discover all migration modules sorted by number."""
    if not MIGRATIONS_DIR.exists():
        return []
    names = []
    for f in MIGRATIONS_DIR.glob("*.py"):
        if f.name.startswith("_") or f.name == "__init__.py":
            continue
        names.append(f.stem)  # e.g. "0001_initial"
    return sorted(names)


def _import_migration(name: str):
    """Import migration module by name."""
    return importlib.import_module(f"aibp.db.migrations.{name}")


def apply_migrations() -> list[str]:
    """Apply all pending migrations. Returns list of applied names."""
    # Ensure base schema exists first
    init_db()
    _ensure_migrations_table()

    applied = _get_applied_migrations()
    all_migrations = _discover_migrations()
    pending = [m for m in all_migrations if m not in applied]

    if not pending:
        log.info("no_pending_migrations", total=len(all_migrations), applied=len(applied))
        return []

    log.info("applying_migrations", pending=len(pending))
    newly_applied = []

    for name in pending:
        log.info("applying", migration=name)
        try:
            module = _import_migration(name)
            with db_conn() as cur:
                # Run migration
                module.up(cur.connection)
                # Record as applied
                cur.execute(
                    "INSERT INTO _migrations (name, applied_at) VALUES (%s, %s)",
                    (name, datetime.now(timezone.utc)),
                )
            newly_applied.append(name)
            log.info("applied", migration=name)
        except Exception as e:
            log.error("migration_failed", migration=name, error=str(e))
            raise

    log.info("migrations_complete", applied=len(newly_applied))
    return newly_applied


def rollback_migration(name: str) -> bool:
    """Rollback one migration by name."""
    applied = _get_applied_migrations()
    if name not in applied:
        log.error("not_applied", migration=name)
        return False

    try:
        module = _import_migration(name)
        with db_conn() as cur:
            module.down(cur.connection)
            cur.execute("DELETE FROM _migrations WHERE name = %s", (name,))
        log.info("rolled_back", migration=name)
        return True
    except NotImplementedError as e:
        log.error("rollback_not_supported", migration=name, error=str(e))
        return False
    except Exception as e:
        log.error("rollback_failed", migration=name, error=str(e))
        return False


def show_status() -> int:
    """Print migration status. Returns count of pending."""
    _ensure_migrations_table()
    applied = _get_applied_migrations()
    all_migrations = _discover_migrations()
    pending = [m for m in all_migrations if m not in applied]

    print(f"\n{'Migration':<35} {'Status':<10}")
    print("-" * 50)
    for m in all_migrations:
        status = "✅ applied" if m in applied else "⏳ pending"
        print(f"{m:<35} {status:<10}")

    print(f"\nTotal: {len(all_migrations)}, Applied: {len(applied)}, Pending: {len(pending)}")
    return len(pending)


def main() -> int:
    """CLI entry point."""
    if "--status" in sys.argv:
        return show_status()

    if "--rollback" in sys.argv:
        idx = sys.argv.index("--rollback")
        if idx + 1 >= len(sys.argv):
            print("Usage: python -m aibp.db.migrate --rollback NNNN_name")
            return 1
        name = sys.argv[idx + 1]
        return 0 if rollback_migration(name) else 1

    # Default: apply all pending
    applied = apply_migrations()
    if not applied:
        print("✅ All migrations already applied")
    else:
        print(f"✅ Applied {len(applied)} migrations: {', '.join(applied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
