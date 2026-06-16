// Per-token daily quota (Phase A / Step 1).
//
// A best-effort daily cap that bounds abuse and protects the free-tier budget.
// No quota applies to admin / legacy tokens (no tokenId) or to tokens whose
// api_tokens.daily_quota is NULL (unlimited).  The check-then-increment is not
// strictly transactional under concurrency — at worst a token slips a few writes
// over its cap on the same edge tick, which is fine for a daily guard.

/**
 * Consume `n` units of the token's daily quota.
 * @param {{ DB: any }} env
 * @param {string|null} tokenId
 * @param {number|null|undefined} dailyQuota
 * @param {number} [n]
 * @returns {Promise<{ ok: boolean, used?: number, dailyQuota?: number, unlimited?: boolean }>}
 */
export async function consumeQuota(env, tokenId, dailyQuota, n = 1) {
  if (!tokenId || dailyQuota == null) return { ok: true, unlimited: true };
  const day = new Date().toISOString().slice(0, 10); // YYYY-MM-DD (UTC)
  const row = await env.DB.prepare(
    'SELECT count FROM token_usage WHERE token_id = ? AND day = ?'
  )
    .bind(tokenId, day)
    .first();
  const used = row && Number.isFinite(row.count) ? row.count : 0;
  if (used >= dailyQuota) return { ok: false, used, dailyQuota };
  await env.DB.prepare(
    `INSERT INTO token_usage (token_id, day, count) VALUES (?, ?, ?)
       ON CONFLICT(token_id, day) DO UPDATE SET count = count + ?`
  )
    .bind(tokenId, day, n, n)
    .run();
  return { ok: true, used: used + n, dailyQuota };
}
