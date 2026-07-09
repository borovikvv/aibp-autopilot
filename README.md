# AIBP Autopilot

> Автопилот для Telegram-канала **@AI_Business_Pulse**. Чистая Python-архитектура с self-learning модулем.
>
> Разворачивается одной командой через Hermes Agent.

## Что это

Полностью автономная система ведения Telegram-канала:

- **Сбор источников** — RSS-фиды через `feedparser`
- **Enrichment** — LLM-классификация и скоринг через OpenRouter (Claude/GPT)
- **Генерация постов** — LLM с детерминированным quality gate (regex-валидация)
- **Публикация** — Python `aiogram` + cron polling
- **Self-learning** — замкнутый цикл «измерение → анализ → shadow test → promote/rollback»
- **Observability** — structured logging + HTML dashboard + Telegram alerts

Архитектура: 6 слоёв, каждый читает/пишет в PostgreSQL через прямой `psycopg2` (без n8n DB gateway хака). Связь между слоями — через статусы строк в БД (transactional outbox pattern).

## Быстрый старт (для Hermes Agent)

**Самый простой путь:** дай Hermes Agent промпт из `prompts/hermes_bootstrap.md` — он развернёт всё сам, включая PostgreSQL.

```bash
# 1. Клонировать (или пусть Hermes сделает это сам)
git clone https://github.com/borovikvv/aibp-autopilot.git
cd aibp-autopilot

# 2. Запустить bootstrap (установит Python-зависимости, PostgreSQL, инициализирует БД)
python3 scripts/bootstrap.py

# 3. Заполнить секреты в .env (TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, etc.)
nano .env

# 4. Повторно проверить
python3 scripts/bootstrap.py --skip-postgres

# 5. Зарегистрировать cron-джобы в Hermes (см. prompts/hermes_bootstrap.md)
```

Перед стартом нужны:
- **TELEGRAM_BOT_TOKEN** (от @BotFather)
- **OPENROUTER_API_KEY** (от https://openrouter.ai/keys)
- ID обоих каналов (prod и test), бот должен быть админом в обоих

После этого система работает автономно. См. `docs/install.md` для подробностей.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│  PostgreSQL (canonical contract)                                 │
│  feed_items, post_features, experiments_log, policies, ...      │
└───────────────▲─────────────────────────────────────────────────┘
                │
   ┌────────────┴────────────────────────────────────────────┐
   │                                                          │
┌──┴──────────────┐  ┌──────────────────┐  ┌────────────────┴──┐
│ Layer 1         │  │ Layer 2          │  │ Layer 3           │
│ RSS Collector   │→ │ Enrichment       │→ │ Generation        │
│ (feedparser)    │  │ (OpenRouter LLM) │  │ (LLM + quality    │
│ cron hourly     │  │ cron every 2h    │  │  gate + image)    │
└─────────────────┘  └──────────────────┘  │ cron 10:00/18:00  │
                                           └────────┬───────────┘
                                                    │
                                           ┌────────▼───────────┐
                                           │ Layer 4            │
                                           │ Publisher          │
                                           │ (aiogram polling)  │
                                           │ cron every 5 min   │
                                           └────────┬───────────┘
                                                    │
                  ┌─────────────────────────────────┴────────────┐
                  │                                              │
          ┌───────▼────────┐                          ┌──────────▼────────┐
          │ Layer 5        │                          │ Layer 6           │
          │ Self-Learning  │─── policy.yaml ─────────►│ Observability     │
          │ (engagement    │                          │ (structlog +      │
          │  collector,    │                          │  dashboard +      │
          │  miner,        │                          │  alerts)          │
          │  shadow test,  │                          └───────────────────┘
          │  decision)     │
          └────────────────┘
```

Слои связаны только через БД и `policy.yaml`. Любой слой можно перезапустить без потери данных.

## Структура проекта

```
aibp-autopilot/
├── src/aibp/                    # основной пакет
│   ├── db/                      # Layer 0: DB connection + migrations
│   │   ├── connection.py        #   psycopg2 connection pool
│   │   ├── init_db.py           #   initial schema (schema.sql)
│   │   ├── migrate.py           #   migration runner
│   │   └── migrations/          #   NNNN_*.py migration files
│   ├── collectors/              # Layer 1: RSS sources
│   ├── enrichment/              # Layer 2: LLM classification
│   ├── generation/              # Layer 3: post writing + quality gate
│   ├── publishing/              # Layer 4: Telegram publisher
│   ├── self_learning/           # Layer 5: engagement + autopilot
│   ├── observability/           # Layer 6: logging + alerts + dashboard
│   ├── utils/                   # shared utilities
│   └── templates/               # Jinja2 templates (prompts, dashboard)
├── prompts/                     # Hermes cron prompts (Markdown)
│   └── hermes_bootstrap.md      #   ← главный промпт для разворачивания
├── presets/ai_business_pulse/   # channel-specific config
├── tests/                       # pytest
├── scripts/
│   ├── bootstrap.py             # ← оркестратор установки (deps + PG + DB)
│   ├── setup_postgres.sh        # ← авто-установка PostgreSQL
│   └── update.sh                # ← git pull + deps + migrations
├── docker/                      # Dockerfile + compose
├── docs/                        # documentation + ADRs
│   ├── install.md               #   установка
│   ├── updating.md              #   обновление (после коммитов в GitHub)
│   └── adr/                     #   Architecture Decision Records
├── config/                      # default configs (policy.yaml, rss_feeds.yaml, schema.sql)
├── Makefile                     # all common commands (bootstrap, update, ...)
└── .env.example                 # template for secrets
```

## Документация

- `docs/install.md` — пошаговая установка через Hermes Agent
- `docs/updating.md` — **как обновлять проект на сервере после коммитов в GitHub**
- `docs/adr/` — Architecture Decision Records
- `prompts/hermes_bootstrap.md` — промпт для Hermes Agent (полное разворачивание)

## Обновление после коммитов в GitHub

После того как ты запушил изменения в GitHub, на сервере выполни:

```bash
cd /root/aibp-autopilot
make update
```

Это:
1. `git pull` — подтянет новый код
2. `pip install` — если изменился `requirements.txt`
3. `python3 -m aibp.db.migrate` — применит миграции БД
4. Smoke-test — проверит что всё работает

**Что сохраняется** (не затрагивается `git pull`, в `.gitignore`):
- `.env` (твои секреты)
- `reports/` (логи, дашборды)

**Что нужно делать вручную:**
- Если изменилось расписание cron-джоб в `prompts/hermes_bootstrap.md` — попроси Hermes перерегистрировать их

См. `docs/updating.md` для подробностей и примеров.

## Текущий статус

🚧 **В разработке.** См. milestones в GitHub Issues.
