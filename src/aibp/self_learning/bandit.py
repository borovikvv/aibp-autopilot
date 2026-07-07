"""Thompson sampling bandit for low-risk policy parameters (issue #18).

Rubric weights, publish hour, image on/off are exploration/exploitation
problems, not hypothesis tests. The experiments_log pipeline
(draft → shadow → decide) is too heavyweight for them and is throttled to
one experiment per day. This module runs a lighter loop in parallel:

  - each arm keeps a Beta(alpha, beta) posterior over "this post beat the
    trailing median engagement rate";
  - at candidate selection, one sample per arm converts into a weight
    multiplier (0.5x–1.5x on top of policy rubric_weights);
  - after each engagement collection, posts older than the 48h horizon are
    scored against the median of the 20 preceding posts and posteriors update.

Dimensions are generic strings; "rubric" is wired into select_candidate,
new dimensions (e.g. "hour") only need ensure_arms + record via
update_from_engagement's dimension extractors.
"""
from __future__ import annotations

import statistics
from datetime import UTC, datetime

import structlog

from aibp.self_learning.db import ENGAGEMENT_HORIZON_HOURS, sqlite_conn

log = structlog.get_logger()

RUBRIC_DIMENSION = "rubric"

# How many preceding posts form the baseline, and how many are required
# before outcomes start counting (too few → noise).
BASELINE_WINDOW = 20
MIN_BASELINE_POSTS = 5


# ═══════════════════════════════════════════════════════════════════
# Posterior state
# ═══════════════════════════════════════════════════════════════════

def ensure_arms(dimension: str, arm_ids: list[str]) -> None:
    """Create missing arms with a uniform Beta(1, 1) prior."""
    now = datetime.now(UTC).isoformat()
    with sqlite_conn() as conn:
        for arm_id in arm_ids:
            conn.execute(
                """
                INSERT OR IGNORE INTO bandit_arms (dimension, arm_id, alpha, beta, updated_at)
                VALUES (?, ?, 1, 1, ?)
                """,
                (dimension, arm_id, now),
            )


def get_arms(dimension: str) -> dict[str, tuple[float, float]]:
    """Return {arm_id: (alpha, beta)} for a dimension."""
    with sqlite_conn() as conn:
        rows = conn.execute(
            "SELECT arm_id, alpha, beta FROM bandit_arms WHERE dimension = ?",
            (dimension,),
        ).fetchall()
        return {r["arm_id"]: (r["alpha"], r["beta"]) for r in rows}


def record_outcome(dimension: str, arm_id: str, success: bool,
                   feed_item_id: int | None = None) -> None:
    """Update the arm posterior; optionally log the observation for idempotency."""
    now = datetime.now(UTC).isoformat()
    with sqlite_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO bandit_arms (dimension, arm_id, alpha, beta, updated_at)
            VALUES (?, ?, 1, 1, ?)
            """,
            (dimension, arm_id, now),
        )
        conn.execute(
            """
            UPDATE bandit_arms
            SET alpha = alpha + ?, beta = beta + ?, updated_at = ?
            WHERE dimension = ? AND arm_id = ?
            """,
            (1 if success else 0, 0 if success else 1, now, dimension, arm_id),
        )
        if feed_item_id is not None:
            conn.execute(
                """
                INSERT OR IGNORE INTO bandit_observations
                    (feed_item_id, dimension, arm_id, success, observed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (feed_item_id, dimension, arm_id, 1 if success else 0, now),
            )


# ═══════════════════════════════════════════════════════════════════
# Thompson sampling
# ═══════════════════════════════════════════════════════════════════

def sample_thompson(dimension: str, arm_ids: list[str], rng=None) -> dict[str, float]:
    """One Beta sample per arm. Unknown arms get the Beta(1, 1) prior."""
    import numpy as np

    if rng is None:
        rng = np.random.default_rng()
    posteriors = get_arms(dimension)
    samples = {}
    for arm_id in arm_ids:
        alpha, beta = posteriors.get(arm_id, (1.0, 1.0))
        samples[arm_id] = float(rng.beta(alpha, beta))
    return samples


def sample_rubric_multipliers(rubrics: list[str], rng=None) -> dict[str, float]:
    """Weight multipliers in [0.5, 1.5] for candidate selection.

    theta ~ Beta(alpha, beta) is shifted by 0.5 so the bandit can at most
    halve or 1.5x a rubric — policy rubric_weights stay the primary signal,
    the bandit only nudges within a bounded band.
    """
    ensure_arms(RUBRIC_DIMENSION, rubrics)
    samples = sample_thompson(RUBRIC_DIMENSION, rubrics, rng=rng)
    return {arm: 0.5 + theta for arm, theta in samples.items()}


# ═══════════════════════════════════════════════════════════════════
# Outcome collection (runs after engagement collection)
# ═══════════════════════════════════════════════════════════════════

def _load_scored_posts() -> list[dict]:
    """Main-channel posts old enough to have a 48h horizon snapshot, oldest first.

    rate = composite reward (issue #37, ADR-0010): views + forwards + clicks
    + Δsubs, weighted and normalized by subscribers — the bandit optimizes
    the same target as the decision engine.
    """
    from aibp.self_learning.reward import compute_rewards_for_posts

    with sqlite_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT pf.feed_item_id, pf.posted_at, pf.strategy_rubric
            FROM post_features pf
            WHERE pf.target_channel = 'main'
              AND datetime(pf.posted_at) <= datetime('now', '-{ENGAGEMENT_HORIZON_HOURS} hours')
            ORDER BY pf.posted_at ASC
            """
        ).fetchall()
        posts = [dict(r) for r in rows]

    scored = compute_rewards_for_posts(posts)
    for post in scored:
        post["rate"] = post["reward"]
    return scored


def update_from_engagement() -> int:
    """Score unprocessed posts against the trailing median; update posteriors.

    success = post's 48h engagement rate strictly above the median of up to
    BASELINE_WINDOW preceding posts. Idempotent via bandit_observations PK.
    Returns the number of new observations recorded.
    """
    posts = _load_scored_posts()
    if not posts:
        return 0

    with sqlite_conn() as conn:
        processed = {
            r["feed_item_id"]
            for r in conn.execute(
                "SELECT feed_item_id FROM bandit_observations WHERE dimension = ?",
                (RUBRIC_DIMENSION,),
            )
        }

    recorded = 0
    for i, post in enumerate(posts):
        if post["feed_item_id"] in processed:
            continue
        rubric = post.get("strategy_rubric")
        if not rubric:
            continue
        baseline_rates = [p["rate"] for p in posts[max(0, i - BASELINE_WINDOW):i]]
        if len(baseline_rates) < MIN_BASELINE_POSTS:
            continue
        success = post["rate"] > statistics.median(baseline_rates)
        record_outcome(RUBRIC_DIMENSION, rubric, success, feed_item_id=post["feed_item_id"])
        recorded += 1

    if recorded:
        log.info("bandit_updated", observations=recorded)
    return recorded


def run() -> int:
    """Cron entry point."""
    update_from_engagement()
    # Offer outcomes (issue #38) ride the same cron: same cadence, same
    # posteriors store. Degrades to 0 observations when PG is unreachable.
    from aibp.monetization.offers import update_offer_outcomes
    update_offer_outcomes()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
