// End-to-end scope + Step 1/2/4 enforcement via the Worker's fetch handler.
// Builds real Request objects and a fake D1, then asserts the access-control
// matrix and the quota / secret / distilled behaviours.
// Zero deps; node:test + global Request/Response (Node >= 18 undici).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker from '../src/index.js';
import { sha256Hex } from '../src/auth.js';

// Fake D1: a statement supports .bind() and the terminal ops directly, so both
// parameterised and parameterless queries work.  Models token_usage so daily
// quota can be exercised end-to-end.
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
      if (sql.includes('COUNT(*)')) {
        return { total: 0, first_ts: null, last_ts: null };
      }
      return null; // events/:content_id → not found
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

function get(path, token) {
  return new Request(ORIGIN + path, {
    method: 'GET',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
}
function postJSON(path, token, body) {
  return new Request(ORIGIN + path, {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
}

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

async function tokenDB(scope, extra = {}) {
  const raw = `crk_${scope}_demo`;
  const db = fakeDB({
    [await sha256Hex(raw)]: {
      token_id: 't',
      contributor_id: 'user',
      scope,
      status: 'active',
      daily_quota: extra.dailyQuota ?? null,
    },
  });
  return { raw, db };
}

test('no token → 401', async () => {
  const res = await worker.fetch(get('/v1/insights/events'), { DB: fakeDB() });
  assert.equal(res.status, 401);
});

test('liveness needs no token', async () => {
  const res = await worker.fetch(get('/health'), { DB: fakeDB() });
  assert.equal(res.status, 200);
});

test('ingest token: POST event allowed, GET raw events forbidden', async () => {
  const { raw, db } = await tokenDB('ingest');
  const ok = await worker.fetch(
    postJSON('/v1/insights/events', raw, { event: goodEvent }),
    { DB: db }
  );
  assert.equal(ok.status, 200);
  const forb = await worker.fetch(get('/v1/insights/events', raw), { DB: db });
  assert.equal(forb.status, 403);
});

test('read token: cannot POST, cannot GET raw, CAN GET distilled', async () => {
  const { raw, db } = await tokenDB('read');
  const p = await worker.fetch(
    postJSON('/v1/insights/events', raw, { event: goodEvent }),
    { DB: db }
  );
  assert.equal(p.status, 403);
  const rawGet = await worker.fetch(get('/v1/insights/events', raw), { DB: db });
  assert.equal(rawGet.status, 403);
  const dist = await worker.fetch(get('/v1/insights/distilled', raw), { DB: db });
  assert.equal(dist.status, 200);
});

test('ingest token cannot read distilled', async () => {
  const { raw, db } = await tokenDB('ingest');
  const dist = await worker.fetch(get('/v1/insights/distilled', raw), { DB: db });
  assert.equal(dist.status, 403);
});

test('admin (legacy secret): GET raw + list tokens + distilled all allowed', async () => {
  const env = { DB: fakeDB(), CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'ADMIN' };
  assert.equal((await worker.fetch(get('/v1/insights/events', 'ADMIN'), env)).status, 200);
  assert.equal((await worker.fetch(get('/v1/admin/tokens', 'ADMIN'), env)).status, 200);
  assert.equal((await worker.fetch(get('/v1/insights/distilled', 'ADMIN'), env)).status, 200);
});

test('non-admin cannot reach admin endpoints', async () => {
  const { raw, db } = await tokenDB('ingest');
  assert.equal((await worker.fetch(get('/v1/admin/tokens', raw), { DB: db })).status, 403);
  assert.equal(
    (await worker.fetch(postJSON('/v1/admin/events/promote', raw, { content_ids: ['x'] }), { DB: db })).status,
    403
  );
});

test('admin can issue a token; raw token returned once', async () => {
  const env = { DB: fakeDB(), CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'ADMIN' };
  const res = await worker.fetch(
    postJSON('/v1/admin/tokens', 'ADMIN', { contributor_id: 'newbie', scope: 'ingest' }),
    env
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.match(body.token, /^crk_[0-9a-f]{64}$/);
  assert.equal(body.scope, 'ingest');
});

test('admin can publish a distilled artifact', async () => {
  const env = { DB: fakeDB(), CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'ADMIN' };
  const res = await worker.fetch(
    postJSON('/v1/admin/distilled', 'ADMIN', { kind: 'avoidance', payload: { x: 1 } }),
    env
  );
  assert.equal(res.status, 200);
});

test('secret in event body → 422 secret_detected', async () => {
  const env = { DB: fakeDB(), CRUCIBLE_RUN_INSIGHTS_API_TOKEN: 'ADMIN' };
  const ev = { ...goodEvent, payload: { leak: 'sk-ant-' + 'a'.repeat(30) } };
  const res = await worker.fetch(postJSON('/v1/insights/events', 'ADMIN', { event: ev }), env);
  assert.equal(res.status, 422);
  const body = await res.json();
  assert.equal(body.reason, 'secret_detected');
});

test('per-token daily quota → second write 429', async () => {
  const { raw, db } = await tokenDB('ingest', { dailyQuota: 1 });
  const first = await worker.fetch(
    postJSON('/v1/insights/events', raw, { event: goodEvent }),
    { DB: db }
  );
  assert.equal(first.status, 200);
  const second = await worker.fetch(
    postJSON('/v1/insights/events', raw, { event: { ...goodEvent, run_id: 'r2' } }),
    { DB: db }
  );
  assert.equal(second.status, 429);
});
