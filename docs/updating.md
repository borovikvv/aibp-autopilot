# Обновление AIBP Autopilot

> Как обновлять проект на production-сервере после коммитов в GitHub.

## TL;DR — одна команда

```bash
cd /root/aibp-autopilot
make update
```

Это:
1. `git pull` — подтянет новый код
2. `pip install` — если изменился `requirements.txt`
3. `python3 -m aibp.db.migrate` — применит миграции БД
4. Smoke-test — проверит что всё работает

## Что сохраняется при обновлении

Эти файлы **НЕ затрагиваются** `git pull` (в `.gitignore`):

| Файл/директория | Что там | Почему не в git |
|---|---|---|
| `.env` | TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, DATABASE_URL | Сервероспецифичные секреты |
| `reports/` | Логи, дашборды, JSON-отчёты | Runtime артефакты |
| `backups/` | Бэкапы БД | Server-specific |

## Что обновляется автоматически

✅ **Код** — все `.py` файлы в `src/aibp/`
✅ **Промпты** — `prompts/*.md`
✅ **Конфиги** — `config/policy.yaml`, `config/rss_feeds.yaml`
✅ **Schema БД** — через миграции (см. ниже)
✅ **Зависимости** — если изменился `requirements.txt`

## Что нужно делать вручную

### 1. Миграции БД — АВТОМАТИЧЕСКИ

Миграции применяются автоматически при `make update`. Не нужно ничего делать вручную.

Чтобы проверить статус:
```bash
make migrate-status
```

Вывод:
```
Migration                           Status
-----------------------------------
0001_initial                        ✅ applied
0002_source_fetched_at              ✅ applied
0003_add_reactions_table            ⏳ pending
```

### 2. Hermes cron-джобы — ВРУЧНУЮ (когда меняется расписание)

Если ты изменил расписание cron-джоб или добавил новую — Hermes не узнает об этом автоматически.

```bash
# Проверить есть ли изменения в cron-промптах
git log --oneline HEAD~5..HEAD -- prompts/

# Если есть — попроси Hermes перерегистрировать:
# "Hermes, перечитай /root/aibp-autopilot/prompts/hermes_bootstrap.md 
#  и обнови cron-джобы AIBP"
```

Если расписание не менялось — **ничего делать не нужно**. Cron-джобы запускают Python-скрипты, а скрипты подтягивают новый код при каждом запуске.

### 3. Long-running процессы — НЕ НУЖНО (пока)

Сейчас все джобы — это cron-задачи (запустился, отработал, завершился). При следующем запуске они подхватят новый код автоматически.

Если в будущем появится long-running процесс (например, webhook-сервер или bot-poller) — его нужно будет перезапускать:
```bash
# Будущее (когда появится systemd-сервис):
sudo systemctl restart aibp-publisher
```

## Как добавить новую миграцию

Когда тебе нужно изменить схему БД:

### 1. Создай файл миграции

```bash
# Формат имени: NNNN_краткое_описание.py
# NNNN — следующий номер (посмотри в src/aibp/db/migrations/)
nano src/aibp/db/migrations/0003_add_reactions_table.py
```

### 2. Напиши код миграции

```python
"""Migration 0003: Add reactions table for tracking emoji reactions."""
from __future__ import annotations


def up(conn) -> None:
    """Apply migration."""
    with conn.cursor() as cur:
        # Всегда делай idempotent-проверки!
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_name = 'reactions_log'
        """)
        if cur.fetchone():
            return  # already applied

        cur.execute("""
            CREATE TABLE reactions_log (
                id              bigserial PRIMARY KEY,
                feed_item_id    bigint REFERENCES feed_items(id),
                emoji           text NOT NULL,
                count           integer DEFAULT 0,
                measured_at     timestamptz DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE INDEX idx_reactions_feed ON reactions_log(feed_item_id, measured_at)
        """)


def down(conn) -> None:
    """Rollback migration."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS reactions_log")
```

### 3. Закоммить и запушь

```bash
git add src/aibp/db/migrations/0003_add_reactions_table.py
git commit -m "feat: add reactions_log table for emoji reactions tracking"
git push
```

### 4. На сервере — `make update`

```bash
cd /root/aibp-autopilot
make update
```

Миграция применится автоматически.

## Типичные сценарии

### Сценарий 1: Ты добавил новый RSS-фид в `config/rss_feeds.yaml`

```bash
# Локально:
nano config/rss_feeds.yaml  # добавил фид
git add config/rss_feeds.yaml
git commit -m "feat: add new RSS feed"
git push

# На сервере:
make update

# Готово. Следующий запуск RSS Collector (через час) подхватит новый фид.
```

### Сценарий 2: Ты изменил логику генерации постов

```bash
# Локально:
nano src/aibp/generation/pipeline.py  # изменил промпт
git add src/aibp/generation/pipeline.py
git commit -m "feat: improve morning post generation"
git push

# На сервере:
make update

# Готово. Следующая утренняя генерация (09:00 MSK) использует новый код.
```

### Сценарий 3: Ты добавил новый столбец в feed_items

```bash
# Локально:
# 1. Создай миграцию (см. "Как добавить новую миграцию" выше)
nano src/aibp/db/migrations/0004_add_engagement_score.py

# 2. Обнови код, использующий новый столбец
nano src/aibp/self_learning/engagement_collector.py

git add -A
git commit -m "feat: track engagement_score per post"
git push

# На сервере:
make update
# Миграция применится автоматически, smoke-test проверит что всё работает
```

### Сценарий 4: Ты изменил расписание cron-джобы

```bash
# Локально:
nano prompts/hermes_bootstrap.md  # изменил schedule='0 6 * * *' на '0 5 * * *'
git commit -am "chore: move morning generation to 08:00 MSK"
git push

# На сервере:
make update

# ВАЖНО: Hermes cron-джоба НЕ обновится автоматически!
# Попроси Hermes:
# "Hermes, обнови cron-джобу 'AIBP — Morning Generation' 
#  с schedule='0 5 * * *' (новое время)"
```

## Откат изменений

### Откат миграции

```bash
# Посмотреть список применённых миграций
make migrate-status

# Откатить одну миграцию (если у неё есть down() функция)
python3 -m aibp.db.migrate --rollback 0003_add_reactions_table
```

### Откат к старому коммиту

```bash
# Посмотреть историю
git log --oneline -10

# Откатиться к конкретному коммиту
git checkout <commit_hash>
make migrate  # применить миграции этого коммита

# Или вернуться к latest:
git checkout main
make update
```

## Что делать если `make update` упал

### На этапе git pull (конфликт)

```bash
# Если есть локальные изменения, которые конфликтуют:
git status
git stash drop  # удалить stash если он создался
# Разрешить конфликт вручную, затем:
git add .
git commit
make update
```

### На этапе миграции

```bash
# Посмотреть что применилось, что нет:
make migrate-status

# Если миграция упала на середине:
# 1. Проверь ошибку:
python3 -m aibp.db.migrate

# 2. Если нужно откатить:
python3 -m aibp.db.migrate --rollback NNNN_name

# 3. Исправь миграцию, закоммить, запуши, повтори:
make update
```

### На этапе smoke-test

```bash
# Запусти подробно:
python3 -m aibp.cli smoke-test

# Если упало на Telegram — проверь .env (TELEGRAM_BOT_TOKEN)
# Если упало на OpenRouter — проверь .env (OPENROUTER_API_KEY)
# Если упало на DB — проверь:
sudo systemctl status postgresql
```

## Best practices

1. **Тестируй локально перед push**. У тебя есть `tests/unit/` — запускай `make test` перед коммитом.

2. **Не делай `force push`** на main. Это сломает `git pull` на сервере.

3. **Один коммит = одна логика**. Не смешивай миграцию + новый код + изменение промпта в одном коммите.

4. **Миграции должны быть idempotent**. Если миграция упала и ты её исправил — она должна корректно примениться при повторном запуске.

5. **Никогда не удаляй применённые миграции**. Если нужно отменить — создавай новую миграцию с `down()`-логикой.

6. **Backup перед большими изменениями**:
   ```bash
   pg_dump $DATABASE_URL | gzip > backups/before_update_$(date +%Y%m%d).sql.gz
   make update
   ```
