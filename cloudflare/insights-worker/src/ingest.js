// Event ingestion — validation, tamper-evidence, R2 spill, and the D1 insert
// statement (INSERT OR IGNORE → natural idempotent dedup on content_id).
//
// prepareEvent() does everything EXCEPT executing the D1 statement: it returns
// the bound statement so the caller can either run it directly (single event)
// or hand many to env.DB.batch() (batch endpoint, one transaction).

import { contentId } from './canonical.js';

const INLINE_DEFAULT = 4096;
// D1-only safety ceiling.  When no R2 bucket is bound (the default, no-credit-
// card deployment), the full event JSON is stored inline in D1.  Cloudflare D1
// caps a single column value at ~1 MB, so an event larger than this conservative
// bound is rejected (kept in the durable local ledger, the source of truth)
// rather than failing the whole D1 insert.  Real insight events are a few KB —
// this is pure defense-in-depth.
const D1_VALUE_CEILING = 950_000;
const VALID_STREAMS = new Set(['output', 'error', 'debate', 'params']);
const REQUIRED = [
  'stream',
  'ts',
  'run_id',
  'project_name',
  'mode',
  'kind',
  'schema_version',
];

/**
 * @typedef {Object} PreparedEvent
 * @property {boolean} ok
 * @property {string} [reason]
 * @property {string} [expected]   // recomputed content_id on mismatch
 * @property {string} [content_id]
 * @property {string|null} [r2]    // R2 key when the payload spilled
 * @property {object} [stmt]       // bound D1 prepared statement
 */

/**
 * Validate, canonicalise, and build the D1 insert for one event.
 * Storage routing (R2 is OPTIONAL):
 *   • R2 bound (env.BLOBS) AND full event > INLINE_MAX_BYTES → spill to R2.
 *   • otherwise → store the full JSON inline in D1 (rejected if it exceeds the
 *     D1 per-value ceiling, so an oversized event never fails the whole batch).
 * @param {{ DB: any, BLOBS?: any, INLINE_MAX_BYTES?: string }} env
 * @param {Record<string, unknown>} ev
 * @returns {Promise<PreparedEvent>}
 */
export async function prepareEvent(env, ev) {
  if (!ev || typeof ev !== 'object' || Array.isArray(ev)) {
    return { ok: false, reason: 'invalid_event' };
  }
  for (const f of REQUIRED) {
    if (ev[f] === undefined || ev[f] === null) {
      return { ok: false, reason: `missing_field:${f}` };
    }
  }
  if (!VALID_STREAMS.has(ev.stream)) {
    return { ok: false, reason: `bad_stream:${ev.stream}` };
  }

  // Tamper-evidence: recompute the content_id from the canonical bytes.  Never
  // trust a client-supplied hash — if one is present and disagrees, reject.
  const cid = await contentId(ev);
  if (ev.content_id && ev.content_id !== cid) {
    return { ok: false, reason: 'content_id_mismatch', expected: cid };
  }

  const inlineMax = parseInt(env.INLINE_MAX_BYTES || String(INLINE_DEFAULT), 10);
  // Store the FULL event JSON (lossless).  D1 columns below are denormalized
  // query indexes; payload_inline / the R2 object are the source of truth, so
  // fields without a dedicated column (signals, reusability, payload) survive.
  const fullJson = JSON.stringify(ev);
  const fullBytes = new TextEncoder().encode(fullJson).length;

  let payloadInline = null;
  let payloadR2Key = null;
  if (env.BLOBS && fullBytes > inlineMax) {
    // R2 configured → spill large payloads (unbounded size).
    payloadR2Key = `insights/${ev.run_id}/${cid}.json`;
    // R2 put is content-addressed → re-putting identical bytes is idempotent.
    await env.BLOBS.put(payloadR2Key, fullJson, {
      httpMetadata: { contentType: 'application/json' },
    });
  } else if (fullBytes > D1_VALUE_CEILING) {
    // D1-only (no R2) and too large to store inline safely.  Reject just this
    // event so the rest of the batch still commits; it stays in the durable
    // local ledger (the source of truth) and is simply not mirrored to D1.
    return { ok: false, reason: 'payload_too_large_no_r2', bytes: fullBytes };
  } else {
    payloadInline = fullJson;
  }

  const outcome =
    ev.outcome && typeof ev.outcome === 'object' && !Array.isArray(ev.outcome)
      ? ev.outcome
      : {};

  const stmt = env.DB.prepare(
    `INSERT OR IGNORE INTO insight_events
       (content_id, stream, ts, run_id, project_name, mode, kind, stage,
        schema_version, payload_inline, payload_r2_key, env_fingerprint,
        outcome_status, outcome_score)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  ).bind(
    cid,
    ev.stream,
    ev.ts,
    ev.run_id,
    ev.project_name,
    ev.mode,
    ev.kind,
    ev.stage ?? null,
    ev.schema_version,
    payloadInline,
    payloadR2Key,
    JSON.stringify(ev.env_fingerprint ?? {}),
    typeof outcome.status === 'string' ? outcome.status : null,
    typeof outcome.score === 'number' && Number.isFinite(outcome.score)
      ? outcome.score
      : null
  );

  return { ok: true, content_id: cid, r2: payloadR2Key, stmt };
}
