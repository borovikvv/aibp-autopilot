"""Tests for traffic-source attribution and the ad-buying pipeline (issue #39).

Covers: chat_member update parsing (joins via invite link vs everything
else), join recording, CPS math, report lines, and the ad-plan document
(forecast present / absent, invite link preserved in the creative).
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.growth.ad_buying import APPLICATION_TEMPLATE, build_plan_md
from aibp.growth.traffic_sources import (
    compute_cps,
    cps_report_lines,
    parse_chat_member_join,
    record_join,
)

PROD = "-1001234567890"
LINK = "https://t.me/+AbCdEf123"


def _member_update(chat_id=PROD, old="left", new="member", invite=LINK,
                   user_id=777, date=1751900000):
    cm = {
        "chat": {"id": int(chat_id)},
        "date": date,
        "old_chat_member": {"status": old, "user": {"id": user_id}},
        "new_chat_member": {"status": new, "user": {"id": user_id}},
    }
    if invite is not None:
        cm["invite_link"] = {"invite_link": invite}
    return {"update_id": 1, "chat_member": cm}


# ═══════════════════════════════════════════════════════════════════
# parse_chat_member_join
# ═══════════════════════════════════════════════════════════════════

def test_parse_join_via_invite_link():
    join = parse_chat_member_join(_member_update(), PROD)
    assert join == {"invite_link": LINK, "user_id": 777,
                    "joined_at": join["joined_at"]}
    assert join["joined_at"].startswith("2025") or join["joined_at"].startswith("2026")


def test_parse_ignores_other_chat():
    assert parse_chat_member_join(_member_update(chat_id="-100999"), PROD) is None


def test_parse_ignores_leave():
    upd = _member_update(old="member", new="left")
    assert parse_chat_member_join(upd, PROD) is None


def test_parse_ignores_existing_member_status_change():
    upd = _member_update(old="member", new="administrator")
    assert parse_chat_member_join(upd, PROD) is None


def test_parse_ignores_join_without_invite_link():
    assert parse_chat_member_join(_member_update(invite=None), PROD) is None


def test_parse_ignores_non_chat_member_update():
    assert parse_chat_member_join({"update_id": 1, "callback_query": {}}, PROD) is None


def test_parse_rejoin_after_kick_counts():
    upd = _member_update(old="kicked", new="member")
    assert parse_chat_member_join(upd, PROD) is not None


# ═══════════════════════════════════════════════════════════════════
# record_join
# ═══════════════════════════════════════════════════════════════════

def test_record_join_unknown_link_returns_false():
    with patch("aibp.db.connection.fetch_one", return_value=None):
        assert record_join(LINK, 777, "2026-07-07T10:00:00+00:00") is False


def test_record_join_attributes_to_source():
    calls = []
    with patch("aibp.db.connection.fetch_one",
               return_value={"id": 5, "slug": "ad_test_20260707"}), \
         patch("aibp.db.connection.execute",
               side_effect=lambda sql, params: calls.append(params) or 1):
        assert record_join(LINK, 777, "2026-07-07T10:00:00+00:00") is True
    assert calls == [(5, 777, "2026-07-07T10:00:00+00:00")]


# ═══════════════════════════════════════════════════════════════════
# CPS math + report
# ═══════════════════════════════════════════════════════════════════

def test_compute_cps():
    assert compute_cps(3000, 60) == 50
    assert compute_cps(3000, 0) is None


def test_cps_report_lines_fact_vs_forecast():
    rows = [{"slug": "ad_donor_20260701", "kind": "ad_buy", "status": "live",
             "channel_username": "donor", "cost_rub": 3000.0, "joins": 60,
             "actual_cps_rub": 50.0, "expected_subscribers": 40.0,
             "expected_cps_rub": 50.0}]
    text = "\n".join(cps_report_lines(rows))
    assert "ad_donor_20260701" in text
    assert "**60**" in text
    assert "50 ₽/подписчик" in text
    assert "~40 подписчиков" in text


def test_cps_report_lines_empty():
    text = "\n".join(cps_report_lines([]))
    assert "Источников пока нет" in text


# ═══════════════════════════════════════════════════════════════════
# Ad plan document
# ═══════════════════════════════════════════════════════════════════

FORECAST = {"worth_buying": True, "expected_subscribers_per_post": 40.0,
            "max_justified_price_rub": 2000, "estimated_cac_at_max_price_rub": 50,
            "notes": "по метрикам выглядит разумно", "action": "..."}


def test_plan_md_with_forecast():
    plan = build_plan_md("ad_donor_20260707", "donor", LINK, "Креатив.",
                         APPLICATION_TEMPLATE.format(donor="donor"), FORECAST)
    assert "✅ стоит рассмотреть" in plan
    assert "**2000 ₽**" in plan
    assert LINK in plan
    assert "aibp source-set ad_donor_20260707 --cost" in plan


def test_plan_md_without_forecast_still_actionable():
    plan = build_plan_md("ad_donor_20260707", "donor", LINK, "Креатив.",
                         APPLICATION_TEMPLATE.format(donor="donor"), None)
    assert "Нет данных TGStat" in plan
    assert LINK in plan


def test_plan_ad_buy_appends_link_if_llm_drops_it(tmp_path):
    """The tracked invite link is the whole point — enforced even if the
    LLM ignores the instruction."""
    from aibp.growth import ad_buying

    source = {"invite_link": LINK, "slug": "ad_donor_x"}
    with patch.object(ad_buying, "forecast_for_channel", return_value=None), \
         patch("aibp.growth.traffic_sources.create_source", return_value=source), \
         patch("aibp.enrichment.llm_client.OpenRouterClient") as client_cls, \
         patch.object(ad_buying, "ADS_DIR", tmp_path):
        client_cls.return_value.chat.return_value = "Пост без ссылки."
        path = ad_buying.plan_ad_buy("@donor")

    content = Path(path).read_text(encoding="utf-8")
    assert f"Подписаться: {LINK}" in content
    assert Path(path).name.startswith("ad_donor_")  # slug strips the @
