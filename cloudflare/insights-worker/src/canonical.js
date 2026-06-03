// Canonical JSON + content_id — FROZEN SPEC.
//
// Produces byte-identical output to the Python implementation in
//   crucible/features/run_insights/schema.py :: canonical_json / compute_content_id
// so that an event written locally and the same event ingested here compute
// the SAME content_id and dedup naturally (D1 PRIMARY KEY + INSERT OR IGNORE).
//
// The algorithm is documented in backends.py's module docstring.  Parity is
// pinned by the shared golden vectors in test/canonical.test.js (this repo)
// and tests/test_run_insights/test_js_canonical_parity.py (Python repo).
// DO NOT change one side without the other.
//
// Parity invariants that callers MUST uphold (else IDs silently diverge):
//   * Object keys are ASCII.  JS `Object.keys().sort()` sorts by UTF-16 code
//     unit; Python `sort_keys=True` sorts by code point — they agree only for
//     ASCII keys.  The ledger schema uses ASCII field names everywhere.
//   * No integers beyond Number.MAX_SAFE_INTEGER (2^53-1) in payloads — JSON
//     round-tripping through a JS number loses precision and the recomputed
//     content_id would mismatch (the ingest path then rejects it, loudly).

/**
 * Canonical UTF-8 byte serialisation of `event`, EXCLUDING `content_id`.
 *   1. Drop the top-level `content_id` key.
 *   2. Recursively map non-finite numbers (NaN / ±Infinity) to null.
 *   3. Recursively sort object keys.
 *   4. JSON.stringify (V8 Number.toString rules, no spaces) → TextEncoder.
 * @param {Record<string, unknown>} event
 * @returns {Uint8Array}
 */
export function canonicalJson(event) {
  const e = { ...event };
  delete e.content_id;
  const norm = (v) => {
    if (typeof v === 'number' && !Number.isFinite(v)) return null;
    if (Array.isArray(v)) return v.map(norm);
    if (v && typeof v === 'object') {
      const sorted = {};
      for (const k of Object.keys(v).sort()) sorted[k] = norm(v[k]);
      return sorted;
    }
    return v;
  };
  return new TextEncoder().encode(JSON.stringify(norm(e)));
}

/**
 * Content-addressable ID: "sha256:" + lowercase hex of SHA-256 over
 * canonicalJson(event).  Idempotent and tamper-evident.
 * @param {Record<string, unknown>} event
 * @returns {Promise<string>}
 */
export async function contentId(event) {
  const buf = await crypto.subtle.digest('SHA-256', canonicalJson(event));
  return (
    'sha256:' +
    Array.from(new Uint8Array(buf))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
  );
}
