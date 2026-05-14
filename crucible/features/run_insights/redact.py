"""
features/run_insights/redact.py
================================
Redaction of sensitive fields before events are persisted to the ledger.

Three-tier strategy
-------------------
Tier 1 — delegate to :func:`crucible.runtime_logging._redact_fields`, the
project-wide sensitive-field redactor (substring match on
``api_key``/``token``/``secret``/etc. plus exact match on bare ``"auth"``).

Tier 2 — additional whitelist specific to the run_insights surface:

* ``api_url``, ``api_token``, ``webhook_url``, ``slack_webhook`` — these are
  configuration values that *can* legitimately appear in ``runtime_params``
  payloads (gate / budget config dumps); tier-1 ``token`` substring already
  matches ``api_token`` but not the bare URL forms.

* Recursive descent — tier 1 only redacts at the top level of a flat dict;
  ``runtime_params`` payloads embed nested dicts (gate_config, budget_policy,
  cli_flags), so we walk the structure.

Tier 3 (v1.1.0) — **value-content** scanning.  Tier 1/2 only check field
*names*; an exception ``message_head`` like ``"401 invalid key sk-ant-
api03-AbCdEf..."`` lives under the innocuously-named ``"message_head"``
field and slips through.  Tier 3 applies regex patterns to every string
value (regardless of parent key) and replaces detected secrets with the
``_REDACTED`` sentinel.  Patterns cover the API-key formats used by every
LLM provider Crucible integrates with (Anthropic, OpenAI, OpenRouter,
Google Gemini, xAI, DeepSeek, Slack/Discord webhooks), JWTs, and bare
``Bearer`` / ``Basic`` Authorization headers.

All tiers are off when the operator explicitly sets
``CRUCIBLE_RUN_INSIGHTS_REDACT=0`` — but redaction stays on for the default
operator who never sets the flag, matching the project boolean-env-whitelist
rule.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Mapping

# Tri-modal import (see recorder.py for the launcher matrix).
try:
    from ..._env import env_bool
    from ...runtime_logging import (
        _SENSITIVE_KEY_EXACT as _RL_EXACT,
        _SENSITIVE_KEY_FRAGMENTS as _RL_FRAGMENTS,
    )
except ImportError:  # pragma: no cover — flat-launcher fallback
    from _env import env_bool  # type: ignore[no-redef]
    from runtime_logging import (  # type: ignore[no-redef]
        _SENSITIVE_KEY_EXACT as _RL_EXACT,
        _SENSITIVE_KEY_FRAGMENTS as _RL_FRAGMENTS,
    )

# v1.1.2 (audit fix G2-B-MED-4): import canonical_json for set-element
# ordering.  Separate try block so an import failure here doesn't
# poison the env_bool / sensitive-key fragment imports above (a
# circular-import or schema.py rename would otherwise break the entire
# redact module instead of degrading gracefully).
try:
    from .schema import canonical_json as _canonical_json
except ImportError:  # pragma: no cover — flat-launcher fallback
    try:
        from features.run_insights.schema import (  # type: ignore[no-redef]
            canonical_json as _canonical_json,
        )
    except ImportError:
        _canonical_json = None  # type: ignore[assignment]

_REDACTED = "***REDACTED***"

# Additional fragments matched as case-insensitive substrings of field names.
# These supplement runtime_logging's set; placed here rather than there so
# the run_insights module owns the policy choice.  The combined fragment set
# is applied recursively (runtime_logging's _redact_fields only handles the
# top level — runtime_params payloads embed nested config dicts).
_EXTRA_FRAGMENTS: frozenset[str] = frozenset({
    "api_url",
    "webhook_url",
    "webhook_secret",
    "slack_webhook",
    "discord_webhook",
    "ledger_token",
    "insights_api_url",
})

# Combined sensitive-field detection sets, applied at every depth.
_COMBINED_FRAGMENTS: frozenset[str] = _RL_FRAGMENTS | _EXTRA_FRAGMENTS
_COMBINED_EXACT: frozenset[str] = _RL_EXACT


# ── Tier 3: value-content patterns ────────────────────────────────────────────
#
# Each entry matches a recognised secret format.  The regex captures the
# whole secret (including any prefix) and the entire match is replaced with
# ``_REDACTED``.  The patterns are intentionally generous (long alphanumeric
# tails) to catch rotated formats; false-positive rates are acceptable
# because the affected field is intended to be observability-only.
#
# Conservative ordering: longer / more-specific patterns first so a shorter
# generic pattern doesn't pre-empt a vendor-specific one.
#
# v1.1.0 third-pass hardening notes:
#   * The generic ``sk-<...>{32,}`` pattern previously fired on any
#     32-char alphanumeric string with a literal ``sk-`` prefix —
#     including legitimate identifiers like ``sk-test-pubcache-abc...``
#     or URL paths containing ``/sk-<hash>/``.  Tightened to require 40+
#     chars (matches the real OpenAI / DeepSeek key length) AND
#     non-alphanumeric word boundaries on both sides so it does not eat
#     surrounding context.
#   * The JWT pattern previously used unbounded ``{8,}`` quantifiers
#     across three segments; long alphanumeric inputs with mis-positioned
#     dots could trigger O(n²) backtracking on pathological inputs.  Now
#     bounded at the maximum realistic per-segment width (header 8-300,
#     payload 8-2000, signature 8-300) so the engine cannot stall.
_VALUE_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # v1.1.0 fourth-pass (F-1): every vendor-specific sk-* pattern now
    # carries the same left-boundary assertion the generic sk- pattern
    # received in T7.  Without it, a hex/base64 blob like
    # ``deadbeefsk-ant-api03-XXX...`` had its `sk-ant-...` substring
    # match mid-token, redacting a chunk of an otherwise legitimate
    # blob.  The right boundary is enforced by the bounded
    # ``{40,N}`` quantifier on each pattern (any [A-Za-z0-9_\-] that
    # follows simply extends the match — still entirely redacted, so
    # no leak risk).
    # Anthropic Claude API key (v1, v2, future): "sk-ant-api<digits>-<base64ish>"
    # v1.1.0 fifth-pass (G-4): also match ``sk-ant-oat<digits>-...`` —
    # this is the OAuth-bearer format used by Claude Code (anyone with
    # ``claude api`` in their env carries one).  Pattern previously only
    # covered api/sid/admin, leaking OAuth tokens through.
    re.compile(r"(?<![A-Za-z0-9])sk-ant-(?:api\d+|sid\d+|admin\d+|oat\d+)-[A-Za-z0-9_\-]{40,200}"),
    # OpenRouter: "sk-or-v1-<64 hex chars>" (and future v2/v3)
    re.compile(r"(?<![A-Za-z0-9])sk-or-v\d+-[A-Fa-f0-9]{32,128}"),
    # OpenAI project keys: "sk-proj-<base64ish>" (current format, ~150 chars)
    re.compile(r"(?<![A-Za-z0-9])sk-proj-[A-Za-z0-9_\-]{40,200}"),
    # v1.1.0 fifth-pass (G-5): OpenAI service-account keys
    # ``sk-svcacct-<base64ish>``.  Dash in the prefix prevents the
    # generic ``sk-[A-Za-z0-9]{40,80}`` pattern from matching, so
    # without an explicit vendor pattern these keys leak verbatim.
    re.compile(r"(?<![A-Za-z0-9])sk-svcacct-[A-Za-z0-9_\-]{40,200}"),
    # v1.1.2 (audit fix G2-B-HIGH-2): DeepSeek API keys are ``sk-`` + 32
    # hex digits (35 chars total) — the v1.1.0 third-pass hardening
    # tightened the generic OpenAI legacy pattern to ``{40,80}`` so DeepSeek
    # keys silently fell through the entire tier-3 scanner.  With v1.1.1's
    # Cost Tracking expansion the surface for DeepSeek error-message
    # redaction grew, making this leak path real.  Must come BEFORE the
    # generic OpenAI legacy pattern so this strict 32-hex format wins on
    # DeepSeek tokens (which would also match the {40,80} alphanumeric
    # pattern in some operator-rotated formats).
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Fa-f0-9]{32}(?![A-Za-z0-9])"),
    # OpenAI legacy keys: "sk-<48 alphanumeric>" — must come AFTER sk-ant /
    # sk-or / sk-proj / DeepSeek-32-hex so the more-specific patterns win.
    # Boundary assertions prevent matching mid-token (e.g. inside a URL path).
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{40,80}(?![A-Za-z0-9])"),
    # Google Gemini / Cloud API keys: "AIza<35 alphanumeric>"
    re.compile(r"(?<![A-Za-z0-9])AIza[A-Za-z0-9_\-]{30,80}"),
    # xAI Grok: "xai-<alphanumeric>"
    re.compile(r"(?<![A-Za-z0-9])xai-[A-Za-z0-9]{40,120}"),
    # Slack bot / user tokens: xoxb-/xoxp-/xoxa-/xoxr-/xoxs-
    re.compile(r"xox[bparseu]-[A-Za-z0-9\-]{10,200}"),
    # GitHub PAT (classic & fine-grained), App tokens, OAuth, server-to-server
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{30,200}"),
    # JSON Web Tokens (compact serialisation: header.payload.signature).
    # Bounded per-segment to prevent catastrophic backtracking on long
    # adversarial inputs.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,300}\.eyJ[A-Za-z0-9_\-]{8,2000}\.[A-Za-z0-9_\-]{8,300}\b"),
    # Authorization: Bearer / Basic / Token <opaque>
    re.compile(r"(?i)(?:Bearer|Basic|Token)\s+[A-Za-z0-9_\-\.=:+/]{20,500}"),
    # Stripe-style: "(sk|rk|pk)_(test|live)_<base58ish>"
    re.compile(r"(?:sk|rk|pk)_(?:test|live)_[A-Za-z0-9]{20,200}"),
    # AWS access key id (always 20 chars beginning AKIA/ASIA)
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    # Generic "password=<value>" / "passwd=<value>" sequences in URLs/text.
    # v1.1.0 fourth-pass: upper bound raised from 200 to 2000 so long
    # opaque session cookies / JWTs embedded in URL query strings are
    # fully redacted rather than leaking the tail.
    re.compile(r"(?i)(?:password|passwd|api[_-]?key)=([^\s&'\"]{6,2000})"),
    # URL-percent-encoded JWT (``eyJ...`` after %2E/%2D etc. survives
    # URL-encoding — detect on the raw prefix because percent-encoded
    # secrets in error messages slipped past tier 3 before).
    # v1.1.0 fourth-pass: accept either ``%2E`` (dot) or literal ``.``
    # between segments so a partially-encoded token (e.g. one segment
    # percent-encoded, the other plain) is still caught.
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,300}(?:%2[eE]|\.)eyJ[A-Za-z0-9_\-]{8,2000}(?:%2[eE]|\.)[A-Za-z0-9_\-]{8,300}"),
)


# v1.1.0 fourth-pass: short-circuit prefix check.  ``_redact_string_value``
# scanned every input string against all 14 patterns regardless of content;
# for a deeply-nested ``runtime_params`` payload with 100 strings this was
# 1400 ``re.sub`` traversals per event.  We now run a single combined
# alternation FIRST; if it doesn't hit, none of the individual patterns
# could, and we skip the per-pattern loop entirely.  The combined regex
# matches any of the recognised secret prefixes so its FN rate is zero
# for inputs containing real secrets.
_ANY_SECRET_PREFIX: re.Pattern[str] = re.compile(
    r"sk-|eyJ|AIza|xai-|xox[bparseu]-|gh[poursr]_|github_pat_"
    r"|(?i:Bearer|Basic|Token)\s+|(?:sk|rk|pk)_(?:test|live)_"
    r"|AKIA|ASIA|(?i:password|passwd|api[_-]?key)="
)


def _redact_string_value(value: str) -> str:
    """Apply every value-content pattern to *value* and replace matches.

    Returns the (possibly modified) string.  Short-circuits on empty input.

    v1.1.0 fourth-pass: added a combined-prefix short-circuit so strings
    that don't contain ANY recognised secret prefix skip the per-pattern
    loop entirely (most strings in a typical payload).  Previously every
    string ran through 14 ``re.sub`` calls regardless.
    """
    if not value:
        return value
    # Fast-path: if no recognised secret prefix appears anywhere, no
    # pattern could possibly match.  One scan vs fourteen.
    if not _ANY_SECRET_PREFIX.search(value):
        return value
    out = value
    for pat in _VALUE_SECRET_PATTERNS:
        out = pat.sub(_REDACTED, out)
    return out


def _is_sensitive_key(key: str) -> bool:
    k_lower = str(key).lower()
    if k_lower in _COMBINED_EXACT:
        return True
    return any(frag in k_lower for frag in _COMBINED_FRAGMENTS)


def _redact_value(
    key: str,
    value: Any,
    _visited: set[int] | None = None,
) -> Any:
    """Recursively redact *value* (or return a redacted copy).

    If the parent *key* is sensitive, the entire subtree is replaced with
    ``_REDACTED``.  Otherwise descend into nested dicts/lists/tuples/sets,
    re-checking each child key, and finally apply value-content scanning
    to leaf strings (Tier 3) so secrets embedded inside error messages /
    log lines / list elements with innocuous parent keys are caught.

    v1.1.0 fifth-pass (G-6): walks ``tuple``/``set``/``frozenset`` too.
    The fourth-pass walker only recursed into ``Mapping`` and ``list``,
    so a payload like ``{'foo': ('hello', '<token>', 'world')}`` left
    the embedded token unredacted — ``_normalise_for_canonical`` then
    converted the tuple to a JSON list at serialisation time, and the
    secret landed in the persisted JSONL verbatim.  Sets are emitted
    as sorted lists to keep the JSON output deterministic.

    v1.1.0 fifth-pass (G-7): decodes ``bytes``/``bytearray`` leaves
    via UTF-8 replace-error and runs them through tier-3 scanning;
    previously they bypassed redaction and then crashed the encoder
    (``TypeError: bytes is not JSON serialisable``) inside ``_emit``,
    where the broad ``except Exception`` silently dropped the event.

    v1.1.0 fifth-pass (G-8): cycle detection via an ``id()`` visited
    set — a self-referential payload (``a = {}; a['self'] = a``) would
    previously trigger ``RecursionError``, which the outer ``_emit``
    swallowed, dropping the event with no diagnostic.  We now return
    the sentinel ``"<cycle>"`` for the back-edge, preserving the rest
    of the payload structure.
    """
    if _is_sensitive_key(key):
        return _REDACTED
    if _visited is None:
        _visited = set()

    # Container cycle detection.  Primitive types (str/int/float/bytes/
    # bool/None) cannot form reference cycles, so we only track
    # containers — this keeps the visited-set small for typical payloads.
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        marker = id(value)
        if marker in _visited:
            return "<cycle>"
        _visited = _visited | {marker}

    if isinstance(value, Mapping):
        return {k: _redact_value(k, v, _visited) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(key, v, _visited) for v in value]
    if isinstance(value, tuple):
        # Emit as list — JSON has no tuple type and ``_normalise_for_canonical``
        # would convert anyway.  Walking now means embedded secrets get
        # redacted before serialisation.
        return [_redact_value(key, v, _visited) for v in value]
    if isinstance(value, (set, frozenset)):
        # Sets are unordered in Python but JSON output must be deterministic
        # (the content-id depends on canonical byte equality).  We sort by
        # the canonical-JSON repr of each element after redaction so order
        # is reproducible regardless of insertion order.
        #
        # v1.1.2 (audit fix G2-B-MED-4): previously the sort key used
        # ``json.dumps(..., sort_keys=True, default=str)`` which is NOT
        # canonical — it uses Python-default float repr (``1.0`` vs V8's
        # ``1``) and ``default=str`` for non-JSON leaves.  Two semantically
        # identical sets containing the same floats could sort differently
        # across Python builds, producing different in-payload list ordering
        # → different canonical_json → different content_id → cross-process
        # dedup breakage (the whole point of content_id).  Now sorts by the
        # same canonical encoder the persistence layer uses, so set ordering
        # is byte-for-byte deterministic across Python releases and
        # platforms.
        redacted = [_redact_value(key, v, _visited) for v in value]
        try:
            if _canonical_json is not None:
                return sorted(
                    redacted,
                    key=lambda x: _canonical_json({"_": x}).decode("utf-8"),
                )
            # Defensive: canonical_json should always be importable in the
            # tri-modal layouts, but if the import failed, fall back to the
            # legacy ``json.dumps`` path so the recorder still works.
            return sorted(
                redacted,
                key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False, default=str),
            )
        except Exception:
            # Fallback: stringify keys for sort if any element is unhashable
            # under repr (shouldn't happen after our recursion strips them).
            return sorted(redacted, key=lambda x: str(x))
    if isinstance(value, (bytes, bytearray)):
        try:
            decoded = bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return _REDACTED
        return _redact_string_value(decoded)
    if isinstance(value, str):
        return _redact_string_value(value)
    return value


def redact_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of *payload* with sensitive fields stripped.

    Unlike ``runtime_logging._redact_fields`` which only inspects top-level
    keys, this walker descends into nested mappings and lists — necessary
    because ``runtime_params`` payloads embed dicts like
    ``{"cli_flags": {"openrouter_api_key": "..."}}``.

    Sensitive-field detection uses the union of:

    * ``runtime_logging._SENSITIVE_KEY_EXACT`` (e.g. bare ``"auth"``)
    * ``runtime_logging._SENSITIVE_KEY_FRAGMENTS`` (e.g. ``"api_key"``,
      ``"token"``, ``"secret"``)
    * The module-local ``_EXTRA_FRAGMENTS`` (URL-style fields not covered
      by the substring patterns above).
    * (v1.1.0) Value-content regex patterns covering API keys for every
      provider Crucible integrates with, JWTs, and Authorization headers.

    Setting ``CRUCIBLE_RUN_INSIGHTS_REDACT=0`` returns a passthrough copy.
    """
    if not env_bool("CRUCIBLE_RUN_INSIGHTS_REDACT", True):
        # Operator explicitly disabled redaction — pass through as a copy
        # so callers cannot accidentally mutate the original.
        return dict(payload)
    return {k: _redact_value(k, v) for k, v in payload.items()}


def redact_signals(signals: Iterable[str]) -> list[str]:
    """Signals are tag-style identifiers (``"asset:gold"``), not values, so
    they should never contain secrets.  This helper exists as a defensive
    final pass: drop any signal whose value half looks suspicious (contains
    ``=``, looks like a token prefix, or is implausibly long).
    """
    suspicious_prefixes = (
        "eyj",       # JWT base64 header
        "sk-",       # OpenAI / Anthropic / OpenRouter / DeepSeek
        "pat_",      # personal-access tokens (generic)
        "ghp_", "gho_", "ghs_", "ghu_", "ghr_", "github_pat_",
        "xoxb-", "xoxp-", "xoxa-", "xoxr-", "xoxs-",
        "aiza",      # Google Gemini (case-insensitive check applied below)
        "xai-",
        "akia", "asia",  # AWS access key id prefixes
    )
    out: list[str] = []
    for tag in signals:
        s = str(tag or "").strip()
        if not s or ":" not in s:
            continue
        prefix, _, value = s.partition(":")
        if len(value) > 64:
            continue  # implausibly long tag value
        v_lower = value.lower()
        if any(v_lower.startswith(p) for p in suspicious_prefixes):
            continue
        if "=" in value:
            continue
        out.append(s)
    return out


__all__ = ["redact_event_payload", "redact_signals"]
