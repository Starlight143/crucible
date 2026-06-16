// Unit tests for the GitHub OAuth helpers (oauth_github.js).  Network calls take
// an injectable fetchImpl so no real GitHub request is made.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildAuthorizeUrl,
  exchangeCode,
  fetchUser,
  contributorIdForGithub,
  parseCookies,
} from '../src/oauth_github.js';

test('buildAuthorizeUrl carries client_id, redirect, scope, state', () => {
  const u = new URL(
    buildAuthorizeUrl({ clientId: 'cid', redirectUri: 'https://w/cb', state: 'st' })
  );
  assert.equal(u.origin + u.pathname, 'https://github.com/login/oauth/authorize');
  assert.equal(u.searchParams.get('client_id'), 'cid');
  assert.equal(u.searchParams.get('redirect_uri'), 'https://w/cb');
  assert.equal(u.searchParams.get('scope'), 'read:user');
  assert.equal(u.searchParams.get('state'), 'st');
});

test('contributorIdForGithub keys on the immutable numeric id', () => {
  assert.equal(contributorIdForGithub(12345), 'gh_12345');
  assert.equal(contributorIdForGithub('99'), 'gh_99');
});

test('parseCookies parses pairs and isolates the state cookie', () => {
  const c = parseCookies('a=1; cruc_oauth_state=xyz; b=2');
  assert.equal(c.cruc_oauth_state, 'xyz');
  assert.equal(c.a, '1');
  assert.equal(c.b, '2');
});

test('parseCookies tolerates null / empty', () => {
  assert.deepEqual(parseCookies(null), {});
  assert.deepEqual(parseCookies(''), {});
});

test('exchangeCode returns the access token on success', async () => {
  const fetchImpl = async () => ({
    async json() {
      return { access_token: 'gho_abc', token_type: 'bearer' };
    },
  });
  const r = await exchangeCode(
    { clientId: 'c', clientSecret: 's', code: 'x', redirectUri: 'u' },
    fetchImpl
  );
  assert.equal(r.ok, true);
  assert.equal(r.accessToken, 'gho_abc');
});

test('exchangeCode surfaces a GitHub error', async () => {
  const fetchImpl = async () => ({
    async json() {
      return { error: 'bad_verification_code' };
    },
  });
  const r = await exchangeCode(
    { clientId: 'c', clientSecret: 's', code: 'x', redirectUri: 'u' },
    fetchImpl
  );
  assert.equal(r.ok, false);
  assert.match(r.reason, /bad_verification_code/);
});

test('fetchUser returns string id + login', async () => {
  const fetchImpl = async () => ({
    ok: true,
    async json() {
      return { id: 4242, login: 'octocat' };
    },
  });
  const r = await fetchUser('tok', fetchImpl);
  assert.equal(r.ok, true);
  assert.equal(r.id, '4242');
  assert.equal(r.login, 'octocat');
});

test('fetchUser reports an HTTP error', async () => {
  const fetchImpl = async () => ({
    ok: false,
    status: 401,
    async json() {
      return {};
    },
  });
  const r = await fetchUser('tok', fetchImpl);
  assert.equal(r.ok, false);
  assert.match(r.reason, /github_user_401/);
});
