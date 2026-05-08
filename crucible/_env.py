"""
Centralised environment-variable parsing helpers.
=================================================

Project-wide rule: boolean env-var whitelist
--------------------------------------------
Boolean env vars must use a strict whitelist:

* ``1`` / ``true`` / ``yes`` / ``on``  →  ``True``
* ``0`` / ``false`` / ``no`` / ``off`` →  ``False``

Anything else (including typos like ``trrue`` or ``yse``) returns the *default*
rather than being silently coerced to truthy.  The optional ``extended`` flag
also accepts the single-letter forms ``y`` / ``n`` for legacy compatibility
(``crucible/modules/section_00_bootstrap_and_utils.py`` historically allowed
those tokens, so the flag preserves that behaviour).

Float helpers
-------------
``env_float`` accepts ``finite_only=True`` to reject ``NaN`` / ``Inf`` payloads
before they propagate into module constants and break downstream comparisons
(``z_threshold=NaN`` makes every ``>`` test ``False``, etc.).  This matches the
existing inline guard pattern present across ``crucible/features/*``.

Optional / sentinel-aware variants
----------------------------------
``env_optional_int`` / ``env_optional_float`` honour the ``unlimited`` / ``inf``
sentinels that ``crucible/modules/section_00_bootstrap_and_utils.py`` exposed
to operators for ``CRUCIBLE_*_LIMIT`` style settings — those tokens map to
``None`` (i.e. "no cap") rather than to the default.

Backward compatibility
----------------------
This module is *additive*.  Existing per-file ``_env_*`` helpers continue to
exist but now delegate to these functions, preserving every documented
behavioural quirk through explicit flags.  No call site is forced to change.
"""

from __future__ import annotations

import math
import os
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────────────────

_TRUE_TOKENS: frozenset[str] = frozenset({"1", "true", "yes", "on"})
_FALSE_TOKENS: frozenset[str] = frozenset({"0", "false", "no", "off"})
_TRUE_TOKENS_EXTENDED: frozenset[str] = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_TOKENS_EXTENDED: frozenset[str] = frozenset({"0", "false", "no", "n", "off"})
_NONE_TOKENS: frozenset[str] = frozenset({"none", "null", "unlimited", "inf", "infinite"})


# ── String ────────────────────────────────────────────────────────────────────

def env_str(name: str, default: str, *, strip: bool = True) -> str:
    """Read a string env var, returning ``default`` when unset or empty.

    Parameters
    ----------
    strip
        If ``True`` (the default), surrounding whitespace is removed and a
        whitespace-only value is treated as missing.  Set to ``False`` to
        preserve raw whitespace (rare; matches the legacy
        ``os.environ.get(name, default)`` shape that a couple of modules
        relied on).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    if strip:
        stripped = raw.strip()
        return stripped or default
    return raw or default


def env_str_passthrough(name: str, default: str) -> str:
    """Read an env var without stripping, falling back to ``default`` only when unset.

    Mirrors the bare ``os.environ.get(name, default)`` semantics where an empty
    string explicitly assigned by the operator (``EXPORT FOO=``) is preserved
    as ``""`` — distinct from "unset", which returns ``default``.
    """
    val = os.environ.get(name)
    return default if val is None else val


# ── Integer ───────────────────────────────────────────────────────────────────

def env_int(
    name: str,
    default: int,
    *,
    clamp_min: Optional[int] = None,
    clamp_max: Optional[int] = None,
) -> int:
    """Read an integer env var, returning ``default`` on absence or parse failure.

    The optional ``clamp_min`` / ``clamp_max`` bounds are applied after parsing
    so that callers can emulate the historical ``max(1, int(v))`` /
    ``max(0, int(v))`` patterns without re-implementing the helper.
    """
    raw = os.environ.get(name, "")
    try:
        result = int(raw) if raw.strip() else default
    except (ValueError, TypeError):
        return default
    if clamp_min is not None and result < clamp_min:
        result = clamp_min
    if clamp_max is not None and result > clamp_max:
        result = clamp_max
    return result


# ── Float ─────────────────────────────────────────────────────────────────────

def env_float(
    name: str,
    default: float,
    *,
    finite_only: bool = False,
    clamp_min: Optional[float] = None,
    clamp_max: Optional[float] = None,
) -> float:
    """Read a float env var, returning ``default`` on absence or parse failure.

    ``finite_only=True`` rejects ``NaN`` / ``±Inf`` (returning *default*) so
    that statistical thresholds and correlation configs cannot be silently
    poisoned by non-finite input.
    """
    raw = os.environ.get(name, "")
    try:
        if not raw.strip():
            return default
        result = float(raw)
    except (ValueError, TypeError):
        return default
    if finite_only and not math.isfinite(result):
        return default
    if clamp_min is not None and result < clamp_min:
        result = clamp_min
    if clamp_max is not None and result > clamp_max:
        result = clamp_max
    return result


# ── Boolean ───────────────────────────────────────────────────────────────────

def env_bool(name: str, default: bool, *, extended: bool = False) -> bool:
    """Read a boolean env var using the project-wide whitelist.

    Recognised truthy tokens (case-insensitive after ``strip()``):
    ``1``, ``true``, ``yes``, ``on`` (plus ``y`` when ``extended=True``).

    Recognised falsy tokens:
    ``0``, ``false``, ``no``, ``off`` (plus ``n`` when ``extended=True``).

    Anything else — including typos and unrecognised tokens — returns
    ``default`` (no silent coercion to truthy).
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    true_set = _TRUE_TOKENS_EXTENDED if extended else _TRUE_TOKENS
    false_set = _FALSE_TOKENS_EXTENDED if extended else _FALSE_TOKENS
    if raw in true_set:
        return True
    if raw in false_set:
        return False
    return default


# ── Optional (sentinel-aware) variants ────────────────────────────────────────

def env_optional_int(name: str, default: Optional[int]) -> Optional[int]:
    """Read an integer env var that may be the sentinel "no cap" value.

    Tokens ``none``, ``null``, ``unlimited``, ``inf``, ``infinite``
    (case-insensitive) map to ``None`` — the convention used across the
    Crucible runtime to represent "no cap" / "unlimited".
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    if raw.lower() in _NONE_TOKENS:
        return None
    try:
        return int(raw)
    except ValueError:
        return default


def env_optional_float(name: str, default: Optional[float]) -> Optional[float]:
    """Float counterpart to :func:`env_optional_int` (sentinel-aware)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    if raw.lower() in _NONE_TOKENS:
        return None
    try:
        return float(raw)
    except ValueError:
        return default


__all__ = [
    "env_str",
    "env_str_passthrough",
    "env_int",
    "env_float",
    "env_bool",
    "env_optional_int",
    "env_optional_float",
]
