# ADR-0004: Update workflow with migrations

**Status:** Accepted
**Date:** 2026-06-30

## Context

After initial deployment, the project will continue to evolve. New commits will be pushed to GitHub, and the production server needs a way to pull and apply these updates safely. Key concerns:

1. Database schema changes (new columns, new tables) must be tracked and applied incrementally
2. Python dependencies may change (`requirements.txt`)
3. Cron jobs in Hermes may need re-registration (manual step)
4. Local server-specific files (`.env`, `data/`, `reports/`) must be preserved during updates
5. Updates must be safe to run on a live system without downtime

## Decision

Implement a 3-component update workflow:

### 1. Migration framework (lightweight, no external deps)

- Migrations are Python modules in `src/aibp/db/migrations/NNNN_*.py`
- Each module has `up(conn)` and optional `down(conn)` functions
- Tracked in `_migrations` table in PostgreSQL
- Runner: `python3 -m aibp.db.migrate`
- No Alembic/Yuniql — keeps dependencies minimal, follows existing "stdlib first" principle

### 2. Update script (`scripts/update.sh`)

Single command that:
- `git fetch` + check for updates
- `git stash` local changes (preserves uncommitted work)
- `git pull`
- `git stash pop` (restores local changes)
- `pip install` if `requirements.txt` changed
- `python3 -m aibp.db.migrate` (apply pending migrations)
- `python3 -m aibp.cli smoke-test` (verify)

### 3. Makefile target

`make update` runs `scripts/update.sh`. Also:
- `make update-check` — check for updates without applying
- `make update-cron` — flag to also re-register Hermes cron jobs
- `make migrate` — apply only migrations
- `make migrate-status` — show applied/pending migrations

## What is preserved during update

Files in `.gitignore` are never touched by `git pull`:
- `.env` (secrets)
- `data/` (SQLite experiments DB)
- `reports/` (logs, dashboards)
- `backups/` (DB backups)

## What requires manual action

- **Hermes cron jobs** — if schedule changes, must re-register via Hermes Agent
- **`.env`** — if new env vars added to `.env.example`, must manually copy them to `.env`

## Alternatives considered

1. **Alembic** (SQLAlchemy migrations) — rejected: too heavy for this project, adds dependency
2. **Raw SQL migrations** — rejected: Python migrations are more flexible (conditional logic, data migrations)
3. **Docker-based deployment with image pulls** — considered for future, but adds complexity for now
4. **Zero-downtime blue-green deployment** — overkill for single-server cron-based system

## Consequences

- Each DB schema change requires a new migration file (small overhead)
- Migrations must be idempotent (defensive coding with IF NOT EXISTS checks)
- Rollback is possible but not automatic — requires manual `--rollback` command
- Update is safe to run anytime (cron jobs continue working with old code until next invocation)
