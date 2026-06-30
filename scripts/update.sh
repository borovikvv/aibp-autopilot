#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# AIBP Autopilot — Update script
# ═══════════════════════════════════════════════════════════════════
#
# Pulls latest changes from git and applies all updates:
#   1. git pull
#   2. pip install -r requirements.txt (if changed)
#   3. Database migrations
#   4. Verification
#
# Does NOT touch:
#   - .env (your secrets)
#   - data/ (SQLite experiments)
#   - reports/ (logs, dashboards)
#   - Hermes cron jobs (use --rebuild-cron for that)
#
# Usage:
#   bash scripts/update.sh                  # full update
#   bash scripts/update.sh --check          # check for updates without applying
#   bash scripts/update.sh --rebuild-cron   # also re-register Hermes cron jobs
#   bash scripts/update.sh --no-pull        # apply local changes without git pull
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn()  { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $*"; }
error() { echo -e "${RED}[$(date +%H:%M:%S)] ERROR:${NC} $*" >&2; }
info()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

CHECK_ONLY=false
REBUILD_CRON=false
NO_PULL=false

for arg in "$@"; do
    case $arg in
        --check) CHECK_ONLY=true ;;
        --rebuild-cron) REBUILD_CRON=true ;;
        --no-pull) NO_PULL=true ;;
        --help|-h)
            cat <<EOF
AIBP Autopilot — Update

Usage:
  bash scripts/update.sh [options]

Options:
  --check         Check for updates without applying (git fetch + diff)
  --rebuild-cron  Also re-register Hermes cron jobs after update
  --no-pull       Apply local changes without git pull
  --help          Show this help
EOF
            exit 0
            ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════
# Step 1: Check for updates
# ═══════════════════════════════════════════════════════════════════
info "Step 1: Check for updates"

if [ "$NO_PULL" = false ]; then
    git fetch origin main

    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)

    if [ "$LOCAL" = "$REMOTE" ] && [ -z "$(git status --porcelain)" ]; then
        log "Already up to date (commit $LOCAL)"
        if [ "$CHECK_ONLY" = true ]; then
            exit 0
        fi
        # Still check for pending migrations
        info "Checking for pending migrations..."
        python3 -m aibp.db.migrate --status
        exit 0
    fi

    if [ "$CHECK_ONLY" = true ]; then
        info "Updates available:"
        git log --oneline HEAD..origin/main
        echo ""
        info "Files changed:"
        git diff --stat HEAD origin/main
        exit 0
    fi
fi

# ═══════════════════════════════════════════════════════════════════
# Step 2: Stash local changes if any
# ═══════════════════════════════════════════════════════════════════
info "Step 2: Preserve local changes"

STASHED=false
if [ -n "$(git status --porcelain)" ]; then
    # Stash only tracked files (.env, data/, reports/ are in .gitignore)
    warn "Local changes detected, stashing..."
    git stash push -m "auto-stash before update $(date +%Y%m%d_%H%M%S)"
    STASHED=true
    log "Changes stashed"
else
    log "No local changes to stash"
fi

# ═══════════════════════════════════════════════════════════════════
# Step 3: Pull latest
# ═══════════════════════════════════════════════════════════════════
if [ "$NO_PULL" = false ]; then
    info "Step 3: Pull latest changes"
    git pull origin main
    log "Code updated"
fi

# ═══════════════════════════════════════════════════════════════════
# Step 4: Restore stashed changes
# ═══════════════════════════════════════════════════════════════════
if [ "$STASHED" = true ]; then
    info "Step 4: Restore local changes"
    if git stash pop; then
        log "Local changes restored"
    else
        error "Conflict when restoring stashed changes"
        error "Your changes are in: git stash list"
        error "Resolve manually: git stash show -p | git apply --3way"
        exit 1
    fi
fi

# ═══════════════════════════════════════════════════════════════════
# Step 5: Install/update Python dependencies
# ═══════════════════════════════════════════════════════════════════
info "Step 5: Update Python dependencies"

# Check if requirements.txt changed in this pull
if git log -1 --name-only --format="" | grep -q "requirements.txt"; then
    warn "requirements.txt changed, reinstalling..."
    pip3 install --break-system-packages -r requirements.txt 2>/dev/null \
        || pip3 install -r requirements.txt
    pip3 install --break-system-packages -e . 2>/dev/null \
        || pip3 install -e .
    log "Dependencies updated"
else
    log "requirements.txt unchanged, skipping"
fi

# ═══════════════════════════════════════════════════════════════════
# Step 6: Apply database migrations
# ═══════════════════════════════════════════════════════════════════
info "Step 6: Apply database migrations"
python3 -m aibp.db.migrate
log "Migrations applied"

# ═══════════════════════════════════════════════════════════════════
# Step 7: Verify
# ═══════════════════════════════════════════════════════════════════
info "Step 7: Verify"
if python3 -m aibp.cli smoke-test 2>&1 | grep -q "All checks passed"; then
    log "Smoke test passed ✓"
else
    error "Smoke test failed! Check:"
    error "  python3 -m aibp.cli smoke-test"
    exit 1
fi

# ═══════════════════════════════════════════════════════════════════
# Step 8: Re-register Hermes cron jobs (optional)
# ═══════════════════════════════════════════════════════════════════
if [ "$REBUILD_CRON" = true ]; then
    info "Step 8: Re-register Hermes cron jobs"
    warn "This will be done by Hermes Agent. See prompts/hermes_bootstrap.md"
    warn "After this script completes, ask Hermes to re-register cron jobs."
fi

# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "════════════════════════════════════════════════════════════════"
echo -e "${GREEN}✅ Update complete${NC}"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Current commit: $(git rev-parse --short HEAD)"
echo "Commit message: $(git log -1 --format='%s')"
echo ""
echo "If cron schedules changed, ask Hermes to re-register jobs."
echo "Otherwise — system is ready, cron jobs will pick up new code automatically."
echo "════════════════════════════════════════════════════════════════"
