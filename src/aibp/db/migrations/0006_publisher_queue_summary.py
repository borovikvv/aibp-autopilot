"""Migration 0006: expose summary + scheduler_priority in v_publisher_queue.

The publisher needs summary (poll data, source-button URL — issue #33) at
publish time, and scheduler_priority for its ORDER BY. CREATE OR REPLACE VIEW
adds the columns without dropping the view.
"""
from __future__ import annotations


def up(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE OR REPLACE VIEW v_publisher_queue AS
            SELECT id, title, post_draft, scheduled_at, need_image, image_url,
                   telegram_file_id, pipeline_env, target_channel, used_as,
                   scheduler_priority, summary
            FROM feed_items
            WHERE review_status = 'approved'
              AND scheduled_at <= now()
              AND posted_at IS NULL
              AND is_used = false
              AND post_draft IS NOT NULL
              AND status IN ('approved', 'stage_ready')
            ORDER BY scheduler_priority ASC, scheduled_at ASC
        """)


def down(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE OR REPLACE VIEW v_publisher_queue AS
            SELECT id, title, post_draft, scheduled_at, need_image, image_url,
                   telegram_file_id, pipeline_env, target_channel, used_as
            FROM feed_items
            WHERE review_status = 'approved'
              AND scheduled_at <= now()
              AND posted_at IS NULL
              AND is_used = false
              AND post_draft IS NOT NULL
              AND status IN ('approved', 'stage_ready')
            ORDER BY scheduler_priority ASC, scheduled_at ASC
        """)
