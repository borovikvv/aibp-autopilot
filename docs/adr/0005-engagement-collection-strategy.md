# ADR-0005: Engagement collection strategy — Bot API copyMessage vs MTProto

**Status:** Superseded in part — see "Amendment 2026-07-22" below
**Date:** 2026-07-01

## Context

The self-learning module needs reliable engagement metrics (views, forwards, reactions) for every published post. The decision_engine uses these to compare shadow vs control policies (Welch's t-test, Cohen's d). Missing or biased engagement data causes experiments to fail (`insufficient_data_after_14d`) or make wrong decisions.

Telegram has two APIs for bots:

1. **Bot API** (HTTP `api.telegram.org/bot<token>/...`) — what we use everywhere
   - `getUpdates` returns channel posts with `views` field, BUT:
     - Only the last 100 updates (~48h of posts)
     - 409 Conflict if webhook is set or another process calls getUpdates
     - Older posts are silently invisible
   - `copyMessage` can copy a post to a private chat; the copy retains `views`/`forwards`/`reactions`
   - No direct "getMessageViews" endpoint exists

2. **MTProto** (via Telethon/Pyrogram) — requires a user session (not bot)
   - `messages.getMessages` with `peer` and `id` returns full message info including views
   - Works for any post, any age
   - No 409 conflicts
   - Requires: `api_id` + `api_hash` from https://my.telegram.org, user session file
   - Risk: account ban if used aggressively; ToS gray area for automated access

## Decision

Implement a two-tier strategy:

### Tier 1 (default): Bot API `copyMessage` workaround

- Configure `TELEGRAM_METRICS_CHAT_ID` in `.env` (owner's personal chat with the bot)
- For each post:
  1. `copyMessage` from channel → metrics chat
  2. Read `views`, `forwards`, `reactions` from the copied message
  3. `deleteMessage` to clean up
- Works for any post age, no 409 conflicts
- Bot must be able to send+delete messages in the metrics chat (default for private chats)

### Tier 2 (fallback): Bot API `getUpdates` scan

- Used automatically if `TELEGRAM_METRICS_CHAT_ID` is not set
- Only finds posts in the last ~100 updates (~48h)
- Logs warning + sends alert to owner on 409 Conflict
- Documented as unreliable — should only be a temporary fallback

### Future (not in scope): MTProto via Telethon

- If Tier 1 proves insufficient (e.g., metrics chat gets rate-limited)
- Would require: `api_id`, `api_hash`, session file, `telethon` dependency
- Separate module `engagement_collector_mtproto.py` to avoid coupling
- Not implemented now — Tier 1 should be sufficient for single-channel scale

## Rationale

1. **Tier 1 is good enough for current scale** (~10 posts/day, 1 channel). `copyMessage` is a clean workaround that uses standard Bot API, no ToS risk.

2. **Tier 2 as fallback** ensures the system works even before the owner configures `TELEGRAM_METRICS_CHAT_ID`, with clear alerting to prompt configuration.

3. **MTProto is overkill** for a single channel with < 100 posts/day. It adds complexity (session management, ban risk) that isn't justified yet.

4. **The copy+delete pattern is invisible to subscribers** — the copy goes to a private chat, gets deleted immediately. No spam, no channel pollution.

## Consequences

- Owner must configure `TELEGRAM_METRICS_CHAT_ID` for reliable collection (otherwise fallback with limited coverage)
- Bot must have been started by the owner in private chat at least once (so `copyMessage` to that chat works)
- One extra API call per post (copyMessage) + one delete — ~2x API usage vs direct read, but still well within rate limits
- If Telegram changes `copyMessage` to strip `views` field, Tier 1 breaks → would need to migrate to MTProto

## Implementation

- `src/aibp/self_learning/engagement_collector.py`:
  - `get_views_via_copy()` — Tier 1
  - `get_views_via_updates()` — Tier 2 (with 409 handling + alert)
  - `collect_engagement_for_post()` — tries Tier 1 first, falls back to Tier 2
- `.env.example`: `TELEGRAM_METRICS_CHAT_ID` documented
- `tests/unit/test_engagement_collector.py`: unit tests for parsing, 409 handling, method selection

---

## Amendment 2026-07-22 (issue #49): both Bot API tiers never worked; web preview is the new Tier 1

**What happened.** From the first prod post (2026-07-11) until this fix, the
collector wrote `views=forwards=reactions=0` for every post — 876 snapshots,
all zeros. Only `subscribers_at` was real. The failure was silent: both tiers
returned `0`, never an error, so nothing alerted.

**Why the 2026-07-01 decision was wrong.** The core premise — "the copied
Message contains `views`/`forwards`/`reactions`" — is false:

- **Tier 1 (`copyMessage`)** returns a [`MessageId`](https://core.telegram.org/bots/api#messageid)
  object — only `message_id`, no `Message` body, so no `views`. And even a full
  copy/forward is a *new* message whose view counter starts at 0; the original's
  count is never carried over. Tier 1 could not work under any configuration.
- **Tier 2 (`getUpdates`)** only sees a `channel_post` at publish time, when
  `views` is 0, and the post leaves the ~100-update window within ~48h — long
  before the 48h reward horizon. Also 0.

The Bot API simply has **no** way to read a channel post's view count.

**New decision — Tier 1 = public web preview `t.me/s/<username>`.** It renders
every post with its real `views` counter and a `data-post="<user>/<msg_id>"`
anchor that joins to `feed_items.published_message_id`. One page ≈ 20 posts, so
the whole window is one or two HTTP GETs turned into a `{message_id: views}`
map — no per-post call, no token, no auth, no ban risk. Requires
`TELEGRAM_CHANNEL_USERNAME_PROD` in `.env` and a public channel (ours is).

Trade-offs accepted:
- **Views only.** The preview exposes no forwards/reactions. At ~300 subs these
  are ≈0 (composite-reward weight × 0 = 0), so the loss is immaterial now.
- **Private/test channel** has no preview → subscriber count only. Test posts
  don't drive statistics (ADR-0007), so this is fine.
- **Settled counts.** The preview gives the *current* cumulative view count. For
  posts past ~48h this equals the horizon value (views have plateaued); recent
  posts keep being re-read by the 4h cron until they settle.

**MTProto stays "Future" (now Tier 3).** If forwards/reactions ever matter
(channel grows to thousands), Telethon + a user session is the documented
upgrade — with its session-management and ban-risk costs, unchanged from above.

**Silent-degradation guardrail.** A prod post older than 1h that is missing from
the preview now logs a warning; a total blackout (0 of N prod posts found) alerts
the owner. Zero views are no longer written silently.

**History backfill.** `scripts/rebuild_engagement_history.py` inserts one
horizon snapshot per pre-fix post from the preview's settled counts (idempotent),
recovering the 2026-07-11 → 07-22 history for reward/bandit.

**Superseded implementation:**
- Removed: `get_views_via_copy()`, `get_views_via_updates()`,
  `collect_engagement_for_post()`, `TELEGRAM_METRICS_CHAT_ID`.
- Added: `parse_views_from_html()`, `fetch_views_map()`, `build_metrics()`,
  `TELEGRAM_CHANNEL_USERNAME_PROD`.
