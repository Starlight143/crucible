// Crucible Run Insights Worker — entry point / router.
//
// Implements the FROZEN HTTP API (backends.py module docstring) plus the Phase A
// opening-up surface (see OPENING_UP.md):
//   GET  /                                         signup page (HTML, no auth)
//   GET  /console                                  admin console (HTML, no auth; token entered client-side)
//   GET  /health                                   liveness (no auth)
//   GET  /oauth/github/start                       begin GitHub OAuth signup (no auth)
//   GET  /oauth/github/callback                    finish OAuth → issue contributor token (no auth)
//   POST /v1/insights/events                       flat body {event:{...}}   (ingest|contributor|admin)
//   POST /v1/insights/batch                        gzip body {events:[...]}  (ingest|contributor|admin)
//   GET  /v1/insights/distilled?kind=              distilled artifacts       (read|contributor|admin)
//   GET  /v1/insights/corpus/stats                 aggregate corpus counts   (read|contributor|admin)
//   GET  /v1/insights/corpus?...                   metadata rows, no payload (read|contributor|admin)
//   GET  /v1/insights/events?...                   raw query                 (admin)
//   GET  /v1/insights/events/:content_id           raw single               (admin)
//   GET  /v1/insights/runs/:run_id/summary         run summary              (admin)
//   GET  /v1/admin/stats                           corpus-wide counts        (admin)
//   POST /v1/admin/tokens                          issue token              (admin)
//   GET  /v1/admin/tokens                          list tokens              (admin)
//   POST /v1/admin/tokens/:token_id/revoke         revoke token             (admin)
//   GET  /v1/admin/staged?limit=&cursor=           list staged events       (admin)
//   POST /v1/admin/events/promote                  staged → approved        (admin)
//   POST /v1/admin/events/reject                   delete staged            (admin)
//   POST /v1/admin/events/delete                   delete ANY events        (admin)
//   GET  /v1/admin/contributors                    list contributors        (admin)
//   POST /v1/admin/contributors/:id                set reputation/status    (admin)
//   POST /v1/admin/distilled                       publish a distilled doc  (admin)
//
// Auth: authenticate() resolves the bearer to {contributorId, scope, tokenId,
// dailyQuota}.  Scopes — ingest: POST events only (lands trust_state='staged');
// read: GET distilled + corpus aggregates/metadata (raw payloads never exposed);
// contributor: ingest + read, self-issued via GitHub OAuth, writes auto-approve;
// admin: everything incl. delete.  Legacy single secret authenticates as admin.
//
// Idempotency: content_id PRIMARY KEY + INSERT OR IGNORE.  Server-to-server only;
// no CORS by design (add explicitly if a browser dashboard is introduced).

import { authenticate, timingSafeEqual } from './auth.js';
import { prepareEvent } from './ingest.js';
import { consumeQuota } from './quota.js';
import {
  issueToken,
  revokeToken,
  listTokens,
  listStaged,
  promoteEvents,
  rejectEvents,
  deleteEvents,
  eventStats,
  listContributors,
  setContributor,
  publishDistilled,
  getDistilled,
} from './admin.js';
import { corpusStats, corpusList } from './corpus.js';
import {
  buildAuthorizeUrl,
  exchangeCode,
  fetchUser,
  contributorIdForGithub,
  parseCookies,
} from './oauth_github.js';
import {
  SIGNUP_HTML,
  CONSOLE_HTML,
  signupDisabledPage,
  resultPage,
  tokenResultPage,
} from './pages.js';

const SERVICE = 'crucible-insights-worker';
const VERSION = '0.1.0';

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
  });
}

function html(body, status = 200, setCookie) {
  const headers = { 'Content-Type': 'text/html; charset=utf-8' };
  if (setCookie) headers['Set-Cookie'] = setCookie;
  return new Response(body, { status, headers });
}

// Short random hex for the OAuth CSRF state (single-use, carried in a cookie).
function randHex(nBytes) {
  const a = new Uint8Array(nBytes);
  crypto.getRandomValues(a);
  return Array.from(a)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

const OAUTH_STATE_COOKIE = 'cruc_oauth_state';
function stateCookie(value, maxAge) {
  return `${OAUTH_STATE_COOKIE}=${value}; HttpOnly; Secure; SameSite=Lax; Path=/oauth; Max-Age=${maxAge}`;
}

// ── Opaque pagination cursor: base64url("<ts>\0<content_id>") ──
function b64urlEncode(s) {
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}
function b64urlDecode(s) {
  const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
  return atob(s.replace(/-/g, '+').replace(/_/g, '/') + pad);
}
function encodeCursor(ts, cid) {
  return b64urlEncode(`${ts} ${cid}`);
}
function decodeCursor(c) {
  try {
    const s = b64urlDecode(c);
    const i = s.indexOf(' ');
    if (i < 0) return null;
    return { ts: s.slice(0, i), cid: s.slice(i + 1) };
  } catch {
    return null;
  }
}

// Hard cap on the DECODED (post-decompression) request-body size.  Guards
// against gzip bombs and oversized bodies exhausting Worker memory.  The POST
// endpoints are authenticated, so this is defense-in-depth — and it contains the
// blast radius if a bearer token ever leaks.  Overridable via MAX_BATCH_BYTES.
const MAX_BODY_BYTES_DEFAULT = 8 * 1024 * 1024; // 8 MiB

export class BodyTooLargeError extends Error {
  constructor() {
    super('request body exceeds the configured limit');
    this.name = 'BodyTooLargeError';
  }
}

function clampMaxBytes(raw) {
  const n = parseInt(raw || '', 10);
  return Number.isFinite(n) && n > 0 ? n : MAX_BODY_BYTES_DEFAULT;
}

/**
 * Read the (optionally gzip'd) request body as text, aborting once the DECODED
 * size exceeds maxBytes.  Unlike `new Response(stream).text()`, this bounds the
 * decompressed output so a small gzip bomb cannot inflate to gigabytes in
 * memory.  Throws {@link BodyTooLargeError} when the cap is exceeded.
 * @param {Request} req
 * @param {number} maxBytes
 * @returns {Promise<string>}
 */
export async function readBodyText(req, maxBytes) {
  if (!req.body) return '';
  const enc = (req.headers.get('Content-Encoding') || '').toLowerCase();
  const stream = enc.includes('gzip')
    ? req.body.pipeThrough(new DecompressionStream('gzip'))
    : req.body;
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let text = '';
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        try { await reader.cancel(); } catch { /* ignore */ }
        throw new BodyTooLargeError();
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
  } finally {
    try { reader.releaseLock(); } catch { /* ignore */ }
  }
  return text;
}

/**
 * Finish the GitHub OAuth signup: verify the CSRF state cookie, exchange the
 * code, read the GitHub identity, refuse banned accounts, then (re)issue a
 * single 'contributor' token and show it once.  All failures render a friendly
 * HTML page and always clear the state cookie.
 * @param {Request} req
 * @param {object} env
 * @returns {Promise<Response>}
 */
async function handleGithubCallback(req, env) {
  const url = new URL(req.url);
  const clearCookie = stateCookie('', 0);
  const fail = (status, title, msg) => html(resultPage(title, msg), status, clearCookie);

  const clientId = env.GITHUB_OAUTH_CLIENT_ID;
  const clientSecret = env.GITHUB_OAUTH_CLIENT_SECRET;
  if (!clientId || !clientSecret) return html(signupDisabledPage(), 503);

  const code = url.searchParams.get('code');
  const state = url.searchParams.get('state');
  const cookies = parseCookies(req.headers.get('Cookie'));
  const expected = cookies[OAUTH_STATE_COOKIE] || '';
  if (!code || !state || !expected || !timingSafeEqual(state, expected)) {
    return fail(
      400,
      'Sign-in could not be verified',
      'The sign-in state was missing or expired. Please return to the signup page and start again.'
    );
  }

  const redirectUri = url.origin + '/oauth/github/callback';
  const ex = await exchangeCode({ clientId, clientSecret, code, redirectUri });
  if (!ex.ok) {
    return fail(502, 'GitHub sign-in failed', 'Could not complete the GitHub handshake. Please try again.');
  }
  const gu = await fetchUser(ex.accessToken);
  if (!gu.ok) {
    return fail(502, 'GitHub sign-in failed', 'Could not read your GitHub identity. Please try again.');
  }

  const contributorId = contributorIdForGithub(gu.id);

  // Banned contributors are refused before any token is minted.
  let banned = null;
  try {
    banned = await env.DB.prepare('SELECT status FROM contributors WHERE contributor_id = ?')
      .bind(contributorId)
      .first();
  } catch {
    banned = null;
  }
  if (banned && banned.status === 'banned') {
    return fail(
      403,
      'Access denied',
      'This account is not permitted to contribute. Contact the operator if you believe this is a mistake.'
    );
  }

  // Re-issue: revoke any existing active contributor token for this account so a
  // repeat sign-in always yields one working token (raw tokens are shown once).
  try {
    await env.DB.prepare(
      "UPDATE api_tokens SET status = 'revoked' WHERE contributor_id = ? AND status = 'active' AND scope = 'contributor'"
    )
      .bind(contributorId)
      .run();
  } catch {
    /* non-fatal — issuance below still proceeds */
  }

  const q = parseInt(env.SIGNUP_DAILY_QUOTA || '2000', 10);
  const dailyQuota = Number.isFinite(q) && q > 0 ? q : null;
  const issued = await issueToken(env, {
    contributorId,
    label: `github:${gu.login}`,
    scope: 'contributor',
    dailyQuota,
  });
  if (!issued.ok) {
    return fail(500, 'Could not issue a token', 'Something went wrong issuing your token. Please try again.');
  }

  // Record the contributor as active (best-effort; reputation untouched).
  try {
    await setContributor(env, contributorId, { status: 'active' });
  } catch {
    /* non-fatal */
  }

  return html(tokenResultPage(issued.token, url.origin, gu.login), 200, clearCookie);
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;

    // ── Liveness (no auth, no data).  The Python client pings /health. ──
    if (req.method === 'GET' && p === '/health') {
      return json({ service: SERVICE, version: VERSION, status: 'ok' });
    }

    // ── Public HTML pages, served same-origin from the Worker (no CORS). ──
    if (req.method === 'GET' && p === '/') {
      return html(SIGNUP_HTML);
    }
    if (req.method === 'GET' && (p === '/console' || p === '/admin')) {
      return html(CONSOLE_HTML);
    }

    // ── GitHub OAuth self-service signup (no bearer; CSRF-protected). ──
    if (req.method === 'GET' && p === '/oauth/github/start') {
      const clientId = env.GITHUB_OAUTH_CLIENT_ID;
      if (!clientId || !env.GITHUB_OAUTH_CLIENT_SECRET) {
        return html(signupDisabledPage(), 503);
      }
      const state = randHex(16);
      const redirectUri = new URL(req.url).origin + '/oauth/github/callback';
      return new Response(null, {
        status: 302,
        headers: {
          Location: buildAuthorizeUrl({ clientId, redirectUri, state }),
          'Set-Cookie': stateCookie(state, 600),
        },
      });
    }
    if (req.method === 'GET' && p === '/oauth/github/callback') {
      return handleGithubCallback(req, env);
    }

    // ── Auth gate for everything under /v1 ──
    const auth = await authenticate(req, env);
    if (!auth) {
      return new Response('unauthorized', { status: 401 });
    }

    // Scope helpers (enforced per-route below).  'contributor' = ingest + read.
    const isAdmin = auth.scope === 'admin';
    const canIngest =
      auth.scope === 'ingest' || auth.scope === 'contributor' || isAdmin;
    const canRead =
      auth.scope === 'read' || auth.scope === 'contributor' || isAdmin;
    const forbidden = (need) =>
      json({ error: 'forbidden', required: need, scope: auth.scope }, 403);
    // Server-side attribution (NEVER derived from the request body).  Owner/admin
    // and OAuth-vetted contributors land 'approved' — the operator chose
    // auto-accept-if-well-formed, with bad rows removed via admin delete.  The
    // legacy single-purpose 'ingest' scope still quarantines to 'staged'.
    const trustState =
      isAdmin || auth.scope === 'contributor' ? 'approved' : 'staged';
    const attribution = {
      contributorId: auth.contributorId,
      trustState,
    };

    try {
      const maxBodyBytes = clampMaxBytes(env.MAX_BATCH_BYTES);

      // Parse a JSON body with the bounded reader (shared by admin POST routes).
      const parseBody = async () => {
        try {
          return { ok: true, body: JSON.parse(await readBodyText(req, maxBodyBytes)) };
        } catch (e) {
          if (e instanceof BodyTooLargeError) {
            return {
              ok: false,
              res: json({ error: 'payload_too_large', max_bytes: maxBodyBytes }, 413),
            };
          }
          return { ok: false, res: json({ error: 'invalid_json' }, 400) };
        }
      };

      // ── Admin: token management (admin scope only) ──
      const revokeMatch = p.match(/^\/v1\/admin\/tokens\/([^/]+)\/revoke$/);
      if (req.method === 'POST' && revokeMatch) {
        if (!isAdmin) return forbidden('admin');
        const r = await revokeToken(env, decodeURIComponent(revokeMatch[1]));
        return json(r, r.ok ? 200 : 404);
      }
      if (req.method === 'POST' && p === '/v1/admin/tokens') {
        if (!isAdmin) return forbidden('admin');
        const pb = await parseBody();
        if (!pb.ok) return pb.res;
        const r = await issueToken(env, {
          contributorId: pb.body?.contributor_id,
          label: pb.body?.label,
          scope: pb.body?.scope,
          dailyQuota: pb.body?.daily_quota,
        });
        return json(r, r.ok ? 200 : 422);
      }
      if (req.method === 'GET' && p === '/v1/admin/tokens') {
        if (!isAdmin) return forbidden('admin');
        return json(await listTokens(env));
      }

      // ── Admin: curation (admin scope only) ──
      if (req.method === 'GET' && p === '/v1/admin/staged') {
        if (!isAdmin) return forbidden('admin');
        return json(
          await listStaged(env, {
            limit: url.searchParams.get('limit'),
            cursor: url.searchParams.get('cursor') || undefined,
          })
        );
      }
      if (req.method === 'POST' && p === '/v1/admin/events/promote') {
        if (!isAdmin) return forbidden('admin');
        const pb = await parseBody();
        if (!pb.ok) return pb.res;
        const r = await promoteEvents(env, {
          contentIds: pb.body?.content_ids,
          contributorId: pb.body?.contributor_id,
        });
        return json(r, r.ok ? 200 : 422);
      }
      if (req.method === 'POST' && p === '/v1/admin/events/reject') {
        if (!isAdmin) return forbidden('admin');
        const pb = await parseBody();
        if (!pb.ok) return pb.res;
        const r = await rejectEvents(env, { contentIds: pb.body?.content_ids });
        return json(r, r.ok ? 200 : 422);
      }
      if (req.method === 'POST' && p === '/v1/admin/events/delete') {
        if (!isAdmin) return forbidden('admin');
        const pb = await parseBody();
        if (!pb.ok) return pb.res;
        const r = await deleteEvents(env, {
          contentIds: pb.body?.content_ids,
          runId: pb.body?.run_id,
          contributorId: pb.body?.contributor_id,
        });
        return json(r, r.ok ? 200 : 422);
      }
      if (req.method === 'GET' && p === '/v1/admin/stats') {
        if (!isAdmin) return forbidden('admin');
        return json(await eventStats(env));
      }
      if (req.method === 'GET' && p === '/v1/admin/contributors') {
        if (!isAdmin) return forbidden('admin');
        return json(await listContributors(env));
      }
      const contribMatch = p.match(/^\/v1\/admin\/contributors\/([^/]+)$/);
      if (req.method === 'POST' && contribMatch) {
        if (!isAdmin) return forbidden('admin');
        const pb = await parseBody();
        if (!pb.ok) return pb.res;
        const r = await setContributor(env, decodeURIComponent(contribMatch[1]), {
          reputation: pb.body?.reputation,
          status: pb.body?.status,
        });
        return json(r, r.ok ? 200 : 422);
      }
      if (req.method === 'POST' && p === '/v1/admin/distilled') {
        if (!isAdmin) return forbidden('admin');
        const pb = await parseBody();
        if (!pb.ok) return pb.res;
        const r = await publishDistilled(env, {
          kind: pb.body?.kind,
          payload: pb.body?.payload,
        });
        return json(r, r.ok ? 200 : 422);
      }

      // POST /v1/insights/events — flat body {event:{...}}  (ingest|admin)
      if (req.method === 'POST' && p === '/v1/insights/events') {
        if (!canIngest) return forbidden('ingest');
        let parsed;
        try {
          parsed = JSON.parse(await readBodyText(req, maxBodyBytes));
        } catch (e) {
          if (e instanceof BodyTooLargeError) {
            return json({ error: 'payload_too_large', max_bytes: maxBodyBytes }, 413);
          }
          return json({ error: 'invalid_json' }, 400);
        }
        const r = await prepareEvent(env, parsed?.event, attribution, {
          strict: !isAdmin,
        });
        if (!r.ok) {
          return json({ ok: false, reason: r.reason, expected: r.expected }, 422);
        }
        const q = await consumeQuota(env, auth.tokenId, auth.dailyQuota, 1);
        if (!q.ok) {
          return json(
            { error: 'quota_exceeded', used: q.used, daily_quota: q.dailyQuota },
            429
          );
        }
        await r.stmt.run();
        return json({ ok: true, content_id: r.content_id });
      }

      // POST /v1/insights/batch — gzip body {events:[...]}  (ingest|admin)
      if (req.method === 'POST' && p === '/v1/insights/batch') {
        if (!canIngest) return forbidden('ingest');
        let text;
        try {
          text = await readBodyText(req, maxBodyBytes);
        } catch (e) {
          if (e instanceof BodyTooLargeError) {
            return json({ error: 'payload_too_large', max_bytes: maxBodyBytes }, 413);
          }
          return json({ error: 'decompress_failed' }, 400);
        }
        let parsed;
        try {
          parsed = JSON.parse(text);
        } catch {
          return json({ error: 'invalid_json' }, 400);
        }
        const events = parsed?.events;
        if (!Array.isArray(events)) {
          return json({ error: 'events_must_be_array' }, 400);
        }
        const maxBatch = parseInt(env.MAX_BATCH_SIZE || '100', 10);
        if (events.length > maxBatch) {
          return json({ error: 'batch_too_large', max: maxBatch }, 413);
        }
        const stmts = [];
        const accepted = [];
        const rejected = [];
        for (const ev of events) {
          const r = await prepareEvent(env, ev, attribution, { strict: !isAdmin });
          if (r.ok) {
            stmts.push(r.stmt);
            accepted.push(r.content_id);
          } else {
            rejected.push({ reason: r.reason, content_id: ev?.content_id ?? null });
          }
        }
        if (accepted.length) {
          const q = await consumeQuota(env, auth.tokenId, auth.dailyQuota, accepted.length);
          if (!q.ok) {
            return json(
              { error: 'quota_exceeded', used: q.used, daily_quota: q.dailyQuota },
              429
            );
          }
          await env.DB.batch(stmts);
        }
        return json({ ingested: accepted.length, rejected });
      }

      // GET /v1/insights/distilled — distilled artifacts (read|contributor|admin)
      if (req.method === 'GET' && p === '/v1/insights/distilled') {
        if (!canRead) return forbidden('read');
        return json(
          await getDistilled(env, { kind: url.searchParams.get('kind') || undefined })
        );
      }

      // GET /v1/insights/corpus/stats — aggregate corpus shape (read|contributor|admin)
      if (req.method === 'GET' && p === '/v1/insights/corpus/stats') {
        if (!canRead) return forbidden('read');
        return json(await corpusStats(env));
      }
      // GET /v1/insights/corpus — metadata rows only, NO payloads / identity
      if (req.method === 'GET' && p === '/v1/insights/corpus') {
        if (!canRead) return forbidden('read');
        return json(
          await corpusList(env, {
            stream: url.searchParams.get('stream') || undefined,
            mode: url.searchParams.get('mode') || undefined,
            runId: url.searchParams.get('run_id') || undefined,
            cursor: url.searchParams.get('cursor') || undefined,
            limit: url.searchParams.get('limit') || undefined,
          })
        );
      }

      // GET /v1/insights/events/:content_id  (admin; must precede the list route)
      let m = p.match(/^\/v1\/insights\/events\/(.+)$/);
      if (req.method === 'GET' && m) {
        if (!isAdmin) return forbidden('admin');
        const cid = decodeURIComponent(m[1]);
        const row = await env.DB.prepare(
          'SELECT * FROM insight_events WHERE content_id = ?'
        )
          .bind(cid)
          .first();
        if (!row) return new Response('not found', { status: 404 });
        let event = null;
        if (row.payload_inline) {
          try {
            event = JSON.parse(row.payload_inline);
          } catch {
            /* leave null if corrupt */
          }
        } else if (row.payload_r2_key) {
          const obj = await env.BLOBS.get(row.payload_r2_key);
          if (obj) {
            try {
              event = JSON.parse(await obj.text());
            } catch {
              /* leave null if corrupt */
            }
          }
        }
        return json({ ...row, event });
      }

      // GET /v1/insights/runs/:run_id/summary  (admin)
      m = p.match(/^\/v1\/insights\/runs\/([^/]+)\/summary$/);
      if (req.method === 'GET' && m) {
        if (!isAdmin) return forbidden('admin');
        const runId = decodeURIComponent(m[1]);
        const totals = await env.DB.prepare(
          'SELECT COUNT(*) AS total, MIN(ts) AS first_ts, MAX(ts) AS last_ts ' +
            'FROM insight_events WHERE run_id = ?'
        )
          .bind(runId)
          .first();
        const breakdown = await env.DB.prepare(
          'SELECT stream, outcome_status, COUNT(*) AS n FROM insight_events ' +
            'WHERE run_id = ? GROUP BY stream, outcome_status'
        )
          .bind(runId)
          .all();
        return json({
          run_id: runId,
          total: totals?.total ?? 0,
          first_ts: totals?.first_ts ?? null,
          last_ts: totals?.last_ts ?? null,
          breakdown: breakdown.results ?? [],
        });
      }

      // GET /v1/insights/events — list/query with stable (ts, content_id) cursor (admin)
      if (req.method === 'GET' && p === '/v1/insights/events') {
        if (!isAdmin) return forbidden('admin');
        const stream = url.searchParams.get('stream');
        const runId = url.searchParams.get('run_id');
        const trustStateFilter = url.searchParams.get('trust_state');
        const contributorFilter = url.searchParams.get('contributor_id');
        const since = url.searchParams.get('since');
        const cursorRaw = url.searchParams.get('cursor');
        const cur = cursorRaw ? decodeCursor(cursorRaw) : null;
        let limit = parseInt(url.searchParams.get('limit') || '100', 10);
        if (!Number.isFinite(limit) || limit <= 0) limit = 100;
        limit = Math.min(limit, 1000);

        const where = [];
        const binds = [];
        if (stream) {
          where.push('stream = ?');
          binds.push(stream);
        }
        if (runId) {
          where.push('run_id = ?');
          binds.push(runId);
        }
        if (trustStateFilter) {
          where.push('trust_state = ?');
          binds.push(trustStateFilter);
        }
        if (contributorFilter) {
          where.push('contributor_id = ?');
          binds.push(contributorFilter);
        }
        if (since) {
          where.push('ts > ?');
          binds.push(since);
        }
        if (cur) {
          where.push('(ts > ? OR (ts = ? AND content_id > ?))');
          binds.push(cur.ts, cur.ts, cur.cid);
        }
        const sql =
          'SELECT * FROM insight_events' +
          (where.length ? ' WHERE ' + where.join(' AND ') : '') +
          ' ORDER BY ts ASC, content_id ASC LIMIT ?';
        binds.push(limit);
        const res = await env.DB.prepare(sql)
          .bind(...binds)
          .all();
        const rows = res.results ?? [];
        let next = null;
        if (rows.length === limit) {
          const last = rows[rows.length - 1];
          next = encodeCursor(last.ts, last.content_id);
        }
        return json({ events: rows, next_cursor: next });
      }

      return new Response('not found', { status: 404 });
    } catch (e) {
      // Visible via `wrangler tail`.  Never leak internals in the response.
      console.log(`error ${req.method} ${p}: ${e && e.stack ? e.stack : e}`);
      return json({ error: 'internal_error' }, 500);
    }
  },
};
