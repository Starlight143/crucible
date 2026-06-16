-- Crucible Run Insights — distilled artifacts (Phase A / Step 4).
--
-- The ONLY thing the `read` scope can fetch: operator/distiller-published,
-- curated summaries derived from APPROVED data — never raw events.  In the
-- interim the operator publishes artifacts via the admin endpoint; the automated
-- LLM distiller (distiller.py, v1.2.0 retrieval) writes here later with the same
-- contract.  `payload` is arbitrary JSON (stored as text).

CREATE TABLE IF NOT EXISTS distilled_artifacts (
    id       TEXT PRIMARY KEY,                -- 'dst_<hex>'
    kind     TEXT NOT NULL,                   -- caller-defined category, e.g. 'avoidance' | 'skills'
    ts       TEXT NOT NULL,                   -- ISO-8601 UTC publish time
    payload  TEXT NOT NULL                    -- JSON
);

CREATE INDEX IF NOT EXISTS idx_distilled_kind_ts ON distilled_artifacts(kind, ts);
