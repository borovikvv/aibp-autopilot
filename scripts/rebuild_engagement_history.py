#!/usr/bin/env python3
"""One-shot backfill of post views from the t.me/s web preview (issue #49).

The engagement collector wrote zero views from 2026-07-11 until the web-preview
fix, because the Bot API cannot report a channel post's view count (ADR-0005).
The posts are still live, so their (now settled) view counts are recoverable
from the public preview.

For every main-channel post already past the 48h reward horizon, this inserts
ONE engagement_metrics snapshot at measured_at = posted_at + 48h carrying the
current preview view count and the subscriber count nearest that time. That is
exactly the row get_snapshot_at_horizon() selects, so composite reward / bandit
history become real without touching the live 4h collector.

Idempotent: a post that already has a non-zero-views snapshot near its horizon
is skipped, so this can be re-run safely.

    /usr/bin/python3 scripts/rebuild_engagement_history.py            # apply
    /usr/bin/python3 scripts/rebuild_engagement_history.py --dry-run  # preview
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from datetime import timedelta

sys.path.insert(0, "src")

from aibp.db.connection import execute, fetch_all  # noqa: E402
from aibp.self_learning.engagement_collector import fetch_views_map  # noqa: E402
from aibp.utils.config import get_settings  # noqa: E402

HORIZON_HOURS = 48


SUBS_WINDOW_DAYS = 2


def subscribers_near(posted_at, horizon_hours: int) -> int | None:
    """Median MAIN-channel subscriber count around posted_at+horizon.

    The old rows carried a correct subscribers_at even while views were 0, so
    this reconstructs the right denominator (reward = views / subscribers).
    Two robustness measures, both needed:

    - Restricted to MAIN-channel rows — the test channel sits at ~3 subs and
      would otherwise leak in and inflate a prod post's reward ~100x.
    - MEDIAN over a ±2d window, not the single nearest reading — even the main
      series has occasional spurious `3` readings; the count moves ~1/day, so
      the window median is a stable estimator that ignores those outliers.
    """
    target = posted_at + timedelta(hours=horizon_hours)
    rows = fetch_all(
        """
        SELECT em.subscribers_at
        FROM engagement_metrics em
        JOIN post_features pf ON pf.feed_item_id = em.feed_item_id
        WHERE em.subscribers_at IS NOT NULL
          AND pf.target_channel = 'main'
          AND em.measured_at BETWEEN %s AND %s
        """,
        (target - timedelta(days=SUBS_WINDOW_DAYS), target + timedelta(days=SUBS_WINDOW_DAYS)),
    )
    vals = [r["subscribers_at"] for r in rows]
    return round(statistics.median(vals)) if vals else None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    args = ap.parse_args()

    s = get_settings()
    username = s.telegram_channel_username_prod
    if not username:
        print("ERROR: TELEGRAM_CHANNEL_USERNAME_PROD is not set in .env", file=sys.stderr)
        return 1

    posts = fetch_all(
        f"""
        SELECT fi.id, fi.published_message_id, fi.posted_at
        FROM feed_items fi
        JOIN post_features pf ON pf.feed_item_id = fi.id
        WHERE pf.target_channel = 'main'
          AND fi.published_message_id IS NOT NULL
          AND fi.posted_at IS NOT NULL
          AND fi.posted_at <= now() - interval '{HORIZON_HOURS} hours'
        ORDER BY fi.posted_at ASC
        """
    )
    if not posts:
        print("No main-channel posts past the 48h horizon — nothing to backfill.")
        return 0

    window_min = min(int(p["published_message_id"]) for p in posts)
    print(f"Fetching views for {len(posts)} posts from t.me/s/{username} "
          f"(back to msg {window_min}) …")
    views_map = await fetch_views_map(username, min_message_id=window_min)
    print(f"Preview returned {len(views_map)} posts.\n")

    written = missing = 0
    for p in posts:
        fid = p["id"]
        msg_id = int(p["published_message_id"])
        if msg_id not in views_map:
            missing += 1
            print(f"  MISSING msg {msg_id} (feed_item {fid}) — not in preview")
            continue

        views = views_map[msg_id]
        subs = subscribers_near(p["posted_at"], HORIZON_HOURS)
        measured_at = p["posted_at"] + timedelta(hours=HORIZON_HOURS)
        print(f"  msg {msg_id} (feed_item {fid}): views={views} subs={subs} @ {measured_at:%Y-%m-%d}")

        if not args.dry_run:
            # Self-correcting: replace this post's horizon snapshot rather than
            # skip-if-exists, so a re-run fixes earlier bad values. The exact
            # measured_at = posted_at + 48h is unique to the backfill; the live
            # 4h collector writes measured_at = now(), so its rows are untouched.
            execute(
                "DELETE FROM engagement_metrics WHERE feed_item_id = %s AND measured_at = %s",
                (fid, measured_at),
            )
            execute(
                """
                INSERT INTO engagement_metrics
                    (feed_item_id, measured_at, views, forwards, replies,
                     reactions_count, reactions_breakdown, subscribers_at)
                VALUES (%s, %s, %s, 0, 0, 0, '[]'::jsonb, %s)
                """,
                (fid, measured_at, views, subs),
            )
        written += 1

    verb = "would write" if args.dry_run else "wrote"
    print(f"\nDone: {verb} {written} horizon snapshots, missing {missing}.")
    if not args.dry_run and written:
        print("Next bandit cron will fold the recovered history into the posteriors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
