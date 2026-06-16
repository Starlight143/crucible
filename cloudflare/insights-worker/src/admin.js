// Admin operations for the insights Worker (Phase A — token mgmt + curation +
// distilled publishing).  Reached only via the admin-scoped /v1/admin/* routes
// in index.js (which enforce the 'admin' scope first).  In production the admin
// routes are additionally locked behind Cloudflare Access (see OPENING_UP.md).
//
// Security: a fresh raw token is returned exactly ONCE from issueToken(); only
// its SHA-256 hash is persisted, so a leaked database never reveals usable
// tokens.

import { sha256Hex } from './auth.js';

const VALID_SCOPES = new Set(['ingest', 'read', 'admin', 'contributor']);

/**
 * Lowercase hex of `nBytes` cryptographically-random bytes.
 * @param {number} nBytes
 * @returns {string}
 */
function randomHex(nBytes) {
  const a = new Uint8Array(nBytes);
  crypto.getRandomValues(a);
  return Array.from(a)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

function changes(res) {
  return (res && res.meta && res.meta.changes) || (res && res.changes) || 0;
}

function safeParse(s) {
  try {
    return JSON.parse(s);
  } catch {
    return null;
  }
}

// ───────────────────────── token management ─────────────────────────

/**
 * Mint a new token for a contributor.  Returns the RAW token exactly once; it is
 * never stored and cannot be recovered afterwards.
 * @param {{ DB: any }} env
 * @param {{ contributorId?: string, label?: string, scope?: string, dailyQuota?: number }} opts
 */
export async function issueToken(env, opts) {
  const contributorId = opts && opts.contributorId;
  const scope = opts && opts.scope;
  if (typeof contributorId !== 'string' || !contributorId.trim()) {
    return { ok: false, reason: 'missing_contributor_id' };
  }
  if (!VALID_SCOPES.has(scope)) {
    return { ok: false, reason: 'bad_scope' };
  }
  const label =
    opts && typeof opts.label === 'string' && opts.label.trim()
      ? opts.label.trim()
      : null;
  const dailyQuota =
    opts && Number.isInteger(opts.dailyQuota) && opts.dailyQuota > 0
      ? opts.dailyQuota
      : null;

  const rawToken = 'crk_' + randomHex(32); // 256-bit secret
  const tokenId = 'tok_' + randomHex(8);
  const tokenHash = await sha256Hex(rawToken);

  await env.DB.prepare(
    `INSERT INTO api_tokens
       (token_id, token_hash, contributor_id, label, scope, status, daily_quota)
     VALUES (?, ?, ?, ?, ?, 'active', ?)`
  )
    .bind(tokenId, tokenHash, contributorId.trim(), label, scope, dailyQuota)
    .run();

  // The raw token is returned ONCE here and never again.
  return {
    ok: true,
    token_id: tokenId,
    token: rawToken,
    contributor_id: contributorId.trim(),
    scope,
    daily_quota: dailyQuota,
  };
}

/**
 * Revoke a token by its public token_id.  ok=false when no matching row changed.
 * @param {{ DB: any }} env
 * @param {string} tokenId
 */
export async function revokeToken(env, tokenId) {
  if (typeof tokenId !== 'string' || !tokenId) {
    return { ok: false, reason: 'missing_token_id' };
  }
  const res = await env.DB.prepare(
    "UPDATE api_tokens SET status = 'revoked' WHERE token_id = ?"
  )
    .bind(tokenId)
    .run();
  return { ok: changes(res) > 0, token_id: tokenId, revoked: changes(res) > 0 };
}

/**
 * List token metadata (never the raw token or its hash).
 * @param {{ DB: any }} env
 */
export async function listTokens(env) {
  const res = await env.DB.prepare(
    `SELECT token_id, contributor_id, label, scope, status, daily_quota,
            created_at, last_used_at
       FROM api_tokens
      ORDER BY created_at DESC
      LIMIT 1000`
  ).all();
  return { ok: true, tokens: (res && res.results) || [] };
}

// ───────────────────────── curation (Step 2) ─────────────────────────

/**
 * List staged (quarantined) events for review.  Lightweight metadata only — the
 * full payload is reachable via the admin GET /v1/insights/events/:content_id.
 * @param {{ DB: any }} env
 * @param {{ limit?: number|string, cursor?: string }} [opts]
 */
export async function listStaged(env, opts = {}) {
  let limit = parseInt(opts.limit, 10);
  if (!Number.isFinite(limit) || limit <= 0) limit = 100;
  limit = Math.min(limit, 1000);
  const binds = [];
  let sql =
    `SELECT content_id, stream, ts, run_id, project_name, mode, kind,
            contributor_id, trust_state
       FROM insight_events
      WHERE trust_state = 'staged'`;
  if (opts.cursor) {
    sql += ' AND content_id > ?';
    binds.push(opts.cursor);
  }
  sql += ' ORDER BY content_id ASC LIMIT ?';
  binds.push(limit);
  const res = await env.DB.prepare(sql)
    .bind(...binds)
    .all();
  const rows = (res && res.results) || [];
  const next = rows.length === limit ? rows[rows.length - 1].content_id : null;
  return { ok: true, staged: rows, next_cursor: next };
}

/**
 * Promote staged events to 'approved' — by explicit content_ids, or all of a
 * contributor's staged events.  Approved data becomes part of the corpus the
 * distiller reads.
 * @param {{ DB: any }} env
 * @param {{ contentIds?: string[], contributorId?: string }} sel
 */
export async function promoteEvents(env, sel = {}) {
  if (Array.isArray(sel.contentIds) && sel.contentIds.length) {
    const ph = sel.contentIds.map(() => '?').join(',');
    const res = await env.DB.prepare(
      `UPDATE insight_events SET trust_state = 'approved'
         WHERE trust_state = 'staged' AND content_id IN (${ph})`
    )
      .bind(...sel.contentIds)
      .run();
    return { ok: true, promoted: changes(res) };
  }
  if (typeof sel.contributorId === 'string' && sel.contributorId) {
    const res = await env.DB.prepare(
      `UPDATE insight_events SET trust_state = 'approved'
         WHERE trust_state = 'staged' AND contributor_id = ?`
    )
      .bind(sel.contributorId)
      .run();
    return { ok: true, promoted: changes(res) };
  }
  return { ok: false, reason: 'nothing_specified' };
}

/**
 * Reject (delete) staged events by content_id.  Only 'staged' rows are touched,
 * so an already-approved event can never be deleted through this path.
 * @param {{ DB: any }} env
 * @param {{ contentIds?: string[] }} sel
 */
export async function rejectEvents(env, sel = {}) {
  if (!Array.isArray(sel.contentIds) || !sel.contentIds.length) {
    return { ok: false, reason: 'no_content_ids' };
  }
  const ph = sel.contentIds.map(() => '?').join(',');
  const res = await env.DB.prepare(
    `DELETE FROM insight_events
       WHERE trust_state = 'staged' AND content_id IN (${ph})`
  )
    .bind(...sel.contentIds)
    .run();
  return { ok: true, rejected: changes(res) };
}

/**
 * Hard-delete events by content_id(s), run_id, or contributor_id — regardless of
 * trust_state.  This is the ADMIN-only delete, distinct from rejectEvents (which
 * only removes 'staged' rows): the operator can remove ANYTHING, including
 * already-approved corpus rows.  When R2 is bound, the spilled payload objects
 * for the matched rows are deleted first (best-effort) so no orphan blobs leak.
 * Exactly one selector is honoured (content_ids, then run_id, then contributor).
 * @param {{ DB: any, BLOBS?: any }} env
 * @param {{ contentIds?: string[], runId?: string, contributorId?: string }} sel
 */
export async function deleteEvents(env, sel = {}) {
  let where = null;
  let binds = [];
  if (Array.isArray(sel.contentIds) && sel.contentIds.length) {
    where = `content_id IN (${sel.contentIds.map(() => '?').join(',')})`;
    binds = sel.contentIds.slice();
  } else if (typeof sel.runId === 'string' && sel.runId) {
    where = 'run_id = ?';
    binds = [sel.runId];
  } else if (typeof sel.contributorId === 'string' && sel.contributorId) {
    where = 'contributor_id = ?';
    binds = [sel.contributorId];
  } else {
    return { ok: false, reason: 'nothing_specified' };
  }

  // Best-effort R2 cleanup for the matched rows (only relevant when BLOBS is
  // bound; the default D1-only deployment stores everything inline in D1).
  if (env.BLOBS) {
    try {
      const keysRes = await env.DB.prepare(
        `SELECT payload_r2_key FROM insight_events
           WHERE ${where} AND payload_r2_key IS NOT NULL`
      )
        .bind(...binds)
        .all();
      for (const row of (keysRes && keysRes.results) || []) {
        if (row && row.payload_r2_key) {
          try {
            await env.BLOBS.delete(row.payload_r2_key);
          } catch {
            /* ignore individual blob delete failures */
          }
        }
      }
    } catch {
      /* ignore — still delete the D1 rows below */
    }
  }

  const res = await env.DB.prepare(`DELETE FROM insight_events WHERE ${where}`)
    .bind(...binds)
    .run();
  return { ok: true, deleted: changes(res) };
}

/**
 * Corpus-wide counts for the admin console: total + first/last ts and breakdowns
 * by stream, trust_state, mode, and top contributors.  Unlike the contributor-
 * facing corpus.js stats this spans ALL trust_states and exposes contributor_id.
 * @param {{ DB: any }} env
 */
export async function eventStats(env) {
  const totals = await env.DB.prepare(
    'SELECT COUNT(*) AS total, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM insight_events'
  ).first();
  const group = async (col) => {
    const res = await env.DB.prepare(
      `SELECT ${col} AS k, COUNT(*) AS n FROM insight_events
         GROUP BY ${col} ORDER BY n DESC LIMIT 200`
    ).all();
    return (res && res.results) || [];
  };
  const [byStream, byTrust, byMode, byContributor] = await Promise.all([
    group('stream'),
    group('trust_state'),
    group('mode'),
    group('contributor_id'),
  ]);
  return {
    ok: true,
    total: (totals && totals.total) || 0,
    first_ts: (totals && totals.first_ts) || null,
    last_ts: (totals && totals.last_ts) || null,
    by_stream: byStream,
    by_trust_state: byTrust,
    by_mode: byMode,
    by_contributor: byContributor,
  };
}

/**
 * List contributors with reputation/status.
 * @param {{ DB: any }} env
 */
export async function listContributors(env) {
  const res = await env.DB.prepare(
    `SELECT contributor_id, reputation, status, created_at
       FROM contributors
      ORDER BY created_at DESC
      LIMIT 1000`
  ).all();
  return { ok: true, contributors: (res && res.results) || [] };
}

/**
 * Upsert a contributor's reputation and/or status (operator-driven for now).
 * @param {{ DB: any }} env
 * @param {string} contributorId
 * @param {{ reputation?: number, status?: string }} patch
 */
export async function setContributor(env, contributorId, patch = {}) {
  if (typeof contributorId !== 'string' || !contributorId.trim()) {
    return { ok: false, reason: 'missing_contributor_id' };
  }
  const rep =
    typeof patch.reputation === 'number' && Number.isFinite(patch.reputation)
      ? patch.reputation
      : null;
  const st = patch.status === 'active' || patch.status === 'banned' ? patch.status : null;
  if (rep === null && st === null) {
    return { ok: false, reason: 'nothing_to_update' };
  }
  await env.DB.prepare(
    `INSERT INTO contributors (contributor_id, reputation, status)
       VALUES (?, COALESCE(?, 0.0), COALESCE(?, 'active'))
     ON CONFLICT(contributor_id) DO UPDATE SET
       reputation = COALESCE(?, contributors.reputation),
       status     = COALESCE(?, contributors.status)`
  )
    .bind(contributorId.trim(), rep, st, rep, st)
    .run();
  return { ok: true, contributor_id: contributorId.trim(), reputation: rep, status: st };
}

// ───────────────────────── distilled artifacts (Step 4) ─────────────────────────

/**
 * Publish a distilled artifact (operator/distiller-produced, derived from
 * APPROVED data).  This is the only thing the read scope can fetch.
 * @param {{ DB: any }} env
 * @param {{ kind?: string, payload?: unknown }} opts
 */
export async function publishDistilled(env, opts = {}) {
  if (typeof opts.kind !== 'string' || !opts.kind.trim()) {
    return { ok: false, reason: 'missing_kind' };
  }
  if (opts.payload === undefined) {
    return { ok: false, reason: 'missing_payload' };
  }
  const id = 'dst_' + randomHex(8);
  const ts = new Date().toISOString();
  await env.DB.prepare(
    'INSERT INTO distilled_artifacts (id, kind, ts, payload) VALUES (?, ?, ?, ?)'
  )
    .bind(id, opts.kind.trim(), ts, JSON.stringify(opts.payload))
    .run();
  return { ok: true, id, kind: opts.kind.trim(), ts };
}

/**
 * Fetch the most recent distilled artifacts (optionally filtered by kind).  This
 * is what `read`-scope tokens consume; it NEVER exposes raw events.
 * @param {{ DB: any }} env
 * @param {{ kind?: string }} [opts]
 */
export async function getDistilled(env, opts = {}) {
  const binds = [];
  let sql = 'SELECT id, kind, ts, payload FROM distilled_artifacts';
  if (opts.kind) {
    sql += ' WHERE kind = ?';
    binds.push(opts.kind);
  }
  sql += ' ORDER BY ts DESC LIMIT 50';
  const stmt = binds.length
    ? env.DB.prepare(sql).bind(...binds)
    : env.DB.prepare(sql);
  const res = await stmt.all();
  const items = ((res && res.results) || []).map((r) => ({
    id: r.id,
    kind: r.kind,
    ts: r.ts,
    payload: safeParse(r.payload),
  }));
  return { ok: true, distilled: items };
}
