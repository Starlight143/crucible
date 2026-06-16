// Unit tests for the cloud-side secret backstop (src/validate.js).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { scanForSecrets } from '../src/validate.js';

test('clean text → null', () => {
  assert.equal(scanForSecrets(JSON.stringify({ a: 1, note: 'hello world' })), null);
});

test('non-string → null', () => {
  assert.equal(scanForSecrets(null), null);
  assert.equal(scanForSecrets(undefined), null);
});

test('detects Anthropic key', () => {
  assert.ok(scanForSecrets('sk-ant-' + 'a'.repeat(30)));
});

test('detects OpenRouter key', () => {
  assert.ok(scanForSecrets('sk-or-v1-' + 'a'.repeat(40)));
});

test('detects AWS access key id', () => {
  assert.ok(scanForSecrets('AKIAABCDEFGHIJ123456'));
});

test('detects GitHub PAT', () => {
  assert.ok(scanForSecrets('ghp_' + 'b'.repeat(36)));
});

test('detects Google API key', () => {
  assert.ok(scanForSecrets('AIza' + 'c'.repeat(35)));
});

test('a sha256 content_id is NOT a false positive', () => {
  // 64 hex chars, no provider prefix → must not match.
  assert.equal(scanForSecrets('sha256:' + 'a'.repeat(64)), null);
});
