# Установка AIBP Autopilot через Hermes Agent

> Эта инструкция написана так, чтобы Hermes Agent мог выполнить её
> самостоятельно — шаг за шагом, с проверкой каждого этапа.

## Предварительные требования

На сервере должны быть установлены:
- Python 3.11+
- PostgreSQL 14+ (или Docker для запуска в контейнере)
- Git
- Hermes Agent (если ещё не установлен)

## Шаг 1: Клонирование репозитория

```bash
cd /root
git clone https://github.com/borovikvv/aibp-autopilot.git
cd aibp-autopilot
```

## Шаг 2: Установка Python-зависимостей

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

## Шаг 3: Настройка окружения

```bash
cp .env.example .env
```

Отредактируйте `.env` — обязательно заполнить:

| Переменная | Где получить |
|------------|--------------|
| `TELEGRAM_BOT_TOKEN` | @BotFather → /newbot или выбери существующего |
| `TELEGRAM_CHANNEL_ID_PROD` | ID канала @AI_Business_Pulse (с -100 префиксом) |
| `TELEGRAM_CHANNEL_ID_TEST` | ID тестового канала |
| `TELEGRAM_ALERT_CHAT_ID` | Твой личный chat_id (для алертов) |
| `DATABASE_URL` | `postgresql://aibp:PASSWORD@localhost:5432/aibp` |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| `XAI_API_KEY` | https://x.ai/api (опционально, для генерации картинок) |

Бот должен быть **админом** в обоих каналах.

## Шаг 4: Инициализация базы данных

### Вариант A: PostgreSQL в Docker (проще)

```bash
# Запустить PostgreSQL
docker-compose -f docker/docker-compose.yml up -d postgres

# Дождаться запуска
sleep 10

# Инициализировать схему
make db-init
```

### Вариант B: Существующий PostgreSQL

```bash
# Создать БД и пользователя (если ещё нет)
sudo -u postgres createuser aibp -P
sudo -u postgres createdb aibp -O aibp

# Инициализировать схему
make db-init
```

## Шаг 5: Smoke-тест

```bash
make smoke-test
```

Ожидаемый вывод:
```
Checking PostgreSQL... ✅ OK
Checking Telegram bot token... ✅ OK — @YourBotName
Checking OpenRouter API key... ✅ OK
Checking policy.yaml... ✅ OK

✅ All checks passed. System ready.
```

Если что-то не получилось — проверь `.env` и что БД запущена.

## Шаг 6: Ручной тест пайплайна

Запусти полный цикл вручную, чтобы убедиться что всё работает:

```bash
# 1. Собрать RSS-источники
make collect-rss

# 2. Обогатить через LLM
make enrich

# 3. Сгенерировать утренний пост
make generate-morning

# 4. Опубликовать (проверит очередь и отправит в Telegram)
make publish
```

После `make publish` проверь тестовый канал — там должен появиться пост.

## Шаг 7: Регистрация cron-джоб в Hermes Agent

Создай следующие cron-джобы в Hermes (через `cronjob(action='create', ...)`):

```python
# RSS Collector — каждый час
cronjob(
    action='create',
    name='AIBP — RSS Collector',
    schedule='0 * * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli collect-rss',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Enrichment — каждые 2 часа
cronjob(
    action='create',
    name='AIBP — Enrichment',
    schedule='30 */2 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli enrich',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Morning generation — 09:00 MSK (06:00 UTC)
cronjob(
    action='create',
    name='AIBP — Morning Generation',
    schedule='0 6 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli generate --slot morning',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Evening generation — 17:00 MSK (14:00 UTC)
cronjob(
    action='create',
    name='AIBP — Evening Generation',
    schedule='0 14 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli generate --slot evening',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Publisher — каждые 5 минут
cronjob(
    action='create',
    name='AIBP — Publisher',
    schedule='*/5 * * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli publish',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Engagement Collector — каждые 4 часа
cronjob(
    action='create',
    name='AIBP — Engagement Collector',
    schedule='0 */4 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli collect-engagement',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Pattern Miner — Sundays 22:00 MSK (19:00 UTC)
cronjob(
    action='create',
    name='AIBP — Pattern Miner',
    schedule='0 19 * * 0',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli mine-patterns',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Policy Updater — Mondays 02:00 MSK (23:00 UTC Sunday)
cronjob(
    action='create',
    name='AIBP — Policy Updater',
    schedule='0 23 * * 0',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli update-policy',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Shadow Runner — daily 02:30 MSK (23:30 UTC)
cronjob(
    action='create',
    name='AIBP — Shadow Runner',
    schedule='30 23 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli run-shadow',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Decision Engine — daily 03:00 MSK (00:00 UTC)
cronjob(
    action='create',
    name='AIBP — Decision Engine',
    schedule='0 0 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli decide',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Rollback Check — daily 04:00 MSK (01:00 UTC)
cronjob(
    action='create',
    name='AIBP — Rollback Check',
    schedule='0 1 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli rollback-check',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Safety Check — daily 05:00 MSK (02:00 UTC)
cronjob(
    action='create',
    name='AIBP — Safety Check',
    schedule='0 2 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli safety-check',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Dashboard — daily 06:00 MSK (03:00 UTC)
cronjob(
    action='create',
    name='AIBP — Dashboard',
    schedule='0 3 * * *',
    prompt='Run: cd /root/aibp-autopilot && source .venv/bin/activate && python3 -m aibp.cli dashboard',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)
```

## Шаг 8: Проверка

Через 1 час проверь:
- `reports/logs/` — должны быть логи cron-джоб
- Тестовый Telegram-канал — должен быть новый пост (если были RSS-источники)
- `data/self_learning.db` — должна существовать SQLite база

Через 24 часа:
- Дашборд должен быть доступен (если настроен Caddy)
- В `experiments_log` могут появиться первые эксперименты (после pattern miner'а)

## Что делать если что-то сломалось

### Autopilot остановился (kill switch)

```bash
# Проверить статус
python3 -m aibp.cli safety-check

# Возобновить (только после ручной проверки!)
make resume-autopilot
```

### Посты не публикуются

```bash
# Проверить очередь
psql $DATABASE_URL -c "SELECT * FROM v_publisher_queue;"

# Проверить последние ошибки
psql $DATABASE_URL -c "SELECT id, title, publish_error FROM feed_items WHERE publish_error IS NOT NULL ORDER BY updated_at DESC LIMIT 5;"
```

### LLM-бюджет превышен

Проверь `reports/llm_cost.jsonl` — там все затраты. При необходимости увеличь `OPENROUTER_DAILY_BUDGET_USD` в `.env`.

## Резервное копирование

```bash
# Ежедневный бэкап (добавь в crontab)
pg_dump $DATABASE_URL | gzip > backups/aibp_$(date +%Y%m%d).sql.gz

# Бэкап SQLite (эксперименты)
cp data/self_learning.db backups/self_learning_$(date +%Y%m%d).db
```

Храни бэкапы off-site (S3, другой сервер).
