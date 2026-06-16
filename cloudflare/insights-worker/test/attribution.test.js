// Unit tests for server-side attribution in prepareEvent() — the contributor_id
// and trust_state are stamped from the (server-resolved) attribution argument,
// never from the event body, and default to (null, 'approved') for backward
// compatibility with 2-arg callers.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { prepareEvent } from '../src/ingest.js';

function fakeEnv() {
  return { DB: { prepare: () => ({ bind: (...args) => ({ args }) }) } };
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

// The two attribution columns are bound LAST in the INSERT:
//   ... , contributor_id, trust_state
// so they are the final two bound args.
test('staged contribution stamps contributor_id + trust_state', async () => {
  const r = await prepareEvent(fakeEnv(), baseEvent(), {
    contributorId: 'alice',
    trustState: 'staged',
  });
  assert.equal(r.ok, true);
  assert.equal(r.stmt.args.at(-1), 'staged');
  assert.equal(r.stmt.args.at(-2), 'alice');
});

test('default (2-arg) → approved + null contributor (backward compatible)', async () => {
  const r = await prepareEvent(fakeEnv(), baseEvent());
  assert.equal(r.ok, true);
  assert.equal(r.stmt.args.at(-1), 'approved');
  assert.equal(r.stmt.args.at(-2), null);
});

test('an invalid trust_state falls back to approved', async () => {
  const r = await prepareEvent(fakeEnv(), baseEvent(), {
    contributorId: 'x',
    trustState: 'whatever',
  });
  assert.equal(r.ok, true);
  assert.equal(r.stmt.args.at(-1), 'approved');
  assert.equal(r.stmt.args.at(-2), 'x');
});

test('a client-supplied contributor field in the body is ignored', async () => {
  // The event body cannot influence attribution; only the 3rd arg can.
  const r = await prepareEvent(
    fakeEnv(),
    baseEvent({ contributor_id: 'attacker', trust_state: 'approved' }),
    { contributorId: 'real-user', trustState: 'staged' }
  );
  assert.equal(r.stmt.args.at(-2), 'real-user');
  assert.equal(r.stmt.args.at(-1), 'staged');
});
