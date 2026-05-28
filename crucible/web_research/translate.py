"""CJK to English query translation for the librarian.

v1.1.8 extended (Phase 6, Q10).  When a CJK-language query returns
fewer than ``LIBRARIAN_BILINGUAL_QUERY_THRESHOLD`` native-language
citations, the librarian auto-issues an English mirror of the query.
Cross-language results are deduped so the same paper found via Chinese
title and English title only counts as one citation.

Design:

* ``contains_cjk(text)`` — pure-Python Unicode-range check.  No
  external dependencies.
* ``translate_cjk_to_en(text, translate_fn)`` — invokes the caller-
  supplied ``translate_fn`` (typically a thin wrapper around the
  librarian LLM client).  Result is cached in a process-local dict so
  identical queries in the same run don't re-pay the LLM cost.
* ``translate_enabled()`` / ``bilingual_threshold()`` — env-gated.

Translation cache is in-memory and per-process; we don't reach into the
v1.1.8 Phase 2 disk cache for translations to keep the boundary simple
(the LLM cost saved by caching is already negligible for translations,
and disk-cached translations risk staleness if the operator switches
models).

Coupling to LLM: deliberately NONE.  Callers inject the translation
function so this module doesn't depend on the librarian's LLM machinery
and can be tested in isolation with synthetic ``translate_fn`` mocks.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

# Tri-modal import.
try:
    from .._env import env_bool, env_int, env_str
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool, env_int, env_str  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


# Unicode ranges for CJK characters.  Covers the common cases:
# * Han ideographs: U+4E00-U+9FFF (basic) + U+3400-U+4DBF (extension A)
# * CJK Symbols and Punctuation: U+3000-U+303F
# * Hiragana: U+3040-U+309F
# * Katakana: U+30A0-U+30FF
# * Hangul Syllables: U+AC00-U+D7AF
# * Half-width / Full-width forms: U+FF00-U+FFEF
_CJK_RANGES = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x3000, 0x303F),   # CJK Symbols and Punctuation
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0xFF00, 0xFFEF),   # Half-width and Full-width Forms
)


def contains_cjk(text: str) -> bool:
    """Return True iff *text* contains at least one CJK character.

    Pure substring scan over a small list of Unicode ranges — O(n).
    """
    if not text:
        return False
    for ch in text:
        codepoint = ord(ch)
        for low, high in _CJK_RANGES:
            if low <= codepoint <= high:
                return True
    return False


def bilingual_enabled() -> bool:
    """Master toggle for bilingual query expansion."""
    return env_bool("LIBRARIAN_BILINGUAL_QUERY_EXPANSION", True)


def bilingual_threshold() -> int:
    """Native-language result count below which English mirror is
    issued.  Clamped to [1, 50].
    """
    val = env_int("LIBRARIAN_BILINGUAL_QUERY_THRESHOLD", 3)
    if val is None or val < 1:
        return 3
    return min(50, int(val))


def translate_model_name() -> str:
    """Override LLM model for translation only.  Empty string =
    reuse the librarian model.
    """
    return env_str("LIBRARIAN_QUERY_TRANSLATE_MODEL", "")


# Process-local translation cache.  Keyed by raw CJK query (after
# normalisation); value is the English mirror.  Lifetime is the whole
# process — translations are expensive enough to want long-lived
# caching, cheap enough to fit in memory.
_TRANSLATION_CACHE: dict[str, str] = {}
_TRANSLATION_CACHE_LOCK = threading.Lock()


# Type alias for the caller-supplied translation function.
#   translate_fn(text) -> english_str | None
# Returning None signals translation failure (caller falls back to
# original query).
TranslateFn = Callable[[str], Optional[str]]


def _normalise_for_cache(text: str) -> str:
    """Normalise a CJK string for stable cache keying.

    Strips whitespace, lowercases ASCII characters (CJK characters are
    untouched — case is not meaningful in CJK), collapses internal
    whitespace.
    """
    return " ".join((text or "").strip().lower().split())


def translate_cjk_to_en(
    text: str,
    translate_fn: TranslateFn,
) -> Optional[str]:
    """Translate a CJK query to English.

    Returns the translated string, or None on failure / no-cjk-content.
    Caches successful translations in a process-local map.

    The caller-supplied *translate_fn* takes the CJK string and returns
    the English string (or None on failure).  Typically a thin wrapper
    around the librarian LLM client; can be a hardcoded dict / fake in
    tests.
    """
    if not text or not bilingual_enabled():
        return None
    if not contains_cjk(text):
        return None
    cache_key = _normalise_for_cache(text)
    with _TRANSLATION_CACHE_LOCK:
        cached = _TRANSLATION_CACHE.get(cache_key)
        if cached is not None:
            return cached
    # Cache miss — call the translation function.  Any exception is
    # swallowed (graceful degradation; original query still works in
    # CJK mode).
    try:
        english = translate_fn(text)
    except Exception as exc:
        LOGGER.debug("translate_cjk_to_en: translate_fn raised: %s", exc)
        return None
    if not english or not isinstance(english, str):
        return None
    english_clean = english.strip()
    if not english_clean:
        return None
    # NOTE (v1.1.11): the only echo we can cheaply detect is a CJK-containing
    # echo (below).  A non-CJK failure sentinel (e.g. the LLM returning the
    # literal string "translation failed") is indistinguishable from a real
    # short translation without a semantic check this module intentionally
    # avoids (no LLM coupling — see module docstring).  Such a value would be
    # cached and issued as one extra English mirror query; downstream dedup +
    # scoring absorb it, so this is an accepted, low-impact limitation.
    # Sanity check: translated output should NOT still contain CJK.
    # If it does, the translation likely failed (LLM echoed the input).
    if contains_cjk(english_clean):
        LOGGER.debug(
            "translate_cjk_to_en: refusing translation that still "
            "contains CJK characters: %r → %r", text, english_clean,
        )
        return None
    with _TRANSLATION_CACHE_LOCK:
        _TRANSLATION_CACHE[cache_key] = english_clean
    return english_clean


def should_translate_for_query(
    query: str,
    native_result_count: int,
) -> bool:
    """Decide whether to issue an English mirror for *query*.

    Returns True iff:
    * Bilingual expansion is enabled (env), AND
    * Query contains CJK characters, AND
    * Native-language results returned < ``bilingual_threshold``.
    """
    if not bilingual_enabled():
        return False
    if not contains_cjk(query):
        return False
    return native_result_count < bilingual_threshold()


def clear_translation_cache() -> None:
    """Clear the process-local translation cache.  Test / admin use."""
    with _TRANSLATION_CACHE_LOCK:
        _TRANSLATION_CACHE.clear()


def translation_cache_size() -> int:
    """Return current number of cached translations.  Observability."""
    with _TRANSLATION_CACHE_LOCK:
        return len(_TRANSLATION_CACHE)
