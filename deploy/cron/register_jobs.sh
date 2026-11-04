#!/bin/bash
# Registers the 19 AIBP jobs in Hermes cron in --no-agent mode.
#
# Schedules are MSK (the server timezone). Do NOT convert them to UTC.
# Idempotent: jobs whose name is already present in `hermes cron list --all`
# are skipped, so re-running after adding a job only creates the missing one.
#
# SAFETY: publishing starts PAUSED by default. Publisher and the four
# generation jobs are created paused so a restore can never post to the real
# channel without an explicit decision (deploy_vps.md hard gate: the first
# real post needs human confirmation). Pass --enable-publishing to start them
# active.
#
# Usage:
#   bash deploy/cron/gen_aibp_jobs.sh              # wrappers first
#   bash deploy/cron/register_jobs.sh              # publishing paused (default)
#   bash deploy/cron/register_jobs.sh --enable-publishing
#   bash deploy/cron/register_jobs.sh --dry-run    # show what would be created
set -euo pipefail

DRY_RUN=false
ENABLE_PUBLISHING=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --enable-publishing) ENABLE_PUBLISHING=true ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO="${REPO:-/root/aibp-autopilot}"
STAGE_REASON="paused: stage 9 / layer-5 criteria not met (see AGENTS.md)"
PUBLISH_REASON="paused: publishing disabled by default; pass --enable-publishing"

# Jobs that can put a post into the channel.
PUBLISHING_KEYS="publish gen_morning gen_weekly_case gen_evening gen_weekly_digest"

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
active_count=0
paused_count=0

for row in "${JOBS[@]}"; do
  IFS='|' read -r key name schedule state <<< "$row"
  reason="$STAGE_REASON"

  if [[ " $PUBLISHING_KEYS " == *" $key "* ]] && [ "$ENABLE_PUBLISHING" = false ]; then
    state="paused"
    reason="$PUBLISH_REASON"
  fi

  if [ "$state" = "active" ]; then
    active_count=$((active_count + 1))
  else
    paused_count=$((paused_count + 1))
  fi

  if grep -qF "$name" <<< "$existing"; then
    echo "skip   $name (already registered, left as is)"
    continue
  fi

  args=(create "$schedule" --name "$name" --script "aibp_${key}.sh" \
        --no-agent --deliver local --workdir "$REPO")
  if [ "$state" = "paused" ]; then
    args+=(--paused --paused-reason "$reason")
  fi

  if [ "$DRY_RUN" = true ]; then
    echo "would create $name [$state] ($schedule)"
    continue
  fi

  hermes cron "${args[@]}" >/dev/null
  echo "create $name [$state] ($schedule)"
done

echo
echo "target state: $active_count active, $paused_count paused"

if [ "$DRY_RUN" = true ]; then
  echo "dry-run: nothing was registered"
  exit 0
fi

hermes cron list --all | grep -c '^  [a-f0-9]\{12\} \[' || true
echo "jobs total (expected 19)"
