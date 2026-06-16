// Strict event-shape conformance gate (Phase A — untrusted contributor uploads).
//
// prepareEvent() ALWAYS enforces the structural minimum (required fields, a
// valid stream, the content_id recompute, and the secret scan).  When a write
// comes from an UNTRUSTED caller (any non-admin token) the router additionally
// runs this stricter gate, so the shared corpus only ever stores events that
// match what Crucible actually emits — hand-crafted / wrong-schema / junk
// uploads are rejected before they can pollute the corpus that other
// contributors read.
//
// The owner/admin path BYPASSES this gate on purpose.  The operator's own
// dual-write is the source of truth, and gating it on a hard-coded enum here
// would silently reject the operator's data the moment the pipeline grows a new
// EventKind / mode (the CLAUDE.md §9.6 producer→consumer self-lock trap).  Only
// third parties, who run the same Crucible code, are held to the known shape.
//
// The lists below are PINNED to crucible/features/run_insights/schema.py
// (EventKind / OutcomeStatus) and the mode set used across the pipeline.  Keep
// them in lockstep when that enum grows — test/event_shape.test.js fails loudly
// if a known kind is dropped from VALID_KINDS or KIND_STREAM.

// Pipeline run modes (schema.py: "SaaS/Agent/Scientist don't trade assets").
export const VALID_MODES = new Set(['Quant', 'SaaS', 'Agent', 'Scientist']);

// schema.py :: EventKind — every allowed value of the `kind` field.
export const VALID_KINDS = new Set([
  'output_method',
  'error_record',
  'direction_debate_rejection',
  'runtime_params',
  'direction_debate_finding',
  'direction_debate_verdict',
  'provider_cooldown_engaged',
  'provider_health_summary',
  'direction_debate_degraded_proceed',
]);

// Each EventKind is emitted onto exactly ONE stream (schema.py routing
// comments).  A contributor event whose stream disagrees with its kind is
// malformed and rejected.
export const KIND_STREAM = {
  output_method: 'output',
  error_record: 'error',
  direction_debate_rejection: 'debate',
  runtime_params: 'params',
  direction_debate_finding: 'debate',
  direction_debate_verdict: 'debate',
  provider_cooldown_engaged: 'error',
  provider_health_summary: 'output',
  direction_debate_degraded_proceed: 'debate',
};

// schema.py :: OutcomeStatus.
export const VALID_OUTCOME_STATUS = new Set([
  'success',
  'failure',
  'partial',
  'skipped',
]);

// ISO-8601 UTC instant, "...Z" with optional fractional seconds — matches the
// Python recorder's timestamp_utc() output.
const ISO_UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$/;

const MAX_ID_LEN = 512;

function isBoundedString(v, max = MAX_ID_LEN) {
  return typeof v === 'string' && v.length > 0 && v.length <= max;
}

/**
 * Strict conformance check, applied to UNTRUSTED (non-admin) uploads only.
 * Assumes prepareEvent() has already verified required-field presence and that
 * `stream` is one of the four valid streams.
 * @param {Record<string, unknown>} ev
 * @returns {{ ok: true } | { ok: false, reason: string }}
 */
export function checkEventShape(ev) {
  // mode ∈ known pipeline modes
  if (typeof ev.mode !== 'string' || !VALID_MODES.has(ev.mode)) {
    return { ok: false, reason: `bad_mode:${String(ev.mode).slice(0, 32)}` };
  }
  // kind ∈ known EventKind
  if (typeof ev.kind !== 'string' || !VALID_KINDS.has(ev.kind)) {
    return { ok: false, reason: `bad_kind:${String(ev.kind).slice(0, 48)}` };
  }
  // kind must be carried on its canonical stream
  if (KIND_STREAM[ev.kind] !== ev.stream) {
    return {
      ok: false,
      reason: `kind_stream_mismatch:${ev.kind}->${String(ev.stream).slice(0, 16)}`,
    };
  }
  // schema_version: positive integer
  if (
    typeof ev.schema_version !== 'number' ||
    !Number.isInteger(ev.schema_version) ||
    ev.schema_version < 1
  ) {
    return { ok: false, reason: 'bad_schema_version' };
  }
  // ts: ISO-8601 UTC and a real calendar instant
  if (
    typeof ev.ts !== 'string' ||
    !ISO_UTC_RE.test(ev.ts) ||
    Number.isNaN(Date.parse(ev.ts))
  ) {
    return { ok: false, reason: 'bad_ts' };
  }
  // run_id / project_name: bounded non-empty strings
  if (!isBoundedString(ev.run_id)) return { ok: false, reason: 'bad_run_id' };
  if (!isBoundedString(ev.project_name)) {
    return { ok: false, reason: 'bad_project_name' };
  }
  // stage: optional; a string when present
  if (ev.stage !== undefined && ev.stage !== null && typeof ev.stage !== 'string') {
    return { ok: false, reason: 'bad_stage' };
  }
  // outcome: optional object; validate status/score shape when present
  if (ev.outcome !== undefined && ev.outcome !== null) {
    if (typeof ev.outcome !== 'object' || Array.isArray(ev.outcome)) {
      return { ok: false, reason: 'bad_outcome' };
    }
    const st = ev.outcome.status;
    if (
      st !== undefined &&
      st !== null &&
      (typeof st !== 'string' || !VALID_OUTCOME_STATUS.has(st))
    ) {
      return { ok: false, reason: `bad_outcome_status:${String(st).slice(0, 24)}` };
    }
    const sc = ev.outcome.score;
    if (sc !== undefined && sc !== null && (typeof sc !== 'number' || !Number.isFinite(sc))) {
      return { ok: false, reason: 'bad_outcome_score' };
    }
  }
  return { ok: true };
}
