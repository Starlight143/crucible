// Server-side content validation — defense-in-depth (Phase A / Step 1, B3).
//
// The Python client already redacts secrets (redact.py) before upload; this is
// the cloud-side backstop so the shared corpus NEVER stores a leaked credential
// even if a client fails to redact.  An event whose serialized JSON matches a
// known provider-secret shape is rejected (reason 'secret_detected') and stays
// only in the contributor's local ledger.
//
// All patterns use bounded character classes with no nested quantifiers, so they
// run in linear time (no ReDoS).  Vendor-specific prefixes come first.

const SECRET_PATTERNS = [
  /sk-ant-[A-Za-z0-9_-]{24,}/, // Anthropic
  /sk-or-v1-[A-Fa-f0-9]{32,}/, // OpenRouter
  /sk-proj-[A-Za-z0-9_-]{24,}/, // OpenAI project key
  /\bsk-[A-Za-z0-9]{32,}\b/, // generic OpenAI-style key
  /\bAKIA[0-9A-Z]{16}\b/, // AWS access key id
  /\bghp_[A-Za-z0-9]{36}\b/, // GitHub personal access token
  /\bAIza[0-9A-Za-z_-]{35}\b/, // Google API key
  /xox[baprs]-[A-Za-z0-9-]{10,}/, // Slack token
];

/**
 * Scan serialized event JSON for an obvious provider secret.
 * @param {string} jsonText
 * @returns {string|null} the matched pattern source, or null when clean
 */
export function scanForSecrets(jsonText) {
  if (typeof jsonText !== 'string') return null;
  for (const re of SECRET_PATTERNS) {
    if (re.test(jsonText)) return re.source;
  }
  return null;
}
