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
    """Initialize PostgreSQL database and load RSS feeds."""
    from aibp.db.init_db import init_db, load_rss_feeds_to_db
    init_db()
    n = load_rss_feeds_to_db()
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


# ─── Offers catalog (issue #38) ─────────────────────────────────────

@cli.command("offer-add")
@click.argument("slug")
@click.option("--title", required=True, help="Link text shown in the post CTA.")
@click.option("--url", "target_url", required=True, help="Partner/CPA target URL.")
@click.option("--topics", default="", help="Comma-separated topic_cluster tags; empty = any topic.")
@click.option("--rate", "revenue_per_click", type=float, default=0.0,
              help="Estimated revenue per click, ₽.")
@click.option("--notes", default=None)
def offer_add(slug: str, title: str, target_url: str, topics: str,
              revenue_per_click: float, notes: str | None) -> None:
    """Add or update an offer (upsert by slug)."""
    from aibp.monetization.offers import add_offer
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    add_offer(slug, title, target_url, topic_list, revenue_per_click, notes)
    click.echo(f"✅ offer '{slug}' saved")


@cli.command("offer-list")
@click.option("--status", default=None, type=click.Choice(["active", "paused"]))
def offer_list(status: str | None) -> None:
    """List offers in the catalog."""
    from aibp.monetization.offers import list_offers
    offers = list_offers(status=status)
    if not offers:
        click.echo("(no offers)")
        return
    for o in offers:
        topics = ",".join(o["topics"] or []) or "any"
        click.echo(f"{o['slug']:24} {o['status']:7} {float(o['revenue_per_click']):>7.2f}₽/click"
                   f"  [{topics}]  {o['title']}")


@cli.command("offer-set-status")
@click.argument("slug")
@click.argument("status", type=click.Choice(["active", "paused"]))
def offer_set_status(slug: str, status: str) -> None:
    """Pause or reactivate an offer."""
    from aibp.monetization.offers import set_offer_status
    if set_offer_status(slug, status):
        click.echo(f"✅ offer '{slug}' → {status}")
    else:
        click.echo(f"❌ offer '{slug}' not found")
        raise SystemExit(1)


# ─── Traffic sources + ad buying (issue #39) ───────────────────────

@cli.command("ad-plan")
@click.argument("donor")
def ad_plan(donor: str) -> None:
    """Prepare an ad buy: forecast + invite link + creative + application draft."""
    from aibp.growth.ad_buying import plan_ad_buy
    path = plan_ad_buy(donor)
    click.echo(f"✅ План закупки готов: {path}")
    click.echo("   Оплата и договорённости — вручную; подписки по ссылке считаются автоматически.")


@cli.command("source-add")
@click.argument("slug")
@click.option("--kind", default="ad_buy",
              type=click.Choice(["ad_buy", "cross_promo", "external", "other"]))
@click.option("--channel", "channel_username", default=None, help="Donor channel username.")
@click.option("--notes", default=None)
def source_add(slug: str, kind: str, channel_username: str | None, notes: str | None) -> None:
    """Create a traffic source with its own invite link."""
    from aibp.growth.traffic_sources import create_source
    source = create_source(slug, kind=kind, channel_username=channel_username, notes=notes)
    click.echo(f"✅ source '{slug}' created")
    click.echo(f"   invite link: {source['invite_link']}")


@cli.command("source-set")
@click.argument("slug")
@click.option("--cost", "cost_rub", type=float, default=None, help="Paid price, ₽.")
@click.option("--status", default=None, type=click.Choice(["draft", "live", "done"]))
@click.option("--posted-at", "ad_posted_at", default=None, help="When the ad went live (ISO).")
def source_set(slug: str, cost_rub: float | None, status: str | None,
               ad_posted_at: str | None) -> None:
    """Record the manual outcome of a placement (cost, status)."""
    from aibp.growth.traffic_sources import set_source
    if set_source(slug, cost_rub=cost_rub, status=status, ad_posted_at=ad_posted_at):
        click.echo(f"✅ source '{slug}' updated")
    else:
        click.echo(f"❌ source '{slug}' not found (or nothing to update)")
        raise SystemExit(1)


@cli.command("source-list")
def source_list() -> None:
    """List traffic sources with joins and actual CPS."""
    from aibp.growth.traffic_sources import cps_summary
    rows = cps_summary()
    if not rows:
        click.echo("(no traffic sources)")
        return
    for r in rows:
        cps = f"{r['actual_cps_rub']:.0f}₽/sub" if r["actual_cps_rub"] is not None else "—"
        channel = f" @{r['channel_username']}" if r["channel_username"] else ""
        click.echo(f"{r['slug']:32} {r['kind']:11} {r['status']:5} "
                   f"joins={r['joins']:<4} cost={r['cost_rub'] or 0:>7.0f}₽ CPS={cps}{channel}")


if __name__ == "__main__":
    cli()
