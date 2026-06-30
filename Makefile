# AIBP Autopilot — Makefile
# Common commands for development and operations

.PHONY: install db-init db-check smoke-test test lint typecheck
.PHONY: collect-rss enrich generate-morning generate-evening publish
.PHONY: collect-engagement mine-patterns update-policy run-shadow decide rollback-check safety-check dashboard
.PHONY: hermes-register docker-build docker-up docker-down

PYTHON := python3
PIP := pip3

# ─── Setup ──────────────────────────────────────────────────────────

install:
        $(PIP) install -r requirements.txt
        $(PIP) install -e .

install-dev:
        $(PIP) install -r requirements.txt
        $(PIP) install -e ".[dev]"

# Full bootstrap — installs deps + PostgreSQL + initializes DB
bootstrap:
        $(PYTHON) scripts/bootstrap.py

bootstrap-docker:
        $(PYTHON) scripts/bootstrap.py --docker

bootstrap-check:
        $(PYTHON) scripts/bootstrap.py --check-only

setup-postgres:
        bash scripts/setup_postgres.sh

setup-postgres-docker:
        bash scripts/setup_postgres.sh --docker

# ─── Database ───────────────────────────────────────────────────────

db-init:
        $(PYTHON) -m aibp.cli db-init

db-check:
        $(PYTHON) -m aibp.cli smoke-test

# ─── Smoke test ─────────────────────────────────────────────────────

smoke-test:
        $(PYTHON) -m aibp.cli smoke-test

# ─── Tests ──────────────────────────────────────────────────────────

test:
        $(PYTHON) -m pytest tests/ -v --tb=short

test-cov:
        $(PYTHON) -m pytest tests/ --cov=aibp --cov-report=term-missing

lint:
        $(PYTHON) -m ruff check src/ tests/

typecheck:
        $(PYTHON) -m mypy src/aibp/

# ─── Pipeline commands ──────────────────────────────────────────────

collect-rss:
        $(PYTHON) -m aibp.cli collect-rss

enrich:
        $(PYTHON) -m aibp.cli enrich

generate-morning:
        $(PYTHON) -m aibp.cli generate --slot morning

generate-evening:
        $(PYTHON) -m aibp.cli generate --slot evening

generate-weekly:
        $(PYTHON) -m aibp.cli generate --slot weekly_digest

publish:
        $(PYTHON) -m aibp.cli publish

# ─── Self-Learning ──────────────────────────────────────────────────

collect-engagement:
        $(PYTHON) -m aibp.cli collect-engagement

mine-patterns:
        $(PYTHON) -m aibp.cli mine-patterns

update-policy:
        $(PYTHON) -m aibp.cli update-policy

run-shadow:
        $(PYTHON) -m aibp.cli run-shadow

decide:
        $(PYTHON) -m aibp.cli decide

rollback-check:
        $(PYTHON) -m aibp.cli rollback-check

safety-check:
        $(PYTHON) -m aibp.cli safety-check

dashboard:
        $(PYTHON) -m aibp.cli dashboard

resume-autopilot:
        $(PYTHON) -m aibp.cli resume-autopilot

# ─── Hermes Agent registration ─────────────────────────────────────

hermes-register:
        @echo "Registering cron jobs in Hermes Agent..."
        @echo "Run this on the server where Hermes is installed:"
        @echo "  python3 scripts/register_hermes_cron.py"
        @echo "Or copy commands from docs/install.md"

# ─── Docker ─────────────────────────────────────────────────────────

docker-build:
        docker build -f docker/Dockerfile -t aibp-autopilot:latest .

docker-up:
        docker-compose -f docker/docker-compose.yml up -d

docker-down:
        docker-compose -f docker/docker-compose.yml down

# ─── Full pipeline run (manual) ─────────────────────────────────────

run-all: collect-rss enrich generate-morning publish
        @echo "✅ Full pipeline run complete"

# ─── Help ───────────────────────────────────────────────────────────

help:
        @echo "AIBP Autopilot — common commands:"
        @echo ""
        @echo "Setup:"
        @echo "  make bootstrap        — full setup (deps + PostgreSQL + DB init + smoke test)"
        @echo "  make bootstrap-docker — same but PostgreSQL in Docker"
        @echo "  make bootstrap-check  — verify current state without changes"
        @echo "  make setup-postgres   — only setup PostgreSQL (writes DATABASE_URL to .env)"
        @echo "  make install          — install Python dependencies only"
        @echo "  make db-init          — initialize databases (after PostgreSQL is set up)"
        @echo "  make smoke-test       — verify all components are reachable"
        @echo ""
        @echo "Pipeline:"
        @echo "  make collect-rss      — fetch RSS feeds"
        @echo "  make enrich           — LLM enrichment"
        @echo "  make generate-morning — write morning post"
        @echo "  make generate-evening — write evening post"
        @echo "  make publish          — publish due posts"
        @echo ""
        @echo "Self-Learning:"
        @echo "  make collect-engagement  — fetch TG views"
        @echo "  make mine-patterns       — weekly LLM analysis"
        @echo "  make update-policy       — create experiments"
        @echo "  make run-shadow          — start shadow tests"
        @echo "  make decide              — decision engine"
        @echo "  make rollback-check      — check for rollbacks"
        @echo "  make safety-check        — daily safety check"
        @echo "  make dashboard           — generate HTML dashboard"
        @echo ""
        @echo "Operations:"
        @echo "  make resume-autopilot  — resume after kill switch"
        @echo "  make hermes-register    — register cron jobs in Hermes"
