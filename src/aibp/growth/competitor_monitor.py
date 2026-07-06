"""Growth monitoring — subscriber dynamics + competitor analytics (issue #19).

Weekly cron. Produces a report with:
  - own subscriber dynamics (collected by engagement_collector, previously
    never aggregated) and anomaly flags;
  - competitor channel stats via TGStat API (ER, growth, avg post reach);
  - a buy/skip recommendation per competitor with the maximum justified
    ad-post price.

Advertising purchases are deliberately NOT automated: the report ends at a
recommendation, decision and payment stay with a human (prohibited actions:
покупка/перевод денег).
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import structlog
import yaml

from aibp.self_learning.db import sqlite_conn
from aibp.utils.config import PROJECT_ROOT

log = structlog.get_logger()

REPORTS_DIR = PROJECT_ROOT / "reports" / "growth"
COMPETITORS_PATH = PROJECT_ROOT / "config" / "competitors.yaml"

TGSTAT_API = "https://api.tgstat.ru"


def load_growth_config() -> dict:
    """Load competitors.yaml; sane defaults when missing."""
    if not COMPETITORS_PATH.exists():
        return {"subscriber_value_rub": 50, "assumed_conversion_pct": 1.0, "channels": []}
    with open(COMPETITORS_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("subscriber_value_rub", 50)
    cfg.setdefault("assumed_conversion_pct", 1.0)
    cfg.setdefault("channels", [])
    return cfg


# ═══════════════════════════════════════════════════════════════════
# Own subscriber dynamics
# ═══════════════════════════════════════════════════════════════════

def get_subscriber_series(days: int = 30) -> list[dict]:
    """Daily subscriber counts for the main channel (max snapshot per day)."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT date(em.measured_at) as day, MAX(em.subscribers_at) as subscribers
            FROM engagement_metrics em
            JOIN post_features pf ON pf.feed_item_id = em.feed_item_id
            WHERE em.measured_at >= ?
              AND pf.target_channel = 'main'
              AND em.subscribers_at IS NOT NULL
            GROUP BY date(em.measured_at)
            ORDER BY day ASC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]


def compute_daily_deltas(series: list[dict]) -> list[dict]:
    """Attach absolute and relative day-over-day deltas to the series."""
    result = []
    prev = None
    for point in series:
        delta = None
        delta_pct = None
        if prev is not None and prev.get("subscribers"):
            delta = point["subscribers"] - prev["subscribers"]
            delta_pct = delta / prev["subscribers"] * 100
        result.append({**point, "delta": delta, "delta_pct": delta_pct})
        prev = point
    return result


def detect_churn_anomaly(deltas: list[dict], threshold_pct: float = 5.0) -> dict | None:
    """Return the worst day if any daily drop exceeds the threshold."""
    worst = None
    for point in deltas:
        if point["delta_pct"] is not None and point["delta_pct"] <= -threshold_pct:
            if worst is None or point["delta_pct"] < worst["delta_pct"]:
                worst = point
    return worst


# ═══════════════════════════════════════════════════════════════════
# Competitor analytics (TGStat)
# ═══════════════════════════════════════════════════════════════════

def fetch_competitor_stats(username: str, token: str) -> dict | None:
    """Fetch channel stats from TGStat. Returns None on any failure."""
    try:
        resp = httpx.get(
            f"{TGSTAT_API}/channels/stat",
            params={"token": token, "channelId": f"@{username.lstrip('@')}"},
            timeout=30,
        )
        data = resp.json()
        if data.get("status") != "ok":
            log.warning("tgstat_error", channel=username, response=str(data)[:200])
            return None
        r = data.get("response", {})
        return {
            "username": username,
            "subscribers": r.get("participants_count"),
            "avg_post_reach": r.get("avg_post_reach"),
            "er_percent": r.get("er_percent"),
            "daily_reach": r.get("daily_reach"),
        }
    except Exception as e:
        log.warning("tgstat_request_failed", channel=username, error=str(e))
        return None


def build_recommendation(stats: dict, subscriber_value_rub: float,
                         assumed_conversion_pct: float, our_er_percent: float | None) -> dict:
    """Buy/skip recommendation with the maximum justified ad-post price.

    Expected new subscribers per ad post ≈ avg_post_reach × conversion.
    Paying more than expected_subscribers × subscriber_value makes CAC
    exceed subscriber value → payback never happens. No payment action here.
    """
    reach = stats.get("avg_post_reach") or 0
    expected_subscribers = reach * assumed_conversion_pct / 100
    max_price_rub = expected_subscribers * subscriber_value_rub

    er = stats.get("er_percent")
    worth_it = bool(reach and expected_subscribers >= 1)
    reasons = []
    if not reach:
        reasons.append("нет данных об охвате")
    if er is not None and our_er_percent is not None and er < our_er_percent / 2:
        worth_it = False
        reasons.append(f"ER конкурента ({er:.1f}%) сильно ниже нашего ({our_er_percent:.1f}%) — аудитория холодная")
    if expected_subscribers < 1:
        reasons.append("ожидаемый приток < 1 подписчика на пост")

    return {
        "username": stats["username"],
        "worth_buying": worth_it,
        "expected_subscribers_per_post": round(expected_subscribers, 1),
        "max_justified_price_rub": round(max_price_rub),
        "estimated_cac_at_max_price_rub": subscriber_value_rub,
        "notes": "; ".join(reasons) if reasons else "по метрикам выглядит разумно",
        "action": ("Запросить цену поста; закупать только если ≤ "
                   f"{round(max_price_rub)} ₽. Решение и оплата — вручную."),
    }


# ═══════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════

def get_our_er_percent() -> float | None:
    """Own ER at the 48h horizon over the last 30 days, in percent."""
    from aibp.self_learning.db import get_snapshot_at_horizon

    since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT feed_item_id FROM post_features
            WHERE posted_at >= ? AND target_channel = 'main'
            """,
            (since,),
        ).fetchall()
    rates = []
    for r in rows:
        snap = get_snapshot_at_horizon(r["feed_item_id"])
        if snap and snap.get("subscribers_at"):
            rates.append((snap["views"] or 0) / snap["subscribers_at"])
    return (sum(rates) / len(rates) * 100) if rates else None


def build_report(deltas: list[dict], anomaly: dict | None,
                 recommendations: list[dict], our_er: float | None) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"# Growth report — {today}", ""]

    lines.append("## Динамика подписчиков (главный канал)")
    if deltas:
        current = deltas[-1]["subscribers"]
        week = [d for d in deltas if d["delta"] is not None][-7:]
        week_delta = sum(d["delta"] for d in week) if week else 0
        lines.append(f"- Сейчас: **{current}** подписчиков")
        lines.append(f"- За последние {len(week)} дн.: {week_delta:+d}")
        if our_er is not None:
            lines.append(f"- Наш ER (48ч горизонт, 30 дн.): {our_er:.1f}%")
    else:
        lines.append("- Данных пока нет (engagement_collector ещё не накопил снимков)")

    if anomaly:
        lines.append("")
        lines.append(f"⚠️ **Аномалия**: {anomaly['day']} отток {anomaly['delta_pct']:.1f}% "
                     f"({anomaly['delta']:+d} подписчиков) — проверить, что публиковалось в этот день.")

    lines.append("")
    lines.append("## Конкуренты и рекомендации по закупке")
    if recommendations:
        for rec in recommendations:
            verdict = "✅ стоит рассмотреть" if rec["worth_buying"] else "❌ не стоит"
            lines.append(f"### @{rec['username']} — {verdict}")
            lines.append(f"- Ожидаемый приток с поста: ~{rec['expected_subscribers_per_post']} подписчиков")
            lines.append(f"- Максимальная оправданная цена поста: {rec['max_justified_price_rub']} ₽ "
                         f"(CAC ≤ {rec['estimated_cac_at_max_price_rub']} ₽/подписчик)")
            lines.append(f"- Заметки: {rec['notes']}")
            lines.append(f"- Действие: {rec['action']}")
            lines.append("")
    else:
        lines.append("Конкуренты не настроены (config/competitors.yaml) или TGSTAT_API_TOKEN не задан.")

    lines.append("")
    lines.append("_Закупка рекламы не автоматизирована: отчёт заканчивается рекомендацией, "
                 "решение и оплата — за человеком._")
    return "\n".join(lines)


def run() -> int:
    """Weekly cron entry point."""
    cfg = load_growth_config()

    series = get_subscriber_series(days=30)
    deltas = compute_daily_deltas(series)
    anomaly = detect_churn_anomaly(deltas)
    our_er = get_our_er_percent()

    recommendations = []
    token = os.getenv("TGSTAT_API_TOKEN", "")
    channels = cfg.get("channels") or []
    if token and channels:
        for ch in channels[:5]:
            username = ch["username"] if isinstance(ch, dict) else str(ch)
            stats = fetch_competitor_stats(username, token)
            if stats:
                recommendations.append(build_recommendation(
                    stats,
                    subscriber_value_rub=cfg["subscriber_value_rub"],
                    assumed_conversion_pct=cfg["assumed_conversion_pct"],
                    our_er_percent=our_er,
                ))
    elif channels and not token:
        log.warning("tgstat_token_missing", hint="Set TGSTAT_API_TOKEN in .env")

    report = build_report(deltas, anomaly, recommendations, our_er)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    (REPORTS_DIR / f"growth_{stamp}.md").write_text(report, encoding="utf-8")
    (REPORTS_DIR / f"growth_{stamp}.json").write_text(
        json.dumps({"deltas": deltas, "anomaly": anomaly,
                    "recommendations": recommendations, "our_er_percent": our_er},
                   indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    log.info("growth_report_written", anomaly=bool(anomaly),
             competitors=len(recommendations))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
