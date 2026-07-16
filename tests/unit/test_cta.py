"""Tests for CTA variants as a policy dimension (issue #16, #26)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest

from aibp.generation.pipeline import CTA_TEMPLATES, append_cta, select_cta_variant
from aibp.generation.quality_gate import validate_cta_text
from aibp.self_learning.policy_updater import apply_change_to_policy, validate_change_spec

# ═══════════════════════════════════════════════════════════════════
# Variant selection
# ═══════════════════════════════════════════════════════════════════

def test_select_respects_weights():
    policy = {"cta_variants": {"save_forward": 1.0, "affiliate_link": 0.0, "comment_prompt": 0.0}}
    for _ in range(20):
        assert select_cta_variant(policy) == "save_forward"


def test_select_returns_none_without_variants():
    assert select_cta_variant({}) is None
    assert select_cta_variant({"cta_variants": {}}) is None
    assert select_cta_variant({"cta_variants": {"unknown_variant": 1.0}}) is None


def test_select_all_default_weights_covers_all_variants():
    policy = {"cta_variants": {v: 1.0 for v in CTA_TEMPLATES}}
    seen = {select_cta_variant(policy) for _ in range(200)}
    assert seen == set(CTA_TEMPLATES)


# ═══════════════════════════════════════════════════════════════════
# CTA insertion
# ═══════════════════════════════════════════════════════════════════

def test_append_cta_before_source_link():
    post = '<b>Заголовок</b>\n\nАбзац текста.\n\n<a href="https://x.com/a">Источник</a>'
    result = append_cta(post, "comment_prompt")
    cta_pos = result.find(CTA_TEMPLATES["comment_prompt"])
    source_pos = result.find("Источник")
    assert cta_pos != -1
    assert cta_pos < source_pos
    assert result.count("Источник") == 1


def test_append_cta_without_source_link_appends_to_end():
    post = "<b>Заголовок</b>\n\nАбзац."
    result = append_cta(post, "save_forward")
    assert result.endswith(f"<i>{CTA_TEMPLATES['save_forward']}</i>")


def test_append_cta_unknown_variant_is_noop():
    post = "<b>Заголовок</b>"
    assert append_cta(post, "nonexistent") == post


# ═══════════════════════════════════════════════════════════════════
# Policy updater: cta experiment type
# ═══════════════════════════════════════════════════════════════════

CURRENT_POLICY = {
    "version": "v1",
    "cta_variants": {"save_forward": 1.0, "affiliate_link": 1.0, "comment_prompt": 1.0},
}


def test_validate_cta_experiment_ok():
    hyp = {"experiment_type": "cta",
           "change_spec": {"variant": "affiliate_link", "new_weight": 1.5}}
    valid, reason = validate_change_spec(hyp, CURRENT_POLICY)
    assert valid, reason


def test_validate_cta_unknown_variant_rejected():
    hyp = {"experiment_type": "cta",
           "change_spec": {"variant": "buy_now", "new_weight": 1.5}}
    valid, reason = validate_change_spec(hyp, CURRENT_POLICY)
    assert not valid
    assert "unknown cta variant" in reason


def test_validate_cta_weight_out_of_range_rejected():
    hyp = {"experiment_type": "cta",
           "change_spec": {"variant": "save_forward", "new_weight": 5.0}}
    valid, reason = validate_change_spec(hyp, CURRENT_POLICY)
    assert not valid


def test_apply_cta_change():
    hyp = {"experiment_type": "cta",
           "change_spec": {"variant": "affiliate_link", "new_weight": 1.5}}
    new_policy = apply_change_to_policy(CURRENT_POLICY, hyp)
    assert new_policy["cta_variants"]["affiliate_link"] == 1.5
    assert CURRENT_POLICY["cta_variants"]["affiliate_link"] == 1.0  # original untouched


# ═══════════════════════════════════════════════════════════════════
# CTA quality gate (issue #26): CTA text bypasses validate_post, so its
# promotional-phrase banlist is enforced separately before appending.
# ═══════════════════════════════════════════════════════════════════

def test_shipped_cta_templates_pass_the_gate():
    """Every authored CTA must clear the promotional banlist — this guards
    against a future edit sneaking a clickbait phrase past the gate."""
    for variant, text in CTA_TEMPLATES.items():
        assert validate_cta_text(text)["ok"], f"{variant} is promotional: {text}"


@pytest.mark.parametrize("phrase", [
    "Подпишитесь, чтобы не пропустить следующие разборы!",
    "Не пропустите новый пост",
    "Жмите на ссылку",
    "Ставьте лайк и переходите по ссылке",
    "Регистрируйтесь прямо сейчас",
    "Успей купить только сегодня",
])
def test_promotional_ctas_are_rejected(phrase):
    result = validate_cta_text(phrase)
    assert result["ok"] is False
    assert result["status"] == "fail"


@pytest.mark.parametrize("phrase", [
    "Где в вашем процессе такая граница уже нужна?",
    "Сохраните пост — пригодится при внедрении.",
    "Расскажите в комментариях, что сработало.",
])
def test_neutral_ctas_pass(phrase):
    assert validate_cta_text(phrase)["ok"] is True


# ═══════════════════════════════════════════════════════════════════
# Anti-repeat: previous post's CTA is excluded (2026-07-16)
# ═══════════════════════════════════════════════════════════════════

def test_select_cta_excludes_previous_variant():
    from aibp.generation.pipeline import select_cta_variant
    policy = {"cta_variants": {"save_forward": 1.0, "comment_prompt": 1.0}}
    for _ in range(20):
        assert select_cta_variant(policy, slot="morning",
                                  exclude="save_forward") == "comment_prompt"


def test_select_cta_keeps_sole_variant_despite_exclude():
    from aibp.generation.pipeline import select_cta_variant
    policy = {"cta_variants": {"save_forward": 1.0}}
    assert select_cta_variant(policy, slot="morning",
                              exclude="save_forward") == "save_forward"
