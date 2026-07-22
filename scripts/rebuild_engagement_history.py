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
import sys
from datetime import timedelta

sys.path.insert(0, "src")

from aibp.db.connection import execute, fetch_all, fetch_one  # noqa: E402
from aibp.self_learning.engagement_collector import fetch_views_map  # noqa: E402
from aibp.utils.config import get_settings  # noqa: E402

HORIZON_HOURS = 48


def subscribers_near(feed_item_id: int, posted_at, horizon_hours: int) -> int | None:
    """Subscriber count from the existing snapshot nearest posted_at+horizon.

    The old rows carried a correct subscribers_at even while views were 0, so
    this reconstructs the right denominator for each post's reward. Falls back
    to the channel-wide nearest reading if the post has no rows yet.
    """
    row = fetch_one(
        """
        SELECT em.subscribers_at
        FROM engagement_metrics em
        WHERE em.subscribers_at IS NOT NULL
        ORDER BY ABS(EXTRACT(EPOCH FROM (em.measured_at - %s)))
        LIMIT 1
        """,
        (posted_at + timedelta(hours=horizon_hours),),
    )
    return row["subscribers_at"] if row else None


def has_real_snapshot(feed_item_id: int) -> bool:
    """True if a non-zero-views snapshot already exists for this post."""
    row = fetch_one(
        "SELECT 1 FROM engagement_metrics WHERE feed_item_id = %s AND views > 0 LIMIT 1",
        (feed_item_id,),
    )
    return row is not None


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

    inserted = skipped_existing = missing = 0
    for p in posts:
        fid = p["id"]
        msg_id = int(p["published_message_id"])
        if has_real_snapshot(fid):
            skipped_existing += 1
            continue
        if msg_id not in views_map:
            missing += 1
            print(f"  MISSING msg {msg_id} (feed_item {fid}) — not in preview")
            continue

        views = views_map[msg_id]
        subs = subscribers_near(fid, p["posted_at"], HORIZON_HOURS)
        measured_at = p["posted_at"] + timedelta(hours=HORIZON_HOURS)
        print(f"  msg {msg_id} (feed_item {fid}): views={views} subs={subs} @ {measured_at:%Y-%m-%d}")

        if not args.dry_run:
            execute(
                """
                INSERT INTO engagement_metrics
                    (feed_item_id, measured_at, views, forwards, replies,
                     reactions_count, reactions_breakdown, subscribers_at)
                VALUES (%s, %s, %s, 0, 0, 0, '[]'::jsonb, %s)
                """,
                (fid, measured_at, views, subs),
            )
        inserted += 1

    verb = "would insert" if args.dry_run else "inserted"
    print(f"\nDone: {verb} {inserted}, skipped {skipped_existing} (already real), "
          f"missing {missing}.")
    if not args.dry_run and inserted:
        print("Next bandit cron will fold the recovered history into the posteriors.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
