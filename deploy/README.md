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
