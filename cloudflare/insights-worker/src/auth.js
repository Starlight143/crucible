// Authentication for the insights Worker.
//
// Two credential models, checked in order, both fail-closed:
//   1. Per-contributor tokens (Phase A) — the raw bearer is SHA-256 hashed and
//      looked up in the `api_tokens` D1 table; an active row yields its
//      {contributorId, scope, dailyQuota}.  Revoked/unknown → denied.
//   2. Legacy single shared secret — the bearer is compared (constant-time)
//      against the CRUCIBLE_RUN_INSIGHTS_API_TOKEN Worker secret and, if it
//      matches, granted implicit 'admin' scope.  This keeps every pre-Phase-A
//      deployment (and the owner's existing token) working unchanged.
//
// Only token HASHES are ever stored server-side; the raw token is shown once at
// issuance (see admin.js) and is never recoverable.

/**
 * Constant-time string comparison.  Length may leak (differing lengths → fast
 * false) but content never short-circuits, so the secret cannot be timed out
 * byte-by-byte.
 * @param {string} a
 * @param {string} b
 * @returns {boolean}
 */
export function timingSafeEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) {
    r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return r === 0;
}

/**
 * Lowercase hex SHA-256 of a UTF-8 string.  Used to hash bearer tokens for
 * table lookup so the raw token is never stored.
 * @param {string} s
 * @returns {Promise<string>}
 */
export async function sha256Hex(s) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

/**
 * @typedef {Object} AuthContext
 * @property {string} contributorId  attribution id ('legacy-admin' for the shared secret)
 * @property {'ingest'|'read'|'admin'} scope
 * @property {string|null} tokenId    api_tokens.token_id, or null for the legacy secret
 * @property {number|null} dailyQuota api_tokens.daily_quota, or null (unlimited / legacy)
 */

/**
 * Resolve the request's bearer token to an {@link AuthContext}, or null when it
 * is missing / malformed / unknown / revoked.  Fail-closed throughout.
 * @param {Request} req
 * @param {{ DB?: any, CRUCIBLE_RUN_INSIGHTS_API_TOKEN?: string }} env
 * @returns {Promise<AuthContext|null>}
 */
export async function authenticate(req, env) {
  const header = req.headers.get('Authorization') || '';
  if (!header.startsWith('Bearer ')) return null;
  const raw = header.slice(7);
  if (!raw) return null;

  // 1) Per-contributor token table (when present and migrated).
  if (env && env.DB) {
    let row = null;
    try {
      const hash = await sha256Hex(raw);
      row = await env.DB.prepare(
        'SELECT token_id, contributor_id, scope, status, daily_quota FROM api_tokens WHERE token_hash = ?'
      )
        .bind(hash)
        .first();
    } catch {
      // Table not migrated yet (or transient DB error) → fall through to legacy.
      row = null;
    }
    if (row) {
      if (row.status !== 'active') return null; // revoked → deny
      // Best-effort usage timestamp; never block or fail auth on this.
      try {
        await env.DB.prepare(
          "UPDATE api_tokens SET last_used_at = datetime('now') WHERE token_id = ?"
        )
          .bind(row.token_id)
          .run();
      } catch {
        /* ignore */
      }
      return {
        contributorId: row.contributor_id,
        scope: row.scope,
        tokenId: row.token_id,
        dailyQuota: row.daily_quota ?? null,
      };
    }
  }

  // 2) Legacy single shared secret → implicit admin (backward compatibility).
  const expected = env && env.CRUCIBLE_RUN_INSIGHTS_API_TOKEN;
  if (expected && timingSafeEqual(raw, expected)) {
    return {
      contributorId: 'legacy-admin',
      scope: 'admin',
      tokenId: null,
      dailyQuota: null,
    };
  }
  return null;
}

/**
 * Legacy boolean gate (single shared secret only).  Retained for backward
 * compatibility; the router now uses {@link authenticate}.  Returns false when
 * no secret is configured.
 * @param {Request} req
 * @param {{ CRUCIBLE_RUN_INSIGHTS_API_TOKEN?: string }} env
 * @returns {boolean}
 */
export function checkAuth(req, env) {
  const expected = env && env.CRUCIBLE_RUN_INSIGHTS_API_TOKEN;
  if (!expected) return false; // fail closed — never accept when unconfigured
  const header = req.headers.get('Authorization') || '';
  if (!header.startsWith('Bearer ')) return false;
  return timingSafeEqual(header.slice(7), expected);
}
