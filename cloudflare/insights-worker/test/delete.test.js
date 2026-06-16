// Unit tests for the admin-only deleteEvents() — deletes ANY trust_state, by
// content_ids / run_id / contributor_id, with best-effort R2 cleanup.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { deleteEvents } from '../src/admin.js';

function fakeDB(cap) {
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    async run() {
      cap.push({ sql, args });
      return { meta: { changes: args.length || 1 } };
    },
    async all() {
      cap.push({ sql, args });
      return { results: [] };
    },
    async first() {
      return null;
    },
  });
  return { prepare: (sql) => stmt(sql) };
}

test('delete by content_ids issues an IN delete with the ids bound', async () => {
  const cap = [];
  const r = await deleteEvents({ DB: fakeDB(cap) }, { contentIds: ['a', 'b'] });
  assert.equal(r.ok, true);
  const del = cap.find((c) => c.sql.startsWith('DELETE'));
  assert.ok(del.sql.includes('content_id IN (?,?)'));
  assert.deepEqual(del.args, ['a', 'b']);
});

test('delete by run_id', async () => {
  const cap = [];
  const r = await deleteEvents({ DB: fakeDB(cap) }, { runId: 'r1' });
  assert.equal(r.ok, true);
  const del = cap.find((c) => c.sql.startsWith('DELETE'));
  assert.ok(del.sql.includes('run_id = ?'));
  assert.deepEqual(del.args, ['r1']);
});

test('delete by contributor_id', async () => {
  const cap = [];
  const r = await deleteEvents({ DB: fakeDB(cap) }, { contributorId: 'gh_1' });
  assert.equal(r.ok, true);
  const del = cap.find((c) => c.sql.startsWith('DELETE'));
  assert.ok(del.sql.includes('contributor_id = ?'));
});

test('no selector → ok:false (never a table-wide delete)', async () => {
  const cap = [];
  const r = await deleteEvents({ DB: fakeDB(cap) }, {});
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'nothing_specified');
  assert.equal(cap.length, 0);
});

test('R2 blobs for matched rows are deleted first when BLOBS is bound', async () => {
  const deleted = [];
  const env = {
    DB: {
      prepare: () => ({
        bind: () => ({
          async all() {
            return { results: [{ payload_r2_key: 'insights/r/a.json' }] };
          },
          async run() {
            return { meta: { changes: 1 } };
          },
        }),
      }),
    },
    BLOBS: {
      delete: async (k) => {
        deleted.push(k);
      },
    },
  };
  const r = await deleteEvents(env, { contentIds: ['a'] });
  assert.equal(r.ok, true);
  assert.deepEqual(deleted, ['insights/r/a.json']);
});
