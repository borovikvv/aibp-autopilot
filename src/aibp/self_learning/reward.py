"""Composite reward — the optimization target of the self-learning loop (issue #37).

views/subscribers measured retention of the existing audience: the autopilot
learned to please current subscribers but never learned what brings new ones
or what earns money. The reward is now a weighted sum of the signals that map
to the channel goals (growth + monetization), normalized by channel size:

    reward = (w_views · views
              + w_forwards · forwards        # organic growth + TG recommendations
              + w_clicks · clicks            # monetization funnel (redirect service)
              + w_subs_delta · Δsubs         # attributed subscriber change
             ) / subscribers_at

All components are taken at the fixed 48h horizon (issue #14). Δsubs is
attributed to a post from the global subscriber series: the window runs from
posted_at to posted_at + subs_attribution_hours, clipped at the next main
post so two posts never share credit. Clicks live in PostgreSQL (issue #15);
when PG is unreachable, clicks degrade to 0 for ALL posts — both experiment
groups lose the component equally, so comparisons stay valid.

Weights and the attribution window are in policy.yaml safety.reward_weights /
safety.subs_attribution_hours — the safety block is NOT modifiable by the
autopilot. See ADR-0010.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from aibp.self_learning.db import (
    ENGAGEMENT_HORIZON_HOURS,
    get_snapshot_at_horizon,
    sqlite_conn,
)
from aibp.utils.config import load_policy

log = structlog.get_logger()

# Heuristic value estimates (see ADR-0010), to be tuned as real click/CPS
# data accumulates. At ~1000 subs / ~300 views a post, each component
# contributes the same order of magnitude to the reward.
DEFAULT_REWARD_WEIGHTS = {
    "views": 1.0,
    "forwards": 25.0,
    "clicks": 15.0,
    "subs_delta": 50.0,
}
DEFAULT_SUBS_ATTRIBUTION_HOURS = 24


def get_reward_config(policy: dict | None = None) -> tuple[dict[str, float], int]:
    """(weights, attribution_hours) from policy safety block, with defaults."""
    if policy is None:
        policy = load_policy()
    safety = policy.get("safety", {})
    weights = {**DEFAULT_REWARD_WEIGHTS, **(safety.get("reward_weights") or {})}
    hours = safety.get("subs_attribution_hours", DEFAULT_SUBS_ATTRIBUTION_HOURS)
    return weights, hours


# ═══════════════════════════════════════════════════════════════════
# Component: clicks (PostgreSQL, bulk)
# ═══════════════════════════════════════════════════════════════════

def fetch_clicks_at_horizon(
    items: list[tuple[int, str]],
    horizon_hours: int = ENGAGEMENT_HORIZON_HOURS,
) -> dict[int, int]:
    """Clicks within [posted_at, posted_at + horizon] per feed_item_id.

    items: (feed_item_id, posted_at ISO). One bulk query. Degrades to {} with
    a warning when PG is unreachable or click tracking is not deployed —
    the reward then simply lacks the click component for every post.
    """
    if not items:
        return {}
    try:
        from aibp.db.connection import fetch_all as pg_fetch_all
        rows = pg_fetch_all(
            "SELECT feed_item_id, clicked_at FROM link_clicks WHERE feed_item_id = ANY(%s)",
            ([item_id for item_id, _ in items],),
        )
    except Exception as e:
        log.warning("clicks_unavailable_reward_degraded", error=str(e))
        return {}

    horizon = timedelta(hours=horizon_hours)
    posted = {item_id: datetime.fromisoformat(ts) for item_id, ts in items}
    clicks: dict[int, int] = {}
    for r in rows:
        posted_at = posted.get(r["feed_item_id"])
        if posted_at is None:
            continue
        clicked_at = r["clicked_at"]
        if clicked_at.tzinfo is None or posted_at.tzinfo is None:
            clicked_at = clicked_at.replace(tzinfo=None)
            posted_at = posted_at.replace(tzinfo=None)
        if posted_at <= clicked_at <= posted_at + horizon:
            clicks[r["feed_item_id"]] = clicks.get(r["feed_item_id"], 0) + 1
    return clicks


# ═══════════════════════════════════════════════════════════════════
# Component: Δsubs (attributed from the global subscriber series)
# ═══════════════════════════════════════════════════════════════════

def load_subscriber_series() -> list[tuple[datetime, int]]:
    """Global (measured_at, subscribers) series for the main channel, ascending."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT em.measured_at, em.subscribers_at
            FROM engagement_metrics em
            JOIN post_features pf ON pf.feed_item_id = em.feed_item_id
            WHERE pf.target_channel = 'main' AND em.subscribers_at IS NOT NULL
            ORDER BY em.measured_at ASC
            """
        ).fetchall()
    return [(datetime.fromisoformat(r["measured_at"]), r["subscribers_at"]) for r in rows]


def subs_at(series: list[tuple[datetime, int]], t: datetime) -> float | None:
    """Subscriber count at time t: linear interpolation, clamped at the ends."""
    if not series:
        return None
    if t <= series[0][0]:
        return float(series[0][1])
    if t >= series[-1][0]:
        return float(series[-1][1])
    for (t0, s0), (t1, s1) in zip(series, series[1:]):
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds()
            if span == 0:
                return float(s0)
            frac = (t - t0).total_seconds() / span
            return s0 + (s1 - s0) * frac
    return None


def compute_subs_delta(
    posted_at: datetime,
    series: list[tuple[datetime, int]],
    attribution_hours: int,
    next_post_at: datetime | None = None,
) -> float | None:
    """Subscriber change attributed to a post. Negative = churn, kept as signal.

    Window: [posted_at, posted_at + attribution_hours], clipped at next_post_at
    so consecutive posts (2/day) never share credit. Overnight growth after an
    evening post is credited to that post — its window reaches the next
    morning's post. None when the series can't cover the window at all.
    """
    window_end = posted_at + timedelta(hours=attribution_hours)
    if next_post_at is not None and next_post_at < window_end:
        window_end = next_post_at
    if window_end <= posted_at:
        return 0.0
    start = subs_at(series, posted_at)
    end = subs_at(series, window_end)
    if start is None or end is None:
        return None
    return end - start


# ═══════════════════════════════════════════════════════════════════
# Reward
# ═══════════════════════════════════════════════════════════════════

def compute_post_reward(
    views: int,
    forwards: int,
    clicks: int,
    subs_delta: float | None,
    subscribers: int,
    weights: dict[str, float],
) -> dict | None:
    """Pure reward computation. Returns {reward, components} or None (no subs).

    components hold each term's contribution to the final reward, for the
    dashboard decomposition and debugging.
    """
    if not subscribers or subscribers <= 0:
        return None
    components = {
        "views": weights["views"] * (views or 0) / subscribers,
        "forwards": weights["forwards"] * (forwards or 0) / subscribers,
        "clicks": weights["clicks"] * (clicks or 0) / subscribers,
        "subs_delta": weights["subs_delta"] * (subs_delta or 0.0) / subscribers,
    }
    return {"reward": sum(components.values()), "components": components}


def compute_rewards_for_posts(posts: list[dict], policy: dict | None = None) -> list[dict]:
    """Attach 'reward' and 'reward_components' to posts that can be scored.

    posts need feed_item_id and posted_at (ISO). Snapshots are taken at the
    48h horizon; posts without a snapshot or subscriber count are dropped.
    Clicks are fetched in one bulk query; the subscriber series and the
    next-post boundaries are loaded once per call.
    """
    if not posts:
        return []
    weights, attribution_hours = get_reward_config(policy)
    series = load_subscriber_series()
    clicks = fetch_clicks_at_horizon([(p["feed_item_id"], p["posted_at"]) for p in posts])

    # Next-post boundaries come from ALL main posts, not only the scored
    # subset — an interleaved control post still clips a variant's window.
    with sqlite_conn() as conn:
        all_posted = [
            datetime.fromisoformat(r["posted_at"])
            for r in conn.execute(
                "SELECT posted_at FROM post_features WHERE target_channel = 'main' ORDER BY posted_at"
            )
        ]

    scored = []
    for post in posts:
        snapshot = get_snapshot_at_horizon(post["feed_item_id"])
        if snapshot is None:
            continue
        posted_at = datetime.fromisoformat(post["posted_at"])
        next_post_at = next((t for t in all_posted if t > posted_at), None)
        subs_delta = compute_subs_delta(posted_at, series, attribution_hours, next_post_at)
        result = compute_post_reward(
            views=snapshot["views"] or 0,
            forwards=snapshot["forwards"] or 0,
            clicks=clicks.get(post["feed_item_id"], 0),
            subs_delta=subs_delta,
            subscribers=snapshot["subscribers_at"],
            weights=weights,
        )
        if result is None:
            continue
        post = dict(post)
        post["reward"] = result["reward"]
        post["reward_components"] = result["components"]
        post["clicks"] = clicks.get(post["feed_item_id"], 0)
        post["subs_delta"] = subs_delta
        post["views"] = snapshot["views"] or 0
        post["forwards"] = snapshot["forwards"] or 0
        scored.append(post)
    return scored
