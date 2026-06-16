// Unit tests for the strict contributor-upload conformance gate (event_shape.js).
// Zero deps; node:test.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  checkEventShape,
  VALID_KINDS,
  KIND_STREAM,
  VALID_MODES,
} from '../src/event_shape.js';

function ev(extra = {}) {
  return {
    schema_version: 1,
    ts: '2026-01-01T00:00:00.000Z',
    run_id: 'r',
    project_name: 'p',
    mode: 'Quant',
    kind: 'output_method',
    stream: 'output',
    ...extra,
  };
}

test('a conforming event passes', () => {
  assert.equal(checkEventShape(ev()).ok, true);
});

test('unknown mode is rejected', () => {
  const r = checkEventShape(ev({ mode: 'Trading' }));
  assert.equal(r.ok, false);
  assert.match(r.reason, /bad_mode/);
});

test('unknown kind is rejected', () => {
  const r = checkEventShape(ev({ kind: 'made_up' }));
  assert.equal(r.ok, false);
  assert.match(r.reason, /bad_kind/);
});

test('a known kind on the wrong stream is rejected', () => {
  const r = checkEventShape(ev({ kind: 'output_method', stream: 'error' }));
  assert.equal(r.ok, false);
  assert.match(r.reason, /kind_stream_mismatch/);
});

test('non-integer / non-numeric schema_version is rejected', () => {
  assert.equal(checkEventShape(ev({ schema_version: 1.5 })).ok, false);
  assert.equal(checkEventShape(ev({ schema_version: '1' })).ok, false);
  assert.equal(checkEventShape(ev({ schema_version: 0 })).ok, false);
});

test('a malformed ts is rejected', () => {
  assert.equal(checkEventShape(ev({ ts: '2026-01-01 00:00:00' })).ok, false);
  assert.equal(checkEventShape(ev({ ts: 'not-a-date' })).ok, false);
  assert.equal(checkEventShape(ev({ ts: '2026-13-99T00:00:00Z' })).ok, false);
});

test('ts without fractional seconds is accepted', () => {
  assert.equal(checkEventShape(ev({ ts: '2026-01-01T00:00:00Z' })).ok, true);
});

test('empty run_id / project_name is rejected', () => {
  assert.equal(checkEventShape(ev({ run_id: '' })).ok, false);
  assert.equal(checkEventShape(ev({ project_name: '' })).ok, false);
});

test('bad outcome status / score is rejected; valid outcome accepted', () => {
  assert.equal(checkEventShape(ev({ outcome: { status: 'great' } })).ok, false);
  assert.equal(checkEventShape(ev({ outcome: { score: Infinity } })).ok, false);
  assert.equal(checkEventShape(ev({ outcome: { status: 'success', score: 0.5 } })).ok, true);
});

// ── Structural pins against the schema.py EventKind enum ──

test('every EventKind maps to a valid stream (lockstep)', () => {
  const streams = new Set(['output', 'error', 'debate', 'params']);
  for (const k of VALID_KINDS) {
    assert.ok(streams.has(KIND_STREAM[k]), `kind ${k} has no/invalid stream mapping`);
  }
  // KIND_STREAM keys and VALID_KINDS must be exactly the same set.
  assert.deepEqual(new Set(Object.keys(KIND_STREAM)), VALID_KINDS);
});

test('all nine schema.py EventKinds are present and no extras', () => {
  const expected = [
    'output_method',
    'error_record',
    'direction_debate_rejection',
    'runtime_params',
    'direction_debate_finding',
    'direction_debate_verdict',
    'provider_cooldown_engaged',
    'provider_health_summary',
    'direction_debate_degraded_proceed',
  ];
  for (const k of expected) assert.ok(VALID_KINDS.has(k), `missing kind ${k}`);
  assert.equal(VALID_KINDS.size, expected.length);
});

test('the four pipeline modes are present', () => {
  for (const m of ['Quant', 'SaaS', 'Agent', 'Scientist']) {
    assert.ok(VALID_MODES.has(m), `missing mode ${m}`);
  }
  assert.equal(VALID_MODES.size, 4);
});
