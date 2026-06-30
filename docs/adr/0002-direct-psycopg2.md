# ADR-0002: Direct psycopg2 instead of n8n DB gateway

**Status:** Accepted
**Date:** 2026-06-30

## Context

The previous project (ai-business-pulse-hermes) used n8n as a SQL gateway:
1. Fetch n8n workflow via API
2. Replace SQL query in Postgres node via PUT
3. Trigger webhook
4. Poll execution history for result
5. Restore original workflow

This was fragile, non-transactional, prone to race conditions, and left workflows in modified state if any step failed.

## Decision

Use direct `psycopg2` with `ThreadedConnectionPool` and parameterized queries.

```python
@contextmanager
def db_conn() -> Iterator[DictCursor]:
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
```

## Rationale

1. **Safety**: parameterized queries prevent SQL injection
2. **Performance**: connection pool, no HTTP overhead
3. **Transactional**: proper BEGIN/COMMIT/ROLLBACK
4. **Observable**: standard psycopg2 logging
5. **Testable**: can use SQLite or test PostgreSQL easily

## Consequences

- n8n is no longer needed (publisher is now Python aiogram)
- All layers (collector, enrichment, generation, publisher, self-learning) use the same DB access pattern
- Easier to debug — just connect to PostgreSQL and query
- Simpler deployment — no n8n server required
