-- Crucible Run Insights — event attribution + staging (Phase A foundation).
--
-- Adds two ADDITIVE columns to insight_events so every ingested event records
-- WHO contributed it (server-stamped, never trusted from the client) and whether
-- it is quarantined.  See OPENING_UP.md (Phases A/B).
--
--   contributor_id : the authenticated api_tokens.contributor_id, or NULL for
--                    legacy/owner writes via the single shared secret.
--   trust_state    : 'approved' (owner/admin data — immediately part of the
--                    corpus) or 'staged' (third-party contribution held in
--                    quarantine until promoted by reputation/review).
--
-- Existing rows take the 'approved' default (they are the owner's own historical
-- data).  Readers / distillation that must see only vetted data filter on
-- trust_state = 'approved'.
--
-- Note: ALTER TABLE ADD COLUMN is not guarded by IF NOT EXISTS in SQLite; the
-- wrangler d1 migrations runner applies each file exactly once, so this is safe.

ALTER TABLE insight_events ADD COLUMN contributor_id TEXT;
ALTER TABLE insight_events ADD COLUMN trust_state TEXT NOT NULL DEFAULT 'approved';

CREATE INDEX IF NOT EXISTS idx_trust       ON insight_events(trust_state);
CREATE INDEX IF NOT EXISTS idx_contributor ON insight_events(contributor_id);
