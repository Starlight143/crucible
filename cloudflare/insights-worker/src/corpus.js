// Contributor-facing READ surface — aggregates + metadata ONLY (Phase A).
//
// The operator-chosen read model: a 'contributor' (or 'read') token can see the
// SHAPE of the shared corpus — counts and per-row INDEX columns — but never the
// raw payloads (a run's ideas / debate text live in payload_inline / R2 and are
// returned only by the admin-scoped raw routes).  Identity is withheld too:
// contributor_id is never exposed here.  Every query is hard-filtered to
// trust_state='approved', so quarantined or rejected data is invisible.

// Index columns a contributor may see.  Deliberately EXCLUDES payload_inline,
// payload_r2_key, env_fingerprint and contributor_id.
const META_COLUMNS =
  'content_id, stream, ts, run_id, project_name, mode, kind, stage, ' +
  'outcome_status, outcome_score';

async function groupCount(env, column) {
  const res = await env.DB.prepare(
    `SELECT ${column} AS k, COUNT(*) AS n FROM insight_events
       WHERE trust_state = 'approved'
       GROUP BY ${column}
       ORDER BY n DESC
       LIMIT 100`
  ).all();
  return (res && res.results) || [];
}

/**
 * Aggregate corpus shape (approved only): total + first/last timestamps and
 * breakdowns by stream / mode / outcome_status, plus the busiest projects.  No
 * payloads, no identities.
 * @param {{ DB: any }} env
 */
export async function corpusStats(env) {
  const totals = await env.DB.prepare(
    "SELECT COUNT(*) AS total, MIN(ts) AS first_ts, MAX(ts) AS last_ts " +
      "FROM insight_events WHERE trust_state = 'approved'"
  ).first();
  const [byStream, byMode, byOutcome, byProject] = await Promise.all([
    groupCount(env, 'stream'),
    groupCount(env, 'mode'),
    groupCount(env, 'outcome_status'),
    groupCount(env, 'project_name'),
  ]);
  return {
    ok: true,
    total: (totals && totals.total) || 0,
    first_ts: (totals && totals.first_ts) || null,
    last_ts: (totals && totals.last_ts) || null,
    by_stream: byStream,
    by_mode: byMode,
    by_outcome: byOutcome,
    top_projects: byProject.slice(0, 50),
  };
}

/**
 * Paginated metadata rows (approved only), index columns only — never payloads
 * or contributor identity.  Stable content_id ASC cursor.
 * @param {{ DB: any }} env
 * @param {{ stream?: string, mode?: string, runId?: string, cursor?: string, limit?: string|number }} [opts]
 */
export async function corpusList(env, opts = {}) {
  let limit = parseInt(opts.limit, 10);
  if (!Number.isFinite(limit) || limit <= 0) limit = 100;
  limit = Math.min(limit, 1000);

  const where = ["trust_state = 'approved'"];
  const binds = [];
  if (opts.stream) {
    where.push('stream = ?');
    binds.push(opts.stream);
  }
  if (opts.mode) {
    where.push('mode = ?');
    binds.push(opts.mode);
  }
  if (opts.runId) {
    where.push('run_id = ?');
    binds.push(opts.runId);
  }
  if (opts.cursor) {
    where.push('content_id > ?');
    binds.push(opts.cursor);
  }
  const sql =
    `SELECT ${META_COLUMNS} FROM insight_events WHERE ` +
    where.join(' AND ') +
    ' ORDER BY content_id ASC LIMIT ?';
  binds.push(limit);

  const res = await env.DB.prepare(sql)
    .bind(...binds)
    .all();
  const rows = (res && res.results) || [];
  const next = rows.length === limit ? rows[rows.length - 1].content_id : null;
  return { ok: true, events: rows, next_cursor: next };
}
