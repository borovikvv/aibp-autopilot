# ADR-0001: Self-Learning Architecture

**Status:** Accepted
**Date:** 2026-06-30

## Context

The previous project (ai-business-pulse-hermes) had a "self-learning" module that was only read-only QA — it generated reports but did not modify prompts or policies. The goal of the new project is full autopilot: system modifies its own behavior based on engagement metrics.

## Decision

Implement a 5-stage closed loop:

1. **Engagement Collector** (every 4h) — fetches views/forwards from TG Bot API, stores in PostgreSQL
2. **Pattern Miner** (weekly) — statistical analysis (scipy) + LLM hypothesis generation (OpenRouter)
3. **Policy Updater** (weekly) — converts hypotheses into policy variants, saves as experiments
4. **Shadow Test Runner** (daily) — applies policy variant to stage/test channel, runs for 7 days
5. **Decision Engine + Auto-promote/Rollback** (daily) — statistical test (Welch's t-test, Cohen's d), promotes to prod if significant improvement, rolls back if regression

## Safety Rails

- **Rate limiter**: max 1 change/day, 3/week per slot
- **Anomaly guard**: freeze if engagement drops >30% in 24h or >50% in 7d
- **Kill switch**: 3 rollbacks in 7 days → permanent pause until manual resume
- **Auto-rollback**: if promoted policy causes <85% of baseline engagement in 48h → revert

## What Autopilot CANNOT Change (hardcoded safety boundaries)

- Hermes cron schedules
- Database schema
- `.env` files / secrets
- Channel mapping (stage→test, prod→main)
- Core regex patterns in quality_gate.py (only ADD new patterns)
- Safety rails themselves

## Alternatives Considered

1. **Human gate** — rejected (user wants full autopilot)
2. **A/B split testing** — out of scope for MVP (requires audience splitting which TG doesn't natively support)
3. **Multi-armed bandit** — out of scope (too complex for first iteration, revisit in v2)

## Consequences

- Risk: bad changes can affect prod channel automatically
- Mitigation: safety rails + rollback + kill switch
- Benefit: true autonomous optimization without human intervention
- Benefit: every change is tracked in `experiments_log` for audit

## Update (issue #43, 2026-07-08)

The self-learning SQLite store has been consolidated into PostgreSQL (migration
0009). Stage 1 (Engagement Collector) now writes to the same PG instance as the
content pipeline. The "separate SQLite" decision is superseded.
