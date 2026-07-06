# ADR-0006: Sync psycopg2 in async functions — conscious compromise (wontfix)

**Status:** Accepted (wontfix)
**Date:** 2026-07-01

## Context

Issue #6 identified that `publisher.py::run_async()` and `engagement_collector.py::run_async()` are `async def` functions that call synchronous psycopg2 operations (`fetch_all()`, `execute()`) directly, without `asyncio.to_thread()` or `run_in_executor()`. This technically blocks the event loop for the duration of each DB query (~10-100ms per query).

## Decision

**Won't fix.** This is a conscious compromise, documented here for future reference.

## Rationale

1. **Current load is negligible.** Publisher runs every 5 minutes and processes ≤ 10 posts. Engagement collector runs every 4 hours and processes ≤ 50 posts. Each DB query takes < 100ms. The total blocking time per cron run is < 1 second — well within acceptable bounds for a cron-based system.

2. **Async is needed only for HTTP.** The `async def` exists because `httpx.AsyncClient` (Telegram API calls) is async. The DB calls are incidental — they happen before/after the HTTP calls, not concurrently with them.

3. **Wrapping in `asyncio.to_thread()` adds complexity without benefit.** It would mean:
   - Every `fetch_all()` call becomes `await asyncio.to_thread(fetch_all, sql, params)`
   - More code, more indentation, harder to read
   - No actual concurrency gain (we're not running DB queries in parallel with HTTP)

4. **The "proper" fix (asyncpg) is overkill.** Switching to `asyncpg` would require:
   - Rewriting the entire DB layer (`db/connection.py`)
   - Different parameter style ($1, $2 instead of %s)
   - Different connection pool API
   - No psycopg2 features we rely on (DictCursor, etc.)
   - Significant refactoring for zero user-visible benefit at current scale

## When to revisit

This decision should be revisited if ANY of these conditions become true:

1. **Publisher becomes a long-running service** (not cron). If publisher runs continuously and processes posts in real-time, blocking DB calls would matter.

2. **Scale increases 10x+.** If we have > 100 posts per publish cycle, or > 1000 engagement metrics per collection cycle, the cumulative blocking time becomes noticeable.

3. **Concurrency requirements emerge.** If we need to publish to multiple channels simultaneously, or collect engagement for multiple channels in parallel, async DB becomes necessary.

4. **DB latency increases.** If we move PostgreSQL to a remote server (different machine), network latency would make each blocking call 10-50ms longer, and async would help overlap queries.

## Migration path (if needed later)

1. Add `asyncpg` to requirements.txt (alongside psycopg2, not replacing)
2. Create `db/async_connection.py` with async pool
3. Migrate publisher.py and engagement_collector.py to async DB calls
4. Keep sync psycopg2 for cron jobs that don't use async (most of them)
5. Eventually remove psycopg2 if all consumers are async

## Consequences

- Code is simpler and more readable
- Slight event loop blocking during DB calls (acceptable at current scale)
- Documented trade-off — future developers know this was considered, not overlooked

## References

- Issue #6: https://github.com/borovikvv/aibp-autopilot/issues/6
- asyncio.to_thread docs: https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread
- asyncpg vs psycopg2 comparison: https://magicstack.github.io/asyncpg/current/
