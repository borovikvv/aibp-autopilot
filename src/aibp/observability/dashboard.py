"""HTML Dashboard — generates self-learning status page.

Daily cron: writes static HTML to dashboard_output_path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from jinja2 import Template

from aibp.db.connection import fetch_all, fetch_one
from aibp.self_learning.db import get_snapshot_at_horizon
from aibp.utils.config import get_settings, load_policy

log = structlog.get_logger()

DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>AIBP Autopilot Dashboard</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }
  .container { max-width: 1200px; margin: 0 auto; }
  h1 { color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 10px; }
  h2 { color: #79c0ff; margin-top: 30px; }
  .paused { background: #da3633; color: white; padding: 15px; border-radius: 6px;
            font-weight: bold; text-align: center; margin-bottom: 20px; }
  .ok { background: #238636; color: white; padding: 15px; border-radius: 6px;
        text-align: center; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; margin: 15px 0; }
  th, td { text-align: left; padding: 10px; border-bottom: 1px solid #30363d; }
  th { background: #161b22; color: #58a6ff; }
  tr:hover { background: #161b22; }
  .status-running { color: #d29922; }
  .status-promoted { color: #3fb950; }
  .status-rolled_back { color: #da3633; }
  .status-rejected { color: #8b949e; }
  .metric { display: inline-block; background: #161b22; padding: 15px;
            border-radius: 6px; margin: 5px; min-width: 150px; }
  .metric-value { font-size: 24px; font-weight: bold; color: #58a6ff; }
  .metric-label { color: #8b949e; font-size: 12px; text-transform: uppercase; }
  .footer { margin-top: 40px; color: #8b949e; font-size: 12px;
            border-top: 1px solid #30363d; padding-top: 10px; }
</style>
</head>
<body>
<div class="container">
  <h1>AIBP Autopilot Dashboard</h1>

  {% if policy.autopilot_paused %}
  <div class="paused">⚠️ AUTOPILOT PAUSED<br>
    <small>{{ policy.get('_pause_reason', 'Unknown reason') }}</small>
  </div>
  {% else %}
  <div class="ok">✅ Autopilot Active</div>
  {% endif %}

  <h2>Metrics (last 7 days)</h2>
  <div class="metric">
    <div class="metric-value">{{ metrics.total_posts }}</div>
    <div class="metric-label">Posts published</div>
  </div>
  <div class="metric">
    <div class="metric-value">{{ metrics.avg_views|round(1) if metrics.avg_views else '—' }}</div>
    <div class="metric-label">Avg views</div>
  </div>
  <div class="metric">
    <div class="metric-value">{{ metrics.active_experiments }}</div>
    <div class="metric-label">Active experiments</div>
  </div>
  <div class="metric">
    <div class="metric-value">{{ metrics.rollbacks_7d }}</div>
    <div class="metric-label">Rollbacks (7d)</div>
  </div>

  <h2>Active Shadow Experiments</h2>
  {% if active_experiments %}
  <table>
    <tr>
      <th>ID</th><th>Type</th><th>Hypothesis</th><th>Started</th><th>Policy After</th>
    </tr>
    {% for exp in active_experiments %}
    <tr>
      <td>{{ exp.id }}</td>
      <td>{{ exp.experiment_type }}</td>
      <td>{{ exp.hypothesis[:80] }}...</td>
      <td>{{ exp.started_at | fmt_ts }}</td>
      <td><code>{{ exp.policy_after }}</code></td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No active experiments.</p>
  {% endif %}

  <h2>Experiment Power</h2>
  {% if experiment_power %}
  <table>
    <tr>
      <th>ID</th><th>Type</th><th>Control n</th><th>Shadow n</th><th>Target n</th>
      <th>Days to decision</th><th>P(shadow&gt;control)</th><th>Status</th>
    </tr>
    {% for exp in experiment_power %}
    <tr>
      <td>{{ exp.id }}</td>
      <td>{{ exp.experiment_type }}</td>
      <td>{{ exp.control_n }}</td>
      <td>{{ exp.shadow_n }}</td>
      <td>{{ exp.target_n }}</td>
      <td>{{ exp.days_to_decision }}</td>
      <td>{{ '%.3f'|format(exp.current_p) if exp.current_p is not none else '—' }}</td>
      <td>{{ exp.status }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No active experiments.</p>
  {% endif %}

  <h2>Bandit State (Thompson sampling)</h2>
  {% if bandit_state %}
  <table>
    <tr>
      <th>Dimension</th><th>Arm</th><th>α</th><th>β</th><th>E[θ]</th>
      <th>Observations</th><th>Multiplier</th>
    </tr>
    {% for arm in bandit_state %}
    <tr>
      <td>{{ arm.dimension }}</td>
      <td>{{ arm.arm_id }}</td>
      <td>{{ '%.1f'|format(arm.alpha) }}</td>
      <td>{{ '%.1f'|format(arm.beta) }}</td>
      <td>{{ '%.2f'|format(arm.e_theta) }}</td>
      <td>{{ arm.observations }}</td>
      <td{% if arm.significant %} class="status-running"{% endif %}>×{{ '%.2f'|format(arm.multiplier) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No bandit data yet (no posts scored against the trailing median).</p>
  {% endif %}

  <h2>Recent Decisions (last 10)</h2>
  {% if recent_decisions %}
  <table>
    <tr>
      <th>ID</th><th>Type</th><th>Status</th><th>Effect</th><th>p-value</th><th>Reason</th>
    </tr>
    {% for exp in recent_decisions %}
    <tr>
      <td>{{ exp.id }}</td>
      <td>{{ exp.experiment_type }}</td>
      <td class="status-{{ exp.status|replace('_', '_') }}">{{ exp.status }}</td>
      <td>{{ exp.effect_size if exp.effect_size is not none else '—' }}</td>
      <td>{{ exp.p_value if exp.p_value is not none else '—' }}</td>
      <td>{{ exp.decision_reason[:60] if exp.decision_reason else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No finished experiments yet.</p>
  {% endif %}

  <h2>Growth — subscribers (last 14 days)</h2>
  {% if growth.anomaly %}
  <div class="paused">⚠️ ОТТОК {{ '%.1f'|format(growth.anomaly.delta_pct) }}% за {{ growth.anomaly.day }}
    ({{ growth.anomaly.delta }} подписчиков)</div>
  {% endif %}
  {% if growth.tgstat_ok is false %}
  <div class="paused">⚠️ TGStat: токен невалиден или подписка истекла — конкурентная аналитика недоступна</div>
  {% endif %}
  {% if growth.deltas %}
  <div class="metric">
    <div class="metric-value">{{ growth.current }}</div>
    <div class="metric-label">Subscribers now</div>
  </div>
  <div class="metric">
    <div class="metric-value">{{ '%+d'|format(growth.week_delta) }}</div>
    <div class="metric-label">Delta (7d)</div>
  </div>
  <table>
    <tr><th>Day</th><th>Subscribers</th><th>Δ</th><th>Δ%</th></tr>
    {% for d in growth.deltas[-14:] %}
    <tr>
      <td>{{ d.day }}</td>
      <td>{{ d.subscribers }}</td>
      <td>{{ '%+d'|format(d.delta) if d.delta is not none else '—' }}</td>
      <td>{{ '%+.2f%%'|format(d.delta_pct) if d.delta_pct is not none else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No subscriber snapshots yet.</p>
  {% endif %}

  <h2>CTR — clicks / views at 48h (last 30 days)</h2>
  {% if ctr.posts %}
  <table>
    <tr>
      <th>Post</th><th>Slot</th><th>Policy</th><th>CTA</th><th>Views (48h)</th><th>Clicks</th><th>CTR</th>
    </tr>
    {% for p in ctr.posts %}
    <tr>
      <td>{{ p.feed_item_id }}</td>
      <td>{{ p.slot }}</td>
      <td><code>{{ p.policy_version[:12] }}</code></td>
      <td>{{ p.cta_variant }}</td>
      <td>{{ p.views if p.views is not none else '—' }}</td>
      <td>{{ p.clicks }}</td>
      <td>{{ '%.2f%%'|format(p.ctr * 100) if p.ctr is not none else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% if ctr.by_cta %}
  <h3>Conversion by CTA variant</h3>
  <table>
    <tr><th>CTA variant</th><th>Posts</th><th>Views (48h)</th><th>Clicks</th><th>CTR</th></tr>
    {% for row in ctr.by_cta %}
    <tr>
      <td>{{ row.cta_variant }}</td>
      <td>{{ row.n_posts }}</td>
      <td>{{ row.views }}</td>
      <td>{{ row.clicks }}</td>
      <td>{{ '%.2f%%'|format(row.ctr * 100) if row.ctr is not none else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
  {% if ctr.by_policy %}
  <h3>CTR by policy version</h3>
  <table>
    <tr><th>Policy</th><th>Posts</th><th>Views (48h)</th><th>Clicks</th><th>CTR</th></tr>
    {% for row in ctr.by_policy %}
    <tr>
      <td><code>{{ row.policy_version[:12] }}</code></td>
      <td>{{ row.n_posts }}</td>
      <td>{{ row.views }}</td>
      <td>{{ row.clicks }}</td>
      <td>{{ '%.2f%%'|format(row.ctr * 100) if row.ctr is not none else '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
  {% else %}
  <p>No click data yet (tracking disabled or no clicks recorded).</p>
  {% endif %}

  <h2>Offers — estimated revenue (last 30 days)</h2>
  {% if offers.by_offer %}
  <div class="metric">
    <div class="metric-value">{{ '%.0f'|format(offers.total_revenue) }} ₽</div>
    <div class="metric-label">Est. revenue (30d)</div>
  </div>
  <table>
    <tr>
      <th>Offer</th><th>Status</th><th>₽/click</th><th>Posts</th><th>Clicks</th><th>Est. revenue</th>
    </tr>
    {% for o in offers.by_offer %}
    <tr>
      <td>{{ o.slug }} — {{ o.title[:40] }}</td>
      <td>{{ o.status }}</td>
      <td>{{ '%.2f'|format(o.rpc) }}</td>
      <td>{{ o.posts }}</td>
      <td>{{ o.clicks }}</td>
      <td><b>{{ '%.0f'|format(o.est_revenue or 0) }} ₽</b></td>
    </tr>
    {% endfor %}
  </table>
  {% if offers.by_post %}
  <h3>Revenue by post</h3>
  <table>
    <tr><th>Post</th><th>Offer</th><th>Clicks</th><th>Est. revenue</th></tr>
    {% for p in offers.by_post %}
    <tr>
      <td>{{ p.feed_item_id }}</td>
      <td>{{ p.slug }}</td>
      <td>{{ p.clicks }}</td>
      <td>{{ '%.0f'|format(p.est_revenue or 0) }} ₽</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}
  {% else %}
  <p>No offers in the catalog yet — add one:
     <code>aibp offer-add &lt;slug&gt; --title ... --url ... --rate ...</code></p>
  {% endif %}

  <h2>Composite Reward — decomposition at 48h (last 14 days)</h2>
  {% if reward_posts %}
  <table>
    <tr>
      <th>Post</th><th>Slot</th><th>Views</th><th>Forwards</th><th>Clicks</th><th>Δsubs</th>
      <th>Reward</th><th>views</th><th>forwards</th><th>clicks</th><th>subs_delta</th>
    </tr>
    {% for p in reward_posts[:15] %}
    <tr>
      <td>{{ p.feed_item_id }}</td>
      <td>{{ p.slot }}</td>
      <td>{{ p.views }}</td>
      <td>{{ p.forwards }}</td>
      <td>{{ p.clicks }}</td>
      <td>{{ '%+.1f'|format(p.subs_delta) if p.subs_delta is not none else '—' }}</td>
      <td><b>{{ '%.3f'|format(p.reward) }}</b></td>
      <td>{{ '%.3f'|format(p.reward_components.views) }}</td>
      <td>{{ '%.3f'|format(p.reward_components.forwards) }}</td>
      <td>{{ '%.3f'|format(p.reward_components.clicks) }}</td>
      <td>{{ '%.3f'|format(p.reward_components.subs_delta) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No scored posts yet (no 48h snapshots).</p>
  {% endif %}

  <h2>Recent Autopilot Events</h2>
  {% if recent_events %}
  <table>
    <tr><th>Time</th><th>Type</th><th>Details</th></tr>
    {% for ev in recent_events %}
    <tr>
      <td>{{ ev.event_at | fmt_ts }}</td>
      <td>{{ ev.event_type }}</td>
      <td><code>{{ ev.details[:80] if ev.details else '' }}</code></td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p>No events.</p>
  {% endif %}

  <div class="footer">
    Generated: {{ generated_at }} | Policy version: <code>{{ policy.version }}</code>
  </div>
</div>
</body>
</html>
"""


def _fmt_ts(value) -> str:
    """Format a datetime or ISO string for the dashboard (YYYY-MM-DD HH:MM).

    Returns "—" for None. PG returns timezone-aware ``datetime`` objects (not
    subscriptable), so the Jinja template must pipe DB-derived timestamps
    through this filter instead of slicing ``[:19]``."""
    if value is None:
        return "—"
    if isinstance(value, str):
        return value[:19].replace("T", " ")
    return value.strftime("%Y-%m-%d %H:%M")


def get_metrics() -> dict:
    """Get summary metrics for dashboard."""
    week_ago = datetime.now(UTC) - timedelta(days=7)
    total_posts = (fetch_one(
        "SELECT COUNT(DISTINCT feed_item_id) as n FROM post_features WHERE posted_at >= %s",
        (week_ago,),
    ) or {}).get("n", 0)
    avg_views = (fetch_one(
        """
        SELECT AVG(em.views) as avg_views
        FROM engagement_metrics em
        JOIN post_features pf ON em.feed_item_id = pf.feed_item_id
        WHERE em.measured_at >= %s
        """,
        (week_ago,),
    ) or {}).get("avg_views")
    active = (fetch_one(
        "SELECT COUNT(*) as n FROM experiments_log WHERE status = 'shadow_running'"
    ) or {}).get("n", 0)
    rollbacks = (fetch_one(
        """
        SELECT COUNT(*) as n FROM autopilot_events
        WHERE event_type = 'rollback' AND event_at >= %s
        """,
        (week_ago,),
    ) or {}).get("n", 0)
    return {
        "total_posts": total_posts,
        "avg_views": avg_views,
        "active_experiments": active,
        "rollbacks_7d": rollbacks,
    }


def get_active_experiments() -> list[dict]:
    return fetch_all(
        """
        SELECT id, started_at, experiment_type, hypothesis, policy_after
        FROM experiments_log WHERE status = 'shadow_running'
        ORDER BY started_at DESC
        """
    )


def _count_posts_for_version(policy_version: str, since=None) -> int:
    """Count main-channel posts published with this policy version.

    When `since` (a datetime) is given, only posts from that point on are
    counted — the control policy is the standing prod policy, so without a
    cutoff its n would include pre-experiment posts and inflate control_n.
    """
    if since is not None:
        row = fetch_one(
            "SELECT COUNT(*) AS n FROM post_features "
            "WHERE policy_version = %s AND target_channel = 'main' AND posted_at >= %s",
            (policy_version, since),
        )
    else:
        row = fetch_one(
            "SELECT COUNT(*) AS n FROM post_features "
            "WHERE policy_version = %s AND target_channel = 'main'",
            (policy_version,),
        )
    return (row or {}).get("n", 0)


def get_experiment_power() -> list[dict]:
    """Per active experiment: how much data we have vs how much we need.

    Practical power visibility — n collected vs n needed until the decision
    window, plus current P(shadow>control) when enough data exists. Each dict
    has keys: id, experiment_type, started_at, control_n, shadow_n, target_n,
    days_to_decision, current_p, status (on_track/behind/ready_to_decide).
    """
    from aibp.self_learning.decision_engine import (
        compute_decision,
        compute_reward_rates,
        get_engagement_for_policy_version,
    )
    from aibp.self_learning.tiers import load_tier_config

    policy = load_policy()
    now = datetime.now(UTC)
    experiments = fetch_all(
        """
        SELECT id, started_at, experiment_type, policy_before, policy_after
        FROM experiments_log WHERE status = 'shadow_running'
        ORDER BY started_at DESC
        """
    )

    result = []
    for exp in experiments:
        tier = load_tier_config(exp["experiment_type"], policy=policy)
        started = exp["started_at"]
        if isinstance(started, str):
            started = datetime.fromisoformat(started)
        exp_age_days = (now - started).days
        window = tier["experiment_window_days"]
        target_n = window * 2  # ~2 posts/day
        days_to_decision = max(0, window - exp_age_days)

        control_n = _count_posts_for_version(exp["policy_before"], since=started)
        shadow_n = _count_posts_for_version(exp["policy_after"], since=started)

        current_p = None
        if control_n >= 5 and shadow_n >= 5:
            control_posts = get_engagement_for_policy_version(exp["policy_before"], since=started)
            shadow_posts = get_engagement_for_policy_version(exp["policy_after"], since=started)
            control_rates = compute_reward_rates(control_posts, policy=policy)
            shadow_rates = compute_reward_rates(shadow_posts, policy=policy)
            if len(control_rates) >= 5 and len(shadow_rates) >= 5:
                decision = compute_decision(
                    control_rates, shadow_rates, exp_age_days,
                    promote_probability=tier["promote_probability"],
                    min_effect=tier["min_effect_pct"] / 100,
                    give_up_days=window + 7,
                )
                current_p = decision.get("p_value")

        total_n = control_n + shadow_n
        if days_to_decision == 0:
            status = "ready_to_decide"
        elif total_n >= target_n * 0.7:
            status = "on_track"
        else:
            status = "behind"

        result.append({
            "id": exp["id"],
            "experiment_type": exp["experiment_type"],
            "started_at": started,
            "control_n": control_n,
            "shadow_n": shadow_n,
            "target_n": target_n,
            "days_to_decision": days_to_decision,
            "current_p": current_p,
            "status": status,
        })
    return result


def get_bandit_state() -> list[dict]:
    """Thompson-sampling posteriors for the dashboard (issue #23).

    The bandit and experiments_log are parallel loops, so bandit drift is
    invisible on the experiments tables — this surfaces it. Mirrors the math
    in aibp.self_learning.bandit: E[θ] = α/(α+β), multiplier = 0.5 + E[θ],
    observations = α + β − 2 (the Beta(1,1) prior contributes 2)."""
    rows = fetch_all(
        """
        SELECT dimension, arm_id, alpha, beta, updated_at
        FROM bandit_arms
        ORDER BY dimension, (alpha * 1.0 / (alpha + beta)) DESC
        """
    )

    state = []
    for r in rows:
        alpha, beta = r["alpha"], r["beta"]
        e_theta = alpha / (alpha + beta) if (alpha + beta) else 0.0
        multiplier = 0.5 + e_theta
        state.append({
            "dimension": r["dimension"],
            "arm_id": r["arm_id"],
            "alpha": alpha,
            "beta": beta,
            "e_theta": e_theta,
            "observations": int(round(alpha + beta - 2)),
            "multiplier": multiplier,
            # Highlight a meaningful drift from the neutral 1.0 baseline.
            "significant": multiplier > 1.3 or multiplier < 0.7,
        })
    return state


def get_recent_decisions(limit: int = 10) -> list[dict]:
    return fetch_all(
        """
        SELECT id, experiment_type, status, effect_size, p_value, decision_reason
        FROM experiments_log
        WHERE status IN ('promoted', 'rolled_back', 'rejected')
        ORDER BY finished_at DESC
        LIMIT %s
        """,
        (limit,),
    )


def get_ctr_stats(days: int = 30) -> dict:
    """CTR per post and per policy version: clicks (PostgreSQL) over views
    at the 48h horizon (SQLite). Degrades to empty when PG is unreachable
    or click tracking is not deployed."""
    try:
        from aibp.db.connection import fetch_all as pg_fetch_all
        click_rows = pg_fetch_all(
            "SELECT feed_item_id, COUNT(*) AS clicks FROM link_clicks GROUP BY feed_item_id"
        )
    except Exception as e:
        log.warning("ctr_stats_unavailable", error=str(e))
        return {"posts": [], "by_policy": []}

    clicks_by_item = {r["feed_item_id"]: r["clicks"] for r in click_rows}

    since = datetime.now(UTC) - timedelta(days=days)
    rows = fetch_all(
        """
        SELECT feed_item_id, slot, policy_version, cta_variant
        FROM post_features
        WHERE posted_at >= %s AND target_channel = 'main'
        ORDER BY posted_at DESC
        """,
        (since,),
    )

    posts = []
    for r in rows:
        snapshot = get_snapshot_at_horizon(r["feed_item_id"])
        views = snapshot["views"] if snapshot else None
        clicks = clicks_by_item.get(r["feed_item_id"], 0)
        posts.append({
            "feed_item_id": r["feed_item_id"],
            "slot": r["slot"],
            "policy_version": r["policy_version"] or "",
            "cta_variant": r["cta_variant"] or "none",
            "views": views,
            "clicks": clicks,
            "ctr": (clicks / views) if views else None,
        })

    by_policy: dict[str, dict] = {}
    for p in posts:
        agg = by_policy.setdefault(
            p["policy_version"], {"policy_version": p["policy_version"],
                                  "n_posts": 0, "views": 0, "clicks": 0}
        )
        agg["n_posts"] += 1
        agg["views"] += p["views"] or 0
        agg["clicks"] += p["clicks"]
    for agg in by_policy.values():
        agg["ctr"] = (agg["clicks"] / agg["views"]) if agg["views"] else None

    by_cta: dict[str, dict] = {}
    for p in posts:
        agg = by_cta.setdefault(
            p["cta_variant"], {"cta_variant": p["cta_variant"],
                               "n_posts": 0, "views": 0, "clicks": 0}
        )
        agg["n_posts"] += 1
        agg["views"] += p["views"] or 0
        agg["clicks"] += p["clicks"]
    for agg in by_cta.values():
        agg["ctr"] = (agg["clicks"] / agg["views"]) if agg["views"] else None

    return {
        "posts": posts[:15],
        "by_policy": list(by_policy.values()),
        "by_cta": list(by_cta.values()),
    }


def get_growth_stats() -> dict:
    """Subscriber dynamics for the Growth section. The 5%/24h churn threshold
    already existed in policy.safety — it just was never visualized."""
    from aibp.growth.competitor_monitor import (
        compute_daily_deltas,
        detect_churn_anomaly,
        get_subscriber_series,
    )

    policy = load_policy()
    threshold = policy.get("safety", {}).get("subscribers_drop_24h_pct", 5)

    series = get_subscriber_series(days=14)
    deltas = compute_daily_deltas(series)
    week = [d for d in deltas if d["delta"] is not None][-7:]
    return {
        "deltas": deltas,
        "current": deltas[-1]["subscribers"] if deltas else None,
        "week_delta": sum(d["delta"] for d in week) if week else 0,
        "anomaly": detect_churn_anomaly(deltas, threshold_pct=threshold),
        "tgstat_ok": _tgstat_healthy(),
    }


def _tgstat_healthy() -> bool | None:
    """Latest TGStat outcome for the dashboard (issue #25): False if the most
    recent event is a token/subscription failure, True if a success, None if
    TGStat has not run yet."""
    row = fetch_one(
        """
        SELECT event_type FROM autopilot_events
        WHERE event_type IN ('tgstat_ok', 'tgstat_token_expired')
        ORDER BY event_at DESC LIMIT 1
        """
    )
    if row is None:
        return None
    return row["event_type"] == "tgstat_ok"


def get_offer_stats(days: int = 30) -> dict:
    """Estimated revenue per offer and per post (issue #38): clicks on
    offer-attributed tracked links × the offer's revenue_per_click.
    Degrades to empty when PG is unreachable or offers are not deployed."""
    try:
        from aibp.db.connection import fetch_all as pg_fetch_all
        by_offer = pg_fetch_all(
            """
            SELECT o.slug, o.title, o.status, o.revenue_per_click::float AS rpc,
                   COUNT(lc.id) AS clicks,
                   COUNT(DISTINCT tl.feed_item_id) AS posts,
                   COUNT(lc.id) * o.revenue_per_click::float AS est_revenue
            FROM offers o
            LEFT JOIN tracked_links tl ON tl.offer_id = o.id
            LEFT JOIN link_clicks lc ON lc.short_id = tl.short_id
                 AND lc.clicked_at >= now() - make_interval(days => %s)
            GROUP BY o.id
            ORDER BY est_revenue DESC, clicks DESC
            """,
            (days,),
        )
        by_post = pg_fetch_all(
            """
            SELECT tl.feed_item_id, o.slug, o.revenue_per_click::float AS rpc,
                   COUNT(lc.id) AS clicks,
                   COUNT(lc.id) * o.revenue_per_click::float AS est_revenue
            FROM tracked_links tl
            JOIN offers o ON o.id = tl.offer_id
            LEFT JOIN link_clicks lc ON lc.short_id = tl.short_id
            WHERE tl.created_at >= now() - make_interval(days => %s)
            GROUP BY tl.feed_item_id, o.slug, o.revenue_per_click
            ORDER BY est_revenue DESC
            """,
            (days,),
        )
    except Exception as e:
        log.warning("offer_stats_unavailable", error=str(e))
        return {"by_offer": [], "by_post": [], "total_revenue": 0.0}

    total = sum(r["est_revenue"] or 0 for r in by_offer)
    return {"by_offer": by_offer, "by_post": by_post[:15], "total_revenue": total}


def get_reward_stats(days: int = 14) -> list[dict]:
    """Composite reward decomposition per recent main-channel post (issue #37).

    Shows what actually drives the optimization target: each component's
    contribution to the reward, plus the raw counts behind it."""
    from aibp.self_learning.reward import compute_rewards_for_posts

    since = datetime.now(UTC) - timedelta(days=days)
    rows = fetch_all(
        """
        SELECT feed_item_id, posted_at, slot
        FROM post_features
        WHERE posted_at >= %s AND target_channel = 'main'
        ORDER BY posted_at DESC
        """,
        (since,),
    )
    return compute_rewards_for_posts(rows)


def get_recent_events(limit: int = 20) -> list[dict]:
    """Recent autopilot_events rows for the dashboard.

    ``autopilot_events.details`` is jsonb, so psycopg2 returns it as a Python
    dict; the template slices it with ``[:80]``, which would raise
    ``TypeError: unhashable type: 'slice'``. Serialize the dict to a JSON
    string here so the template's slice keeps working."""
    import json

    rows = fetch_all(
        """
        SELECT event_at, event_type, details
        FROM autopilot_events
        ORDER BY event_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    result = []
    for r in rows:
        r = dict(r)
        if r.get("details") is not None and not isinstance(r["details"], str):
            r["details"] = json.dumps(r["details"], ensure_ascii=False)
        result.append(r)
    return result


def run() -> int:
    """Generate dashboard HTML."""
    s = get_settings()
    policy = load_policy()

    template = Template(DASHBOARD_TEMPLATE)
    template.environment.filters["fmt_ts"] = _fmt_ts
    html = template.render(
        policy=policy,
        metrics=get_metrics(),
        active_experiments=get_active_experiments(),
        experiment_power=get_experiment_power(),
        bandit_state=get_bandit_state(),
        recent_decisions=get_recent_decisions(),
        recent_events=get_recent_events(),
        ctr=get_ctr_stats(),
        reward_posts=get_reward_stats(),
        offers=get_offer_stats(),
        growth=get_growth_stats(),
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    output_path = s.dashboard_output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("dashboard_generated", path=str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
