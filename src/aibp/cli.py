"""AIBP CLI — unified entry point for all operations."""
from __future__ import annotations

import sys

import click

from aibp.observability.logging import configure_logging


@click.group()
def cli() -> None:
    """AIBP Autopilot — autonomous Telegram channel management."""
    configure_logging()


@cli.command()
def db_init() -> None:
    """Initialize PostgreSQL and SQLite databases."""
    from aibp.db.init_db import init_db, load_rss_feeds_to_db
    from aibp.self_learning.db import init_db as init_sqlite
    init_db()
    n = load_rss_feeds_to_db()
    init_sqlite()
    click.echo(f"✅ DB initialized. Loaded {n} RSS feeds.")


@cli.command()
def smoke_test() -> None:
    """Run smoke test — verify all components are reachable."""
    from aibp.db.init_db import check_connection
    from aibp.utils.config import get_settings

    s = get_settings()

    # DB
    click.echo("Checking PostgreSQL...")
    if check_connection():
        click.echo("  ✅ OK")
    else:
        click.echo("  ❌ FAILED")
        sys.exit(1)

    # Telegram
    click.echo("Checking Telegram bot token...")
    import httpx
    resp = httpx.get(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe", timeout=10)
    if resp.json().get("ok"):
        click.echo(f"  ✅ OK — @{resp.json()['result']['username']}")
    else:
        click.echo("  ❌ FAILED")
        sys.exit(1)

    # OpenRouter
    click.echo("Checking OpenRouter API key...")
    resp = httpx.get(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {s.openrouter_api_key}"},
        timeout=10,
    )
    if resp.status_code == 200:
        click.echo("  ✅ OK")
    else:
        click.echo(f"  ❌ FAILED (status {resp.status_code})")
        sys.exit(1)

    # Policy
    click.echo("Checking policy.yaml...")
    from aibp.utils.config import load_policy
    p = load_policy()
    click.echo(f"  ✅ OK — version {p.get('version')}, autopilot_paused={p.get('autopilot_paused')}")

    click.echo("\n✅ All checks passed. System ready.")


@cli.command()
@click.option("--slot", default="morning", type=click.Choice(["morning", "evening", "weekly_digest"]))
@click.option(
    "--env",
    "pipeline_env",
    default="prod",
    type=click.Choice(["prod", "stage"]),
    help="prod → main channel + config/policy.yaml | stage → test channel + config/policy.stage.yaml",
)
def generate(slot: str, pipeline_env: str) -> None:
    """Generate one post for the slot."""
    from aibp.generation.pipeline import run
    raise SystemExit(run(slot=slot, pipeline_env=pipeline_env))


@cli.command()
def collect_rss() -> None:
    """Collect RSS feeds."""
    from aibp.collectors.rss_collector import run
    raise SystemExit(run())


@cli.command()
def enrich() -> None:
    """Enrich new feed items via LLM."""
    from aibp.enrichment.pipeline import run
    raise SystemExit(run())


@cli.command()
def publish() -> None:
    """Publish due posts to Telegram."""
    from aibp.publishing.publisher import run
    raise SystemExit(run())


@cli.command()
def collect_engagement() -> None:
    """Collect engagement metrics (self-learning)."""
    from aibp.self_learning.engagement_collector import run
    raise SystemExit(run())


@cli.command()
def mine_patterns() -> None:
    """Run weekly pattern miner (self-learning)."""
    from aibp.self_learning.pattern_miner import run
    raise SystemExit(run())


@cli.command()
def update_policy() -> None:
    """Create experiments from latest hypotheses."""
    from aibp.self_learning.policy_updater import run
    raise SystemExit(run())


@cli.command()
def run_shadow() -> None:
    """Start shadow experiments."""
    from aibp.self_learning.shadow_runner import run
    raise SystemExit(run())


@cli.command()
def decide() -> None:
    """Run decision engine on ready experiments."""
    from aibp.self_learning.decision_engine import run
    raise SystemExit(run())


@cli.command()
def rollback_check() -> None:
    """Check promoted experiments for rollback."""
    from aibp.self_learning.auto_rollback import run
    raise SystemExit(run())


@cli.command()
def safety_check() -> None:
    """Run daily safety check."""
    from aibp.self_learning.safety import daily_safety_check
    raise SystemExit(daily_safety_check())


@cli.command()
def dashboard() -> None:
    """Generate HTML dashboard."""
    from aibp.observability.dashboard import run
    raise SystemExit(run())


@cli.command()
def resume_autopilot() -> None:
    """Resume autopilot after kill switch."""
    from aibp.self_learning.safety import resume_autopilot
    if resume_autopilot():
        click.echo("✅ Autopilot resumed")
    else:
        click.echo("ℹ️  Autopilot was not paused")


if __name__ == "__main__":
    cli()
