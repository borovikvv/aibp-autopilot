# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Agent OS — контракт
Прочитай и выполняй агентский контракт (`contract_version: 1.0`): локально `~/my-wiki/topics/agent-contract.md`, удалённо `https://wiki.borovikvv.ru/topics/agent-contract`. Контекст проекта — в wiki: `projects/ai-business-pulse/`; значимые результаты сессии сохраняй по контракту в `raw/new/`.

## Repository location

The git repo and all code live in `aibp-autopilot/` (on this Mac: `/Users/borovikvyacheslav/Projects/active/aibp-autopilot`). **All `make`/`python3`/`git` commands must run from inside `aibp-autopilot/`.** In production the same tree lives at `/root/aibp-autopilot`.

`AGENTS.md` (Russian) holds the **production operator rules and hard prohibitions** for the live VPS — read it before doing anything that touches the running system, cron jobs, or `config/policy.yaml`.

## Commands

Python is used **system-wide, no active venv assumed**. `.venv/` exists but jobs run under the system interpreter.

```bash
make install-dev      # deps + editable install with [dev] extras
make test             # full pytest suite (unit + whatever runs without a live DB)
make test-integration # needs live PostgreSQL WITH pgvector; see below
make lint             # ruff check src/ tests/
make lint-fix         # ruff --fix
make typecheck        # mypy src/aibp/ (non-strict)
make smoke-test       # verify DB + Telegram + OpenRouter + policy.yaml reachable
```

Run a single test: `python3 -m pytest tests/unit/test_reward.py -v` or a single case with `-k test_name`. Default pytest opts (`-v --tb=short`) come from `pyproject.toml`.

Integration tests need Postgres **with the `vector` extension** (migration 0010 uses `pgvector`):
```bash
docker run --rm -e POSTGRES_DB=aibp_test -e POSTGRES_USER=aibp -e POSTGRES_PASSWORD=aibp -p 5432:5432 pgvector/pgvector:pg16
TEST_DATABASE_URL=postgresql://aibp:aibp@localhost:5432/aibp_test make test-integration
```

Ruff config lives in `pyproject.toml`: line length 120, rules `E,F,W,I,N,B,UP`. Several pipeline/prompt files are exempted from `E501` because they contain long inline Jinja2 prompts — keep prompt text inline rather than reflowing it to satisfy the linter. Pre-commit runs `ruff --fix` on `src/` and `tests/`.

The CLI is the single entry point for every pipeline stage: `python3 -m aibp.cli <command>` (installed as `aibp`). Every `make` pipeline target is a thin wrapper over a CLI command — see `src/aibp/cli.py`.

## Architecture

Fully autonomous manager of the Telegram channel **@AI_Business_Pulse**. Six layers, each a self-contained CLI command run on its own cron schedule. **Layers never call each other directly** — they communicate only through PostgreSQL row state (transactional-outbox pattern) and `config/policy.yaml`. Any layer can be re-run independently without data loss.

```
RSS collect → enrich (LLM) → generate (LLM + quality gate + image) → publish (aiogram)
                                                                          │
                          self-learning loop  ◄──── engagement metrics ───┘
                          (measure → mine → shadow test → decide → promote/rollback)
                                     │ writes
                                     ▼
                              config/policy.yaml  ──► read by generation + quality gate
```

- **`src/aibp/db/`** — the canonical contract. Direct `psycopg2` with a `ThreadedConnectionPool`; use the `db_conn()` context manager and the `fetch_one/fetch_all/execute/execute_returning` helpers in `connection.py`. No ORM, no async DB. Schema is owned by numbered migrations, **not** `config/schema.sql`.
- **`collectors/`** (Layer 1) — RSS via `feedparser` → `feed_items`.
- **`enrichment/`** (Layer 2) — LLM classification/scoring + `audience_relevance` (1–5, weights RU/CIS relevance) → `post_features`.
- **`generation/`** (Layer 3) — `pipeline.py` renders Jinja2 prompts from `policy.yaml`, then a **two-stage gate**: (1) deterministic regex `quality_gate.py` (core `FORBIDDEN_RE`/`CLICHE_RE` patterns are hard-coded and autopilot cannot touch them; `policy.yaml regex_gates` only *adds* patterns), then (2) `llm_editor.py` — a holistic LLM review that **degrades open** (an infra failure still publishes the regex-validated post). Failures trigger an informed retry. `image_gen.py` renders per-post images via OpenRouter.
- **`publishing/`** (Layer 4) — `aiogram` publisher, cron-polled every 5 min; publishes posts whose row state is due.
- **`self_learning/`** (Layer 5) — the autopilot loop: `engagement_collector` → `pattern_miner` → `policy_updater` → `shadow_runner` → `decision_engine` (`bandit.py` Bayesian/bootstrap criterion, ADR-0008) → `auto_rollback`, gated by `safety.py` and `approvals.py`. Reward is a composite (views/forwards/clicks/subs) measured at a fixed **48h horizon** (`ENGAGEMENT_HORIZON_HOURS`) so posts of different ages compare fairly.
- **`growth/`, `monetization/`, `tracking/`, `observability/`** — competitor scraping, offers/CPA + invite-link attribution, the click-redirect service, and structlog + HTML dashboard + Telegram alerts.

### Environment split (prod / stage / interleave) — read `utils/config.py` + ADR-0007

Three distinct things, easy to conflate:
- **prod** → main channel, `config/policy.yaml`.
- **stage** → test channel, `config/policy.stage.yaml`. This is a **human-QA preview policy only** — it does *not* drive statistics.
- **interleave variant** → the actual experiment. `self_learning/interleave.py` alternates control/variant policies by day-of-year parity **within the main channel** (cross-channel comparison is statistically invalid). The variant policy is loaded from the PostgreSQL `policies` table, *not* from `policy.stage.yaml`.

### policy.yaml is the single source of truth

`config/policy.yaml` drives generation prompts, the quality gate's dynamic patterns, and source scoring; the self-learning loop *writes back* to it (versioned by sha256, stored in the `policies` table). The `safety:` section (reward weights, rate limits, rollback thresholds, approval tiers) and `autopilot_paused` are **guardrails autopilot must never modify** — see `AGENTS.md`.

### LLM routing is split by cost (see `AGENTS.md` "LLM-роутинг")

Flagship (`anthropic/claude-sonnet-5` via OpenRouter) for **post generation and pattern mining**; a cheap model (`deepseek-v4-flash`, routed through the opencode-zen gateway when `OPENCODE_API_KEY` is set, else OpenRouter fallback) for **high-volume enrichment and dedup**. All settings resolve through `Settings.from_env()` in `utils/config.py`. A daily LLM budget (`OPENROUTER_DAILY_BUDGET_USD`) caps spend — when exhausted, generation silently produces nothing rather than erroring.

## Database migrations

Schema changes go in **numbered migrations only** — never edit `config/schema.sql` for schema evolution. Add `src/aibp/db/migrations/NNNN_description.py` with `up(conn)` and (strongly preferred) `down(conn)` functions operating on a raw psycopg2 connection.

```bash
make migrate          # apply all pending
make migrate-status   # show applied/pending
python3 -m aibp.db.migrate --rollback NNNN   # roll back one
```

`make update` (production update after a push) runs `git pull` + deps + `migrate` + smoke-test.

## Gotchas

- **Stale SQLite references.** Self-learning state was consolidated from a standalone SQLite DB into the main PostgreSQL store (migration 0009, issue #43). Some docstrings (e.g. in `interleave.py`, `utils/config.py`) still say "SQLite" — **the canonical store is PostgreSQL everywhere.** Don't reintroduce SQLite.
- **`/usr/bin/python3` in cron wrappers.** On the VPS, bare `python3` resolves into the Hermes venv where the `aibp` package isn't installed. Cron wrappers must use `/usr/bin/python3` (see `AGENTS.md`).
- DB helpers commit on success and roll back on exception automatically — don't add manual commit/rollback around `db_conn()`.
- `.env` and `reports/` are gitignored (secrets + generated output); `src/aibp_autopilot.egg-info/` is a build artifact.

## Decision records

Non-obvious design choices are documented in `docs/adr/` (indexed in `docs/adr/README.md`) — e.g. direct psycopg2 over an n8n gateway (0002), sync psycopg2 inside async as wontfix (0006), interleaving (0007), Bayesian decision criterion (0008), composite reward (0010). Consult the relevant ADR before changing self-learning behavior. **Architecture, new logic, and self-learning thresholds are owner-approval decisions** (open a GitHub Issue); only small fixes go straight to `main`.

## Wiki Knowledge Base

Проект документирован в wiki: https://wiki.borovikvv.ru

**Новая система (aibp-autopilot):**
- `/projects/ai-business-pulse/overview` — обзор проекта, статус миграции
- `/projects/ai-business-pulse/decisions/structure-as-guardrails` — ADR: невидимый каркас
- `/projects/ai-business-pulse/chats/claude-content-review-sessions` — ревью и рекомендации

**Старая система (ai-business-pulse-hermes) — legacy:**
- `/projects/ai-business-pulse/legacy/` — документация, аудит, история багов

**Как пользоваться:**
```bash
# Поиск по wiki
curl -s "https://wiki.borovikvv.ru/api/search?q={запрос}"

# Создать/обновить страницу
curl -X PUT https://wiki.borovikvv.ru/api/wiki/{path} \
  -H 'Content-Type: application/json' \
  -d '{"title": "...", "content": "..."}'

# Перестроить поиск после изменений
curl -s -X POST https://wiki.borovikvv.ru/api/index/reindex
```
