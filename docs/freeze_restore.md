# Заморозка и восстановление AIBP Autopilot

Состояние на **2026-11-04**: production-сервер и GitHub выровнены, код
заморожен. Проверено чистым клоном с GitHub: дерево файлов совпадает с
рабочим каталогом сервера байт-в-байт (кроме runtime-каталогов).

- Ветка: `main`
- Тег заморозки: `freeze-2026-11-04` (на него и восстанавливаться)
- Конвейер остановлен 2026-11-04: RSS Collector, Enrichment, Publisher и
  4 генерации на паузе — LLM не вызывается, бюджет не тратится. Активны
  4 мониторинговые джобы: engagement, dashboard, safety, redirect health.
- `config/policy.yaml`: `autopilot_paused: true`

## Что лежит в GitHub

Всё, что нужно для запуска кода: `src/`, `config/`, `prompts/`, миграции,
тесты, systemd-юнит, шаблон Caddy, генератор cron-обёрток и регистрация джоб.

```bash
git clone https://github.com/borovikvv/aibp-autopilot.git
```

## Что НЕ в git (сохранить отдельно)

| Что | Где на сервере | Как восстановить |
|---|---|---|
| `.env` — все секреты | `/root/aibp-autopilot/.env` | Из менеджера паролей владельца. **В git не попадает никогда.** |
| База PostgreSQL | дамп `/root/backups/aibp_20261104.sql.gz` (16 МБ) | `gunzip -c … \| psql "$DATABASE_URL"` |
| Caddy + bcrypt-хэш basic-auth | `/etc/caddy/Caddyfile` | Шаблон `deploy/caddy/Caddyfile.example`; хэш генерируется заново `caddy hash-password`. В git — только placeholder. |
| Hermes cron state | `~/.hermes/` | Пересоздаётся: `deploy/cron/gen_aibp_jobs.sh` + `register_jobs.sh`. ID джоб будут другими — это нормально. |
| Дашборд и картинки | `/srv/static/aibp/` | Дашборд — `make dashboard`; картинки — runtime, для старых постов не нужны. |
| Логи и LLM-косты | `reports/` | Не восстанавливаются, исторические. |

> Репозиторий **публичный**. `.env`, дамп БД, реальный bcrypt-хэш и любые
> токены в него не коммитить.

## Восстановление с нуля

1. **VPS** — Ubuntu/Debian, ≥2 vCPU / 2 GB RAM / ≥20 GB. Пакеты и timezone:
   см. [`deploy_vps.md`](deploy_vps.md) Phase 1 (в списке есть `caddy`).
   Таймзона — `Europe/Moscow`: все cron-расписания в MSK.

2. **Код**:
   ```bash
   git clone https://github.com/borovikvv/aibp-autopilot.git /root/aibp-autopilot
   cd /root/aibp-autopilot
   git checkout freeze-2026-11-04   # тег заморозки; новее main не брать без причины
   ```

3. **Секреты**: положить `.env` из менеджера паролей (образец — `.env.example`).
   Минимум: `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `DATABASE_URL`,
   `TELEGRAM_CHANNEL_ID_PROD`, `TELEGRAM_ALERT_CHAT_ID`,
   `TELEGRAM_CHANNEL_USERNAME_PROD`, `TRACKING_BASE_URL`.

4. **Зависимости и схема**:
   ```bash
   make bootstrap        # deps + PostgreSQL + миграции + проверка секретов
   # или вручную: pip install -r requirements.txt -e . && make migrate
   ```

5. **База**: только в пустую БД, не поверх живой.
   ```bash
   gunzip -c /root/backups/aibp_20261104.sql.gz | psql "$DATABASE_URL"
   make migrate-status   # все миграции должны быть applied
   ```

6. **Caddy**:
   ```bash
   # взять блок aibp.borovikvv.ru из deploy/caddy/Caddyfile.example,
   # заменить <BCRYPT_HASH> на вывод `caddy hash-password --plaintext '…'`
   caddy validate --config /etc/caddy/Caddyfile
   systemctl reload caddy
   ```

7. **Redirect daemon**:
   ```bash
   cp deploy/systemd/aibp-redirect.service /etc/systemd/system/
   systemctl daemon-reload && systemctl enable --now aibp-redirect
   curl -fsS http://127.0.0.1:8091/healthz
   ```

8. **Cron-джобы**:
   ```bash
   bash deploy/cron/gen_aibp_jobs.sh
   bash deploy/cron/register_jobs.sh
   hermes cron list --all        # 19 джоб, конвейер по умолчанию на паузе
   ```
   `register_jobs.sh` идемпотентен и не трогает уже созданные джобы. По
   умолчанию RSS, Enrichment, Publisher и 4 генерации создаются **на паузе**
   (4 active / 15 paused): ни LLM-расхода, ни публикаций без явного решения.
   Включать только после проверки в тест-канале и подтверждения владельца:
   `bash deploy/cron/register_jobs.sh --enable-publishing` (или
   `hermes cron resume <id>` для каждой из 7 джоб, Publisher — последним).

9. **Проверка**:
   ```bash
   make smoke-test
   curl -s -o /dev/null -w '%{http_code}\n' https://<домен>/r/deadbeef   # 404
   ```
   Затем — пробный прогон генерации в тестовый канал, только после этого
   возвращать прод-публикацию.

## Конвейер остановлен (2026-11-04, выполнено)

По команде владельца поставлены на паузу 7 джоб:

| Джоба | ID |
|---|---|
| RSS Collector | `ca53fc96e9a7` |
| Enrichment | `95af1e9490f0` |
| Publisher | `0c7817557025` |
| Morning Generation | `2323fc7092af` |
| Weekly Case Generation | `35729056c16d` |
| Evening Generation | `88c8f63eb1eb` |
| Weekly Digest Generation | `276fbf2ff28c` |

Очередь публикации на момент остановки пуста (`v_publisher_queue` = 0).
Активны 4 джоба: Engagement Collector, Dashboard, Safety Check,
Redirect Health Check — они не вызывают LLM и не создают постов. ID джоб
после восстановления будут другими — искать по имени через
`hermes cron list --all`.

Возобновление — только по команде владельца, в таком порядке:

```bash
hermes cron resume ca53fc96e9a7   # RSS Collector
hermes cron resume 95af1e9490f0   # Enrichment
hermes cron resume 2323fc7092af   # Morning Generation
hermes cron resume 35729056c16d   # Weekly Case Generation
hermes cron resume 88c8f63eb1eb   # Evening Generation
hermes cron resume 276fbf2ff28c   # Weekly Digest Generation
hermes cron resume 0c7817557025   # Publisher — последним
```

`aibp-redirect` не останавливать: без него перестают работать уже
опубликованные трекаемые ссылки.

## Что проверено 2026-11-04

- `git status` на сервере чист, `HEAD == origin/main`.
- Свежий `git clone` с GitHub: tree diff с сервером пуст; `import aibp` и
  `compileall` проходят.
- Сгенерированные обёртки совпадают с живыми `~/.hermes/scripts/aibp_*.sh`
  (diff пуст).
- `register_jobs.sh --dry-run` видит все 19 живых джоб и пропускает их.
- `make smoke-test` — все проверки OK.
- Дамп БД сжат и проверен `gzip -t`.
- CI на GitHub для `main` — зелёный.

## Известные мелочи

- `GITHUB_TOKEN` в `.env` кодом не используется — можно не восстанавливать.
- Remote на сервере — публичный HTTPS (fetch работает без токена, push нет).
  Это правильно для замороженного сервера: пушить должен только владелец.
- В коде остались дефолты `cockpit.borovikvv.ru` для `IMAGE_PUBLIC_BASE_URL` и
  `DASHBOARD_BASE_URL`; прод перекрывает их через `.env`
  (`https://aibp.borovikvv.ru`). При восстановлении `.env` это неважно.
