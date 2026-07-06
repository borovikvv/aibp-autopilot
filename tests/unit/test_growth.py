"""Tests for growth monitoring (issue #19)."""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.growth import competitor_monitor
from aibp.growth.competitor_monitor import (
    TGStatAuthError,
    build_recommendation,
    build_report,
    compute_daily_deltas,
    detect_churn_anomaly,
    fetch_competitor_stats,
    get_subscriber_series,
)
from aibp.self_learning import db as sl_db


@pytest.fixture()
def temp_db(tmp_path):
    with patch.object(sl_db, "get_db_path", return_value=tmp_path / "test.db"):
        sl_db.init_db()
        yield


# ═══════════════════════════════════════════════════════════════════
# Daily deltas + anomaly detection
# ═══════════════════════════════════════════════════════════════════

def test_compute_daily_deltas():
    series = [
        {"day": "2026-07-01", "subscribers": 100},
        {"day": "2026-07-02", "subscribers": 110},
        {"day": "2026-07-03", "subscribers": 99},
    ]
    deltas = compute_daily_deltas(series)
    assert deltas[0]["delta"] is None
    assert deltas[1]["delta"] == 10
    assert deltas[1]["delta_pct"] == pytest.approx(10.0)
    assert deltas[2]["delta"] == -11
    assert deltas[2]["delta_pct"] == pytest.approx(-10.0)


def test_detect_churn_anomaly_triggers_on_5pct_drop():
    deltas = compute_daily_deltas([
        {"day": "2026-07-01", "subscribers": 1000},
        {"day": "2026-07-02", "subscribers": 940},  # -6%
    ])
    anomaly = detect_churn_anomaly(deltas, threshold_pct=5.0)
    assert anomaly is not None
    assert anomaly["day"] == "2026-07-02"


def test_detect_churn_anomaly_quiet_on_small_drop():
    deltas = compute_daily_deltas([
        {"day": "2026-07-01", "subscribers": 1000},
        {"day": "2026-07-02", "subscribers": 980},  # -2%
    ])
    assert detect_churn_anomaly(deltas, threshold_pct=5.0) is None


def test_subscriber_series_from_snapshots(temp_db):
    now = datetime.now(UTC)
    with sl_db.sqlite_conn() as conn:
        conn.execute(
            """
            INSERT INTO post_features (feed_item_id, posted_at, slot, pipeline_env,
                                       target_channel, policy_version, policy_blob)
            VALUES (1, ?, 'morning', 'prod', 'main', 'v1', '{}')
            """,
            ((now - timedelta(days=3)).isoformat(),),
        )
        for days_ago, subs in [(2, 100), (1, 105), (0, 103)]:
            conn.execute(
                """
                INSERT INTO engagement_metrics (feed_item_id, measured_at, views, subscribers_at)
                VALUES (1, ?, 50, ?)
                """,
                ((now - timedelta(days=days_ago)).isoformat(), subs),
            )

    series = get_subscriber_series(days=14)
    assert len(series) == 3
    assert [p["subscribers"] for p in series] == [100, 105, 103]


# ═══════════════════════════════════════════════════════════════════
# Recommendations (no payment automation)
# ═══════════════════════════════════════════════════════════════════

STATS = {"username": "chan", "subscribers": 20000, "avg_post_reach": 5000, "er_percent": 25.0}


def test_recommendation_computes_max_justified_price():
    rec = build_recommendation(STATS, subscriber_value_rub=50,
                               assumed_conversion_pct=1.0, our_er_percent=20.0)
    # 5000 reach * 1% = 50 subscribers; 50 * 50₽ = 2500₽
    assert rec["expected_subscribers_per_post"] == 50
    assert rec["max_justified_price_rub"] == 2500
    assert rec["worth_buying"] is True


def test_recommendation_rejects_cold_audience():
    cold = {**STATS, "er_percent": 3.0}
    rec = build_recommendation(cold, subscriber_value_rub=50,
                               assumed_conversion_pct=1.0, our_er_percent=20.0)
    assert rec["worth_buying"] is False
    assert "холодная" in rec["notes"]


def test_recommendation_contains_no_payment_action():
    """The action text must keep the human in the loop."""
    rec = build_recommendation(STATS, subscriber_value_rub=50,
                               assumed_conversion_pct=1.0, our_er_percent=None)
    assert "вручную" in rec["action"]
    assert not any(k in str(rec).lower() for k in ("автоплатеж", "auto_pay", "purchase("))


def test_report_renders_without_data():
    report = build_report([], None, [], None)
    assert "Growth report" in report
    assert "не автоматизирована" in report


def test_report_includes_anomaly_and_recommendations():
    deltas = compute_daily_deltas([
        {"day": "2026-07-01", "subscribers": 1000},
        {"day": "2026-07-02", "subscribers": 930},
    ])
    anomaly = detect_churn_anomaly(deltas)
    rec = build_recommendation(STATS, 50, 1.0, 20.0)
    report = build_report(deltas, anomaly, [rec], 20.0)
    assert "Аномалия" in report
    assert "@chan" in report
    assert "2500" in report


def test_report_surfaces_tgstat_error():
    report = build_report([], None, [], None, tgstat_status="токен невалиден")
    assert "TGStat недоступен" in report
    assert "токен невалиден" in report


# ═══════════════════════════════════════════════════════════════════
# TGStat auth-error detection (issue #25)
# ═══════════════════════════════════════════════════════════════════

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_fetch_raises_on_token_error():
    payload = {"status": "error", "error": "token is invalid"}
    with patch.object(competitor_monitor.httpx, "get", return_value=_FakeResp(payload)):
        with pytest.raises(TGStatAuthError):
            fetch_competitor_stats("chan", "BADTOKEN")


def test_fetch_raises_on_subscription_expired():
    payload = {"status": "error", "error": "subscription expired"}
    with patch.object(competitor_monitor.httpx, "get", return_value=_FakeResp(payload)):
        with pytest.raises(TGStatAuthError):
            fetch_competitor_stats("chan", "TOKEN")


def test_fetch_returns_none_on_transient_error():
    """A non-auth error (e.g. unknown channel) is not an auth failure."""
    payload = {"status": "error", "error": "channel not found"}
    with patch.object(competitor_monitor.httpx, "get", return_value=_FakeResp(payload)):
        assert fetch_competitor_stats("chan", "TOKEN") is None


def test_fetch_returns_stats_on_ok():
    payload = {"status": "ok", "response": {"participants_count": 100, "avg_post_reach": 40,
                                            "er_percent": 12.0, "daily_reach": 30}}
    with patch.object(competitor_monitor.httpx, "get", return_value=_FakeResp(payload)):
        stats = fetch_competitor_stats("chan", "TOKEN")
    assert stats["subscribers"] == 100
    assert stats["avg_post_reach"] == 40


# ═══════════════════════════════════════════════════════════════════
# run(): alert + event + circuit breaker (issue #25)
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def growth_env(temp_db, tmp_path, monkeypatch):
    """Temp SQLite + competitors configured + token set + reports to tmp."""
    monkeypatch.setenv("TGSTAT_API_TOKEN", "TOKEN")
    monkeypatch.setattr(competitor_monitor, "REPORTS_DIR", tmp_path / "growth")
    monkeypatch.setattr(competitor_monitor, "load_growth_config",
                        lambda: {"subscriber_value_rub": 50, "assumed_conversion_pct": 1.0,
                                 "channels": [{"username": "chan"}]})
    monkeypatch.setattr(competitor_monitor, "get_subscriber_series", lambda days=30: [])
    monkeypatch.setattr(competitor_monitor, "get_our_er_percent", lambda: None)
    yield


def _count_events(event_type: str) -> int:
    with sl_db.sqlite_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM autopilot_events WHERE event_type = ?",
            (event_type,),
        ).fetchone()["n"]


def test_run_alerts_and_logs_event_on_token_error(growth_env):
    def boom(username, token):
        raise TGStatAuthError("token invalid")

    with patch.object(competitor_monitor, "fetch_competitor_stats", boom), \
         patch.object(competitor_monitor, "_send_alert") as alert:
        competitor_monitor.run()

    alert.assert_called_once()
    assert _count_events("tgstat_token_expired") == 1


def test_run_circuit_breaker_skips_after_repeated_failures(growth_env):
    from aibp.self_learning.db import log_autopilot_event

    # Three prior weekly auth failures within the window → breaker open
    for _ in range(3):
        log_autopilot_event("tgstat_token_expired", details={"error": "token invalid"})

    with patch.object(competitor_monitor, "fetch_competitor_stats") as fetch, \
         patch.object(competitor_monitor, "_send_alert") as alert:
        competitor_monitor.run()

    fetch.assert_not_called()   # breaker prevented any TGStat call
    alert.assert_not_called()


def test_run_logs_ok_event_on_success(growth_env):
    ok_stats = {"username": "chan", "subscribers": 20000, "avg_post_reach": 5000,
                "er_percent": 25.0, "daily_reach": 3000}
    with patch.object(competitor_monitor, "fetch_competitor_stats", return_value=ok_stats), \
         patch.object(competitor_monitor, "_send_alert"):
        competitor_monitor.run()

    assert _count_events("tgstat_ok") == 1
    assert _count_events("tgstat_token_expired") == 0
