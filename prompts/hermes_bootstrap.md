# Hermes Bootstrap: AIBP Autopilot — Full Deployment

## Mission

You are deploying the AIBP Autopilot project on this server. This is a fully autonomous Telegram channel management system.

**Repository:** https://github.com/borovikvv/aibp-autopilot
**Target channel:** @AI_Business_Pulse
**Project root:** `/root/aibp-autopilot`

## Prerequisites

Before starting, you MUST collect these secrets from the user (Vyacheslav):

1. **TELEGRAM_BOT_TOKEN** — get from @BotFather (existing bot or new one)
2. **TELEGRAM_CHANNEL_ID_PROD** — numeric ID of @AI_Business_Pulse (with -100 prefix)
3. **TELEGRAM_CHANNEL_ID_TEST** — numeric ID of test channel (with -100 prefix)
4. **TELEGRAM_ALERT_CHAT_ID** — Vyacheslav's personal chat ID (for alerts)
5. **OPENROUTER_API_KEY** — get from https://openrouter.ai/keys
6. **XAI_API_KEY** (optional) — for image generation, from https://x.ai/api

If any of these are missing, STOP and ask Vyacheslav before proceeding.

The bot MUST be admin in both channels. If not, ask Vyacheslav to add it first.

## Execution Plan

Execute these steps IN ORDER. After each step, report the result. If a step fails, stop and report the error.

### Step 1: Clone repository

```bash
cd /root
if [ -d aibp-autopilot ]; then
    cd aibp-autopilot
    git pull origin main
else
    git clone https://github.com/borovikvv/aibp-autopilot.git
    cd aibp-autopilot
fi
```

Verify: `ls -la /root/aibp-autopilot/Makefile` should exist.

### Step 2: Run bootstrap script

```bash
cd /root/aibp-autopilot
python3 scripts/bootstrap.py
```

This will:
- Check Python version and tools
- Install Python dependencies
- Install and configure PostgreSQL (or run in Docker)
- Initialize database schema
- Run smoke test

**IMPORTANT:** If bootstrap returns exit code 2, it means secrets are missing in `.env`. Proceed to Step 3.

### Step 3: Fill in secrets

If bootstrap reported missing secrets, edit `.env`:

```bash
nano /root/aibp-autopilot/.env
```

Fill in these values (Vyacheslav must provide):

```
TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_CHANNEL_ID_PROD=-1003300906776
TELEGRAM_CHANNEL_ID_TEST=-1003825827505
TELEGRAM_ALERT_CHAT_ID=<Vyacheslav's chat_id>
OPENROUTER_API_KEY=<from openrouter.ai/keys>
XAI_API_KEY=<optional, from x.ai/api>
```

After saving, re-run smoke test:

```bash
cd /root/aibp-autopilot
python3 scripts/bootstrap.py --skip-postgres
```

This should now pass all checks.

### Step 4: Verify end-to-end

Run the full pipeline manually once to verify everything works:

```bash
cd /root/aibp-autopilot
make run-all
```

This will:
1. Collect RSS feeds (1-2 minutes)
2. Enrich new items via LLM (~30 seconds per item)
3. Generate morning post (~20 seconds)
4. Publish to Telegram

Check the test channel — there should be a new post. If yes, deployment is successful.

### Step 5: Register Hermes cron jobs

Register these 17 cron jobs in Hermes. Run each `cronjob(action='create', ...)` call:

```python
# RSS Collector — every hour
cronjob(
    action='create',
    name='AIBP — RSS Collector',
    schedule='0 * * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli collect-rss 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Enrichment — every 2 hours
cronjob(
    action='create',
    name='AIBP — Enrichment',
    schedule='30 */2 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli enrich 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Morning Generation — 09:00 MSK (06:00 UTC)
cronjob(
    action='create',
    name='AIBP — Morning Generation',
    schedule='0 6 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot morning 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Weekly Case Generation — 09:00 MSK (06:00 UTC), same time as morning (issue #40).
# Runs daily alongside Morning Generation. On the configured weekly_case weekday
# (policy.weekly_case.weekday) _should_skip_for_weekly_case makes Morning skip
# and weekly_case fire instead; on every other day weekly_case skips and morning
# runs as usual — so exactly one of the two produces a post each day.
cronjob(
    action='create',
    name='AIBP — Weekly Case Generation',
    schedule='0 6 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot weekly_case 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Evening Generation — 17:00 MSK (14:00 UTC)
cronjob(
    action='create',
    name='AIBP — Evening Generation',
    schedule='0 14 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot evening 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Stage Morning Generation (shadow test) — 09:30 MSK (06:30 UTC)
# Generates post for TEST channel using policy.stage.yaml (if exists)
cronjob(
    action='create',
    name='AIBP — Stage Morning Generation',
    schedule='30 6 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot morning --env stage 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Stage Evening Generation (shadow test) — 17:30 MSK (14:30 UTC)
cronjob(
    action='create',
    name='AIBP — Stage Evening Generation',
    schedule='30 14 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli generate --slot evening --env stage 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Publisher — every 5 minutes
cronjob(
    action='create',
    name='AIBP — Publisher',
    schedule='*/5 * * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli publish 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Engagement Collector — every 4 hours
# NOTE (issue #24): the collector uses getUpdates ONLY as a fallback when
# TELEGRAM_METRICS_CHAT_ID is unset. getUpdates is exclusive per bot, so it
# shares a cross-process lock with the Approval Gate job below (never a 409
# from concurrency). Set TELEGRAM_METRICS_CHAT_ID so the collector uses
# copyMessage and the Approval Gate owns getUpdates exclusively.
cronjob(
    action='create',
    name='AIBP — Engagement Collector',
    schedule='0 */4 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli collect-engagement 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Pattern Miner — Sundays 22:00 MSK (19:00 UTC)
cronjob(
    action='create',
    name='AIBP — Pattern Miner',
    schedule='0 19 * * 0',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli mine-patterns 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Policy Updater — Mondays 02:00 MSK (23:00 UTC Sunday)
cronjob(
    action='create',
    name='AIBP — Policy Updater',
    schedule='0 23 * * 0',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli update-policy 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Shadow Runner — daily 02:30 MSK (23:30 UTC)
cronjob(
    action='create',
    name='AIBP — Shadow Runner',
    schedule='30 23 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli run-shadow 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Decision Engine — daily 03:00 MSK (00:00 UTC)
cronjob(
    action='create',
    name='AIBP — Decision Engine',
    schedule='0 0 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli decide 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Rollback Check — daily 04:00 MSK (01:00 UTC)
cronjob(
    action='create',
    name='AIBP — Rollback Check',
    schedule='0 1 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli rollback-check 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Safety Check — daily 05:00 MSK (02:00 UTC)
cronjob(
    action='create',
    name='AIBP — Safety Check',
    schedule='0 2 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli safety-check 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Dashboard — daily 06:00 MSK (03:00 UTC)
cronjob(
    action='create',
    name='AIBP — Dashboard',
    schedule='0 3 * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.cli dashboard 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Approval Gate — every 5 minutes, offset by 2 min so it never starts at :00
# together with the Engagement Collector (issue #24). Processes approve/reject
# button taps for high-risk experiments; shares the getUpdates lock.
cronjob(
    action='create',
    name='AIBP — Approval Gate',
    schedule='2-59/5 * * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.self_learning.approvals 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)

# Redirect Health Check — every 5 minutes; alerts after 3 consecutive failures
# (issue #21). Monitors the redirect DAEMON registered in Step 5.5 below.
cronjob(
    action='create',
    name='AIBP — Redirect Health Check',
    schedule='*/5 * * * *',
    prompt='cd /root/aibp-autopilot && python3 -m aibp.tracking.healthcheck 2>&1',
    workdir='/root/aibp-autopilot',
    deliver='origin',
    enabled_toolsets=['terminal'],
)
```

### Step 5.5: Register the redirect service (persistent daemon, NOT cron)

The click-tracking redirect service (`aibp.tracking.redirect_service`) is a
long-running process, so it is **not** a cron job — it must run 24/7 and
restart on reboot. Requires `TRACKING_BASE_URL` in `.env`. Register it as a
systemd unit (preferred on a VPS):

```bash
terminal('sudo cp /root/aibp-autopilot/deploy/systemd/aibp-redirect.service /etc/systemd/system/ '
         '&& sudo systemctl daemon-reload && sudo systemctl enable --now aibp-redirect '
         '&& systemctl is-active aibp-redirect')
```

Then confirm health:

```bash
terminal('curl -fsS http://localhost:${TRACKING_PORT:-8091}/healthz')   # → ok
```

If the deployment is Docker-based instead, run `docker compose up -d redirect`
from `docker/` (service defined in `docker/docker-compose.yml`). See
`deploy/README.md` for both paths.

### Step 6: Final verification

After all cron jobs are registered, verify by listing them:

```python
cronjob(action='list')
```

You should see 13 jobs with names starting with "AIBP —".

## Reporting

After completing all steps, report to Vyacheslav:

1. ✅ or ❌ for each step
2. URL of the test channel post (if Step 4 succeeded)
3. List of registered cron jobs
4. Any warnings or issues encountered

## Failure handling

If any step fails:
1. Report the exact error message
2. Do NOT continue to next steps
3. Suggest a fix based on the error
4. If the error is about missing secrets — ask Vyacheslav to provide them

## What NOT to do

- Do NOT modify files in `src/aibp/` or `config/` (these are versioned in git)
- Do NOT change the database schema manually
- Do NOT disable safety rails in `policy.yaml`
- Do NOT run cron jobs manually except for testing in Step 4
- Do NOT publish to production channel (@AI_Business_Pulse) during testing — only test channel
