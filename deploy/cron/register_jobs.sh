#!/bin/bash
# Registers the 19 AIBP jobs in Hermes cron in --no-agent mode.
#
# Schedules are MSK (the server timezone). Do NOT convert them to UTC.
# Idempotent: jobs whose name is already present in `hermes cron list --all`
# are skipped, so re-running after adding a job only creates the missing one.
#
# Usage:
#   bash deploy/cron/gen_aibp_jobs.sh     # wrappers first
#   bash deploy/cron/register_jobs.sh
#   bash deploy/cron/register_jobs.sh --dry-run   # show what would be created
set -euo pipefail

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

REPO="${REPO:-/root/aibp-autopilot}"
ACTIVE_REASON="production"
PAUSED_REASON="paused: stage 9 / layer-5 criteria not met (see AGENTS.md)"

# key|display name|schedule MSK|state
JOBS=(
  "rss|AIBP — RSS Collector|0 * * * *|active"
  "enrich|AIBP — Enrichment|30 */2 * * *|active"
  "gen_morning|AIBP — Morning Generation|0 9 * * *|active"
  "gen_weekly_case|AIBP — Weekly Case Generation|0 9 * * *|active"
  "gen_evening|AIBP — Evening Generation|0 17 * * *|active"
  "gen_weekly_digest|AIBP — Weekly Digest Generation|0 18 * * 0|active"
  "publish|AIBP — Publisher|*/5 * * * *|active"
  "engagement|AIBP — Engagement Collector|0 */4 * * *|active"
  "safety_check|AIBP — Safety Check|0 5 * * *|active"
  "dashboard|AIBP — Dashboard|0 6 * * *|active"
  "redirect_health|AIBP — Redirect Health Check|*/5 * * * *|active"
  "gen_stage_morning|AIBP — Stage Morning Generation|30 9 * * *|paused"
  "gen_stage_evening|AIBP — Stage Evening Generation|30 17 * * *|paused"
  "mine_patterns|AIBP — Pattern Miner|0 22 * * 0|paused"
  "update_policy|AIBP — Policy Updater|0 2 * * 1|paused"
  "run_shadow|AIBP — Shadow Runner|30 2 * * *|paused"
  "decide|AIBP — Decision Engine|0 3 * * *|paused"
  "rollback_check|AIBP — Rollback Check|0 4 * * *|paused"
  "approvals|AIBP — Approval Gate|2-59/5 * * * *|paused"
)

existing="$(hermes cron list --all 2>&1 || true)"

for row in "${JOBS[@]}"; do
  IFS='|' read -r key name schedule state <<< "$row"

  if grep -qF "$name" <<< "$existing"; then
    echo "skip   $name (already registered)"
    continue
  fi

  args=(create "$schedule" --name "$name" --script "aibp_${key}.sh" \
        --no-agent --deliver local --workdir "$REPO")
  if [ "$state" = "paused" ]; then
    args+=(--paused --paused-reason "$PAUSED_REASON")
  fi

  if [ "$DRY_RUN" = true ]; then
    echo "would create $name [$state] ($schedule)"
    continue
  fi

  hermes cron "${args[@]}" >/dev/null
  echo "create $name [$state] ($schedule)"
done

if [ "$DRY_RUN" = true ]; then
  echo
  echo "dry-run: nothing was registered"
  exit 0
fi

echo
hermes cron list --all | grep -c '^  [a-f0-9]\{12\} \[' || true
echo "jobs total (expected 19, 11 active)"
