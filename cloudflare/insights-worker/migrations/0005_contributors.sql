-- Crucible Run Insights — contributor reputation/status (Phase A / Step 2).
--
-- One row per contributor_id (the attribution id stamped on insight_events).
-- `reputation` is set/adjusted by the operator for now; the AUTOMATIC scoring
-- algorithm (evomap CONFIDENCE_HALFLIFE_DAYS / BAN_THRESHOLD=0.18 /
-- SIMILARITY_THRESHOLD) lands with the v1.2.0 retrieval work (see OPENING_UP.md
-- "Deferred BY DESIGN").  `status='banned'` lets the operator cut a bad actor
-- off (enforced at issuance/curation time).

CREATE TABLE IF NOT EXISTS contributors (
    contributor_id  TEXT PRIMARY KEY,
    reputation      REAL NOT NULL DEFAULT 0.0,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'banned')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
