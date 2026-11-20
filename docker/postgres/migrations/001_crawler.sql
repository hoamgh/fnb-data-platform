CREATE SCHEMA IF NOT EXISTS crawler;
CREATE TABLE IF NOT EXISTS crawler.store_state (
 store_id text PRIMARY KEY,
 status text NOT NULL,
 last_success_at timestamptz,
 watermark timestamptz,
 run_id text NOT NULL,
 checkpoint_ref text NOT NULL,
 attempt_count bigint NOT NULL DEFAULT 1,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
