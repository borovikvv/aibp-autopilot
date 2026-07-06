"""HTML Dashboard — generates self-learning status page.

Daily cron: writes static HTML to dashboard_output_path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from jinja2 import Template

from aibp.self_learning.db import get_snapshot_at_horizon, sqlite_conn
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
      <td>{{ exp.started_at[:19] }}</td>
      <td><code>{{ exp.policy_after }}</code></td>
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

  <h2>Recent Autopilot Events</h2>
  {% if recent_events %}
  <table>
    <tr><th>Time</th><th>Type</th><th>Details</th></tr>
    {% for ev in recent_events %}
    <tr>
      <td>{{ ev.event_at[:19] }}</td>
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


def get_metrics() -> dict:
    """Get summary metrics for dashboard."""
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    with sqlite_conn() as conn:
        # Total posts
        row = conn.execute(
            "SELECT COUNT(DISTINCT feed_item_id) as n FROM post_features WHERE posted_at >= ?",
            (week_ago,),
        ).fetchone()
        total_posts = row["n"] if row else 0

        # Avg views
        row = conn.execute(
            """
            SELECT AVG(em.views) as avg_views
            FROM engagement_metrics em
            JOIN post_features pf ON em.feed_item_id = pf.feed_item_id
            WHERE em.measured_at >= ?
            """,
            (week_ago,),
        ).fetchone()
        avg_views = row["avg_views"] if row else None

        # Active experiments
        row = conn.execute(
            "SELECT COUNT(*) as n FROM experiments_log WHERE status = 'shadow_running'"
        ).fetchone()
        active = row["n"] if row else 0

        # Rollbacks 7d
        row = conn.execute(
            """
            SELECT COUNT(*) as n FROM autopilot_events
            WHERE event_type = 'rollback' AND event_at >= ?
            """,
            (week_ago,),
        ).fetchone()
        rollbacks = row["n"] if row else 0

    return {
        "total_posts": total_posts,
        "avg_views": avg_views,
        "active_experiments": active,
        "rollbacks_7d": rollbacks,
    }


def get_active_experiments() -> list[dict]:
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, started_at, experiment_type, hypothesis, policy_after
            FROM experiments_log WHERE status = 'shadow_running'
            ORDER BY started_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_bandit_state() -> list[dict]:
    """Thompson-sampling posteriors for the dashboard (issue #23).

    The bandit and experiments_log are parallel loops, so bandit drift is
    invisible on the experiments tables — this surfaces it. Mirrors the math
    in aibp.self_learning.bandit: E[θ] = α/(α+β), multiplier = 0.5 + E[θ],
    observations = α + β − 2 (the Beta(1,1) prior contributes 2)."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT dimension, arm_id, alpha, beta, updated_at
            FROM bandit_arms
            ORDER BY dimension, (alpha * 1.0 / (alpha + beta)) DESC
            """
        ).fetchall()

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
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, experiment_type, status, effect_size, p_value, decision_reason
            FROM experiments_log
            WHERE status IN ('promoted', 'rolled_back', 'rejected')
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


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

    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT feed_item_id, slot, policy_version, cta_variant
            FROM post_features
            WHERE posted_at >= ? AND target_channel = 'main'
            ORDER BY posted_at DESC
            """,
            (since,),
        ).fetchall()

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
    with sqlite_conn() as conn:
        row = conn.execute(
            """
            SELECT event_type FROM autopilot_events
            WHERE event_type IN ('tgstat_ok', 'tgstat_token_expired')
            ORDER BY event_at DESC LIMIT 1
            """
        ).fetchone()
    if row is None:
        return None
    return row["event_type"] == "tgstat_ok"


def get_recent_events(limit: int = 20) -> list[dict]:
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT event_at, event_type, details
            FROM autopilot_events
            ORDER BY event_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def run() -> int:
    """Generate dashboard HTML."""
    s = get_settings()
    policy = load_policy()

    html = Template(DASHBOARD_TEMPLATE).render(
        policy=policy,
        metrics=get_metrics(),
        active_experiments=get_active_experiments(),
        bandit_state=get_bandit_state(),
        recent_decisions=get_recent_decisions(),
        recent_events=get_recent_events(),
        ctr=get_ctr_stats(),
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
