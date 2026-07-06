"""HTML Dashboard — generates self-learning status page.

Daily cron: writes static HTML to dashboard_output_path.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from jinja2 import Template

from aibp.self_learning.db import sqlite_conn
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
        recent_decisions=get_recent_decisions(),
        recent_events=get_recent_events(),
        generated_at=datetime.now().isoformat(timespec="seconds"),
    )

    output_path = s.dashboard_output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("dashboard_generated", path=str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
