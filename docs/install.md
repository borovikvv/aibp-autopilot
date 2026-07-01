# Установка AIBP Autopilot через Hermes Agent

> **TL;DR:** Дай Hermes Agent промпт `prompts/hermes_bootstrap.md` — он развернёт всё сам.

## Быстрый старт (одна команда для Hermes)

После установки Hermes Agent на сервере, дай ему этот промпт:

```
Прочитай файл /root/aibp-autopilot/prompts/hermes_bootstrap.md
и выполни все шаги, описанные там.
```

Hermes выполнит:
1. Клонирование репозитория
2. Установку Python-зависимостей
3. **Установку и настройку PostgreSQL** (автоматически)
4. Инициализацию схемы БД
5. Smoke-тест (проверка DB, Telegram, OpenRouter)
6. Регистрацию 15 cron-джоб

Перед стартом у тебя должны быть готовы:
- **TELEGRAM_BOT_TOKEN** (от @BotFather)
- **OPENROUTER_API_KEY** (от https://openrouter.ai/keys)
- ID обоих каналов (prod и test)
- Бот должен быть **админом** в обоих каналах

---

## Ручная установка (если без Hermes)

### Шаг 1: Клонирование

```bash
cd /root
git clone https://github.com/borovikvv/aibp-autopilot.git
cd aibp-autopilot
```

### Шаг 2: Установка зависимостей

```bash
# Создать venv (рекомендуется)
python3 -m venv .venv
source .venv/bin/activate

# Или без venv (системный Python)
pip3 install --break-system-packages -r requirements.txt
pip3 install --break-system-packages -e .
```

### Шаг 3: Настройка PostgreSQL

**Вариант A: Автоматическая установка (Debian/Ubuntu)**

```bash
bash scripts/setup_postgres.sh
```

Скрипт сам:
- Установит PostgreSQL через apt (если не установлен)
- Создаст пользователя `aibp` и БД `aibp`
- Сгенерирует пароль и запишет `DATABASE_URL` в `.env`
- Проверит подключение

**Вариант B: PostgreSQL в Docker**

```bash
bash scripts/setup_postgres.sh --docker
```

**Вариант C: Существующий PostgreSQL**

Если у тебя уже есть PostgreSQL — создай БД и пользователя вручную:

```bash
sudo -u postgres createuser aibp -P
sudo -u postgres createdb aibp -O aibp
```

Затем впиши `DATABASE_URL` в `.env`:
```
DATABASE_URL=postgresql://aibp:ТВОЙ_ПАРОЛЬ@localhost:5432/aibp
```

И запусти:
```bash
bash scripts/setup_postgres.sh --no-install
```

### Шаг 4: Заполнение секретов

```bash
cp .env.example .env  # если ещё не создан
nano .env
```

Обязательно заполни:

| Переменная | Где получить |
|------------|--------------|
| `TELEGRAM_BOT_TOKEN` | @BotFather → /newbot или выбери существующего |
| `TELEGRAM_CHANNEL_ID_PROD` | ID канала @AI_Business_Pulse (с -100 префиксом) |
| `TELEGRAM_CHANNEL_ID_TEST` | ID тестового канала |
| `TELEGRAM_ALERT_CHAT_ID` | Твой личный chat_id (для алертов) |
| `DATABASE_URL` | Уже заполнен после Step 3 (если использовал setup_postgres.sh) |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys |
| `XAI_API_KEY` | https://x.ai/api (опционально) |

### Шаг 5: Инициализация БД

```bash
make db-init
```

Это создаст все таблицы в PostgreSQL + SQLite (для self-learning).

### Шаг 6: Smoke-тест

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

### Шаг 7: Ручной тест пайплайна

```bash
make run-all
```

Это запустит: RSS → Enrichment → Generation → Publish. Проверь тестовый канал — там должен появиться пост.

### Шаг 8: Регистрация cron-джоб

Если используешь Hermes Agent — см. `prompts/hermes_bootstrap.md` (Step 5).

Если без Hermes — добавь в системный crontab:

```bash
crontab -e
```

```cron
# RSS Collector — каждый час
0 * * * * cd /root/aibp-autopilot && python3 -m aibp.cli collect-rss >> reports/logs/cron.log 2>&1

# Enrichment — каждые 2 часа
30 */2 * * * cd /root/aibp-autopilot && python3 -m aibp.cli enrich >> reports/logs/cron.log 2>&1

# Morning Generation — 09:00 MSK (06:00 UTC)
0 6 * * * cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot morning >> reports/logs/cron.log 2>&1

# Evening Generation — 17:00 MSK (14:00 UTC)
0 14 * * * cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot evening >> reports/logs/cron.log 2>&1

# Publisher — каждые 5 минут
*/5 * * * * cd /root/aibp-autopilot && python3 -m aibp.cli publish >> reports/logs/cron.log 2>&1

# Engagement Collector — каждые 4 часа
0 */4 * * * cd /root/aibp-autopilot && python3 -m aibp.cli collect-engagement >> reports/logs/cron.log 2>&1

# Pattern Miner — Sundays 22:00 MSK
0 19 * * 0 cd /root/aibp-autopilot && python3 -m aibp.cli mine-patterns >> reports/logs/cron.log 2>&1

# Safety Check — daily 05:00 MSK
0 2 * * * cd /root/aibp-autopilot && python3 -m aibp.cli safety-check >> reports/logs/cron.log 2>&1

# Dashboard — daily 06:00 MSK
0 3 * * * cd /root/aibp-autopilot && python3 -m aibp.cli dashboard >> reports/logs/cron.log 2>&1
```

---

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

Проверь `reports/llm_cost.jsonl`. При необходимости увеличь `OPENROUTER_DAILY_BUDGET_USD` в `.env`.

### PostgreSQL упал

```bash
# Проверить статус
sudo systemctl status postgresql

# Перезапустить
sudo systemctl restart postgresql

# Если в Docker:
docker restart aibp-postgres
```

## Резервное копирование

```bash
# Ежедневный бэкап (добавь в crontab)
pg_dump $DATABASE_URL | gzip > backups/aibp_$(date +%Y%m%d).sql.gz

# Бэкап SQLite (эксперименты)
cp data/self_learning.db backups/self_learning_$(date +%Y%m%d).db
```

Храни бэкапы off-site (S3, другой сервер).
