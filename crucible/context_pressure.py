"""
crucible/context_pressure.py
=====================================
Context window pressure monitoring with graduated warnings.

Distinct from ``context_budget.py`` (which performs compaction):
this module *monitors* real-time token usage and emits warnings as
the model's context window fills, mirroring Claude Code's
AutoCompactState utilization thresholds.

Key design
----------
* CJK-aware token estimation via ``count_tokens()``.  Uses tiktoken
  (cl100k_base) when available; falls back to a heuristic that counts
  CJK characters as 1 token each and remaining characters as 1/4 token.
  The ``chars_per_token`` parameter on ``ContextWindowMonitor`` is
  retained for backwards compatibility but acts as a fallback coefficient
  only when tiktoken is absent and the text contains no CJK characters.
* Warning thresholds: 70%, 80%, 90%, 95% — each fires at most once per
  monitor instance (idempotent).
* Thread-safe: cumulative usage tracked under a lock.
* ``ContextWindowMonitor`` integrates with ``telemetry.emit()`` for
  threshold-crossing events.
* At 95% capacity, ``raise_if_critical()`` raises ``ContextWindowCriticalError``
  so callers can abort or compact before hitting the hard limit.

Public API
----------
* ``count_tokens(text)`` — CJK-aware token estimator (importable directly).
* ``ContextWindowMonitor`` — stateful usage tracker.

Usage::

    from crucible.context_pressure import ContextWindowMonitor, count_tokens

    n = count_tokens("Hello 世界")           # accurate CJK-aware count
    monitor = ContextWindowMonitor(max_tokens=100_000)
    monitor.record_text(prompt_text)
    monitor.record_text(response_text)
    monitor.raise_if_critical()   # raises at 95%+
    pct = monitor.utilization()   # 0.0 – 1.0
"""
from __future__ import annotations

import math
import threading
from typing import Any, Dict, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
    from .telemetry import emit as _telemetry_emit
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]
    from telemetry import emit as _telemetry_emit  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# Empirical char-per-token estimate (matches context_budget.py)
_DEFAULT_CHARS_PER_TOKEN: float = 4.0

# CJK Unicode ranges treated as 1 token per character
_CJK_RANGES: tuple = (
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x3000, 0x303F),    # CJK Symbols and Punctuation
    (0xFF00, 0xFFEF),    # Halfwidth and Fullwidth Forms
    (0x20000, 0x2A6DF),  # CJK Extension B (supplementary)
)


def _is_cjk(cp: int) -> bool:
    """Return True if Unicode codepoint *cp* falls in a CJK range."""
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def count_tokens(text: str) -> int:
    """
    Estimate the number of tokens in *text*.

    Strategy (in priority order):

    1. **tiktoken** — if ``tiktoken`` is installed, uses the ``cl100k_base``
       encoding for an exact token count.
    2. **CJK-aware heuristic** — counts CJK characters (Unicode ranges
       4E00-9FFF, 3000-303F, FF00-FFEF, 3400-4DBF, 20000-2A6DF) as 1 token
       each; counts remaining characters as 1/4 token.  Result is ceiling'd
       and clamped to a minimum of 1.

    Args:
        text: Input text (may be empty).

    Returns:
        Estimated token count (>= 1 for non-empty text, 0 for empty).
    """
    if not text:
        return 0
    # Attempt tiktoken first
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        pass
    # CJK-aware heuristic fallback
    cjk_count = 0
    non_cjk_count = 0
    for ch in text:
        if _is_cjk(ord(ch)):
            cjk_count += 1
        else:
            non_cjk_count += 1
    raw = cjk_count + non_cjk_count / 4.0
    return max(1, math.ceil(raw))

# Graduated warning thresholds (ascending order required)
_WARNING_THRESHOLDS: tuple[float, ...] = (0.70, 0.80, 0.90, 0.95)

# Critical threshold above which raise_if_critical() fires
_CRITICAL_THRESHOLD: float = 0.95


class ContextWindowCriticalError(RuntimeError):
    """
    Raised when context utilization reaches or exceeds the critical threshold
    (default 95%).  Callers should compact or abort before the model hits its
    hard context limit.
    """

    def __init__(self, utilization: float, max_tokens: int, used_tokens: int) -> None:
        self.utilization = utilization
        self.max_tokens = max_tokens
        self.used_tokens = used_tokens
        super().__init__(
            f"Context window critical: {utilization:.1%} used "
            f"({used_tokens:,}/{max_tokens:,} estimated tokens). "
            "Compact or reduce context before next LLM call."
        )


class ContextWindowMonitor:
    """
    Tracks estimated token usage and fires graduated warnings.

    Parameters
    ----------
    max_tokens:
        Hard context-window limit for the model.
        Defaults to ``CONTEXT_WINDOW_MAX_TOKENS`` env var → 100 000.
    chars_per_token:
        Character-to-token ratio for estimation.
        Defaults to ``CONTEXT_BUDGET_CHARS_PER_TOKEN`` env var → 4.0
        (same as context_budget.py for consistency).
    stage:
        Optional stage label included in emitted telemetry events.
    """

    def __init__(
        self,
        *,
        max_tokens: Optional[int] = None,
        chars_per_token: Optional[float] = None,
        stage: str = "",
    ) -> None:
        self._max_tokens: int = max(
            1_000,
            int(
                max_tokens
                if max_tokens is not None
                else _env_int("CONTEXT_WINDOW_MAX_TOKENS", 100_000)
            ),
        )
        self._chars_per_token: float = max(
            1.0,
            float(
                chars_per_token
                if chars_per_token is not None
                else _env_float("CONTEXT_BUDGET_CHARS_PER_TOKEN", _DEFAULT_CHARS_PER_TOKEN)
            ),
        )
        self._stage = stage
        self._used_tokens: int = 0
        self._lock = threading.Lock()
        # Tracks which warning thresholds have already been emitted
        self._warned: set[float] = set()

    # ── Token estimation ──────────────────────────────────────────────────────

    def _estimate(self, text: str) -> int:
        """
        Estimate token count for *text* using ``count_tokens()``.

        ``self._chars_per_token`` is kept for backwards compatibility but is no
        longer used as the primary estimation path — it only served as the old
        coefficient and is superseded by the CJK-aware ``count_tokens`` function.
        """
        return count_tokens(text)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_text(self, text: str) -> None:
        """
        Add estimated token count for *text* to the cumulative usage.

        Thread-safe.  Also triggers warning events for newly crossed thresholds.
        """
        tokens = self._estimate(text)
        self._add_tokens(tokens)

    def record_tokens(self, tokens: int) -> None:
        """
        Add an explicit *tokens* count (when the caller has an accurate count
        from the API response) to the cumulative usage.

        Thread-safe.
        """
        self._add_tokens(max(0, int(tokens)))

    def _add_tokens(self, tokens: int) -> None:
        if tokens <= 0:
            return
        # Capture newly-crossed thresholds atomically inside _lock so that
        # two concurrent record_text() calls can never both see the same
        # threshold as un-warned and double-emit the warning event.
        newly_crossed: list = []
        with self._lock:
            self._used_tokens += tokens
            used = self._used_tokens
            util = used / self._max_tokens
            for threshold in _WARNING_THRESHOLDS:
                if util >= threshold and threshold not in self._warned:
                    self._warned.add(threshold)
                    newly_crossed.append((threshold, util, used))
        # Emit log/telemetry outside the lock to avoid holding it during I/O.
        for threshold, util_val, used_val in newly_crossed:
            self._emit_threshold_warning(threshold, util_val, used_val)

    # ── Threshold monitoring ──────────────────────────────────────────────────

    def _emit_threshold_warning(self, threshold: float, util: float, used: int) -> None:
        """Emit a single threshold-crossing warning (called outside _lock)."""
        level = 40 if threshold >= _CRITICAL_THRESHOLD else 30
        msg = (
            f"Context window at {util:.1%} capacity "
            f"({used:,}/{self._max_tokens:,} est. tokens)"
        )
        log_event(
            LOGGER, level,
            "context_window_pressure",
            msg,
            utilization=round(util, 4),
            used_tokens=used,
            max_tokens=self._max_tokens,
            threshold=threshold,
            stage=self._stage,
        )
        _telemetry_emit(
            "context.window.pressure",
            payload={
                "utilization": round(util, 4),
                "used_tokens": used,
                "max_tokens": self._max_tokens,
                "threshold": threshold,
                "stage": self._stage,
            },
            source="context_pressure",
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    def utilization(self) -> float:
        """Return current utilization as a fraction (0.0 – 1.0+)."""
        with self._lock:
            return self._used_tokens / self._max_tokens

    @property
    def used_tokens(self) -> int:
        """Current cumulative estimated token count."""
        with self._lock:
            return self._used_tokens

    @property
    def max_tokens(self) -> int:
        """The configured context window size."""
        return self._max_tokens

    def remaining_tokens(self) -> int:
        """Estimated remaining token capacity (may be negative if over limit)."""
        with self._lock:
            return self._max_tokens - self._used_tokens

    def raise_if_critical(self) -> None:
        """
        Raise ``ContextWindowCriticalError`` if utilization >= critical threshold
        (default 95%).  Call before each LLM invocation to prevent hard failures.
        """
        with self._lock:
            used = self._used_tokens
            # Compute util and the raise decision inside the lock so that a
            # concurrent reset() cannot zero _used_tokens between the read of
            # ``used`` and the comparison, causing a spurious exception.
            util = used / self._max_tokens
            should_raise = util >= _CRITICAL_THRESHOLD
        if should_raise:
            raise ContextWindowCriticalError(util, self._max_tokens, used)

    def reset(self) -> None:
        """Reset usage to zero (e.g. after a context compaction)."""
        with self._lock:
            self._used_tokens = 0
            self._warned.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Return a snapshot dict of current monitor state."""
        with self._lock:
            used = self._used_tokens
            util = used / self._max_tokens
        return {
            "max_tokens": self._max_tokens,
            "used_tokens": used,
            "remaining_tokens": self._max_tokens - used,
            "utilization": round(util, 4),
            "utilization_pct": f"{util:.1%}",
            "is_critical": util >= _CRITICAL_THRESHOLD,
            "stage": self._stage,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

try:
    from . import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default)
