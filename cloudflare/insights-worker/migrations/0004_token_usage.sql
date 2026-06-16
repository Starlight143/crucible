-- Crucible Run Insights — per-token daily usage counter (Phase A / Step 1).
--
-- Backs the best-effort daily quota in src/quota.js.  One row per (token, UTC
-- day); the ingest path reads the day's count and rejects with HTTP 429 once it
-- reaches the token's api_tokens.daily_quota.  Admin / legacy tokens and tokens
-- with a NULL daily_quota are unlimited and never touch this table.

CREATE TABLE IF NOT EXISTS token_usage (
    token_id  TEXT NOT NULL,                  -- api_tokens.token_id
    day       TEXT NOT NULL,                  -- 'YYYY-MM-DD' (UTC)
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_id, day)
);
