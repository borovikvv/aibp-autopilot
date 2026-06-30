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

```bash
# 1. Клонировать
git clone https://github.com/borovikvv/aibp-autopilot.git
cd aibp-autopilot

# 2. Установить зависимости
make install

# 3. Скопировать .env и заполнить секретами
cp .env.example .env
# Отредактировать .env — вписать TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, DATABASE_URL

# 4. Инициализировать БД
make db-init

# 5. Запустить smoke-тест
make smoke-test

# 6. Зарегистрировать cron-джобы в Hermes
make hermes-register
```

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
│   ├── collectors/              # Layer 1: RSS sources
│   ├── enrichment/              # Layer 2: LLM classification
│   ├── generation/              # Layer 3: post writing + quality gate
│   ├── publishing/              # Layer 4: Telegram publisher
│   ├── self_learning/           # Layer 5: engagement + autopilot
│   ├── observability/           # Layer 6: logging + alerts + dashboard
│   ├── utils/                   # shared utilities
│   └── templates/               # Jinja2 templates (prompts, dashboard)
├── prompts/                     # Hermes cron prompts (Markdown)
├── presets/ai_business_pulse/   # channel-specific config
├── tests/                       # pytest
├── scripts/                     # CLI helpers
├── docker/                      # Dockerfile + compose
├── docs/                        # documentation + ADRs
├── config/                      # default configs
├── Makefile                     # all common commands
└── .env.example                 # template for secrets
```

## Документация

- `docs/install.md` — пошаговая установка через Hermes Agent
- `docs/architecture.md` — детальная архитектура
- `docs/adr/` — Architecture Decision Records
- `docs/operations.md` — рунбук (что делать при сбоях)
- `docs/self_learning.md` — как работает самообучение

## Текущий статус

🚧 **В разработке.** См. milestones в GitHub Issues.
