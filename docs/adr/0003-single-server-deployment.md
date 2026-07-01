# ADR-0003: Single-server deployment

**Status:** Accepted
**Date:** 2026-06-30

## Context

Previous project used 2 servers: one for n8n (collection, classification, publishing) and one for Hermes Agent (post generation). This added operational complexity: network between servers, sync issues, harder debugging.

## Decision

Deploy everything on a single server:
- PostgreSQL
- Python application (all 6 layers)
- Hermes Agent (cron orchestration)
- Caddy (static dashboard)

## Rationale

1. **Simplicity**: one server to manage, backup, monitor
2. **No network latency**: all components local
3. **Easier debugging**: all logs in one place
4. **Lower cost**: one VPS instead of two
5. **Sufficient for scale**: Telegram channel with ~1000 subscribers doesn't need distributed architecture

## Consequences

- Single point of failure (mitigated by regular backups)
- Resource contention possible (mitigated by limiting LLM concurrency)
- If scale grows beyond ~10K subscribers, may need to split (but not now)

## Migration path (if needed later)

If we outgrow single server:
1. Move PostgreSQL to managed RDS
2. Move Python app to separate server
3. Keep Hermes on its own server
4. Use Tailscale for private network between servers
