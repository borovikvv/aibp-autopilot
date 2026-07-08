"""Migration 0009: self-learning tables moved from SQLite to PostgreSQL (issue #43).

Eliminates data/self_learning.db — all self-learning state (experiments,
policies, engagement snapshots, bandit posteriors, autopilot events) now lives
alongside the content store in PostgreSQL, so cross-store joins (engagement ×
post_features × link_clicks) become single-DB queries.

The schema is a direct port of the SQLite SCHEMA in self_learning/db.py, with
SQLite dialect constructs replaced: AUTOINCREMENT→bigserial, TEXT-JSON→jsonb,
text timestamps→timestamptz. The _COLUMN_MIGRATIONS backfill (assignment_mode,
cta_variant) is folded into the base CREATE — the migration produces the final
schema.
"""
from __future__ import annotations


def up(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS post_features (
                feed_item_id      bigint PRIMARY KEY REFERENCES feed_items(id),
                posted_at         timestamptz NOT NULL,
                slot              text NOT NULL,
                pipeline_env      text NOT NULL,
                target_channel    text NOT NULL,
                strategy_rubric   text,
                topic_cluster     text,
                source_domain     text,
                source_url        text,
                char_count        integer,
                paragraph_count   integer,
                bold_count        integer,
                emoji_count       integer,
                has_image         integer,
                visual_kind       text,
                scheduled_hour    integer,
                cta_variant       text,
                policy_version    text NOT NULL,
                policy_blob       jsonb NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS engagement_metrics (
                id                bigserial PRIMARY KEY,
                feed_item_id      bigint NOT NULL REFERENCES post_features(feed_item_id),
                measured_at       timestamptz NOT NULL,
                views             integer,
                forwards          integer,
                replies           integer,
                reactions_count   integer,
                reactions_breakdown jsonb,
                subscribers_at    integer
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_engagement_post
                ON engagement_metrics (feed_item_id, measured_at)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS policies (
                version           text PRIMARY KEY,
                created_at        timestamptz NOT NULL,
                created_by        text NOT NULL,
                parent_version    text,
                yaml_content      text NOT NULL,
                json_blob         jsonb NOT NULL,
                applies_to        text NOT NULL,
                status            text NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS experiments_log (
                id                bigserial PRIMARY KEY,
                started_at        timestamptz NOT NULL,
                finished_at       timestamptz,
                experiment_type   text NOT NULL,
                hypothesis        text,
                policy_before     text NOT NULL,
                policy_after      text NOT NULL,
                applies_to        text NOT NULL,
                status            text NOT NULL,
                assignment_mode   text NOT NULL DEFAULT 'interleave',
                control_posts     jsonb,
                shadow_posts      jsonb,
                control_engagement jsonb,
                shadow_engagement jsonb,
                effect_size       double precision,
                p_value           double precision,
                decision_reason   text,
                rolled_back_at    timestamptz,
                rollback_reason   text
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS prompt_changes (
                id                bigserial PRIMARY KEY,
                applied_at        timestamptz NOT NULL,
                prompt_file       text NOT NULL,
                diff_before       text NOT NULL,
                diff_after        text NOT NULL,
                experiment_id     bigint REFERENCES experiments_log(id),
                reverted_at       timestamptz
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bandit_arms (
                dimension   text NOT NULL,
                arm_id      text NOT NULL,
                alpha       double precision NOT NULL DEFAULT 1,
                beta        double precision NOT NULL DEFAULT 1,
                updated_at  timestamptz,
                PRIMARY KEY (dimension, arm_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bandit_observations (
                feed_item_id bigint NOT NULL,
                dimension    text NOT NULL,
                arm_id       text NOT NULL,
                success      integer NOT NULL,
                observed_at  timestamptz NOT NULL,
                PRIMARY KEY (feed_item_id, dimension)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS autopilot_events (
                id                bigserial PRIMARY KEY,
                event_at          timestamptz NOT NULL,
                event_type        text NOT NULL,
                experiment_id     bigint REFERENCES experiments_log(id),
                details           jsonb
            )
        """)


def down(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS autopilot_events")
        cur.execute("DROP TABLE IF EXISTS bandit_observations")
        cur.execute("DROP TABLE IF EXISTS bandit_arms")
        cur.execute("DROP TABLE IF EXISTS prompt_changes")
        cur.execute("DROP TABLE IF EXISTS experiments_log")
        cur.execute("DROP TABLE IF EXISTS policies")
        cur.execute("DROP TABLE IF EXISTS engagement_metrics")
        cur.execute("DROP TABLE IF EXISTS post_features")
