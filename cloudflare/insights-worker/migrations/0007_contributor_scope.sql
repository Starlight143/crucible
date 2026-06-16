-- Crucible Run Insights — add the combined 'contributor' scope (Phase A →
-- public self-service onboarding via GitHub OAuth).
--
-- A 'contributor' token may BOTH ingest events AND read the (aggregated /
-- metadata-only) corpus, but can never delete or reach the admin routes.  It is
-- the scope handed out by the self-service signup flow; the operator keeps the
-- single 'admin' token, and the older single-purpose 'ingest' / 'read' scopes
-- remain valid for manual grants.
--
-- SQLite cannot ALTER an existing CHECK constraint, so api_tokens is rebuilt
-- with the widened scope set.  Every existing row (notably the operator's admin
-- token) is copied verbatim — token_id, token_hash, quotas and timestamps are
-- preserved, so live tokens keep working without re-issuance.  No foreign keys
-- reference this table, so the drop/rename is safe.  The wrangler d1 migrations
-- runner applies each file exactly once.

CREATE TABLE api_tokens_new (
    token_id        TEXT PRIMARY KEY,
    token_hash      TEXT NOT NULL UNIQUE,
    contributor_id  TEXT NOT NULL,
    label           TEXT,
    scope           TEXT NOT NULL
                        CHECK (scope IN ('ingest', 'read', 'admin', 'contributor')),
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'revoked')),
    daily_quota     INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT
);

INSERT INTO api_tokens_new
    (token_id, token_hash, contributor_id, label, scope, status, daily_quota,
     created_at, last_used_at)
SELECT
    token_id, token_hash, contributor_id, label, scope, status, daily_quota,
    created_at, last_used_at
FROM api_tokens;

DROP TABLE api_tokens;
ALTER TABLE api_tokens_new RENAME TO api_tokens;

CREATE INDEX IF NOT EXISTS idx_tokens_contributor ON api_tokens(contributor_id);
CREATE INDEX IF NOT EXISTS idx_tokens_status      ON api_tokens(status);
