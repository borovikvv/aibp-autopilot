# ADR-0012: Атрибуция подписок invite-ссылками и полуавтоматическая закупка

**Status:** Accepted
**Date:** 2026-07-07
**Related:** ADR-0005 (Bot API), issue #39, issues #19/#24

## Контекст

Все подписчики выглядели одинаково: фактический CPS (цена за подписчика)
любой закупки или ВП был невычислим, отчёт роста опирался на оценочный
`assumed_conversion_pct`. Слоя привлечения трафика не было вовсе.

## Решение

1. **Источник трафика = своя invite-ссылка.** `traffic_sources` (миграция
   0008) хранит источник (закупка/ВП/внешняя платформа) и его ссылку из
   `createChatInviteLink`; `invite_joins` — подписки по ней. Фактический
   CPS = cost_rub / joins, в еженедельном growth-отчёте — прогноз vs факт.

2. **chat_member — через существующий approvals-поллер.** getUpdates
   эксклюзивен per bot (issue #24), поэтому новый consumer не создаётся:
   поллер approvals запрашивает `allowed_updates=[callback_query,
   chat_member]` и передаёт join-события в traffic_sources. Подписка без
   invite-ссылки (по @username) не атрибуцируется — это baseline-органика.
   Требование: бот — админ основного канала (уже выполняется).

3. **`aibp ad-plan <donor>`** готовит закупку целиком: прогноз по TGStat
   (reuse build_recommendation), invite-ссылка, LLM-креатив (трекинг-ссылка
   принудительно сохраняется в тексте), черновик заявки админу — в
   `reports/ads/<slug>.md`. Оплата и договорённости — вручную
   (prohibited actions); итог фиксируется `aibp source-set`.

## Последствия

- Появляется правдивый CPS → `subscriber_value_rub` и `assumed_conversion_pct`
  в config/competitors.yaml можно калибровать по факту.
- chat_member-апдейты живут на сервере Telegram 24h: при остановке поллера
  дольше суток часть join-событий теряется (CPS занижается — консервативно).
- PG недоступен в момент join → событие подтверждается вместе с батчем и
  теряется; допустимо при текущих масштабах, логируется warning.
- competitors.yaml заполнен стартовым пулом RU-каналов; перед закупкой
  username сверять на tgstat.ru.
