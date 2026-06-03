-- Crucible Run Insights — D1 schema (v1.2.0 cloud backend, Phase 0)
--
-- This schema is FROZEN in crucible/features/run_insights/backends.py
-- (module docstring).  It is reproduced here verbatim (plus IF NOT EXISTS
-- guards so migrations are idempotent).  Do NOT diverge the column set from
-- the docstring without updating both — the Python DualWriteBackend (Phase 1)
-- maps event fields onto these columns and the JS ingest path binds them
-- positionally.
--
-- Source-of-truth note: `payload_inline` (when set) and the R2 object (when
-- `payload_r2_key` is set) each hold the COMPLETE event JSON, not just the
-- `payload` field.  The other columns are denormalized indexes for querying;
-- fields without a dedicated column (signals, reusability, payload) are
-- preserved losslessly inside the full-event JSON.  Read paths reconstruct
-- the event from payload_inline or R2.

CREATE TABLE IF NOT EXISTS insight_events (
    content_id      TEXT PRIMARY KEY,            -- "sha256:<hex>"
    stream          TEXT NOT NULL,               -- 'output'|'error'|'debate'|'params'
    ts              TEXT NOT NULL,               -- ISO-8601 UTC, "...Z"
    run_id          TEXT NOT NULL,
    project_name    TEXT NOT NULL,
    mode            TEXT NOT NULL,               -- 'Quant'|'SaaS'|'Agent'|'Scientist'
    kind            TEXT NOT NULL,               -- EventKind value
    stage           TEXT,
    schema_version  INTEGER NOT NULL,
    payload_inline  TEXT,                        -- full event JSON if <= inline limit
    payload_r2_key  TEXT,                        -- 'insights/<run_id>/<content_id>.json' otherwise
    env_fingerprint TEXT,                        -- JSON
    outcome_status  TEXT,
    outcome_score   REAL,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_run        ON insight_events(run_id);
CREATE INDEX IF NOT EXISTS idx_project_ts ON insight_events(project_name, ts);
CREATE INDEX IF NOT EXISTS idx_stream_ts  ON insight_events(stream, ts);
CREATE INDEX IF NOT EXISTS idx_outcome    ON insight_events(outcome_status, ts);
