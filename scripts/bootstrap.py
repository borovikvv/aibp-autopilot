#!/usr/bin/env python3
"""AIBP Autopilot — Bootstrap orchestrator.

This script is the SINGLE ENTRY POINT for deployment.
Hermes Agent runs this one script, and it sets up everything.

Usage:
    python3 scripts/bootstrap.py                    # full setup
    python3 scripts/bootstrap.py --skip-postgres    # if PostgreSQL already configured
    python3 scripts/bootstrap.py --check-only       # just verify, don't change anything
    python3 scripts/bootstrap.py --docker           # use Docker for PostgreSQL

What it does (in order):
    1. Verify environment (Python version, OS, required tools)
    2. Clone/update repo (if running standalone)
    3. Install Python dependencies (pip install -r requirements.txt)
    4. Setup PostgreSQL (via scripts/setup_postgres.sh)
    5. Initialize database schema (make db-init)
    6. Prompt user to fill .env secrets (TELEGRAM_BOT_TOKEN, OPENROUTER_API_KEY)
    7. Run smoke test (verify DB, Telegram, OpenRouter connectivity)
    8. Print next steps (how to register Hermes cron jobs)
"""
from __future__ import annotations

import os
import subprocess
import sys
import shutil
from pathlib import Path

# Colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
BOLD = "\033[1m"
NC = "\033[0m"

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(f"{GREEN}[bootstrap]{NC} {msg}")

def info(msg: str) -> None:
    print(f"{BLUE}[bootstrap]{NC} {msg}")

def warn(msg: str) -> None:
    print(f"{YELLOW}[bootstrap] WARN:{NC} {msg}")

def error(msg: str) -> None:
    print(f"{RED}[bootstrap] ERROR:{NC} {msg}", file=sys.stderr)

def header(msg: str) -> None:
    print(f"\n{BOLD}{BLUE}═══ {msg} ═══{NC}")


def run(cmd: list[str] | str, check: bool = True, capture: bool = False, shell: bool = False) -> subprocess.CompletedProcess:
    """Run shell command."""
    if isinstance(cmd, str) and not shell:
        cmd = cmd.split()
    return subprocess.run(
        cmd,
        check=check,
        shell=shell,
        capture_output=capture,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def check_environment() -> bool:
    """Step 1: Verify environment."""
    header("Step 1: Check environment")

    # Python version
    py_version = sys.version_info
    if py_version < (3, 11):
        error(f"Python 3.11+ required, got {py_version.major}.{py_version.minor}")
        return False
    log(f"Python {py_version.major}.{py_version.minor}.{py_version.micro} ✓")

    # pip
    if not shutil.which("pip3") and not shutil.which("pip"):
        error("pip not found. Install: sudo apt-get install python3-pip")
        return False
    log("pip ✓")

    # git
    if not shutil.which("git"):
        error("git not found. Install: sudo apt-get install git")
        return False
    log("git ✓")

    # OS info
    import platform
    info(f"OS: {platform.system()} {platform.release()}")
    info(f"Project root: {PROJECT_ROOT}")

    return True


def install_python_deps() -> bool:
    """Step 2: Install Python dependencies."""
    header("Step 2: Install Python dependencies")

    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.exists():
        error(f"requirements.txt not found at {req_file}")
        return False

    log("Installing Python dependencies (this may take 1-2 minutes)...")
    try:
        run("pip3 install --break-system-packages -r requirements.txt", check=True)
        run("pip3 install --break-system-packages -e .", check=True)
        log("Dependencies installed ✓")
        return True
    except subprocess.CalledProcessError as e:
        # Try without --break-system-packages (for venv)
        warn("Retrying without --break-system-packages (assuming venv)...")
        try:
            run("pip3 install -r requirements.txt", check=True)
            run("pip3 install -e .", check=True)
            log("Dependencies installed ✓")
            return True
        except subprocess.CalledProcessError as e2:
            error(f"Failed to install dependencies: {e2}")
            return False


def setup_postgres(use_docker: bool = False, skip: bool = False) -> bool:
    """Step 3: Setup PostgreSQL."""
    header("Step 3: Setup PostgreSQL")

    if skip:
        warn("Skipping PostgreSQL setup (--skip-postgres)")
        # Verify .env has DATABASE_URL
        env_file = PROJECT_ROOT / ".env"
        if not env_file.exists():
            error(".env file not found. Run without --skip-postgres first.")
            return False
        content = env_file.read_text()
        if "DATABASE_URL=" not in content or "postgresql://aibp:aibp@" in content:
            error("DATABASE_URL not configured in .env")
            return False
        log("DATABASE_URL already configured ✓")
        return True

    script = PROJECT_ROOT / "scripts" / "setup_postgres.sh"
    if not script.exists():
        error(f"setup_postgres.sh not found at {script}")
        return False

    cmd = ["bash", str(script)]
    if use_docker:
        cmd.append("--docker")

    try:
        result = run(cmd, check=False)
        if result.returncode != 0:
            error("PostgreSQL setup failed")
            return False
        log("PostgreSQL setup complete ✓")
        return True
    except Exception as e:
        error(f"PostgreSQL setup error: {e}")
        return False


def init_database() -> bool:
    """Step 4: Initialize database schema."""
    header("Step 4: Initialize database schema")

    try:
        result = run(
            [sys.executable, "-m", "aibp.cli", "db-init"],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            error(f"db-init failed:\n{result.stderr}")
            return False
        log(result.stdout.strip())
        log("Database schema initialized ✓")
        return True
    except Exception as e:
        error(f"db-init error: {e}")
        return False


def check_secrets() -> tuple[bool, list[str]]:
    """Step 5: Check that required secrets are filled in .env."""
    header("Step 5: Check secrets in .env")

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        error(".env not found")
        return False, []

    required = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNEL_ID_PROD",
        "OPENROUTER_API_KEY",
        "DATABASE_URL",
    ]

    missing_or_empty = []
    content = env_file.read_text()
    for key in required:
        # Simple check: KEY=non_empty_value
        found = False
        for line in content.splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value and not value.startswith("your_") and value != "":
                    found = True
                    break
        if not found:
            missing_or_empty.append(key)

    if missing_or_empty:
        warn("The following secrets are missing or empty in .env:")
        for k in missing_or_empty:
            print(f"  - {k}")
        print()
        info("Edit .env before running smoke test:")
        info(f"  nano {env_file}")
        return False, missing_or_empty

    log("All required secrets present ✓")
    return True, []


def run_smoke_test() -> bool:
    """Step 6: Run smoke test."""
    header("Step 6: Smoke test")

    try:
        result = run(
            [sys.executable, "-m", "aibp.cli", "smoke-test"],
            check=False,
        )
        return result.returncode == 0
    except Exception as e:
        error(f"Smoke test error: {e}")
        return False


def print_next_steps() -> None:
    """Print instructions for what to do after bootstrap."""
    header("Next steps")

    print(f"""
{BOLD}✅ Bootstrap complete! System is ready.{NC}

{BOLD}1. Register Hermes cron jobs:{NC}

   See {BLUE}docs/install.md{NC} — section "Step 7: Регистрация cron-джоб в Hermes Agent"

   Quick reference (run in Hermes):
     cronjob(action='create', name='AIBP — RSS Collector', schedule='0 * * * *', ...)

{BOLD}2. Manual test (optional):{NC}

   Run full pipeline once to verify end-to-end:
     {BLUE}make run-all{NC}

{BOLD}3. Monitor:{NC}

   - Logs: {BLUE}reports/logs/{NC}
   - LLM costs: {BLUE}reports/llm_cost.jsonl{NC}
   - Self-learning data: PostgreSQL (migration 0009)
   - Dashboard (after first cron run): {BLUE}reports/self_learning/dashboard.html{NC}

{BOLD}4. If something breaks:{NC}

   - Autopilot kill switch: {BLUE}make resume-autopilot{NC}
   - DB connection check: {BLUE}make db-check{NC}
   - Smoke test: {BLUE}make smoke-test{NC}
   - Full docs: {BLUE}docs/install.md{NC}, {BLUE}docs/adr/{NC}
""")


def check_only() -> bool:
    """Just verify current state, don't change anything."""
    header("Check-only mode")
    ok = True

    if not check_environment():
        ok = False

    # Check .env
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        warn(".env not found")
        ok = False
    else:
        secrets_ok, _ = check_secrets()
        if not secrets_ok:
            ok = False

    # Check DB
    try:
        result = run([sys.executable, "-c", "from aibp.db.init_db import check_connection; exit(0 if check_connection() else 1)"],
                     check=False, capture=True)
        if result.returncode == 0:
            log("DB connection ✓")
        else:
            warn("DB connection failed")
            ok = False
    except Exception:
        warn("Cannot check DB")
        ok = False

    # Check Python deps
    try:
        import structlog  # noqa
        import feedparser  # noqa
        import psycopg2  # noqa
        log("Python deps ✓")
    except ImportError as e:
        warn(f"Missing Python dep: {e}")
        ok = False

    return ok


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="AIBP Autopilot bootstrap")
    parser.add_argument("--skip-postgres", action="store_true", help="Skip PostgreSQL setup (already configured)")
    parser.add_argument("--check-only", action="store_true", help="Just verify, don't change anything")
    parser.add_argument("--docker", action="store_true", help="Use Docker for PostgreSQL")
    args = parser.parse_args()

    if args.check_only:
        ok = check_only()
        return 0 if ok else 1

    # Full bootstrap
    steps = [
        ("Environment check", check_environment),
        ("Install Python deps", install_python_deps),
    ]

    for name, fn in steps:
        if not fn():
            error(f"Step failed: {name}")
            return 1

    # PostgreSQL setup
    if not setup_postgres(use_docker=args.docker, skip=args.skip_postgres):
        error("PostgreSQL setup failed")
        return 1

    # Database initialization
    if not init_database():
        error("Database init failed")
        return 1

    # Check secrets
    secrets_ok, missing = check_secrets()
    if not secrets_ok:
        warn(f"Fill in missing secrets in .env: {', '.join(missing)}")
        warn("Then run: python3 scripts/bootstrap.py --skip-postgres")
        return 2  # special code: needs user action

    # Smoke test
    if not run_smoke_test():
        error("Smoke test failed")
        return 1

    print_next_steps()
    return 0


if __name__ == "__main__":
    sys.exit(main())
