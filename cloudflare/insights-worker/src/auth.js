// Bearer-token authentication for the insights Worker.
//
// The token is the ONLY gate on write/query.  It is stored as a Worker secret
// (`wrangler secret put CRUCIBLE_RUN_INSIGHTS_API_TOKEN`) and sent by the
// Python client as `Authorization: Bearer <token>`.  Fail-closed: if no token
// is configured in the environment, every authenticated request is denied.

/**
 * Constant-time string comparison.  The length is allowed to leak (lengths
 * differ → immediate false) but content does not short-circuit, so an
 * attacker cannot byte-by-byte time the secret.
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
 * Validate the `Authorization: Bearer <token>` header against the configured
 * secret.  Returns false (deny) when no secret is configured.
 * @param {Request} req
 * @param {{ CRUCIBLE_RUN_INSIGHTS_API_TOKEN?: string }} env
 * @returns {boolean}
 */
export function checkAuth(req, env) {
  const expected = env.CRUCIBLE_RUN_INSIGHTS_API_TOKEN;
  if (!expected) return false; // fail closed — never accept when unconfigured
  const header = req.headers.get('Authorization') || '';
  if (!header.startsWith('Bearer ')) return false;
  return timingSafeEqual(header.slice(7), expected);
}
