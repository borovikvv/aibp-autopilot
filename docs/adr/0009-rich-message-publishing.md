# ADR-0009: Rich-message publishing (media + full text)

**Status:** Accepted
**Date:** 2026-07-06
**Related:** issue #28, #33

## Контекст

`visual_policy` описан в policy и тюнится self-learning'ом, `has_image` пишется
в обучение, но публикация уходила чистым текстом: `generation/pipeline.py`
хардкодил `need_image = false`, `image_url` не заполнялся, а `publisher`
слал `sendPhoto` только при `need_image and image_url` — условие не
выполнялось никогда. Итог: `visual_policy` — мёртвый код, `has_image` всегда 0.

## Актуальный Bot API (проверено 2026-07, core.telegram.org/bots/api)

- `sendMessage.text` — **1–4096** символов.
- `sendPhoto.caption` / `sendVideo.caption` — **0–1024** символа (after
  entities parsing). Отдельного метода «медиа + >1024 символов одним
  сообщением» **нет**.
- `LinkPreviewOptions` (Bot API 7.0) заменил `disable_web_page_preview` в
  `sendMessage`; поля `url`, `prefer_large_media`, `show_above_text` позволяют
  прикрепить к длинному тексту **большое медиа-превью** одним сообщением.
- `sendPoll` — `question` **1–300**, `options` **2–12** штук
  (`InputPollOption`, текст **1–100**). Опрос — отдельное сообщение.

Целевые длины постов: morning 800–1400, evening 400–700, weekly 2500–4500.
Значит: evening всегда влезает в caption; morning иногда, weekly — никогда.

## Решение

Выбор пути в `publisher._publish_post_message` по длине:

1. **media + текст ≤ 1024** → `sendPhoto`, весь текст в `caption` — одно
   сообщение с настоящим медиа (подходит для evening).
2. **media + текст > 1024** → `sendMessage` + `link_preview_options`
   (`url=image_url`, `prefer_large_media=true`, `show_above_text=true`) —
   одно сообщение: полный текст (до 4096) + большое превью (morning/weekly).
3. **без медиа или ошибка отправки медиа** → обычный `sendMessage`
   (fallback: пост без картинки лучше, чем провал публикации).

Источник `image_url`: `visual_policy.static_image_url` при
`visual_policy.enabled` (оператор задаёт схему/баннер). `need_image`
выводится из наличия URL, не хардкодится.

## Осознанно вне скоупа

- **Авто-рендер схемы** по `visual_policy.kind` — в репозитории нет генератора
  изображений (`xai_api_key` объявлен, но нигде не используется). Это отдельная
  подсистема; весь publish-путь ниже уже готов принять URL, когда рендер
  появится.
- **Видео** (`sendVideo`) — генерация видео тоже отсутствует; ветку добавим,
  когда будет источник.

## Последствия

- `has_image` в обучении теперь отражает реальность (1, когда медиа реально
  прикреплено).
- Оператор может включить визуальный слой сегодня, задав `static_image_url`.
- Длинные посты публикуются целиком (link-preview), без обрезки в caption.
