"""
Per-pattern unit tests for ``_VALUE_SECRET_PATTERNS`` (Tier 3 value-content
redaction).

v1.1.0 third-pass: the existing ``test_redaction.py`` only exercised
field-name redaction and a handful of end-to-end cases; ten of the
thirteen patterns had no positive / negative coverage.  Each test below
pins ONE pattern with:

* A POSITIVE case — a representative secret of that format — must be
  replaced with ``***REDACTED***``.
* A NEGATIVE case — a legitimate string that LOOKS similar but should
  not match (catches over-eager regex tightening).

Run after any change to the pattern bank: a tightened regex that
misses real secrets, or a relaxed regex that fires on legitimate
strings, will show up here as a clear pass/fail.
"""
from __future__ import annotations

import pytest

from crucible.features.run_insights.redact import _redact_string_value


_REDACTED = "***REDACTED***"


@pytest.mark.parametrize(
    "pattern_name,secret,context",
    [
        # Anthropic Claude API key
        ("anthropic", "sk-ant-api03-" + "A" * 50, "401 Unauthorized: invalid key"),
        ("anthropic_admin", "sk-ant-admin01-" + "X" * 60, "admin token used"),
        # OpenRouter
        ("openrouter", "sk-or-v1-" + "abcdef0123456789" * 4, "request to openrouter.ai"),
        # OpenAI project keys
        ("openai_proj", "sk-proj-" + "P" * 50, "OpenAI project key"),
        # OpenAI legacy
        ("openai_legacy", "sk-" + "A" * 48, "auth: sk-AAAA..."),
        # Google Gemini
        ("gemini", "AIza" + "B" * 35, "Gemini key blocked"),
        # xAI Grok
        ("xai", "xai-" + "G" * 50, "grok api key xai-..."),
        # Slack
        ("slack_bot", "xoxb-1234567890-" + "S" * 30, "Slack bot token leaked"),
        # GitHub PAT classic
        ("github_pat_classic", "ghp_" + "G" * 36, "github auth header"),
        ("github_pat_fine", "github_pat_" + "X" * 50, "fine-grained PAT"),
        # JWT
        ("jwt", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "Bearer token"),
        # Authorization header
        ("bearer", "Bearer " + "A" * 50, "header with Bearer prefix"),
        ("basic", "Basic " + "B" * 30, "header with Basic prefix"),
        # Stripe
        ("stripe_test", "sk_test_" + "S" * 30, "Stripe test key"),
        ("stripe_live", "sk_live_" + "L" * 40, "Stripe live key"),
        # AWS access key id
        ("aws_akia", "AKIA" + "0" * 16, "AWS credentials in error log"),
        ("aws_asia", "ASIA" + "Z" * 16, "AWS STS temporary credentials"),
        # password=<value> URLs
        ("password_url", "https://example.com/?password=verysecret123", "URL with embedded password"),
        ("api_key_url", "https://example.com/?api_key=abc123xyz789", "URL with embedded api_key"),
    ],
)
def test_value_redaction_positive_each_pattern(
    pattern_name: str, secret: str, context: str,
):
    """Each known secret format MUST be replaced with ``_REDACTED``."""
    input_text = f"{context}: {secret}"
    out = _redact_string_value(input_text)
    assert secret not in out, (
        f"[{pattern_name}] secret leaked verbatim: {out!r}"
    )
    assert _REDACTED in out, (
        f"[{pattern_name}] redaction sentinel missing: {out!r}"
    )


@pytest.mark.parametrize(
    "legitimate_text",
    [
        # Code identifiers that LOOK like secret prefixes but are not.
        "function sk_helper() { return 1; }",
        "sql query: SELECT sk FROM table",
        # Short ``sk-`` prefix below the 40-char threshold — should NOT match.
        "see ticket sk-bug-12345",
        # ``AIza`` prefix without enough trailing chars.
        "constant AIza must be 30+ chars",
        # Documentation reference (Anthropic placeholder, too short).
        "your key is sk-ant-api03-XXXXX (replace XXXXX)",
        # Bearer keyword in unrelated context.
        "She is a bearer of bad news.",
        # AWS-shaped string but mixed case (real keys are uppercase only).
        "AKIAabcdefghijklmnop",  # 4 + 16 alphanumeric BUT lowercase doesn't match [A-Z0-9]
    ],
)
def test_value_redaction_negative_legitimate_strings(legitimate_text: str):
    """Legitimate strings that resemble secret prefixes must NOT be touched.

    A regression that relaxes a regex to match short prefixes / lowercase
    variants would fire here as a false positive.
    """
    out = _redact_string_value(legitimate_text)
    assert out == legitimate_text, (
        f"false positive: legitimate {legitimate_text!r} -> {out!r}"
    )


def test_value_redaction_url_encoded_jwt():
    """v1.1.0 fourth-pass (T20 follow-up): URL-percent-encoded JWTs
    should be caught even when the dots between segments are
    encoded as ``%2E`` (case-insensitive).
    """
    encoded = "eyJhbGc" + "A" * 30 + "%2EeyJzdWIi" + "B" * 30 + "%2E" + "C" * 30
    out = _redact_string_value(f"see error log: {encoded} after timeout")
    assert encoded not in out
    assert _REDACTED in out


def test_value_redaction_mixed_dot_and_percent_jwt():
    """A partially-encoded JWT (one dot literal, one ``%2E``) must
    still match — fourth-pass pattern now uses ``(?:%2[eE]|\\.)``
    between segments.
    """
    encoded = "eyJhbGc" + "A" * 30 + ".eyJzdWIi" + "B" * 30 + "%2E" + "C" * 30
    out = _redact_string_value(encoded)
    assert encoded not in out
    assert _REDACTED in out


def test_vendor_patterns_have_left_boundary_assertion():
    """v1.1.0 fourth-pass (F-1): the vendor-specific sk- patterns
    (sk-ant, sk-or-v, sk-proj) carry the same left-boundary
    assertion the generic sk- pattern received in T7.  Without
    it, mid-token false positives like ``"deadbeefsk-ant-api03-
    ...deadbeef"`` (random hex blob that happens to contain the
    prefix) would be redacted as if they were real secrets,
    destroying debugging context.

    With the boundary, mid-token matches are correctly REJECTED
    → the string passes through unchanged.  Clean-boundary
    secrets ("error: <token>") are still caught — see the
    positive cases above.

    This test detects a regression that drops the boundary: such
    a regression would cause the mid-token match to fire and
    redact the secret, breaking the ``out == contaminated``
    assertion.
    """
    sk_ant = "sk-ant-api03-" + "X" * 50
    contaminated = f"deadbeef{sk_ant}deadbeef"
    out = _redact_string_value(contaminated)
    # Boundary correctly blocked the match → no redaction → input
    # unchanged.  If F-1 is regressed (boundary missing), the
    # vendor pattern fires and redacts the embedded prefix,
    # changing the output.
    assert out == contaminated, (
        f"mid-token match fired despite boundary — F-1 regressed: {out!r}"
    )


def test_vendor_patterns_still_catch_clean_boundary_secrets():
    """Companion to the boundary test above: a sk-ant key with
    clean (non-alphanumeric) left boundary MUST still be redacted.
    Ensures the boundary fix didn't over-tighten and stop catching
    real secrets.
    """
    sk_ant = "sk-ant-api03-" + "X" * 50
    # Clean left boundary: space before the token.
    clean = f"401 Unauthorized: {sk_ant} (rotate the key)"
    out = _redact_string_value(clean)
    assert sk_ant not in out, (
        f"clean-boundary sk-ant secret was NOT redacted: {out!r}"
    )
    assert _REDACTED in out


def test_value_redaction_pattern_ordering_vendor_before_generic():
    """The ``_VALUE_SECRET_PATTERNS`` tuple MUST keep
    vendor-specific patterns (sk-ant, sk-or-v, sk-proj) BEFORE
    the generic ``sk-`` pattern.  If reordered, the generic
    pattern would match a vendor secret first and the more-
    specific pattern would never fire.  Functionally both still
    redact, but the audit trail (which pattern matched) gets
    less useful for forensics.
    """
    from crucible.features.run_insights.redact import _VALUE_SECRET_PATTERNS

    patterns_src = [p.pattern for p in _VALUE_SECRET_PATTERNS]
    # Find the indices of the three vendor patterns and the generic.
    ant_idx = next(i for i, p in enumerate(patterns_src) if "sk-ant-" in p)
    or_idx = next(i for i, p in enumerate(patterns_src) if "sk-or-v" in p)
    proj_idx = next(i for i, p in enumerate(patterns_src) if "sk-proj-" in p)
    generic_idx = next(
        i for i, p in enumerate(patterns_src)
        if "sk-[A-Za-z0-9]{40,80}" in p
    )
    assert ant_idx < generic_idx, "sk-ant must precede generic sk- pattern"
    assert or_idx < generic_idx, "sk-or-v must precede generic sk- pattern"
    assert proj_idx < generic_idx, "sk-proj must precede generic sk- pattern"


def test_redact_string_value_empty_input_short_circuits():
    """Empty/whitespace input must return unchanged without any
    pattern work."""
    assert _redact_string_value("") == ""
    # Whitespace passes through (no secret prefix matches).
    assert _redact_string_value("   ") == "   "


def test_jwt_pattern_no_catastrophic_backtracking():
    """Pathological long input with mis-positioned dots must not stall.

    Before v1.1.0 third-pass the JWT pattern used unbounded ``{8,}``
    quantifiers on three segments; pathological inputs (e.g.
    ``"eyJ" + "A" * 10000 + ".eyJ" + "A" * 10000``) could trigger
    O(n²) backtracking.  Bounded ``{8,N}`` quantifiers cap the cost.
    """
    import time as _time
    pathological = "eyJ" + "A" * 5000 + ".eyJ" + "A" * 5000
    t0 = _time.monotonic()
    out = _redact_string_value(pathological)
    elapsed = _time.monotonic() - t0
    # Should complete in well under 1 second; we allow 2 s for slow CI.
    assert elapsed < 2.0, (
        f"JWT regex stalled on pathological input ({elapsed:.2f}s elapsed)"
    )
    # The text has only 2 segments (no third "."), so no JWT match —
    # output equals input.
    assert out == pathological
