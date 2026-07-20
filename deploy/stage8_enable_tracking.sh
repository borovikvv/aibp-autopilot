#!/usr/bin/env bash
# Stage 8: enable click tracking on the VPS (owner-approved 2026-07-20).
# Idempotent — safe to re-run. Run as root on the VPS:
#   cd /root/aibp-autopilot && git pull && bash deploy/stage8_enable_tracking.sh
set -euo pipefail

REPO=/root/aibp-autopilot
CADDYFILE=/etc/caddy/Caddyfile
BASE_URL=https://aibp.borovikvv.ru

echo "── 1. TRACKING_BASE_URL in .env"
if grep -q "^TRACKING_BASE_URL=${BASE_URL}$" "$REPO/.env"; then
    echo "   already set"
elif grep -q '^TRACKING_BASE_URL=' "$REPO/.env"; then
    sed -i "s|^TRACKING_BASE_URL=.*|TRACKING_BASE_URL=${BASE_URL}|" "$REPO/.env"
    echo "   updated"
else
    echo "TRACKING_BASE_URL=${BASE_URL}" >> "$REPO/.env"
    echo "   appended"
fi

echo "── 2. Caddy: public /r/* route → 127.0.0.1:8091"
cp "$CADDYFILE" "${CADDYFILE}.bak-stage8"
if ! grep -q 'not path /r/\*' "$CADDYFILE"; then
    sed -i '/not path \/img\/\*/a\        not path /r/*' "$CADDYFILE"
fi
if ! grep -q 'reverse_proxy /r/\*' "$CADDYFILE"; then
    sed -i '/^    file_server/i\    reverse_proxy /r/* 127.0.0.1:8091' "$CADDYFILE"
fi
caddy validate --config "$CADDYFILE" >/dev/null
systemctl reload caddy
echo "   caddy reloaded"

echo "── 3. systemd unit aibp-redirect"
mkdir -p "$REPO/reports/logs"
cp "$REPO/deploy/systemd/aibp-redirect.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aibp-redirect
sleep 2
systemctl is-active aibp-redirect

echo "── 4. Health checks"
curl -fsS "http://localhost:${TRACKING_PORT:-8091}/healthz" && echo "  ← local healthz"
# Unknown short id through the public route must give 404 (not a basicauth 401)
code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/r/deadbeef")
echo "   public /r/deadbeef → HTTP ${code} (expected 404)"
[ "$code" = "404" ]

echo "── 5. Hermes: Redirect Health Check cron"
if hermes cron list --all | grep -i "redirect"; then
    id=$(hermes cron list --all | grep -i "redirect" | awk '{print $1}' | head -1)
    hermes cron resume "$id" 2>/dev/null || true
    echo "   resumed (id=$id)"
else
    echo "   NOT FOUND — register per prompts/hermes_bootstrap.md (Redirect Health Check, */5)"
fi

echo "── done: stage-8 tracking is live"
