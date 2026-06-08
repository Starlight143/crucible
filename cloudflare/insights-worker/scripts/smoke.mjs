#!/usr/bin/env node
// End-to-end smoke test against a RUNNING Worker (local `wrangler dev` or a
// deployed URL).  Zero dependencies — global fetch + node:zlib (Node >=20).
//
// Exercises: liveness, auth gate, single ingest, dedup (re-send → same id),
// tamper rejection, gzip batch, R2 spill + read-back, query by run_id, and the
// run summary.  Stamps content_id client-side via the shared canonical module
// so the verify path is covered.
//
// Usage:
//   1. Terminal A:  npm run dev          # starts wrangler dev on :8787
//   2. Terminal B:  npm run smoke        # or: INSIGHTS_URL=... INSIGHTS_TOKEN=... npm run smoke
//
// Env:
//   INSIGHTS_URL    base URL   (default http://127.0.0.1:8787)
//   INSIGHTS_TOKEN  bearer tok (default dev-local-token — matches .dev.vars.example)

import { gzipSync } from 'node:zlib';
import { contentId } from '../src/canonical.js';

const BASE = (process.env.INSIGHTS_URL || 'http://127.0.0.1:8787').replace(/\/+$/, '');
const TOKEN = process.env.INSIGHTS_TOKEN || 'dev-local-token';
const RUN = 'smoke-' + Date.now();
const authHeaders = { Authorization: `Bearer ${TOKEN}` };

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`);
    failures++;
  }
}

function baseEvent(overrides = {}) {
  return {
    schema_version: 1,
    ts: new Date().toISOString(),
    run_id: RUN,
    project_name: 'smoke',
    mode: 'Quant',
    kind: 'output_method',
    stream: 'output',
    stage: '07',
    signals: ['mode:quant'],
    env_fingerprint: { arch: 'x86_64' },
    outcome: { status: 'success', score: 0.5 },
    payload: { note: 'hello' },
    ...overrides,
  };
}
async function stamp(ev) {
  return { ...ev, content_id: await contentId(ev) };
}
async function postEvent(ev) {
  const r = await fetch(`${BASE}/v1/insights/events`, {
    method: 'POST',
    headers: { ...authHeaders, 'Content-Type': 'application/json' },
    body: JSON.stringify({ event: ev }),
  });
  return { status: r.status, body: await r.json().catch(() => ({})) };
}

async function main() {
  console.log(`smoke → ${BASE}  (run_id=${RUN})`);

  // 1) liveness (no auth)
  let r = await fetch(`${BASE}/`);
  check('GET / liveness → 200', r.status === 200);

  // 2) auth required
  r = await fetch(`${BASE}/v1/insights/events`, { method: 'POST', body: '{}' });
  check('POST without token → 401', r.status === 401);

  // 3) ingest single (client-stamped content_id exercises the verify path)
  const ev1 = await stamp(baseEvent({ payload: { note: 'single' } }));
  let res = await postEvent(ev1);
  check('POST single → ok', res.status === 200 && res.body.ok === true, JSON.stringify(res.body));
  check('single content_id echoed', res.body.content_id === ev1.content_id);

  // 4) re-send identical → dedup (same id, no error)
  res = await postEvent(ev1);
  check('POST duplicate → same id', res.body.content_id === ev1.content_id);

  // 5) tampered content_id rejected
  res = await postEvent({ ...ev1, content_id: 'sha256:0000' });
  check('POST tampered content_id → 422', res.status === 422 && res.body.reason === 'content_id_mismatch');

  // 6) gzip batch of 3 distinct events
  const evs = await Promise.all([
    stamp(baseEvent({ kind: 'error_record', stream: 'error', payload: { e: 1 } })),
    stamp(baseEvent({ kind: 'runtime_params', stream: 'params', payload: { p: 2 } })),
    stamp(baseEvent({ payload: { b: 3 } })),
  ]);
  const gz = gzipSync(Buffer.from(JSON.stringify({ events: evs })));
  r = await fetch(`${BASE}/v1/insights/batch`, {
    method: 'POST',
    headers: { ...authHeaders, 'Content-Type': 'application/json', 'Content-Encoding': 'gzip' },
    body: gz,
  });
  let body = await r.json();
  check('POST batch (gzip) → ingested 3', body.ingested === 3, JSON.stringify(body));

  // 7) large payload (> inline limit) → stored (R2 spill if R2 is bound, else
  //    inline in D1), then read back the full event losslessly either way.
  const big = await stamp(baseEvent({ payload: { blob: 'x'.repeat(6000) } }));
  res = await postEvent(big);
  check('POST large payload → ok', res.body.ok === true, JSON.stringify(res.body));
  r = await fetch(`${BASE}/v1/insights/events/${encodeURIComponent(big.content_id)}`, {
    headers: authHeaders,
  });
  body = await r.json();
  check(
    'GET big event stored (inline or R2)',
    !!(body.payload_inline || body.payload_r2_key)
  );
  check(
    'GET big event reconstructs full payload',
    body.event && body.event.payload && body.event.payload.blob?.length === 6000
  );

  // 8) query by run_id
  r = await fetch(`${BASE}/v1/insights/events?run_id=${encodeURIComponent(RUN)}&limit=100`, {
    headers: authHeaders,
  });
  body = await r.json();
  check('GET events?run_id → >=5 rows', Array.isArray(body.events) && body.events.length >= 5, `got ${body.events?.length}`);

  // 9) run summary
  r = await fetch(`${BASE}/v1/insights/runs/${encodeURIComponent(RUN)}/summary`, {
    headers: authHeaders,
  });
  body = await r.json();
  check('GET run summary total >= 5', (body.total ?? 0) >= 5, JSON.stringify(body));

  console.log(failures === 0 ? '\nSMOKE PASSED' : `\nSMOKE FAILED (${failures} check(s))`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error('smoke crashed:', e);
  process.exit(1);
});
