// Unit tests for authenticate() — per-contributor token-hash lookup with a
// legacy single-secret admin fallback.  Zero deps; node:test + a fake D1 keyed
// by token_hash.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { authenticate, sha256Hex, timingSafeEqual, checkAuth } from '../src/auth.js';

function req(token) {
  return {
    headers: {
      get: (k) =>
        k.toLowerCase() === 'authorization' && token ? `Bearer ${token}` : null,
    },
  };
}

// Fake D1: api_tokens lookups resolve against `rowsByHash`; UPDATE is a no-op.
function fakeDB(rowsByHash = {}) {
  return {
    prepare(sql) {
      return {
        bind(...args) {
          return {
            async first() {
              if (sql.includes('FROM api_tokens') && sql.includes('token_hash')) {
                return rowsByHash[args[0]] || null;
              }
              return null;
            },
            async run() {
              return { meta: { changes: 1 } };
            },
            async all() {
              return { results: [] };
            },
          };
        },
      };
    },
  };
}

test('missing / non-Bearer / empty header → null', async () => {
  assert.equal(await authenticate({ headers: { get: () => null } }, {}), null);
  assert.equal(
    await authenticate(req(''), { CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'x' }),
    null
  );
});

test('legacy shared secret → implicit admin', async () => {
  const auth = await authenticate(req('SECRET'), {
    CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'SECRET',
  });
  assert.deepEqual(auth, {
    contributorId: 'legacy-admin',
    scope: 'admin',
    tokenId: null,
    dailyQuota: null,
  });
});

test('unknown token + no legacy secret → null (fail closed)', async () => {
  assert.equal(await authenticate(req('nope'), { DB: fakeDB({}) }), null);
});

test('active table token → its scope + contributor', async () => {
  const raw = 'crk_abc';
  const hash = await sha256Hex(raw);
  const db = fakeDB({
    [hash]: {
      token_id: 'tok_1',
      contributor_id: 'alice',
      scope: 'ingest',
      status: 'active',
    },
  });
  const auth = await authenticate(req(raw), { DB: db });
  assert.equal(auth.scope, 'ingest');
  assert.equal(auth.contributorId, 'alice');
  assert.equal(auth.tokenId, 'tok_1');
});

test('revoked table token → null (denied)', async () => {
  const raw = 'crk_revoked';
  const hash = await sha256Hex(raw);
  const db = fakeDB({
    [hash]: {
      token_id: 'tok_2',
      contributor_id: 'mallory',
      scope: 'ingest',
      status: 'revoked',
    },
  });
  assert.equal(await authenticate(req(raw), { DB: db }), null);
});

test('legacy secret still works as fallback when token not in table', async () => {
  const auth = await authenticate(req('LEG'), {
    DB: fakeDB({}),
    CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'LEG',
  });
  assert.equal(auth.scope, 'admin');
});

test('sha256Hex matches the known empty-string vector', async () => {
  assert.equal(
    await sha256Hex(''),
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
  );
});

test('timingSafeEqual basic behaviour', () => {
  assert.equal(timingSafeEqual('abc', 'abc'), true);
  assert.equal(timingSafeEqual('abc', 'abd'), false);
  assert.equal(timingSafeEqual('abc', 'ab'), false);
  assert.equal(timingSafeEqual('abc', 123), false);
});

test('legacy checkAuth still works (backward compat)', () => {
  assert.equal(checkAuth(req('S'), { CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'S' }), true);
  assert.equal(checkAuth(req('S'), {}), false);
  assert.equal(checkAuth(req(null), { CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'S' }), false);
});
