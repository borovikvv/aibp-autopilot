"""Tests for the multi-source weekly_digest (issue #45).

Three surfaces:
  - validate_post link-block gate for slot=weekly_digest;
  - select_digest_candidates (multi-source selection, DB mocked);
  - generate_post rendering the digest prompt with every source URL.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from aibp.generation import pipeline
from aibp.generation.quality_gate import validate_post

DIGEST_URLS = [
    "https://a.example.com/1",
    "https://b.example.com/2",
    "https://c.example.com/3",
    "https://d.example.com/4",
]


def _digest_post(urls, headline="<b>Сигнал недели</b>"):
    """A well-formed digest: headline, prose, then a link block."""
    block = "\n".join(f'<a href="{u}">Материал {i}</a>' for i, u in enumerate(urls, 1))
    return (
        f"{headline}\n\n"
        "На этой неделе несколько независимых сдвигов складываются в одну картину: "
        "инструменты дешевеют, а узкое место смещается в проверку результата.\n\n"
        "Что это меняет в работе — можно перестроить рутину так, чтобы человек "
        "тратил время на решение, а не на сверку.\n\n"
        f"{block}"
    )


# ═══════════════════════════════════════════════════════════════════
# Quality gate — link-block membership
# ═══════════════════════════════════════════════════════════════════

def test_digest_all_urls_present_passes():
    post = _digest_post(DIGEST_URLS)
    result = validate_post(post, slot="weekly_digest", digest_urls=DIGEST_URLS)
    assert result["verdicts"]["source_link"]["status"] == "pass"
    assert result["ok"] is True


def test_digest_missing_url_fails():
    post = _digest_post(DIGEST_URLS[:-1])  # drop the last source's link
    result = validate_post(post, slot="weekly_digest", digest_urls=DIGEST_URLS)
    assert result["ok"] is False
    assert "source_link" in result["hard_fail_keys"]
    hits = [h["text"] for h in result["verdicts"]["source_link"]["hits"]]
    assert any(DIGEST_URLS[-1] in h and "missing" in h for h in hits)


def test_digest_foreign_link_fails():
    post = _digest_post(DIGEST_URLS + ["https://evil.example.com/spam"])
    result = validate_post(post, slot="weekly_digest", digest_urls=DIGEST_URLS)
    assert result["ok"] is False
    hits = [h["text"] for h in result["verdicts"]["source_link"]["hits"]]
    assert any("foreign_link" in h and "evil.example.com" in h for h in hits)


def test_digest_duplicate_link_fails():
    post = _digest_post(DIGEST_URLS + [DIGEST_URLS[0]])  # first source twice
    result = validate_post(post, slot="weekly_digest", digest_urls=DIGEST_URLS)
    assert result["ok"] is False
    hits = [h["text"] for h in result["verdicts"]["source_link"]["hits"]]
    assert any("duplicate_source_link" in h for h in hits)


def test_digest_without_expected_set_requires_a_block():
    """No digest_urls (bare call) → the post must still carry some link block."""
    no_links = "<b>Сигнал недели</b>\n\nПрочный текст без единой ссылки в конце."
    result = validate_post(no_links, slot="weekly_digest")
    assert result["ok"] is False
    assert "source_link" in result["hard_fail_keys"]


def test_digest_core_gates_still_apply():
    """Forbidden terms remain a hard fail in a digest, not just the link block."""
    post = _digest_post(DIGEST_URLS, headline="<b>Дайджест для малого бизнеса</b>")
    result = validate_post(post, slot="weekly_digest", digest_urls=DIGEST_URLS)
    assert result["ok"] is False
    assert "forbidden_terms" in result["hard_fail_keys"]


# ═══════════════════════════════════════════════════════════════════
# select_digest_candidates
# ═══════════════════════════════════════════════════════════════════

def _row(id_, rubric, score, domain, publish_worthy=True):
    return {
        "id": id_,
        "url": f"https://{domain}/{id_}",
        "title": f"Post {id_}",
        "text": "excerpt",
        "source": domain,
        "source_domain": domain,
        "source_lang": "en",
        "source_published_at": "2026-07-15T10:00:00Z",
        "summary": json.dumps({"editorial": {
            "publish_worthy": publish_worthy, "strategy_rubric": rubric,
        }}),
        "rank_score": score,
        "relevance": 4.0,
    }


_POLICY = {"rubric_weights": {"anti_hype": 1.0, "process_under_ai": 1.0}}


def test_select_digest_orders_by_score_and_dedups_domains():
    rows = [
        _row(1, "anti_hype", 90, "a.com"),
        _row(2, "anti_hype", 80, "b.com"),
        _row(3, "anti_hype", 70, "a.com"),  # dup domain of #1 → skipped
        _row(4, "anti_hype", 60, "c.com"),
    ]
    with patch.object(pipeline, "fetch_all", return_value=rows):
        result = pipeline.select_digest_candidates(_POLICY, pipeline_env="prod")
    ids = [r["id"] for r in result]
    assert ids == [1, 2, 4]  # #3 dropped as a duplicate domain, ordered by score


def test_select_digest_respects_max():
    rows = [_row(i, "anti_hype", 100 - i, f"d{i}.com") for i in range(10)]
    with patch.object(pipeline, "fetch_all", return_value=rows):
        result = pipeline.select_digest_candidates(_POLICY, pipeline_env="prod")
    assert len(result) == pipeline.DIGEST_MAX_MATERIALS


def test_select_digest_too_few_returns_empty():
    rows = [_row(1, "anti_hype", 90, "a.com"), _row(2, "anti_hype", 80, "b.com")]
    with patch.object(pipeline, "fetch_all", return_value=rows):
        result = pipeline.select_digest_candidates(_POLICY, pipeline_env="prod")
    assert result == []  # below DIGEST_MIN_MATERIALS


def test_select_digest_relaxes_domain_dedup_when_scarce():
    """If unique domains are too few, fall back to the raw top-N."""
    rows = [
        _row(1, "anti_hype", 90, "a.com"),
        _row(2, "anti_hype", 80, "a.com"),
        _row(3, "anti_hype", 70, "a.com"),
    ]
    with patch.object(pipeline, "fetch_all", return_value=rows):
        result = pipeline.select_digest_candidates(_POLICY, pipeline_env="prod")
    assert [r["id"] for r in result] == [1, 2, 3]


def test_select_digest_empty_pool():
    with patch.object(pipeline, "fetch_all", return_value=[]):
        assert pipeline.select_digest_candidates(_POLICY, pipeline_env="prod") == []


# ═══════════════════════════════════════════════════════════════════
# generate_post — digest prompt
# ═══════════════════════════════════════════════════════════════════

class _CaptureClient:
    default_model = "x"

    def __init__(self):
        self.calls = []

    def chat(self, messages, temperature=0.4, max_tokens=2000):
        self.calls.append({"prompt": messages[0]["content"]})
        return _digest_post(DIGEST_URLS)


def test_generate_post_digest_includes_all_urls():
    materials = [_row(i, "anti_hype", 90 - i, f"d{i}.com") for i in range(1, 5)]
    for m, u in zip(materials, DIGEST_URLS):
        m["url"] = u
    policy = {"rubric_weights": {"anti_hype": 1.0},
              "post_params": {"weekly_digest": {"target_chars": [2500, 4500], "paragraphs": [5, 8], "max_bold": 2}}}
    client = _CaptureClient()
    pipeline.generate_post(materials[0], "weekly_digest", policy, client,
                           digest_materials=materials)
    prompt = client.calls[0]["prompt"]
    for u in DIGEST_URLS:
        assert u in prompt
    assert "WEEKLY DIGEST" in prompt


# ═══════════════════════════════════════════════════════════════════
# run() wiring — prod digest INSERTs a fresh row, no in-place UPDATE
# ═══════════════════════════════════════════════════════════════════

def test_run_prod_digest_inserts_new_row(monkeypatch):
    """A prod weekly_digest is stored via INSERT (new row), never UPDATE, so an
    already-published source row is not clobbered."""
    materials = [_row(i, "anti_hype", 90 - i, f"d{i}.com") for i in range(1, 5)]
    for m, u in zip(materials, DIGEST_URLS):
        m["url"] = u

    class FakeClient:
        default_model = "x"

        def __init__(self, *a, **k):
            pass

        def chat(self, messages, temperature=0.4, max_tokens=2000):
            return _digest_post(DIGEST_URLS)

    policy = {
        "rubric_weights": {"anti_hype": 1.0},
        "post_params": {"weekly_digest": {
            "target_chars": [2500, 4500], "paragraphs": [5, 8],
            "max_bold": 2, "scheduled_hour_msk": 19}},
        "llm_editor": {"enabled": False},
        "visual_policy": {"slots": ["morning"]},
        "regex_gates": [],
    }
    inserts, updates = [], []

    import aibp.self_learning.interleave as interleave
    monkeypatch.setattr(pipeline, "OpenRouterClient", FakeClient)
    monkeypatch.setattr(pipeline, "load_policy", lambda *a, **k: policy)
    monkeypatch.setattr(pipeline, "select_digest_candidates", lambda *a, **k: materials)
    monkeypatch.setattr(interleave, "resolve_policy_for_today", lambda p: p)
    monkeypatch.setattr(pipeline, "wrap_tracked_links", lambda post, _id: post)
    monkeypatch.setattr(pipeline, "execute",
                        lambda sql, params=(): updates.append(sql) or 1)
    monkeypatch.setattr(pipeline, "execute_returning",
                        lambda sql, params=(): inserts.append((sql, params)) or {"id": 999})

    rc = pipeline.run(slot="weekly_digest", pipeline_env="prod")

    assert rc == 0
    assert len(inserts) == 1
    assert "INSERT INTO feed_items" in inserts[0][0]
    # No source row was flipped to rejected / approved via UPDATE.
    assert not any("UPDATE feed_items\n            SET\n" in s for s in updates)


def test_run_prod_digest_empty_pool_returns_zero(monkeypatch):
    import aibp.self_learning.interleave as interleave
    monkeypatch.setattr(pipeline, "OpenRouterClient", lambda *a, **k: object())
    monkeypatch.setattr(pipeline, "load_policy", lambda *a, **k: {"post_params": {}})
    monkeypatch.setattr(interleave, "resolve_policy_for_today", lambda p: p)
    monkeypatch.setattr(pipeline, "select_digest_candidates", lambda *a, **k: [])
    assert pipeline.run(slot="weekly_digest", pipeline_env="prod") == 0
