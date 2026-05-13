"""
JS ↔ Python canonical JSON parity tests.

v1.1.0: the run_insights ledger is content-addressable; ``content_id =
sha256(canonical_json(event))``.  When v1.2.0 introduces the Cloudflare
Worker (the JS spec for which is frozen in
``crucible/features/run_insights/backends.py`` module docstring), the
Worker MUST produce byte-identical canonical JSON for the same event,
otherwise two writes of the "same" event from local and cloud paths
will dedup as distinct rows.

These tests pin the algorithm against a set of edge-case fixtures that
historically diverged between Python ``json.dumps`` and V8
``JSON.stringify``:

1. Non-finite floats (NaN / Inf) → null.
2. Unicode (non-ASCII / astral plane) → identical UTF-8 bytes.
3. Float repr — Python and V8 round-trip identically (IEEE 754 shortest
   decimal) but produce different lexical forms for "smaller than
   1e-4" / "larger than 1e+21" values.  The Python canonicaliser must
   not depend on these representations.
4. Key ordering — both sides sort keys lexicographically on the
   _canonical_ form, INCLUDING nested objects.
5. The ``content_id`` field is dropped from the input before hashing
   (so re-emit-with-known-id produces the same ID).

If any of these fixtures diverges, v1.2.0 cloud sync will silently
produce duplicate rows.
"""
from __future__ import annotations

import json

from crucible.features.run_insights.schema import (
    canonical_json,
    compute_content_id,
)


# ── Reference fixtures: (label, event, expected_canonical_bytes) ──────────────
#
# Each ``expected`` string is what V8's ``JSON.stringify`` produces for
# the same input AFTER applying the same canonicaliser steps documented
# in backends.py's module docstring.  These fixtures were computed by
# running the equivalent JavaScript and pasting the output here; if the
# algorithm changes either side, regenerate via the Worker test harness.

_FIXTURES = [
    (
        "trivial-empty",
        {},
        b"{}",
    ),
    (
        "simple-flat",
        {"a": 1, "b": "x"},
        b'{"a":1,"b":"x"}',
    ),
    (
        "key-ordering",
        {"z": 1, "a": 2, "m": 3},
        b'{"a":2,"m":3,"z":1}',
    ),
    (
        "nested-key-ordering",
        {"outer": {"z": 1, "a": 2}, "alpha": 5},
        b'{"alpha":5,"outer":{"a":2,"z":1}}',
    ),
    (
        "nan-becomes-null",
        {"score": float("nan")},
        b'{"score":null}',
    ),
    (
        "posinf-becomes-null",
        {"score": float("inf")},
        b'{"score":null}',
    ),
    (
        "neginf-becomes-null",
        {"score": float("-inf")},
        b'{"score":null}',
    ),
    (
        "content-id-is-dropped",
        {"content_id": "sha256:deadbeef", "a": 1},
        b'{"a":1}',
    ),
    (
        "unicode-cjk-preserved",
        {"name": "嗨"},
        # ensure_ascii=False → raw UTF-8 bytes for U+55E8
        b'{"name":"' + "嗨".encode("utf-8") + b'"}',
    ),
    (
        "unicode-emoji-preserved",
        {"name": "🚀"},
        b'{"name":"' + "🚀".encode("utf-8") + b'"}',
    ),
    (
        "list-of-strings",
        {"tags": ["a", "b", "c"]},
        b'{"tags":["a","b","c"]}',
    ),
    (
        "nested-list-with-nan",
        {"vals": [1.0, float("nan"), 3.0]},
        # V8 drops the trailing ".0" for integer-valued floats; the
        # _V8FloatJSONEncoder in schema.py now matches that behaviour
        # so canonical bytes round-trip identically across both sides.
        b'{"vals":[1,null,3]}',
    ),
    (
        "float-non-integer",
        {"a": 0.5, "b": 1.25},
        b'{"a":0.5,"b":1.25}',
    ),
    (
        "float-exponent-small",
        {"a": 1e-7},
        # V8: "1e-7" (no leading zero in exponent); Python json default
        # would have produced "1e-07".  The encoder strips the leading
        # zero so we match V8.
        b'{"a":1e-7}',
    ),
    (
        "float-exponent-large",
        {"a": 1e21},
        # V8 emits "1e+21" for magnitudes above 1e21.
        b'{"a":1e+21}',
    ),
    (
        "negative-zero",
        {"a": -0.0},
        b'{"a":0}',
    ),
    (
        "bool-and-null",
        {"flag": True, "missing": None, "other": False},
        b'{"flag":true,"missing":null,"other":false}',
    ),
]


def test_canonical_json_matches_js_fixtures():
    """Every fixture's canonical_json output must match the JS spec byte-for-byte."""
    mismatches = []
    for label, event, expected in _FIXTURES:
        got = canonical_json(event)
        if got != expected:
            mismatches.append((label, expected, got))
    assert not mismatches, (
        "JS↔Python canonical JSON divergence:\n"
        + "\n".join(
            f"  [{label}]\n    expected: {exp!r}\n    got:      {got!r}"
            for label, exp, got in mismatches
        )
    )


def test_content_id_stable_under_key_reorder():
    """Permuting input keys must not change the content_id (sort_keys does
    the heavy lifting; this is a regression canary)."""
    e1 = {"a": 1, "b": 2, "c": 3}
    e2 = {"c": 3, "a": 1, "b": 2}
    assert compute_content_id(e1) == compute_content_id(e2)


def test_content_id_stable_when_content_id_field_present_or_absent():
    """The canonicaliser drops the ``content_id`` field before hashing so
    a record's hash is invariant under the presence/absence of the
    self-reference.  Required for the read-back-then-re-emit case.
    """
    e_without = {"a": 1, "b": "x"}
    e_with = dict(e_without, content_id="sha256:placeholder")
    assert compute_content_id(e_without) == compute_content_id(e_with)


def test_canonical_json_rejects_non_finite_via_null_substitution():
    """Bare ``allow_nan=False`` on json.dumps would RAISE on NaN/Inf —
    confirm the upstream normaliser substitutes ``None`` so the dumps
    call succeeds.  This is what prevents a stray NaN somewhere in a
    payload from crashing the recorder.
    """
    event = {"x": float("nan"), "y": [float("inf"), 1.0, float("-inf")]}
    out = canonical_json(event)
    # Both NaN and ±Inf should appear as ``null`` in the output.
    decoded = json.loads(out)
    assert decoded == {"x": None, "y": [None, 1.0, None]}


def test_canonical_json_is_pure_ascii_safe_for_control_chars():
    """JSON requires control characters in strings to be escaped; both
    Python and V8 emit the canonical ``\\uXXXX`` form.  This test pins
    the escape so a future change in Python's json module that started
    emitting raw control bytes (unlikely) would be caught.
    """
    event = {"msg": "line1\nline2\ttab"}
    out = canonical_json(event)
    # Newline and tab must be escaped, not literal.
    assert b"\\n" in out
    assert b"\\t" in out
    assert b"\nline2" not in out


def test_unicode_round_trip_via_canonical():
    """Round-trip an event through canonical_json + parse, confirm the
    decoded form equals the input (modulo float-NaN-substitution).
    """
    event = {"name": "Hello, 世界 🌍", "n": 42}
    decoded = json.loads(canonical_json(event))
    assert decoded == event
