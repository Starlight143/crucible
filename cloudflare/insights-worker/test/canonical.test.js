// Canonical-JSON parity tests (node:test, zero dependencies — needs Node >=20
// for the global Web Crypto `crypto.subtle`).
//
// These fixtures are the SAME ones pinned on the Python side in
//   tests/test_run_insights/test_js_canonical_parity.py
// If a fixture's expected bytes disagree between the two files, cloud-side
// content_id dedup is broken.  The three `ANCHORS` below additionally pin the
// EXACT sha256 the Python `compute_content_id` produces, giving true
// cross-language lockstep (regenerate with the one-liner in README.md if the
// algorithm ever legitimately changes on both sides).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { canonicalJson, contentId } from '../src/canonical.js';

// (label, event, expected canonical JSON string) — must match Python byte-for-byte.
const FIXTURES = [
  ['trivial-empty', {}, '{}'],
  ['simple-flat', { a: 1, b: 'x' }, '{"a":1,"b":"x"}'],
  ['key-ordering', { z: 1, a: 2, m: 3 }, '{"a":2,"m":3,"z":1}'],
  [
    'nested-key-ordering',
    { outer: { z: 1, a: 2 }, alpha: 5 },
    '{"alpha":5,"outer":{"a":2,"z":1}}',
  ],
  ['nan-becomes-null', { score: NaN }, '{"score":null}'],
  ['posinf-becomes-null', { score: Infinity }, '{"score":null}'],
  ['neginf-becomes-null', { score: -Infinity }, '{"score":null}'],
  ['content-id-is-dropped', { content_id: 'sha256:deadbeef', a: 1 }, '{"a":1}'],
  ['unicode-cjk-preserved', { name: '嗨' }, '{"name":"嗨"}'],
  ['unicode-emoji-preserved', { name: '🚀' }, '{"name":"🚀"}'],
  ['list-of-strings', { tags: ['a', 'b', 'c'] }, '{"tags":["a","b","c"]}'],
  ['nested-list-with-nan', { vals: [1.0, NaN, 3.0] }, '{"vals":[1,null,3]}'],
  ['float-non-integer', { a: 0.5, b: 1.25 }, '{"a":0.5,"b":1.25}'],
  ['float-exponent-small', { a: 1e-7 }, '{"a":1e-7}'],
  ['float-exponent-large', { a: 1e21 }, '{"a":1e+21}'],
  ['negative-zero', { a: -0.0 }, '{"a":0}'],
  [
    'bool-and-null',
    { flag: true, missing: null, other: false },
    '{"flag":true,"missing":null,"other":false}',
  ],
];

for (const [label, event, expected] of FIXTURES) {
  test(`canonical parity: ${label}`, () => {
    const got = Buffer.from(canonicalJson(event));
    const exp = Buffer.from(expected, 'utf8');
    assert.ok(
      got.equals(exp),
      `\n  expected: ${JSON.stringify(expected)}\n  got:      ${got.toString('utf8')}`
    );
  });
}

// Cross-language content_id anchors — these hex digests were produced by the
// Python compute_content_id() for the identical input.  They MUST match.
const ANCHORS = [
  [
    'empty',
    {},
    'sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
  ],
  [
    'simple-flat',
    { a: 1, b: 'x' },
    'sha256:ecf9e98ec0641e23113ff3ce8bdc78d0ddd249886517fd4a7f68cc83d4e65667',
  ],
  [
    'realistic-event',
    {
      schema_version: 1,
      ts: '2026-06-03T00:00:00.000Z',
      run_id: 'r1',
      project_name: 'p',
      mode: 'Quant',
      kind: 'output_method',
      stage: '07',
      signals: ['mode:quant'],
      env_fingerprint: { arch: 'x86_64' },
      outcome: { status: 'success', score: 0.5 },
      payload: { note: 'hi' },
    },
    'sha256:21cd8c4b1d0a189645b1595cd3e2160da89cec290a3738db84ac02033ff504d0',
  ],
];

for (const [label, event, expected] of ANCHORS) {
  test(`content_id cross-language anchor: ${label}`, async () => {
    assert.equal(await contentId(event), expected);
  });
}

test('content_id is stable under key reorder', async () => {
  assert.equal(
    await contentId({ a: 1, b: 2, c: 3 }),
    await contentId({ c: 3, a: 1, b: 2 })
  );
});

test('content_id ignores a pre-existing content_id field', async () => {
  assert.equal(
    await contentId({ a: 1, b: 'x' }),
    await contentId({ a: 1, b: 'x', content_id: 'sha256:placeholder' })
  );
});

test('content_id format is sha256:<64 hex>', async () => {
  assert.match(await contentId({ a: 1 }), /^sha256:[0-9a-f]{64}$/);
});
