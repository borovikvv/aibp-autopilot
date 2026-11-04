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

Register all 19 cron jobs in Hermes in `--no-agent` mode (the LLM is never
called — each job runs a small bash wrapper and delivers its stdout). Wrappers
and registration live in the repo:

```bash
cd /root/aibp-autopilot
bash deploy/cron/gen_aibp_jobs.sh      # writes ~/.hermes/scripts/aibp_*.sh
bash deploy/cron/register_jobs.sh      # hermes cron create for all 19 jobs
```

The registration script is idempotent: it skips a job whose name already exists
in `hermes cron list --all`. **Schedules are in MSK** (the server timezone) —
do not convert them to UTC. 11 jobs start active; 8 start paused (stage
generations + self-learning) and must stay paused until the layer-5 criteria in
`AGENTS.md` are met.

Verify:

```bash
hermes cron list --all
# → 19 jobs with names starting with "AIBP —", 11 active / 8 paused
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

You should see 19 jobs with names starting with "AIBP —".

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
