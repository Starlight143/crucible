// Unit tests for admin token operations — issue / revoke / list.  Verifies that
// only a SHA-256 hash is persisted (never the raw token) and that the raw token
// is returned exactly once.  Zero deps; node:test + a fake env whose DB models
// the D1 statement API (prepare → bind / run / all / first).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { issueToken, revokeToken, listTokens } from '../src/admin.js';
import { sha256Hex } from '../src/auth.js';

function fakeEnv() {
  const rows = [];
  // A statement supports .bind() AND the terminal ops directly (D1 allows
  // .all()/.run() on a parameterless prepared statement, e.g. listTokens).
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    async run() {
      if (sql.includes('INSERT INTO api_tokens')) {
        rows.push({
          token_id: args[0],
          token_hash: args[1],
          contributor_id: args[2],
          label: args[3],
          scope: args[4],
          status: 'active',
          daily_quota: args[5],
        });
        return { meta: { changes: 1 } };
      }
      if (sql.includes('UPDATE api_tokens') && sql.includes("status = 'revoked'")) {
        const row = rows.find((r) => r.token_id === args[0]);
        if (row) {
          row.status = 'revoked';
          return { meta: { changes: 1 } };
        }
        return { meta: { changes: 0 } };
      }
      return { meta: { changes: 0 } };
    },
    async all() {
      // Mirror the real query: metadata only, never token_hash.
      return {
        results: rows.map((r) => ({
          token_id: r.token_id,
          contributor_id: r.contributor_id,
          label: r.label,
          scope: r.scope,
          status: r.status,
          daily_quota: r.daily_quota,
        })),
      };
    },
    async first() {
      return null;
    },
  });
  return { DB: { prepare: (sql) => stmt(sql) }, _rows: rows };
}

test('issueToken returns a raw token once and stores only its hash', async () => {
  const env = fakeEnv();
  const r = await issueToken(env, {
    contributorId: 'alice',
    label: 'gh:alice',
    scope: 'ingest',
  });
  assert.equal(r.ok, true);
  assert.match(r.token, /^crk_[0-9a-f]{64}$/);
  assert.equal(r.scope, 'ingest');
  assert.equal(r.contributor_id, 'alice');

  const row = env._rows[0];
  // The stored value is the HASH of the raw token, not the raw token itself.
  assert.equal(row.token_hash, await sha256Hex(r.token));
  assert.notEqual(row.token_hash, r.token);
  assert.equal(row.contributor_id, 'alice');
  assert.equal(row.scope, 'ingest');
});

test('issueToken rejects a bad scope and a missing contributor', async () => {
  const env = fakeEnv();
  assert.equal(
    (await issueToken(env, { contributorId: 'a', scope: 'superuser' })).ok,
    false
  );
  assert.equal((await issueToken(env, { scope: 'ingest' })).ok, false);
  assert.equal(env._rows.length, 0);
});

test('issueToken accepts a positive integer daily_quota, ignores junk', async () => {
  const env = fakeEnv();
  const a = await issueToken(env, { contributorId: 'q', scope: 'ingest', dailyQuota: 500 });
  assert.equal(a.daily_quota, 500);
  const b = await issueToken(env, { contributorId: 'q2', scope: 'ingest', dailyQuota: -5 });
  assert.equal(b.daily_quota, null);
});

test('revokeToken flips status; unknown id → ok:false', async () => {
  const env = fakeEnv();
  const r = await issueToken(env, { contributorId: 'bob', scope: 'read' });
  const rv = await revokeToken(env, r.token_id);
  assert.equal(rv.ok, true);
  assert.equal(env._rows[0].status, 'revoked');
  const rv2 = await revokeToken(env, 'tok_does_not_exist');
  assert.equal(rv2.ok, false);
});

test('listTokens returns metadata and never the hash/raw token', async () => {
  const env = fakeEnv();
  await issueToken(env, { contributorId: 'carol', scope: 'admin' });
  const r = await listTokens(env);
  assert.equal(r.ok, true);
  assert.equal(r.tokens.length, 1);
  assert.equal(r.tokens[0].contributor_id, 'carol');
  assert.equal(r.tokens[0].token_hash, undefined);
  assert.equal(r.tokens[0].token, undefined);
});
