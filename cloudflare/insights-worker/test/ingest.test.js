// Unit tests for prepareEvent() storage routing — R2 is OPTIONAL.
//
// Verifies the D1-only default (no BLOBS binding → everything stored inline in
// D1, with an oversized-event guard) and the R2-enabled path (BLOBS bound →
// large events spill to R2).  Zero dependencies; node:test + a fake env whose
// DB.prepare(...).bind(...) just records its bound args.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { prepareEvent } from '../src/ingest.js';

function fakeEnv({ r2 = false, inlineMax } = {}) {
  const puts = [];
  const env = {
    DB: { prepare: () => ({ bind: (...args) => ({ args }) }) },
    BLOBS: r2
      ? {
          put: async (key, body) => {
            puts.push({ key, body });
          },
        }
      : undefined,
    _puts: puts,
  };
  if (inlineMax !== undefined) env.INLINE_MAX_BYTES = String(inlineMax);
  return env;
}

function baseEvent(extra = {}) {
  return {
    schema_version: 1,
    ts: '2026-01-01T00:00:00.000Z',
    run_id: 'r',
    project_name: 'p',
    mode: 'Quant',
    kind: 'output_method',
    stream: 'output',
    payload: {},
    ...extra,
  };
}

test('D1-only: small event stored inline, no R2 key', async () => {
  const env = fakeEnv();
  const r = await prepareEvent(env, baseEvent());
  assert.equal(r.ok, true);
  assert.equal(r.r2, null);
  assert.equal(env._puts.length, 0);
});

test('D1-only: event over inline limit still stored inline (no R2 to spill to)', async () => {
  const env = fakeEnv({ inlineMax: 100 });
  const r = await prepareEvent(env, baseEvent({ payload: { blob: 'x'.repeat(5000) } }));
  assert.equal(r.ok, true);
  assert.equal(r.r2, null);
});

test('D1-only: oversized event rejected (kept local), not crashed', async () => {
  const env = fakeEnv();
  const r = await prepareEvent(env, baseEvent({ payload: { blob: 'x'.repeat(1_000_000) } }));
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'payload_too_large_no_r2');
  assert.ok(typeof r.bytes === 'number' && r.bytes > 950_000);
});

test('R2 enabled: large event spills to R2', async () => {
  const env = fakeEnv({ r2: true, inlineMax: 100 });
  const r = await prepareEvent(env, baseEvent({ payload: { blob: 'x'.repeat(5000) } }));
  assert.equal(r.ok, true);
  assert.ok(r.r2 && r.r2.startsWith('insights/'));
  assert.equal(env._puts.length, 1);
  assert.equal(env._puts[0].key, r.r2);
});

test('R2 enabled: small event still stored inline (no needless R2 put)', async () => {
  const env = fakeEnv({ r2: true, inlineMax: 4096 });
  const r = await prepareEvent(env, baseEvent());
  assert.equal(r.ok, true);
  assert.equal(r.r2, null);
  assert.equal(env._puts.length, 0);
});

test('tamper-evident: mismatched client content_id rejected (both modes)', async () => {
  for (const r2 of [false, true]) {
    const env = fakeEnv({ r2 });
    const r = await prepareEvent(env, baseEvent({ content_id: 'sha256:0000' }));
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'content_id_mismatch');
  }
});
