#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# AIBP Autopilot — PostgreSQL setup script
# ═══════════════════════════════════════════════════════════════════
#
# What this script does:
#   1. Checks if PostgreSQL is installed
#   2. If not — installs it via apt (Debian/Ubuntu)
#   3. Creates database user 'aibp' with generated password
#   4. Creates database 'aibp' owned by that user
#   5. Writes DATABASE_URL to .env (creates .env from .env.example if missing)
#   6. Verifies connection
#
# Usage:
#   bash scripts/setup_postgres.sh          # interactive
#   bash scripts/setup_postgres.sh --docker # run PostgreSQL in Docker
#   bash scripts/setup_postgres.sh --no-install # skip apt install, just create user/db
#
# Exit codes:
#   0 — success, DATABASE_URL is in .env
#   1 — failure (see error message)
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log()   { echo -e "${GREEN}[$(date +%H:%M:%S)]${NC} $*"; }
warn()  { echo -e "${YELLOW}[$(date +%H:%M:%S)] WARN:${NC} $*"; }
error() { echo -e "${RED}[$(date +%H:%M:%S)] ERROR:${NC} $*" >&2; }
info()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }

# Resolve project root (script is in scripts/ subdirectory)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"

DB_NAME="aibp"
DB_USER="aibp"
DB_HOST="localhost"
DB_PORT="5432"

# ─── Parse args ─────────────────────────────────────────────────────
USE_DOCKER=false
NO_INSTALL=false

for arg in "$@"; do
    case $arg in
        --docker)
            USE_DOCKER=true
            ;;
        --no-install)
            NO_INSTALL=true
            ;;
        --help|-h)
            cat <<EOF
AIBP Autopilot — PostgreSQL setup

Usage:
  bash scripts/setup_postgres.sh [options]

Options:
  --docker       Run PostgreSQL in Docker container (instead of apt install)
  --no-install   Skip installation, only create user/db (assume PostgreSQL already installed)
  --help         Show this help

Examples:
  bash scripts/setup_postgres.sh              # install via apt + create user/db
  bash scripts/setup_postgres.sh --docker     # run in Docker
  bash scripts/setup_postgres.sh --no-install # only create user/db
EOF
            exit 0
            ;;
        *)
            error "Unknown argument: $arg"
            exit 1
            ;;
    esac
done

# ═══════════════════════════════════════════════════════════════════
# Step 1: Check if .env exists, create from .env.example if not
# ═══════════════════════════════════════════════════════════════════
info "Step 1: Prepare .env file"

if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        log "Created .env from .env.example"
    else
        error "Neither .env nor .env.example found in $PROJECT_ROOT"
        exit 1
    fi
else
    log ".env already exists"
fi

# ═══════════════════════════════════════════════════════════════════
# Step 2: Install or verify PostgreSQL
# ═══════════════════════════════════════════════════════════════════
info "Step 2: Ensure PostgreSQL is available"

if [ "$USE_DOCKER" = true ]; then
    # ─── Docker mode ───────────────────────────────────────────────
    if ! command -v docker &>/dev/null; then
        error "Docker is not installed. Install it first:"
        error "  curl -fsSL https://get.docker.com | sh"
        exit 1
    fi

    # Check if container already running
    if docker ps --format '{{.Names}}' | grep -q '^aibp-postgres$'; then
        log "PostgreSQL container 'aibp-postgres' already running"
    else
        # Generate password
        DB_PASSWORD=$(openssl rand -hex 16 2>/dev/null || head -c 32 /dev/urandom | xxd -p | head -c 32)
        info "Starting PostgreSQL in Docker..."
        docker run -d \
            --name aibp-postgres \
            --restart unless-stopped \
            -e POSTGRES_USER="$DB_USER" \
            -e POSTGRES_PASSWORD="$DB_PASSWORD" \
            -e POSTGRES_DB="$DB_NAME" \
            -p 5432:5432 \
            -v aibp_pgdata:/var/lib/postgresql/data \
            postgres:16-alpine

        log "PostgreSQL container started"
        # Wait for it to be ready
        info "Waiting for PostgreSQL to be ready..."
        for i in $(seq 1 30); do
            if docker exec aibp-postgres pg_isready -U "$DB_USER" &>/dev/null; then
                log "PostgreSQL is ready"
                break
            fi
            sleep 1
        done
    fi

    # Get password from container if already running
    if [ -z "${DB_PASSWORD:-}" ]; then
        DB_PASSWORD=$(docker inspect aibp-postgres \
            --format '{{range .Config.Env}}{{println .}}{{end}}' \
            | grep POSTGRES_PASSWORD \
            | cut -d= -f2)
    fi

    DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

else
    # ─── apt mode ──────────────────────────────────────────────────
    if ! command -v psql &>/dev/null; then
        if [ "$NO_INSTALL" = true ]; then
            error "PostgreSQL not found, but --no-install was specified"
            exit 1
        fi

        if ! command -v apt-get &>/dev/null; then
            error "This script supports only Debian/Ubuntu (apt)."
            error "For other OS: install PostgreSQL manually, then run with --no-install"
            exit 1
        fi

        info "Installing PostgreSQL via apt..."
        export DEBIAN_FRONTEND=noninteractive
        sudo apt-get update -qq
        # postgresql-16-pgvector enables the `vector` type (migration 0010 /
        # competitor_posts dedup-by-embeddings, issue #40). Match the PG major
        # version: the default on Debian/Ubuntu here is 16.
        sudo apt-get install -y -qq postgresql postgresql-contrib postgresql-16-pgvector
        log "PostgreSQL installed"

        # Start service
        sudo systemctl enable postgresql
        sudo systemctl start postgresql
        log "PostgreSQL service started"
    else
        log "PostgreSQL already installed: $(psql --version)"
    fi

    # Ensure service is running
    if ! sudo systemctl is-active --quiet postgresql 2>/dev/null; then
        warn "PostgreSQL service not active, starting..."
        sudo systemctl start postgresql 2>/dev/null || true
    fi

    # ═══════════════════════════════════════════════════════════════
    # Step 3: Create database user and database
    # ═══════════════════════════════════════════════════════════════
    info "Step 3: Create database user '$DB_USER' and database '$DB_NAME'"

    # Check if user already exists
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
        log "Database user '$DB_USER' already exists"
        # Reset password to a known value
        DB_PASSWORD=$(openssl rand -hex 16 2>/dev/null || head -c 32 /dev/urandom | xxd -p | head -c 32)
        sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" >/dev/null
        log "Password for '$DB_USER' updated"
    else
        DB_PASSWORD=$(openssl rand -hex 16 2>/dev/null || head -c 32 /dev/urandom | xxd -p | head -c 32)
        sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" >/dev/null
        log "User '$DB_USER' created"
    fi

    # Check if database exists
    if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
        log "Database '$DB_NAME' already exists"
    else
        sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" >/dev/null
        log "Database '$DB_NAME' created"
    fi

    # Grant privileges
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" >/dev/null
    log "Privileges granted"

    # Create the pgvector extension as superuser (migration 0010 also runs
    # CREATE EXTENSION IF NOT EXISTS, but that requires superuser privileges —
    # creating it here means the `aibp` user can rely on it). Needed for the
    # competitor_posts dedup-by-embeddings table (issue #40).
    if sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null 2>&1; then
        log "pgvector extension ready in '$DB_NAME'"
    else
        warn "Could not create pgvector extension — install postgresql-16-pgvector"
        warn "(migration 0010 will retry; needs superuser)"
    fi

    # Allow password auth (modify pg_hba.conf if needed)
    PG_HBA=$(sudo -u postgres psql -tAc "SHOW hba_file")
    if [ -f "$PG_HBA" ]; then
        if ! grep -q "host.*all.*all.*127.0.0.1/32.*md5\|host.*all.*all.*127.0.0.1/32.*scram-sha-256" "$PG_HBA"; then
            warn "Adding password auth rule to pg_hba.conf"
            echo "host    all             all             127.0.0.1/32            scram-sha-256" | sudo tee -a "$PG_HBA" > /dev/null
            sudo systemctl reload postgresql
        fi
    fi

    DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

# ═══════════════════════════════════════════════════════════════════
# Step 4: Write DATABASE_URL to .env
# ═══════════════════════════════════════════════════════════════════
info "Step 4: Write DATABASE_URL to .env"

# URL-encode the password (in case it has special chars — unlikely with hex, but be safe)
ENCODED_PASSWORD=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$DB_PASSWORD', safe=''))" 2>/dev/null || echo "$DB_PASSWORD")
DATABASE_URL_ENCODED="postgresql://${DB_USER}:${ENCODED_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"

# Update or add DATABASE_URL in .env
if grep -q "^DATABASE_URL=" "$ENV_FILE"; then
    # Replace existing
    if [[ "$(uname)" == "Darwin" ]]; then
        # macOS sed needs -i ''
        sed -i '' "s|^DATABASE_URL=.*|DATABASE_URL=$DATABASE_URL_ENCODED|" "$ENV_FILE"
    else
        sed -i "s|^DATABASE_URL=.*|DATABASE_URL=$DATABASE_URL_ENCODED|" "$ENV_FILE"
    fi
    log "DATABASE_URL updated in .env"
else
    # Append
    echo "" >> "$ENV_FILE"
    echo "DATABASE_URL=$DATABASE_URL_ENCODED" >> "$ENV_FILE"
    log "DATABASE_URL added to .env"
fi

# ═══════════════════════════════════════════════════════════════════
# Step 5: Verify connection
# ═══════════════════════════════════════════════════════════════════
info "Step 5: Verify connection"

export PGPASSWORD="$DB_PASSWORD"
if psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" >/dev/null 2>&1; then
    log "✅ Connection to PostgreSQL successful"
else
    # Try via Docker exec
    if [ "$USE_DOCKER" = true ]; then
        if docker exec aibp-postgres psql -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1;" >/dev/null 2>&1; then
            log "✅ Connection via Docker exec successful"
        else
            error "Cannot connect to PostgreSQL"
            exit 1
        fi
    else
        error "Cannot connect to PostgreSQL. Check service status:"
        error "  sudo systemctl status postgresql"
        exit 1
    fi
fi

# ═══════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "════════════════════════════════════════════════════════════════"
echo -e "${GREEN}✅ PostgreSQL setup complete${NC}"
echo "════════════════════════════════════════════════════════════════"
echo ""
echo "Database: $DB_NAME"
echo "User:     $DB_USER"
echo "Host:     $DB_HOST:$DB_PORT"
echo "URL:      $DATABASE_URL_ENCODED"
echo ""
echo "Next steps:"
echo "  1. Fill in other secrets in .env (TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY, etc.)"
echo "  2. Run: make db-init"
echo "  3. Run: make smoke-test"
echo "════════════════════════════════════════════════════════════════"
