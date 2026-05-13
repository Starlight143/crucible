"""
Direct unit tests for ``_v8_float_repr``.

v1.1.0 third-pass: the existing ``test_js_canonical_parity.py`` test
suite exercises the encoder via ``canonical_json``, but several
boundary cases are not pinned by any fixture there.  These direct
tests verify the formatter matches V8's
``Number.prototype.toString`` algorithm at the exact ECMA-262
boundaries:

* Integer-valued floats (the original v1.1.0 fix target).
* The 1e-6 / 1e-7 boundary (V8 switches from fixed to scientific).
* The 1e21 boundary (V8 switches from fixed to scientific on the
  upper side).
* Negative zero (V8 emits bare ``"0"``, no minus sign).
* Subnormal floats (5e-324, the smallest positive double).
* Negative integer-valued floats (-3.0 → "-3").
* Non-finite leftovers (NaN / ±Inf — must not produce raw "NaN"
  tokens that downstream JSON parsers would reject).
"""
from __future__ import annotations

import math

import pytest

from crucible.features.run_insights.schema import _v8_float_repr


@pytest.mark.parametrize(
    "value,expected",
    [
        # Trivial integer cases
        (0.0, "0"),
        (-0.0, "0"),       # V8 strips the sign on negative zero
        (1.0, "1"),
        (-1.0, "-1"),
        (3.0, "3"),
        (-3.0, "-3"),
        (100.0, "100"),     # Integer-valued, fixed notation
        (1000.0, "1000"),
        # Decimal fractions in the fixed-notation regime
        (0.5, "0.5"),
        (1.5, "1.5"),
        (100.5, "100.5"),
        (-100.5, "-100.5"),
        # Small fractions just inside the fixed/scientific boundary
        # V8: 1e-6 → "0.000001"; 1e-7 → "1e-7"
        (0.000001, "0.000001"),
        (1e-6, "0.000001"),
        (1.5e-6, "0.0000015"),
        (1e-7, "1e-7"),
        (5e-324, "5e-324"),         # Smallest positive subnormal
        # Large magnitude cases — fixed/scientific boundary at 1e21
        (1e15, "1000000000000000"),
        (1e16, "10000000000000000"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1.5e21, "1.5e+21"),
        (1.23e+18, "1230000000000000000"),
        # Negatives in the scientific regime
        (-1e-7, "-1e-7"),
        (-1e21, "-1e+21"),
    ],
)
def test_v8_float_repr_matches_ecma262(value: float, expected: str):
    """Each parametrised case pins one boundary of the V8 algorithm.

    A regression in any branch of ``_v8_float_repr`` (or in the
    ECMA-262 boundary constants 21, -6) would surface as a mismatch
    here long before it broke content-id parity with the future
    Cloudflare Worker.
    """
    assert _v8_float_repr(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_v8_float_repr_non_finite_raises(value: float):
    """v1.1.0 fourth-pass: non-finite values should be substituted by
    ``_normalise_for_canonical`` upstream.  If one leaks through
    (caller bypassed normalisation), the formatter NOW raises
    ``ValueError`` rather than silently returning the literal string
    ``"null"`` — that silent path could let a NaN payload hash-
    collide with one storing actual ``None``.  Fail loud.
    """
    with pytest.raises(ValueError, match="non-finite"):
        _v8_float_repr(value)


@pytest.mark.parametrize(
    "value,expected",
    [
        # v1.1.0 fourth-pass: extra boundary cases.
        # Smallest positive normal double.
        (2.2250738585072014e-308, "2.2250738585072014e-308"),
        # 9.999e-7 — mantissa ≠ 1 at the 1e-6/1e-7 transition (fixed→sci).
        # n = 1 + (-7) = -6.  ``-6 < n ≤ 0`` is False (n == -6, not < -6),
        # so falls through to scientific. ECMA-262 stops fixed at n > -6.
        (9.999e-7, "9.999e-7"),
        # IEEE precision boundary: 1e16 + 1 is NOT exactly representable
        # (the gap from 1e16 to next double is 2.0), so 1e16+1 rounds
        # back to 1e16.  V8 emits "10000000000000000".
        (1e16 + 1, "10000000000000000"),
        # 1e16 + 2 IS the next representable double; emits exactly.
        (1e16 + 2, "10000000000000002"),
        # Negative integer with magnitude >= 1e16
        (-1e18, "-1000000000000000000"),
    ],
)
def test_v8_float_repr_additional_boundaries(value: float, expected: str):
    """Pin extra edge cases discovered in the fourth-pass audit."""
    assert _v8_float_repr(value) == expected


def test_v8_float_repr_round_trip_via_json_python_repr():
    """For finite floats Python's ``repr`` is the shortest round-trip
    representation.  We do NOT claim V8 and Python produce the SAME
    string — only that V8's chosen form, when interpreted as JSON, has
    the SAME numeric value as the input.

    This is the round-trip property the canonical-id depends on: two
    payloads that V8 and Python both reduce to "1e-7" must hash the
    same; one to "1e-7" and one to "1.0000000000000001e-7" must NOT.
    """
    import json
    for v in [0.5, 1.5, -1.5, 1e-6, 1e-7, 1.5e21, 1e20, math.pi, math.e]:
        repr_v8 = _v8_float_repr(v)
        # The string must parse back to a float (any valid JSON number).
        parsed = json.loads(repr_v8)
        # And the parsed value must be VERY close to the original (we
        # accept ULP-level differences because V8 emits the shortest
        # round-trip string, which is monotone to Python's repr but not
        # necessarily byte-identical).
        assert math.isclose(parsed, v, rel_tol=1e-15, abs_tol=0.0), (
            f"V8 form {repr_v8!r} does not round-trip to {v!r}"
        )


def test_v8_float_repr_no_leading_zero_exponent():
    """V8 never emits leading zeros in the exponent.  Python's repr
    produces "1e-07"; the encoder must drop the leading zero so the
    canonical bytes are "1e-7" and match V8."""
    out = _v8_float_repr(1e-10)
    assert "e-10" in out and "e-010" not in out and "e-0" not in out


def test_v8_float_repr_explicit_minus_exponent():
    """Scientific notation in V8 uses "e-N" (no plus sign for negative
    exponents) but "e+N" (with plus) for positive ones.  Both must be
    preserved."""
    assert _v8_float_repr(1e-7).startswith("1e-")
    assert _v8_float_repr(1e21).startswith("1e+")
