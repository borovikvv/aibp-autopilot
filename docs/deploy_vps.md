# Deploy runbook — dedicated VPS, from scratch

Operational plan for standing this project up on a fresh VPS as the sole system
running the channel (replacing the current n8n + Hermes setup). Written to be
executed step by step, including by an automated agent over SSH.

**Guiding stance (why it's staged, not one-shot):** all tests are unit-level
with mocks — the code has never touched real PostgreSQL, Telegram, or
OpenRouter. So we bring it up under supervision: test channel first, autopilot
paused, monetization off; prove each layer on real data before trusting it.

**Hard gates — STOP and get explicit human confirmation before:**
- the first real post to any channel,
- cutover (disabling n8n / pointing at the prod channel),
- enabling any monetization feature (tracking, images, button),
- unpausing the autopilot.

---

## Phase 0 — What the operator must provide

- [ ] SSH access to the VPS (user with sudo).
- [ ] VPS basics: Ubuntu 22.04+ (or Debian 12), ≥2 vCPU / 2 GB RAM, ≥20 GB disk.
- [ ] `OPENROUTER_API_KEY` and a sane `OPENROUTER_DAILY_BUDGET_USD`.
- [ ] `TELEGRAM_BOT_TOKEN` for a bot that is **admin** of both a TEST channel and the PROD channel.
- [ ] `TELEGRAM_CHANNEL_ID_PROD`, `TELEGRAM_CHANNEL_ID_TEST`, `TELEGRAM_ALERT_CHAT_ID`.
- [ ] A private "metrics" chat id (`TELEGRAM_METRICS_CHAT_ID`) — the bot's own chat with the owner; needed so the engagement collector uses `copyMessage` and does not fight the approval poller over `getUpdates` (issue #24).
- [ ] Decision: monetization now or later? If now, also: a public domain for the redirect service (`TRACKING_BASE_URL`) and a web-served static dir for images (`IMAGE_PUBLIC_BASE_URL` → `IMAGE_OUTPUT_DIR`).
- [ ] Confirm the channel is currently run by n8n so we plan the cutover (no parallel posting).

---

## Phase 1 — Provision the VPS

```bash
sudo apt-get update && sudo apt-get install -y \
    python3 python3-venv python3-pip git postgresql postgresql-contrib \
    build-essential libpq-dev curl
sudo timedatectl set-timezone Europe/Moscow   # cron times in install.md are MSK-derived
```

Create a dedicated user + checkout (the systemd unit and cron examples assume
`/root/aibp-autopilot`; adjust paths consistently if you use a non-root user).

```bash
git clone https://github.com/borovikvv/aibp-autopilot.git /root/aibp-autopilot
cd /root/aibp-autopilot
```

---

## Phase 2 — Install + configure

```bash
make bootstrap        # installs deps, sets up PostgreSQL, inits DB, checks secrets
# (or manually: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -e .)

cp .env.example .env
# Fill in every secret from Phase 0. Keep monetization/autopilot OFF for now:
#   TRACKING_BASE_URL=            (empty → tracking off)
#   TELEGRAM_METRICS_CHAT_ID=<private metrics chat>
#   OPENROUTER_DAILY_BUDGET_USD=<real cap>
```

In `config/policy.yaml`, confirm the safe starting posture:
- `autopilot_paused: true`
- `visual_policy.enabled: false`, `visual_policy.generate: false`
- `telegram.source_button: false`

---

## Phase 3 — Database

```bash
make db-init          # create schema
make migrate          # apply migrations 0001..0006
make migrate-status   # verify all applied
```

---

## Phase 4 — First real contact (smoke test)

```bash
make smoke-test       # verifies DB + Telegram + OpenRouter connectivity
```

This is the first time real credentials are exercised. Fix any connectivity /
permission issues here (bot admin rights, DB DSN, API key) before proceeding.

---

## Phase 5 — Test-channel dry run (autopilot paused, monetization off)  ⟵ CONFIRM before first post

Run the content pipeline manually against the TEST channel and inspect output.

```bash
.venv/bin/python -m aibp.cli collect-rss
.venv/bin/python -m aibp.cli enrich
.venv/bin/python -m aibp.cli generate --slot morning   # generates into prod path;
# for a pure test-channel dry run use the stage pipeline_env (test channel):
.venv/bin/python -c "from aibp.generation import pipeline; pipeline.run(slot='morning', pipeline_env='stage')"
.venv/bin/python -m aibp.cli publish                    # publishes due posts
.venv/bin/python -m aibp.cli collect-engagement
.venv/bin/python -m aibp.cli dashboard
```

Verify on the real TEST channel: formatting, hashtag, morning bold lead-ins,
evening poll, source link, no double posts, engagement collection works. Expect
to fix small integration issues here — this is their first live run.

---

## Phase 6 — Schedule the jobs (still test channel or paused prod)

The recurring jobs are **not** run by docker-compose — use system crontab.
Copy the 16-job block from [`install.md`](install.md#шаг-8-регистрация-cron-джоб)
(collect-rss, enrich, generate morning/evening, publish, collect-engagement,
mine-patterns, safety-check, dashboard, + approvals, + redirect health check).
Note the getUpdates coordination section in install.md.

Redirect service (only if monetization will be enabled) is a systemd daemon,
not cron — see [`deploy/README.md`](../deploy/README.md).

---

## Phase 7 — Cutover to the prod channel  ⟵ CONFIRM (disables n8n)

1. Stop the n8n workflow(s) and the other Hermes agent that post to the channel
   (avoid double posting — this project must be the sole publisher).
2. Ensure cron targets the PROD path (default `pipeline_env='prod'` → `main`).
3. Keep `autopilot_paused: true`. Now running: collect → enrich → generate →
   publish → collect-engagement + dashboard, on the real channel.
4. Watch for a few days: posting cadence, quality-gate pass rate, alerts.

---

## Phase 8 — Enable monetization (optional, incremental)  ⟵ CONFIRM each

Only after the relevant infra is actually live:
- **Tracking/CTR:** set `TRACKING_BASE_URL`, deploy the redirect systemd service,
  verify `/healthz`. Clicks then flow to `link_clicks`; CTR shows on the dashboard.
- **Source button:** `telegram.source_button: true` (needs tracking).
- **Images:** ensure `IMAGE_OUTPUT_DIR` is web-served at `IMAGE_PUBLIC_BASE_URL`,
  then `visual_policy.enabled: true` + `generate: true`. Watch the OpenRouter budget.

---

## Phase 9 — Calibrate + hand over to autopilot  ⟵ CONFIRM (unpause)

After ~4–6 weeks of real engagement data:
1. Recompute the real engagement-rate variance and revisit
   `safety.min_effect_pct` / `promote_probability` (ADR-0008).
2. Review the dashboard's Bandit State and experiment history.
3. `make resume-autopilot` (or `autopilot_paused: false`) to let self-learning
   promote/reject changes. The kill switch and rollback guards stay active.

---

## Safety rails already in the system

- `autopilot_paused` kill switch; `make resume-autopilot` / `safety.py --status`.
- Rate limits (`max_changes_per_day/week`), anomaly guard (engagement/subscriber
  drop), auto-rollback, approval gate for high-risk experiment types (#20).
- Daily LLM budget guard (`OPENROUTER_DAILY_BUDGET_USD`).
- Redirect health check alerts after 3 failures (#21); TGStat/token alerts (#25).

## Rollback

- Bad posting behavior → pause autopilot + disable the relevant cron job.
- Bad deploy → `git checkout <prev>` + `make migrate` (migrations have `down()`),
  restart the redirect service.
- Emergency → re-enable n8n as the publisher and stop this project's `publish` cron.
