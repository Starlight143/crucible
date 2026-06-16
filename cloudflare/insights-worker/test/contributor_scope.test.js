// End-to-end access-control matrix for the v2 surface (contributor scope, strict
// gate, corpus read, admin delete/stats, public pages) via the Worker's fetch
// handler.  Zero deps; node:test + global Request/Response.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker from '../src/index.js';
import { sha256Hex } from '../src/auth.js';

function fakeDB(tokensByHash = {}) {
  const usage = new Map();
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    async first() {
      if (sql.includes('FROM api_tokens') && sql.includes('token_hash')) {
        return tokensByHash[args[0]] || null;
      }
      if (sql.includes('SELECT count FROM token_usage')) {
        const k = `${args[0]}|${args[1]}`;
        return usage.has(k) ? { count: usage.get(k) } : null;
      }
      if (sql.includes('COUNT(*)')) return { total: 0, first_ts: null, last_ts: null };
      return null;
    },
    async all() {
      return { results: [] };
    },
    async run() {
      if (sql.includes('INSERT INTO token_usage')) {
        const k = `${args[0]}|${args[1]}`;
        usage.set(k, (usage.get(k) || 0) + args[2]);
      }
      return { meta: { changes: 1 } };
    },
  });
  return { prepare: (sql) => stmt(sql), async batch() { return []; } };
}

const ORIGIN = 'https://w.example';
const get = (p, t) =>
  new Request(ORIGIN + p, { method: 'GET', headers: t ? { Authorization: `Bearer ${t}` } : {} });
const postJSON = (p, t, b) =>
  new Request(ORIGIN + p, {
    method: 'POST',
    headers: { ...(t ? { Authorization: `Bearer ${t}` } : {}), 'Content-Type': 'application/json' },
    body: JSON.stringify(b),
  });

const goodEvent = {
  schema_version: 1,
  ts: '2026-01-01T00:00:00.000Z',
  run_id: 'r',
  project_name: 'p',
  mode: 'Quant',
  kind: 'output_method',
  stream: 'output',
  payload: {},
};

async function tok(scope) {
  const raw = `crk_${scope}_demo`;
  const db = fakeDB({
    [await sha256Hex(raw)]: {
      token_id: 't',
      contributor_id: 'gh_1',
      scope,
      status: 'active',
      daily_quota: null,
    },
  });
  return { raw, db };
}

test('contributor: can ingest a conforming event', async () => {
  const { raw, db } = await tok('contributor');
  const r = await worker.fetch(postJSON('/v1/insights/events', raw, { event: goodEvent }), { DB: db });
  assert.equal(r.status, 200);
});

test('contributor: can read corpus stats + corpus list + distilled', async () => {
  const { raw, db } = await tok('contributor');
  assert.equal((await worker.fetch(get('/v1/insights/corpus/stats', raw), { DB: db })).status, 200);
  assert.equal((await worker.fetch(get('/v1/insights/corpus', raw), { DB: db })).status, 200);
  assert.equal((await worker.fetch(get('/v1/insights/distilled', raw), { DB: db })).status, 200);
});

test('contributor: cannot read RAW events, cannot delete, cannot reach admin', async () => {
  const { raw, db } = await tok('contributor');
  assert.equal((await worker.fetch(get('/v1/insights/events', raw), { DB: db })).status, 403);
  assert.equal(
    (await worker.fetch(postJSON('/v1/admin/events/delete', raw, { content_ids: ['x'] }), { DB: db })).status,
    403
  );
  assert.equal((await worker.fetch(get('/v1/admin/stats', raw), { DB: db })).status, 403);
  assert.equal((await worker.fetch(get('/v1/admin/tokens', raw), { DB: db })).status, 403);
});

test('contributor strict gate: a malformed event is rejected 422', async () => {
  const { raw, db } = await tok('contributor');
  const r = await worker.fetch(
    postJSON('/v1/insights/events', raw, { event: { ...goodEvent, kind: 'made_up' } }),
    { DB: db }
  );
  assert.equal(r.status, 422);
  const b = await r.json();
  assert.match(b.reason, /bad_kind/);
});

test('admin BYPASSES the strict enum gate (no self-lock on a future kind)', async () => {
  const env = { DB: fakeDB(), CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'ADMIN' };
  const r = await worker.fetch(
    postJSON('/v1/insights/events', 'ADMIN', {
      event: { ...goodEvent, kind: 'future_kind_not_in_enum', stream: 'output' },
    }),
    env
  );
  assert.equal(r.status, 200);
});

test('admin: corpus + admin stats + delete all allowed', async () => {
  const env = { DB: fakeDB(), CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'ADMIN' };
  assert.equal((await worker.fetch(get('/v1/insights/corpus/stats', 'ADMIN'), env)).status, 200);
  assert.equal((await worker.fetch(get('/v1/admin/stats', 'ADMIN'), env)).status, 200);
  assert.equal(
    (await worker.fetch(postJSON('/v1/admin/events/delete', 'ADMIN', { content_ids: ['x'] }), env)).status,
    200
  );
});

test('read scope: can read corpus, cannot ingest', async () => {
  const { raw, db } = await tok('read');
  assert.equal((await worker.fetch(get('/v1/insights/corpus/stats', raw), { DB: db })).status, 200);
  assert.equal(
    (await worker.fetch(postJSON('/v1/insights/events', raw, { event: goodEvent }), { DB: db })).status,
    403
  );
});

test('ingest scope: can ingest, cannot read corpus (write-only)', async () => {
  const { raw, db } = await tok('ingest');
  assert.equal(
    (await worker.fetch(postJSON('/v1/insights/events', raw, { event: goodEvent }), { DB: db })).status,
    200
  );
  assert.equal((await worker.fetch(get('/v1/insights/corpus/stats', raw), { DB: db })).status, 403);
});

test('public pages: GET / and /console are HTML 200 without auth', async () => {
  const env = { DB: fakeDB() };
  const s = await worker.fetch(get('/'), env);
  assert.equal(s.status, 200);
  assert.match(s.headers.get('content-type') || '', /text\/html/);
  const c = await worker.fetch(get('/console'), env);
  assert.equal(c.status, 200);
});

test('oauth start is disabled (503) until secrets are configured', async () => {
  const r = await worker.fetch(get('/oauth/github/start'), { DB: fakeDB() });
  assert.equal(r.status, 503);
});

test('liveness /health still returns JSON ok', async () => {
  const r = await worker.fetch(get('/health'), { DB: fakeDB() });
  assert.equal(r.status, 200);
  const b = await r.json();
  assert.equal(b.status, 'ok');
});
