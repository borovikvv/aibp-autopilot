"""Pattern Miner — weekly statistical + LLM analysis of engagement.

Cron: weekly (Sun 22:00 MSK).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import structlog
from scipy import stats as scipy_stats

from aibp.enrichment.llm_client import OpenRouterClient
from aibp.self_learning.db import get_snapshot_at_horizon, sqlite_conn
from aibp.utils.config import PROJECT_ROOT, load_policy

log = structlog.get_logger()

REPORTS_DIR = PROJECT_ROOT / "reports" / "self_learning"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_post_data(days: int = 7) -> list[dict]:
    """Load posts with features and engagement at the fixed 48h horizon.

    The horizon snapshot (not the last one) removes time-decay bias: views
    grow ~72h after publishing, so the last snapshot favors older posts.
    """
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT pf.feed_item_id, pf.posted_at, pf.slot, pf.target_channel,
                   pf.strategy_rubric, pf.topic_cluster, pf.source_domain,
                   pf.char_count, pf.paragraph_count, pf.bold_count, pf.emoji_count,
                   pf.has_image, pf.scheduled_hour, pf.cta_variant, pf.policy_version
            FROM post_features pf
            WHERE pf.posted_at >= ?
              AND pf.target_channel = 'main'
            ORDER BY pf.posted_at DESC
            """,
            ((datetime.now(UTC) - timedelta(days=days)).isoformat(),),
        ).fetchall()
        posts = [dict(r) for r in rows]

    for post in posts:
        snapshot = get_snapshot_at_horizon(post["feed_item_id"])
        post["latest_views"] = snapshot["views"] if snapshot else None
        post["latest_subs"] = snapshot["subscribers_at"] if snapshot else None
    return posts


def compute_statistical_baseline(posts: list[dict], slot: str | None = None) -> dict:
    """Compute statistical baseline for a set of posts."""
    if slot:
        posts = [p for p in posts if p["slot"] == slot]

    if len(posts) < 3:
        return {"status": "insufficient_data", "n": len(posts), "slot": slot}

    # Normalize views by subscribers (engagement rate)
    views = [p["latest_views"] or 0 for p in posts if p["latest_views"] is not None]
    subs = [p["latest_subs"] or 1 for p in posts if p["latest_subs"] is not None]
    engagement_rates = [v / s for v, s in zip(views, subs) if s > 0]

    if not engagement_rates:
        return {"status": "no_engagement_data", "n": len(posts)}

    result = {
        "n": len(posts),
        "slot": slot,
        "mean_views": sum(views) / len(views),
        "mean_engagement_rate": sum(engagement_rates) / len(engagement_rates),
    }

    # Group by rubric
    rubric_groups: dict[str, list[float]] = {}
    for p, er in zip(posts, engagement_rates):
        rubric = p.get("strategy_rubric") or "unknown"
        rubric_groups.setdefault(rubric, []).append(er)

    result["rubric_stats"] = {
        r: {"n": len(ers), "mean_engagement": sum(ers) / len(ers)}
        for r, ers in rubric_groups.items() if len(ers) >= 2
    }

    # Group by CTA variant (monetization funnel, issue #16)
    cta_groups: dict[str, list[float]] = {}
    for p, er in zip(posts, engagement_rates):
        variant = p.get("cta_variant") or "none"
        cta_groups.setdefault(variant, []).append(er)

    result["cta_stats"] = {
        v: {"n": len(ers), "mean_engagement": sum(ers) / len(ers)}
        for v, ers in cta_groups.items() if len(ers) >= 2
    }

    # Correlation: char_count vs engagement
    char_counts = [p["char_count"] for p in posts if p["latest_views"] is not None]
    if len(char_counts) >= 5:
        try:
            r_pearson, p_value = scipy_stats.pearsonr(char_counts, engagement_rates)
            result["char_count_correlation"] = {
                "pearson_r": round(r_pearson, 3),
                "p_value": round(p_value, 4),
                "significant": p_value < 0.05,
            }
        except Exception:
            pass

    # Group by has_image
    image_posts = [er for p, er in zip(posts, engagement_rates) if p["has_image"]]
    no_image_posts = [er for p, er in zip(posts, engagement_rates) if not p["has_image"]]
    if len(image_posts) >= 2 and len(no_image_posts) >= 2:
        try:
            t_stat, p_value = scipy_stats.ttest_ind(image_posts, no_image_posts)
            result["image_ttest"] = {
                "t": round(t_stat, 3),
                "p_value": round(p_value, 4),
                "image_mean": sum(image_posts) / len(image_posts),
                "no_image_mean": sum(no_image_posts) / len(no_image_posts),
                "significant": p_value < 0.05,
            }
        except Exception:
            pass

    return result


def llm_pattern_mine(stats: dict, posts: list[dict], client: OpenRouterClient, policy: dict) -> list[dict]:
    """Use LLM to generate actionable hypotheses from stats."""
    # Top and bottom posts
    sorted_posts = sorted(
        [p for p in posts if p["latest_views"] is not None],
        key=lambda p: p["latest_views"],
        reverse=True,
    )
    top = sorted_posts[:3]
    bottom = sorted_posts[-3:]

    # Prepare context as separate variables to avoid f-string nesting issues
    stats_json = json.dumps(stats, indent=2, ensure_ascii=False, default=str)
    policy_json = json.dumps(policy, indent=2, ensure_ascii=False, default=str)

    feature_keys = (
        'strategy_rubric', 'char_count', 'paragraph_count',
        'has_image', 'scheduled_hour',
    )

    top_summaries = []
    for p in top:
        features = {k: v for k, v in p.items() if k in feature_keys}
        top_summaries.append({"features": features, "views": p.get('latest_views')})
    top_json = json.dumps(top_summaries, indent=2, ensure_ascii=False, default=str)

    bottom_summaries = []
    for p in bottom:
        features = {k: v for k, v in p.items() if k in feature_keys}
        bottom_summaries.append({"features": features, "views": p.get('latest_views')})
    bottom_json = json.dumps(bottom_summaries, indent=2, ensure_ascii=False, default=str)

    prompt = f"""You are a data scientist for a Russian Telegram channel about AI in business (@AI_Business_Pulse).

Analyze the past week's data and propose 3-5 hypotheses for improving engagement.

## Statistical baseline (last 7 days)
{stats_json}

## Top 3 posts (highest views)
{top_json}

## Bottom 3 posts (lowest views)
{bottom_json}

## Current policy
{policy_json}

## Output format

Return JSON array of 3-5 hypotheses. Each hypothesis:
{{
  "experiment_type": "rubric_weight" | "post_param" | "regex_gate" | "source_score" | "visual" | "cta",
  "hypothesis": "natural language description of what to change",
  "change_spec": {{
    "rubric": "anti_hype",
    "new_weight": 1.3
  }},
  "expected_effect": "+25% engagement",
  "confidence": 0.0-1.0,
  "target_metric": "views_per_subscriber"
}}

For "cta" experiments change_spec is {{"variant": "affiliate_link", "new_weight": 1.5}} —
variants come from policy cta_variants; target_metric may be "clicks_per_view"
(conversion) when cta_stats show a difference.

Only propose changes that are supported by the data. Be conservative.
Return ONLY the JSON array, no other text.
"""
    try:
        return client.chat_json(
            messages=[{"role": "user", "content": prompt}],
            model=client.default_model,
            temperature=0.3,
            max_tokens=2000,
        )
    except Exception as e:
        log.error("llm_mine_failed", error=str(e))
        return []


def run() -> int:
    """Main entry point — weekly pattern mining."""
    policy = load_policy()
    client = OpenRouterClient(default_model=policy.get("openrouter_miner_model"))

    posts = load_post_data(days=7)
    if len(posts) < 5:
        log.info("insufficient_posts", count=len(posts))
        return 0

    # Compute stats per slot
    stats = {
        "period": "last_7_days",
        "total_posts": len(posts),
        "by_slot": {},
    }
    for slot in ("morning", "evening", "weekly_digest"):
        stats["by_slot"][slot] = compute_statistical_baseline(posts, slot=slot)
    stats["overall"] = compute_statistical_baseline(posts)

    # Save stats
    stats_file = REPORTS_DIR / f"stats_baseline_{datetime.now().strftime('%Y%m%d')}.json"
    stats_file.write_text(json.dumps(stats, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("stats_saved", file=str(stats_file))

    # LLM pattern mining
    hypotheses = llm_pattern_mine(stats, posts, client, policy)
    log.info("hypotheses_generated", count=len(hypotheses))

    hyp_file = REPORTS_DIR / f"hypotheses_{datetime.now().strftime('%Y%m%d')}.json"
    hyp_file.write_text(json.dumps(hypotheses, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("hypotheses_saved", file=str(hyp_file))

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
