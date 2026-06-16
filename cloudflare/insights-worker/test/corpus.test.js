// Unit tests for the contributor read surface (corpus.js): approved-only, and
// NEVER exposing raw payloads or contributor identity.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { corpusStats, corpusList } from '../src/corpus.js';

function fakeDB(captured) {
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    async first() {
      if (sql.includes('COUNT(*)')) return { total: 3, first_ts: 'a', last_ts: 'b' };
      return null;
    },
    async all() {
      captured.push({ sql, args });
      return {
        results: [
          {
            content_id: 'sha256:1',
            stream: 'output',
            ts: 't',
            run_id: 'r',
            project_name: 'p',
            mode: 'Quant',
            kind: 'output_method',
            stage: null,
            outcome_status: 'success',
            outcome_score: 1,
          },
        ],
      };
    },
    async run() {
      return { meta: { changes: 0 } };
    },
  });
  return { prepare: (sql) => stmt(sql) };
}

test('corpusList filters to approved and never selects payload/identity columns', async () => {
  const cap = [];
  const r = await corpusList({ DB: fakeDB(cap) }, { limit: 50 });
  assert.equal(r.ok, true);
  const listSql = cap.map((c) => c.sql).find((s) => s.includes('ORDER BY content_id'));
  assert.ok(listSql.includes("trust_state = 'approved'"));
  assert.ok(
    !/payload_inline|payload_r2_key|env_fingerprint|contributor_id/.test(listSql),
    'corpus list must not expose payloads or identity'
  );
});

test('corpusList applies stream / mode / run_id filters', async () => {
  const cap = [];
  await corpusList({ DB: fakeDB(cap) }, { stream: 'output', mode: 'Quant', runId: 'r' });
  const c = cap[cap.length - 1];
  assert.ok(c.sql.includes('stream = ?'));
  assert.ok(c.sql.includes('mode = ?'));
  assert.ok(c.sql.includes('run_id = ?'));
  assert.deepEqual(c.args.slice(0, 3), ['output', 'Quant', 'r']);
});

test('corpusList clamps limit and returns a next_cursor when full', async () => {
  const cap = [];
  const r = await corpusList({ DB: fakeDB(cap) }, { limit: 1 });
  // one row returned, limit 1 → next_cursor is that row's content_id
  assert.equal(r.next_cursor, 'sha256:1');
});

test('corpusStats returns approved-only aggregates', async () => {
  const cap = [];
  const r = await corpusStats({ DB: fakeDB(cap) });
  assert.equal(r.ok, true);
  assert.equal(r.total, 3);
  assert.ok(Array.isArray(r.by_stream));
  assert.ok(Array.isArray(r.top_projects));
  for (const c of cap) {
    assert.ok(c.sql.includes("trust_state = 'approved'"), 'every group query is approved-only');
  }
});
