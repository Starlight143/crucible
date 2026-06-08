// Unit tests for the bounded request-body reader (gzip-bomb / oversized-body
// DoS guard in src/index.js).  Builds web ReadableStreams over fixture bytes
// and asserts readBodyText() honours the decoded-size cap.  Zero external deps
// (global ReadableStream / DecompressionStream / TextEncoder, Node >= 20).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';

import { readBodyText, BodyTooLargeError } from '../src/index.js';

function streamFrom(bytes) {
  return new ReadableStream({
    start(c) {
      // Emit in two chunks to exercise the accumulation/cap path mid-stream.
      const mid = Math.floor(bytes.length / 2);
      c.enqueue(bytes.subarray(0, mid));
      c.enqueue(bytes.subarray(mid));
      c.close();
    },
  });
}

function fakeReq(bytes, { gzip = false } = {}) {
  return {
    body: streamFrom(bytes),
    headers: {
      get: (k) => (k.toLowerCase() === 'content-encoding' && gzip ? 'gzip' : null),
    },
  };
}

test('plain body under the cap is returned intact', async () => {
  const text = 'x'.repeat(1000);
  const out = await readBodyText(fakeReq(new TextEncoder().encode(text)), 10_000);
  assert.equal(out, text);
});

test('plain body over the cap throws BodyTooLargeError', async () => {
  const bytes = new TextEncoder().encode('x'.repeat(5000));
  await assert.rejects(
    () => readBodyText(fakeReq(bytes), 1000),
    (e) => e instanceof BodyTooLargeError
  );
});

test('gzip body under the cap decompresses correctly', async () => {
  const payload = JSON.stringify({ events: [{ a: 1 }] });
  const gz = new Uint8Array(gzipSync(Buffer.from(payload)));
  const out = await readBodyText(fakeReq(gz, { gzip: true }), 1_000_000);
  assert.equal(out, payload);
});

test('gzip BOMB over the cap throws (counts DECODED size, not compressed)', async () => {
  // 5 MiB of one repeated byte → only a few KB gzip'd, but 5 MiB decoded.
  const big = Buffer.alloc(5 * 1024 * 1024, 0x61);
  const gz = new Uint8Array(gzipSync(big));
  assert.ok(gz.length < 100_000, 'fixture should be a small compressed bomb');
  await assert.rejects(
    () => readBodyText(fakeReq(gz, { gzip: true }), 1024 * 1024),
    (e) => e instanceof BodyTooLargeError
  );
});

test('missing body returns empty string', async () => {
  const out = await readBodyText({ body: null, headers: { get: () => null } }, 1000);
  assert.equal(out, '');
});
