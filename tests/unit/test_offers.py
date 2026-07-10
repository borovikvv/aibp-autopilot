"""Tests for the offers catalog and offer selection (issue #38, ADR-0011).

Hermetic: offers read/write via the PG ``fetch_all``/``fetch_one``/``execute``
helpers (and the bandit helpers that wrap them). The selection / outcome tests
patch those names with an in-memory fake so no PostgreSQL (or SQLite) is needed.

Covers: eligibility filtering, expected-revenue Thompson selection, empty
catalog / gate-reject / no-tracking fallbacks in the generation pipeline,
outcome scoring, and idempotent posterior updates.
"""
import contextlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import pytest

from aibp.monetization import offers as offers_mod
from aibp.monetization.offers import (
    OFFER_DIMENSION,
    eligible_offers,
    pick_offer,
    score_offer_rows,
)
from aibp.self_learning import bandit


class OfferFake:
    """In-memory bandit arms + observations + offer outcomes, standing in for
    the PG helpers the offers/bandit code issues."""

    def __init__(self):
        self.arms: dict[tuple[str, str], dict] = {}
        self.observations: set[tuple[int, str]] = set()

    def execute(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("INSERT INTO bandit_arms") and "DO NOTHING" in sql_stripped:
            dim = params[0]
            arm = params[1]
            self.arms.setdefault((dim, arm), {"alpha": 1.0, "beta": 1.0})
            return 1
        if sql_stripped.startswith("UPDATE bandit_arms SET alpha = alpha"):
            dim = params[3]
            arm = params[4]
            a = self.arms.setdefault((dim, arm), {"alpha": 1.0, "beta": 1.0})
            a["alpha"] += params[0]
            a["beta"] += params[1]
            return 1
        if sql_stripped.startswith("INSERT INTO bandit_observations") and "DO NOTHING" in sql_stripped:
            self.observations.add((params[0], params[1]))
            return 1
        raise AssertionError(f"unexpected execute: {sql!r}")

    def fetch_all(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("SELECT arm_id, alpha, beta FROM bandit_arms"):
            dim = params[0]
            return [{"arm_id": arm, "alpha": r["alpha"], "beta": r["beta"]}
                    for (d, arm), r in self.arms.items() if d == dim]
        if sql_stripped.startswith("SELECT o.slug, tl.feed_item_id"):
            # update_offer_outcomes SELECT — caller patches this directly.
            return []
        raise AssertionError(f"unexpected fetch_all: {sql!r}")

    def fetch_one(self, sql, params=()):
        sql_stripped = " ".join(sql.split())
        if sql_stripped.startswith("SELECT 1 FROM bandit_observations"):
            return {"?column?": 1} if (params[0], params[1]) in self.observations else None
        raise AssertionError(f"unexpected fetch_one: {sql!r}")

    def patches(self):
        import aibp.db.connection as conn
        return [
            patch.object(bandit, "execute", self.execute),
            patch.object(bandit, "fetch_all", self.fetch_all),
            # offers.py imports execute/fetch_one/fetch_all locally inside its
            # functions, so patch them at the source module.
            patch.object(conn, "execute", self.execute),
            patch.object(conn, "fetch_one", self.fetch_one),
            patch.object(conn, "fetch_all", self.fetch_all),
        ]


@pytest.fixture()
def fake():
    return OfferFake()


@contextlib.contextmanager
def patched(fake):
    with contextlib.ExitStack() as stack:
        for p in fake.patches():
            stack.enter_context(p)
        yield


def _offer(slug, topics=(), rpc=0.0, status="active", **extra):
    return {"id": hash(slug) % 1000, "slug": slug, "title": f"Offer {slug}",
            "target_url": f"https://partner.example/{slug}", "topics": list(topics),
            "revenue_per_click": rpc, "status": status, **extra}


# ═══════════════════════════════════════════════════════════════════
# Eligibility (pure)
# ═══════════════════════════════════════════════════════════════════

def test_eligible_matches_topic():
    catalog = [_offer("a", topics=["ai_tools"]), _offer("b", topics=["hr_training"])]
    assert [o["slug"] for o in eligible_offers("ai_tools", catalog)] == ["a"]


def test_eligible_untagged_offer_fits_any_topic():
    catalog = [_offer("generic")]
    assert eligible_offers("sales_revenue", catalog) == catalog
    assert eligible_offers(None, catalog) == catalog


def test_eligible_unknown_topic_only_untagged():
    catalog = [_offer("tagged", topics=["ai_tools"]), _offer("generic")]
    assert [o["slug"] for o in eligible_offers(None, catalog)] == ["generic"]


def test_eligible_excludes_paused():
    catalog = [_offer("paused", status="paused")]
    assert eligible_offers("ai_tools", catalog) == []


# ═══════════════════════════════════════════════════════════════════
# Selection
# ═══════════════════════════════════════════════════════════════════

def test_pick_offer_empty_catalog_returns_none(fake):
    with patched(fake):
        assert pick_offer("ai_tools", offers=[]) is None


def test_pick_offer_revenue_dominates_equal_theta(fake):
    """With equal fresh priors, the higher revenue_per_click must win."""
    catalog = [_offer("cheap", rpc=1.0), _offer("rich", rpc=100.0)]
    wins = {"cheap": 0, "rich": 0}
    with patched(fake):
        for seed in range(20):
            offer = pick_offer("any", offers=catalog, rng=np.random.default_rng(seed))
            wins[offer["slug"]] += 1
    assert wins["rich"] > wins["cheap"]


def test_pick_offer_all_zero_revenue_falls_back_to_theta(fake):
    """Unknown revenues → pure Thompson: both offers get picked sometimes."""
    catalog = [_offer("a"), _offer("b")]
    picked = set()
    with patched(fake):
        for s in range(20):
            picked.add(pick_offer("any", offers=catalog, rng=np.random.default_rng(s))["slug"])
    assert picked == {"a", "b"}


def test_pick_offer_learns_from_outcomes(fake):
    """An offer with many click successes should dominate a losing one."""
    from aibp.self_learning.bandit import record_outcome
    catalog = [_offer("winner"), _offer("loser")]
    with patched(fake):
        for i in range(30):
            record_outcome(OFFER_DIMENSION, "winner", True, feed_item_id=1000 + i)
            record_outcome(OFFER_DIMENSION, "loser", False, feed_item_id=2000 + i)
        wins = sum(
            pick_offer("any", offers=catalog, rng=np.random.default_rng(s))["slug"] == "winner"
            for s in range(20)
        )
    assert wins >= 18


def test_pick_offer_pg_failure_returns_none(fake):
    with patched(fake):
        with patch.object(offers_mod, "list_offers", side_effect=RuntimeError("pg down")):
            assert pick_offer("ai_tools") is None


# ═══════════════════════════════════════════════════════════════════
# Pipeline integration — attach + fallback
# ═══════════════════════════════════════════════════════════════════

def _settings_with_tracking(url):
    class S:
        tracking_base_url = url
    return S()


def test_attach_offer_disabled_without_tracking_url():
    from aibp.generation import pipeline
    with patch.object(pipeline, "get_settings",
                      return_value=_settings_with_tracking("")):
        assert pipeline.attach_affiliate_offer("post", {"id": 1}) is None


def test_attach_offer_none_when_no_offer(fake):
    from aibp.generation import pipeline
    with patched(fake):
        with patch.object(pipeline, "get_settings",
                          return_value=_settings_with_tracking("https://t.example")), \
             patch("aibp.monetization.offers.pick_offer", return_value=None):
            assert pipeline.attach_affiliate_offer("post", {"id": 1, "summary": None}) is None


def test_attach_offer_appends_tracked_cta(fake):
    from aibp.generation import pipeline
    offer = _offer("gpt-agg", topics=["ai_tools"], rpc=20.0)
    post = 'Текст поста.\n<a href="https://src.example">Источник</a>'
    with patched(fake):
        with patch.object(pipeline, "get_settings",
                          return_value=_settings_with_tracking("https://t.example")), \
             patch("aibp.monetization.offers.pick_offer", return_value=offer), \
             patch("aibp.tracking.redirect_service.register_link",
                   return_value="ab12cd34") as register, \
             patch("aibp.tracking.redirect_service.short_url",
                   return_value="https://t.example/r/ab12cd34"):
            result = pipeline.attach_affiliate_offer(
                post, {"id": 7, "summary": {"editorial": {"topic_cluster": "ai_tools"}}})

    assert result is not None
    new_post, slug = result
    assert slug == "gpt-agg"
    assert "https://t.example/r/ab12cd34" in new_post
    assert new_post.rstrip().endswith('<a href="https://src.example">Источник</a>')
    register.assert_called_once_with(7, offer["target_url"], offer_id=offer["id"])


def test_attach_offer_rejected_title_falls_back(fake):
    from aibp.generation import pipeline
    offer = _offer("spammy", rpc=99.0)
    offer["title"] = "Только сегодня! Успейте купить со скидкой"
    with patched(fake):
        with patch.object(pipeline, "get_settings",
                          return_value=_settings_with_tracking("https://t.example")), \
             patch("aibp.monetization.offers.pick_offer", return_value=offer):
            assert pipeline.attach_affiliate_offer("post", {"id": 1, "summary": None}) is None


def test_cta_fallback_weights_exclude_affiliate():
    """Empty catalog → resample among the remaining variants only."""
    from aibp.generation.pipeline import AFFILIATE_VARIANT, select_cta_variant
    weights = {"save_forward": 1.0, "affiliate_link": 1.0, "comment_prompt": 1.0}
    fallback = {k: v for k, v in weights.items() if k != AFFILIATE_VARIANT}
    for _ in range(20):
        variant = select_cta_variant({"cta_variants": fallback}, slot="morning")
        assert variant != AFFILIATE_VARIANT


# ═══════════════════════════════════════════════════════════════════
# Outcome scoring (pure)
# ═══════════════════════════════════════════════════════════════════

def test_score_offer_rows_success_is_any_click():
    rows = [
        {"slug": "a", "feed_item_id": 1, "clicks": 3},
        {"slug": "a", "feed_item_id": 2, "clicks": 0},
        {"slug": "b", "feed_item_id": 3, "clicks": None},
    ]
    assert score_offer_rows(rows) == [("a", 1, True), ("a", 2, False), ("b", 3, False)]


def test_update_offer_outcomes_idempotent(fake):
    rows = [{"slug": "a", "feed_item_id": 1, "clicks": 2}]
    with patched(fake):
        # First call: the SELECT returns one row → one observation recorded.
        with patch("aibp.db.connection.fetch_all", return_value=rows):
            assert offers_mod.update_offer_outcomes() == 1
        # Second call: fetch_one sees the observation already exists → 0.
        with patch("aibp.db.connection.fetch_all", return_value=rows):
            assert offers_mod.update_offer_outcomes() == 0

    arm = fake.arms[(OFFER_DIMENSION, "a")]
    assert (arm["alpha"], arm["beta"]) == (2.0, 1.0)  # exactly one success recorded


def test_update_offer_outcomes_pg_down_is_noop(fake):
    with patched(fake):
        with patch("aibp.db.connection.fetch_all", side_effect=RuntimeError("pg down")):
            assert offers_mod.update_offer_outcomes() == 0
