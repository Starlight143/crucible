// GitHub OAuth (Authorization Code flow) — self-service contributor onboarding.
//
// The signup page links to /oauth/github/start, which redirects to GitHub with a
// random CSRF `state` stored in an HttpOnly cookie.  GitHub redirects back to
// /oauth/github/callback, where the Worker verifies the state, exchanges the
// code for a short-lived access token, reads the user's stable numeric id, and
// mints a 'contributor' token (see index.js).  The access token is used once and
// never stored; only the issued Crucible token's hash is persisted.
//
// Every network call takes an injectable `fetchImpl` so the callback can be
// unit-tested without hitting GitHub (test/oauth.test.js).  GitHub requires a
// User-Agent header on API requests, so one is always sent.

const GH_AUTHORIZE = 'https://github.com/login/oauth/authorize';
const GH_TOKEN = 'https://github.com/login/oauth/access_token';
const GH_USER = 'https://api.github.com/user';
const UA = 'crucible-insights-oauth/1.0';

/**
 * Build the GitHub authorize URL.  `read:user` is the minimal scope needed to
 * read the user's numeric id + login for attribution.
 * @param {{ clientId: string, redirectUri: string, state: string }} o
 * @returns {string}
 */
export function buildAuthorizeUrl({ clientId, redirectUri, state }) {
  const u = new URL(GH_AUTHORIZE);
  u.searchParams.set('client_id', clientId);
  u.searchParams.set('redirect_uri', redirectUri);
  u.searchParams.set('scope', 'read:user');
  u.searchParams.set('state', state);
  u.searchParams.set('allow_signup', 'true');
  return u.toString();
}

/**
 * Exchange an authorization code for an access token.
 * @param {{ clientId: string, clientSecret: string, code: string, redirectUri: string }} o
 * @param {typeof fetch} [fetchImpl]
 * @returns {Promise<{ ok: true, accessToken: string } | { ok: false, reason: string }>}
 */
export async function exchangeCode(
  { clientId, clientSecret, code, redirectUri },
  fetchImpl = fetch
) {
  let res;
  try {
    res = await fetchImpl(GH_TOKEN, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'User-Agent': UA,
      },
      body: JSON.stringify({
        client_id: clientId,
        client_secret: clientSecret,
        code,
        redirect_uri: redirectUri,
      }),
    });
  } catch {
    return { ok: false, reason: 'token_exchange_network_error' };
  }
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (!data || typeof data.access_token !== 'string' || !data.access_token) {
    return {
      ok: false,
      reason: data && data.error ? String(data.error).slice(0, 64) : 'token_exchange_failed',
    };
  }
  return { ok: true, accessToken: data.access_token };
}

/**
 * Read the authenticated user's stable numeric id and login.
 * @param {string} accessToken
 * @param {typeof fetch} [fetchImpl]
 * @returns {Promise<{ ok: true, id: string, login: string } | { ok: false, reason: string }>}
 */
export async function fetchUser(accessToken, fetchImpl = fetch) {
  let res;
  try {
    res = await fetchImpl(GH_USER, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Accept: 'application/vnd.github+json',
        'User-Agent': UA,
        'X-GitHub-Api-Version': '2022-11-28',
      },
    });
  } catch {
    return { ok: false, reason: 'github_user_network_error' };
  }
  if (!res.ok) return { ok: false, reason: `github_user_${res.status}` };
  let u = null;
  try {
    u = await res.json();
  } catch {
    u = null;
  }
  if (!u || (typeof u.id !== 'number' && typeof u.id !== 'string')) {
    return { ok: false, reason: 'github_user_malformed' };
  }
  const login = typeof u.login === 'string' && u.login ? u.login : String(u.id);
  return { ok: true, id: String(u.id), login };
}

/**
 * Stable contributor id for a GitHub account.  Keyed on the immutable numeric
 * id (survives username changes), never on the mutable login.
 * @param {string|number} id
 * @returns {string}
 */
export function contributorIdForGithub(id) {
  return `gh_${id}`;
}

/**
 * Parse a Cookie header into a plain object.
 * @param {string|null|undefined} header
 * @returns {Record<string,string>}
 */
export function parseCookies(header) {
  const out = {};
  if (typeof header !== 'string') return out;
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i < 0) continue;
    const k = part.slice(0, i).trim();
    if (!k) continue;
    out[k] = part.slice(i + 1).trim();
  }
  return out;
}
