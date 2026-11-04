#!/bin/bash
# Generates one wrapper script per AIBP cron job (Hermes --no-agent mode).
#
# Each wrapper runs a Python module with the SYSTEM interpreter and appends a
# "[date] command rc=N" line to reports/logs/cron.log. Hermes runs the wrapper
# on schedule and delivers its stdout; the LLM is never invoked.
#
# Usage:
#   bash deploy/cron/gen_aibp_jobs.sh
#
# Overridable env:
#   REPO          checkout path           (default /root/aibp-autopilot)
#   SCRIPTS_DIR   Hermes scripts dir      (default ~/.hermes/scripts)
#   PYTHON        interpreter             (default /usr/bin/python3)
#
# IMPORTANT: keep PYTHON=/usr/bin/python3. A bare `python3` may resolve to the
# Hermes venv, which does not have the `aibp` package installed.
set -euo pipefail

REPO="${REPO:-/root/aibp-autopilot}"
SCRIPTS_DIR="${SCRIPTS_DIR:-$HOME/.hermes/scripts}"
PYTHON="${PYTHON:-/usr/bin/python3}"

mkdir -p "$SCRIPTS_DIR" "$REPO/reports/logs"

declare -A JOBS=(
  [rss]="collect-rss"
  [enrich]="enrich"
  [gen_morning]="generate --slot morning"
  [gen_weekly_case]="generate --slot weekly_case"
  [gen_evening]="generate --slot evening"
  [gen_weekly_digest]="generate --slot weekly_digest"
  [gen_stage_morning]="generate --slot morning --env stage"
  [gen_stage_evening]="generate --slot evening --env stage"
  [publish]="publish"
  [engagement]="collect-engagement"
  [mine_patterns]="mine-patterns"
  [update_policy]="update-policy"
  [run_shadow]="run-shadow"
  [decide]="decide"
  [rollback_check]="rollback-check"
  [safety_check]="safety-check"
  [dashboard]="dashboard"
)

for key in "${!JOBS[@]}"; do
  cmd="${JOBS[$key]}"
  cat > "$SCRIPTS_DIR/aibp_${key}.sh" << EOF
#!/bin/bash
cd $REPO
out=\$($PYTHON -m aibp.cli ${cmd} 2>&1)
rc=\$?
{ echo "[\$(date -Is)] ${cmd} rc=\$rc"; echo "\$out"; } >> $REPO/reports/logs/cron.log
if [ \$rc -ne 0 ]; then echo "AIBP ${cmd} FAILED rc=\$rc"; echo "\$out" | tail -20; fi
EOF
done

# approvals + redirect healthcheck are python modules, not cli subcommands
for pair in "approvals:aibp.self_learning.approvals" "redirect_health:aibp.tracking.healthcheck"; do
  key="${pair%%:*}"; mod="${pair#*:}"
  cat > "$SCRIPTS_DIR/aibp_${key}.sh" << EOF
#!/bin/bash
cd $REPO
out=\$($PYTHON -m ${mod} 2>&1)
rc=\$?
{ echo "[\$(date -Is)] ${mod} rc=\$rc"; echo "\$out"; } >> $REPO/reports/logs/cron.log
if [ \$rc -ne 0 ]; then echo "AIBP ${mod} FAILED rc=\$rc"; echo "\$out" | tail -20; fi
EOF
done

chmod +x "$SCRIPTS_DIR"/aibp_*.sh
echo "wrappers written to $SCRIPTS_DIR: $(ls "$SCRIPTS_DIR" | grep -c '^aibp_.*\.sh$')"
