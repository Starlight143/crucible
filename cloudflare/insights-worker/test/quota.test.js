// Unit tests for the per-token daily quota (src/quota.js).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { consumeQuota } from '../src/quota.js';

// Fake env whose DB models token_usage (keyed by `${tokenId}|${day}`).
function fakeEnv() {
  const usage = new Map();
  const DB = {
    prepare(sql) {
      const mk = (args = []) => ({
        bind: (...a) => mk(a),
        async first() {
          if (sql.includes('SELECT count FROM token_usage')) {
            const k = `${args[0]}|${args[1]}`;
            return usage.has(k) ? { count: usage.get(k) } : null;
          }
          return null;
        },
        async run() {
          if (sql.includes('INSERT INTO token_usage')) {
            const k = `${args[0]}|${args[1]}`;
            usage.set(k, (usage.get(k) || 0) + args[2]);
          }
          return { meta: { changes: 1 } };
        },
      });
      return mk();
    },
  };
  return { DB, _usage: usage };
}

test('no tokenId → unlimited (admin/legacy)', async () => {
  const r = await consumeQuota(fakeEnv(), null, 100, 1);
  assert.equal(r.ok, true);
  assert.equal(r.unlimited, true);
});

test('null dailyQuota → unlimited', async () => {
  const r = await consumeQuota(fakeEnv(), 'tok_1', null, 1);
  assert.equal(r.ok, true);
  assert.equal(r.unlimited, true);
});

test('under quota increments and stays ok', async () => {
  const env = fakeEnv();
  assert.equal((await consumeQuota(env, 'tok_1', 3, 1)).ok, true);
  assert.equal((await consumeQuota(env, 'tok_1', 3, 1)).ok, true);
  assert.equal((await consumeQuota(env, 'tok_1', 3, 1)).ok, true);
});

test('at quota → rejected', async () => {
  const env = fakeEnv();
  await consumeQuota(env, 'tok_1', 2, 1); // 1
  await consumeQuota(env, 'tok_1', 2, 1); // 2
  const c = await consumeQuota(env, 'tok_1', 2, 1); // used 2 >= 2 → reject
  assert.equal(c.ok, false);
  assert.equal(c.dailyQuota, 2);
});

test('batch consumes n units at once', async () => {
  const env = fakeEnv();
  const a = await consumeQuota(env, 'tok_1', 10, 7);
  assert.equal(a.ok, true);
  assert.equal(env._usage.get(`tok_1|${new Date().toISOString().slice(0, 10)}`), 7);
});
