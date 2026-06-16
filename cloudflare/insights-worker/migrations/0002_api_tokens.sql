-- Crucible Run Insights — per-contributor API tokens (Phase A foundation).
--
-- Replaces the single shared Worker secret with a table of individually scoped,
-- revocable, attributed tokens.  See cloudflare/insights-worker/OPENING_UP.md
-- (Phase A).  BACKWARD COMPATIBLE: with NO rows here, the Worker still accepts
-- the legacy CRUCIBLE_RUN_INSIGHTS_API_TOKEN secret as an implicit 'admin'
-- token, so existing deployments keep working unchanged.
--
-- Security: only the SHA-256 HASH of each raw token is stored.  The raw token is
-- shown exactly once at issuance and is never recoverable (like a GitHub PAT).

CREATE TABLE IF NOT EXISTS api_tokens (
    token_id        TEXT PRIMARY KEY,            -- public id, e.g. 'tok_<hex>'
    token_hash      TEXT NOT NULL UNIQUE,        -- sha256 hex of the raw token
    contributor_id  TEXT NOT NULL,               -- stable id for attribution
    label           TEXT,                        -- human label (e.g. github handle)
    scope           TEXT NOT NULL                -- 'ingest' | 'read' | 'admin'
                        CHECK (scope IN ('ingest', 'read', 'admin')),
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'revoked')),
    daily_quota     INTEGER,                     -- NULL = unlimited (enforced in Step 1)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_tokens_contributor ON api_tokens(contributor_id);
CREATE INDEX IF NOT EXISTS idx_tokens_status      ON api_tokens(status);
