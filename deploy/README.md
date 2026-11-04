# Deployment artifacts

## Click-tracking redirect service (issue #21)

The redirect service (`aibp.tracking.redirect_service`) is the only long-running
daemon in AIBP — every other component is a cron job. It serves
`/r/{short_id} → 302` and logs clicks. If it is down, all tracked links in
published posts stop working, so it must run 24/7 and survive host reboots.

Pick **one** of the two options below.

### Option A — systemd (bare-metal / VPS)

```bash
# 1. Copy the unit (adjust User/paths inside if your checkout is not /root/aibp-autopilot)
sudo cp deploy/systemd/aibp-redirect.service /etc/systemd/system/

# 2. Enable + start (enable = auto-start on reboot)
sudo systemctl daemon-reload
sudo systemctl enable --now aibp-redirect

# 3. Verify
systemctl status aibp-redirect
curl -fsS http://localhost:${TRACKING_PORT:-8091}/healthz   # → ok

# 4. Verify it survives a reboot
sudo reboot          # ... then after boot:
systemctl is-active aibp-redirect                            # → active
```

`Restart=on-failure` restarts the process if it crashes; `enable` brings it
back after a reboot.

### Option B — Docker Compose

The `redirect` service is defined in [`docker/docker-compose.yml`](../docker/docker-compose.yml)
with `restart: unless-stopped` and a `/healthz` healthcheck:

```bash
cd docker
docker compose up -d redirect
docker compose ps           # redirect should be "healthy"
```

## Uptime monitoring

Regardless of A or B, register the health-check cron so an outage pages you:

```cron
# Redirect health check — every 5 minutes; alerts after 3 consecutive failures
*/5 * * * * cd /root/aibp-autopilot && python3 -m aibp.tracking.healthcheck >> reports/logs/cron.log 2>&1
```

It curls `/healthz`, and after 3 failures in a row sends a Telegram alert to
`TELEGRAM_ALERT_CHAT_ID` (a single blip does not page; a real outage does).

## Public route (Caddy)

The VPS uses **Caddy**, not nginx. The production site block is versioned as
[`caddy/Caddyfile.example`](caddy/Caddyfile.example):

- `/r/*` → `reverse_proxy 127.0.0.1:8091` (tracked links, public);
- `/img/*` → static generated images (public);
- everything else → basic-auth protected dashboard.

`deploy/stage8_enable_tracking.sh` edits an existing `/etc/caddy/Caddyfile`
(idempotently) — use it on an already-provisioned host. On a fresh host, merge
the template into the Caddyfile, then `caddy validate` + `systemctl reload caddy`.
Never commit the real bcrypt hash; generate one locally with
`caddy hash-password`.

## Cron jobs (Hermes, `--no-agent`)

All 19 recurring jobs are Hermes cron jobs that run bash wrappers — no LLM is
invoked. Both halves live in [`cron/`](cron/):

```bash
bash deploy/cron/gen_aibp_jobs.sh      # → ~/.hermes/scripts/aibp_*.sh
bash deploy/cron/register_jobs.sh      # → hermes cron create × 19
```

`register_jobs.sh` is idempotent. Publishing starts **paused** by default:
Publisher + the four generation jobs are created paused so a fresh restore
cannot post to the real channel without an explicit decision. Default target is
6 active / 13 paused; pass `--enable-publishing` to start the 5 publishing jobs
active (11 active / 8 paused). Schedules are **MSK** (server timezone).
Wrappers must call `/usr/bin/python3`: a bare `python3` may resolve to the
Hermes venv, which lacks the `aibp` package.

For a host without Hermes, see the plain crontab block in
[`../docs/install.md`](../docs/install.md#шаг-8-регистрация-cron-джоб).

Full freeze/restore checklist (what is not in git: `.env`, DB dump, Caddy
hash): [`../docs/freeze_restore.md`](../docs/freeze_restore.md).

