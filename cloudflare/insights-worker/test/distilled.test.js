// Unit tests for distilled-artifact publish/read (src/admin.js).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { publishDistilled, getDistilled } from '../src/admin.js';

function fakeEnv() {
  const arts = [];
  const DB = {
    prepare(sql) {
      const mk = (args = []) => ({
        bind: (...a) => mk(a),
        async run() {
          if (sql.includes('INSERT INTO distilled_artifacts')) {
            arts.push({ id: args[0], kind: args[1], ts: args[2], payload: args[3] });
            return { meta: { changes: 1 } };
          }
          return { meta: { changes: 0 } };
        },
        async all() {
          let rows = arts.slice().reverse();
          if (sql.includes('WHERE kind = ?')) rows = rows.filter((r) => r.kind === args[0]);
          return { results: rows };
        },
        async first() {
          return null;
        },
      });
      return mk();
    },
  };
  return { DB, _arts: arts };
}

test('publishDistilled stores; getDistilled returns parsed payload', async () => {
  const env = fakeEnv();
  const pub = await publishDistilled(env, { kind: 'avoidance', payload: { items: ['x'] } });
  assert.equal(pub.ok, true);
  assert.match(pub.id, /^dst_[0-9a-f]{16}$/);
  const got = await getDistilled(env, {});
  assert.equal(got.ok, true);
  assert.equal(got.distilled.length, 1);
  assert.deepEqual(got.distilled[0].payload, { items: ['x'] });
});

test('publishDistilled rejects missing kind / payload', async () => {
  const env = fakeEnv();
  assert.equal((await publishDistilled(env, { payload: {} })).ok, false);
  assert.equal((await publishDistilled(env, { kind: 'x' })).ok, false);
});

test('getDistilled filters by kind', async () => {
  const env = fakeEnv();
  await publishDistilled(env, { kind: 'avoidance', payload: 1 });
  await publishDistilled(env, { kind: 'skills', payload: 2 });
  const got = await getDistilled(env, { kind: 'skills' });
  assert.equal(got.distilled.length, 1);
  assert.equal(got.distilled[0].payload, 2);
});
