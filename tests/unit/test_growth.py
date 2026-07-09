"""Tests for growth monitoring (issue #19).

Hermetic: growth reads via the PG ``fetch_all``/``fetch_one`` helpers and logs
events via ``log_autopilot_event``. The run()-level tests already patch the
high-level growth functions; the DB-touching tests patch those helpers with an
in-memory fake so no PostgreSQL (or SQLite) is needed.
"""
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

# ═══════════════════════════════════════════════════════════════════
# Daily deltas + anomaly detection (pure functions)
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


def test_subscriber_series_from_snapshots():
    """get_subscriber_series aggregates the daily MAX snapshot from PG;
    patch competitor_monitor.fetch_all to return canned rows."""
    now = datetime.now(UTC)
    rows = [
        {"day": (now - timedelta(days=2)).date(), "subscribers": 100},
        {"day": (now - timedelta(days=1)).date(), "subscribers": 105},
        {"day": now.date(), "subscribers": 103},
    ]
    with patch.object(competitor_monitor, "fetch_all", return_value=rows):
        series = get_subscriber_series(days=14)
    assert len(series) == 3
    assert [p["subscribers"] for p in series] == [100, 105, 103]


# ═══════════════════════════════════════════════════════════════════
# Recommendations (no payment automation) — pure functions
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
def growth_env(tmp_path, monkeypatch):
    """Temp reports dir + competitors configured + token set + no live DB."""
    monkeypatch.setenv("TGSTAT_API_TOKEN", "TOKEN")
    monkeypatch.setattr(competitor_monitor, "REPORTS_DIR", tmp_path / "growth")
    monkeypatch.setattr(competitor_monitor, "load_growth_config",
                        lambda: {"subscriber_value_rub": 50, "assumed_conversion_pct": 1.0,
                                 "channels": [{"username": "chan"}]})
    monkeypatch.setattr(competitor_monitor, "get_subscriber_series", lambda days=30: [])
    monkeypatch.setattr(competitor_monitor, "get_our_er_percent", lambda: None)
    # _recent_auth_failures + traffic_sources CPS summary hit PG; stub them so
    # run() stays hermetic (no live connection).
    monkeypatch.setattr(competitor_monitor, "_recent_auth_failures", lambda days=21: 0)
    monkeypatch.setattr("aibp.growth.traffic_sources.cps_summary", lambda: [])
    yield


def test_run_alerts_and_logs_event_on_token_error(growth_env):
    events = []

    def boom(username, token):
        raise TGStatAuthError("token invalid")

    with patch.object(competitor_monitor, "fetch_competitor_stats", boom), \
         patch.object(competitor_monitor, "_send_alert"), \
         patch.object(competitor_monitor, "log_autopilot_event",
                      side_effect=lambda *a, **kw: events.append(a)):
        competitor_monitor.run()

    assert len(events) == 1
    assert events[0][0] == "tgstat_token_expired"


def test_run_circuit_breaker_skips_after_repeated_failures(growth_env):
    # Three prior weekly auth failures within the window → breaker open.
    # _recent_auth_failures reads via fetch_one; patch it to report 3.
    with patch.object(competitor_monitor, "_recent_auth_failures", return_value=3), \
         patch.object(competitor_monitor, "fetch_competitor_stats") as fetch, \
         patch.object(competitor_monitor, "_send_alert") as alert:
        competitor_monitor.run()

    fetch.assert_not_called()   # breaker prevented any TGStat call
    alert.assert_not_called()


def test_run_logs_ok_event_on_success(growth_env):
    events = []
    ok_stats = {"username": "chan", "subscribers": 20000, "avg_post_reach": 5000,
                "er_percent": 25.0, "daily_reach": 3000}
    with patch.object(competitor_monitor, "fetch_competitor_stats", return_value=ok_stats), \
         patch.object(competitor_monitor, "_send_alert"), \
         patch.object(competitor_monitor, "log_autopilot_event",
                      side_effect=lambda *a, **kw: events.append(a)):
        competitor_monitor.run()

    assert [e[0] for e in events] == ["tgstat_ok"]
