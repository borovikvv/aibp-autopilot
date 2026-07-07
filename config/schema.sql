-- ═══════════════════════════════════════════════════════════════════
-- AIBP Autopilot — PostgreSQL Schema
-- ═══════════════════════════════════════════════════════════════════
--
-- Canonical contract between all 6 layers. Every layer reads/writes
-- through psycopg2 with parameterized queries.

-- ─── Main content table ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS feed_items (
    id                       bigserial PRIMARY KEY,
    -- Source identification
    url                      text NOT NULL,
    url_hash                 text UNIQUE,              -- sha256(url) for dedup
    title                    text,
    text                     text,                     -- raw excerpt from source
    source                   text,                     -- feed name or "manual"
    source_domain            text,                     -- extracted from url
    source_lang              text,                     -- en | ru
    source_published_at      timestamptz,              -- original article date
    -- Classification (enrichment layer)
    category                 text,                     -- news | blog | research | company
    rubric                   text,                     -- operator_note | analysis
    topic                    text,                     -- automation | ai_tools | ...
    summary                  jsonb,                    -- all editorial metadata
    rank_score               integer,                  -- 0-100
    importance_hint          text,                     -- A | B | C
    rank_breakdown           jsonb,
    relevance                numeric DEFAULT 0,        -- source relevance score
    -- Pipeline state (transactional outbox)
    -- Lifecycle: new → enriched → approved → published (prod path)
    --            new → enriched → stage_ready → published (stage/shadow path)
    --            new → enriched → rejected (quality gate fail)
    --            new → failed (enrichment error)
    status                   text NOT NULL DEFAULT 'new',  -- new|enriched|stage_ready|approved|published|rejected|failed
    pipeline_env             text NOT NULL DEFAULT 'prod', -- stage|prod
    target_channel           text NOT NULL DEFAULT 'main', -- test|main
    review_status            text,                     -- auto|needs_review|approved|rejected
    -- Post content (generation layer)
    post_draft               text,                     -- final Telegram HTML
    post_draft_history       jsonb,                    -- [{version, text, created_at}]
    thesis                   text,                     -- one-sentence angle
    -- Scheduling
    scheduler_priority       integer DEFAULT 0,        -- lower = higher priority
    scheduled_at             timestamptz,              -- when to publish
    -- Publishing state
    posted_at                timestamptz,
    published_message_id     text,                     -- Telegram message ID
    is_used                  boolean DEFAULT false,
    used_as                  text,                     -- daily_post|weekly_digest|...
    publish_error            text,
    publish_attempts         integer DEFAULT 0,
    -- Image
    need_image               boolean DEFAULT false,
    image_prompt             text,
    image_url                text,
    telegram_file_id         text,
    image_status             text,                     -- pending|generated|failed
    -- Marketing
    campaign_tag             text,
    cta_variant              text,
    offer_type               text,
    -- Self-learning (filled at publish time)
    policy_version           text,                     -- sha256 of policy.yaml
    -- Audit
    created_at               timestamptz DEFAULT now(),
    updated_at               timestamptz DEFAULT now(),
    dupe_key                 text UNIQUE               -- idempotency key
);

CREATE INDEX IF NOT EXISTS idx_feed_items_status ON feed_items (status);
CREATE INDEX IF NOT EXISTS idx_feed_items_pipeline ON feed_items (pipeline_env, target_channel);
CREATE INDEX IF NOT EXISTS idx_feed_items_review ON feed_items (review_status, scheduled_at)
    WHERE posted_at IS NULL AND is_used = false;
CREATE INDEX IF NOT EXISTS idx_feed_items_source_date ON feed_items (source_published_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_items_posted ON feed_items (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_items_summary ON feed_items ((summary ->> 'strategy_rubric'));

-- ─── RSS feed sources registry ────────────────────────────────────
CREATE TABLE IF NOT EXISTS rss_feeds (
    id          bigserial PRIMARY KEY,
    name        text NOT NULL,
    url         text NOT NULL UNIQUE,
    category    text,
    weight      numeric DEFAULT 1.0,
    lang        text DEFAULT 'en',
    enabled     boolean DEFAULT true,
    last_fetched_at timestamptz,
    last_error  text,
    created_at  timestamptz DEFAULT now()
);

-- ─── Click tracking (monetization) ─────────────────────────────────
-- Views don't convert to revenue — clicks do. Every external link in a post
-- goes through the redirect service (/r/{short_id} → 302 target_url) which
-- logs a row in link_clicks.
CREATE TABLE IF NOT EXISTS tracked_links (
    short_id     text PRIMARY KEY,          -- short slug used in /r/{short_id}
    feed_item_id bigint REFERENCES feed_items(id),
    target_url   text NOT NULL,
    created_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tracked_links_item ON tracked_links (feed_item_id);

CREATE TABLE IF NOT EXISTS link_clicks (
    id           bigserial PRIMARY KEY,
    feed_item_id bigint,
    short_id     text NOT NULL REFERENCES tracked_links(short_id),
    clicked_at   timestamptz NOT NULL DEFAULT now(),
    target_url   text,
    user_agent   text
);

CREATE INDEX IF NOT EXISTS idx_link_clicks_item ON link_clicks (feed_item_id, clicked_at);
CREATE INDEX IF NOT EXISTS idx_link_clicks_short ON link_clicks (short_id);

-- ─── Cron job execution log (observability) ───────────────────────
CREATE TABLE IF NOT EXISTS cron_runs (
    id          bigserial PRIMARY KEY,
    job_name    text NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL,                     -- running|success|failed|timeout
    error       text,
    metadata    jsonb
);

CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON cron_runs (job_name, started_at DESC);

-- ─── Trigger: update updated_at automatically ─────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_feed_items_updated ON feed_items;
CREATE TRIGGER trg_feed_items_updated
    BEFORE UPDATE ON feed_items
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ─── View: due posts for publisher ────────────────────────────────
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
ORDER BY scheduler_priority ASC, scheduled_at ASC;

-- ─── View: enrichment candidates ──────────────────────────────────
CREATE OR REPLACE VIEW v_enrichment_candidates AS
SELECT id, url, title, text, source, source_domain, source_published_at
FROM feed_items
WHERE status = 'new'
  AND source_published_at > now() - interval '14 days'
ORDER BY source_published_at DESC;
