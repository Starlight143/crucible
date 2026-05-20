"""Per-provider adaptive cooldown for librarian search dispatch.

v1.1.8 extended (Phase 2, Q2).  When a provider returns 429 (rate limit)
or 202 (DuckDuckGo bot-detection mode), the cooldown registry records a
future "do not call until" timestamp.  Cooldown duration doubles on each
successive trigger up to a configured cap.

Cooldown is process-local — restart of Crucible clears all cooldowns.
This is intentional: long-lived cooldowns belong in a distributed cache,
not in librarian state.  The 30-minute default cap is sufficient for a
single Crucible run because typical run wall-clock is < 30 minutes.

Failure modes are non-fatal: a cooldown lookup never raises.

Typical usage in the search dispatcher (Phase 3)::

    registry = CooldownRegistry.get_default()
    if registry.is_cooling_down("websearch"):
        # skip this provider, try fallback
        return None
    try:
        result = _safe_http_text(...)
    except HTTPStatusError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status in (429, 202):
            registry.trigger("websearch", reason=f"http_{status}")
        raise
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

try:
    from .._env import env_int
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_int  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


@dataclass
class _CooldownState:
    """Per-provider cooldown state, mutable under the registry lock."""

    cooldown_until_ts: float = 0.0
    last_duration_seconds: int = 0
    trigger_count: int = 0
    last_trigger_reason: str = ""
    last_triggered_at_ts: float = 0.0


def _initial_seconds() -> int:
    val = env_int("LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS", 60)
    if val is None or val <= 0:
        return 60
    return int(val)


def _max_seconds() -> int:
    val = env_int("LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS", 1800)
    if val is None or val <= 0:
        return 1800
    return int(val)


class CooldownRegistry:
    """Process-local registry of per-provider cooldown timers.

    Thread-safe (single internal lock).  Singleton via ``get_default()``.
    """

    _instance_lock = threading.Lock()
    _instance: Optional["CooldownRegistry"] = None

    @classmethod
    def get_default(cls) -> "CooldownRegistry":
        """Process-wide singleton accessor."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_default(cls) -> None:
        """Reset the process-wide singleton.  Test-only."""
        with cls._instance_lock:
            cls._instance = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, _CooldownState] = {}

    def is_cooling_down(self, provider: str) -> bool:
        """True iff *provider* is currently in cooldown.

        Calling this is cheap; safe to use as a gate before every
        request.  Uses ``time.monotonic()`` so the comparison is robust
        against wall-clock jumps (NTP adjustments, etc).
        """
        if not provider:
            return False
        now = time.monotonic()
        with self._lock:
            st = self._state.get(provider)
            if st is None:
                return False
            return st.cooldown_until_ts > now

    def remaining_seconds(self, provider: str) -> float:
        """How many seconds until *provider* is callable again.

        Returns 0.0 if not in cooldown.  Result is ``max(0, ...)`` to
        keep it sensible.
        """
        if not provider:
            return 0.0
        now = time.monotonic()
        with self._lock:
            st = self._state.get(provider)
            if st is None:
                return 0.0
            return max(0.0, st.cooldown_until_ts - now)

    def trigger(self, provider: str, reason: str = "") -> int:
        """Record a 429 / 202 trigger for *provider*.

        Returns the duration in seconds the provider will now be in
        cooldown.  Pattern:

        * First trigger (no prior state) = ``initial`` seconds.
        * Subsequent trigger while still cooling = double last_duration.
        * Trigger after cooldown ended = back to ``initial``.

        All capped at ``max_seconds``.
        """
        if not provider:
            return 0
        initial = _initial_seconds()
        cap = _max_seconds()
        now = time.monotonic()
        with self._lock:
            st = self._state.setdefault(provider, _CooldownState())
            if st.last_duration_seconds <= 0:
                duration = initial
            elif st.cooldown_until_ts > now:
                # Still cooling — double.
                duration = min(cap, st.last_duration_seconds * 2)
            else:
                # Cooldown ended; a fresh burst starts at initial again.
                duration = initial
            st.last_duration_seconds = duration
            st.cooldown_until_ts = now + duration
            st.trigger_count += 1
            st.last_trigger_reason = reason or ""
            st.last_triggered_at_ts = now
            new_trigger_count = st.trigger_count
        LOGGER.warning(
            "librarian provider %s entering cooldown for %ds "
            "(reason=%r, trigger_count=%d)",
            provider, duration, reason, new_trigger_count,
        )
        return duration

    def clear(self, provider: str) -> None:
        """Manually clear cooldown for *provider*.

        Test / admin use only — production code should let cooldowns
        expire naturally.
        """
        if not provider:
            return
        with self._lock:
            self._state.pop(provider, None)

    def clear_all(self) -> None:
        """Manually clear ALL provider cooldowns.  Test-only."""
        with self._lock:
            self._state.clear()

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        """Return a copy of current cooldown state for observability."""
        now = time.monotonic()
        out: Dict[str, Dict[str, float]] = {}
        with self._lock:
            for provider, st in self._state.items():
                out[provider] = {
                    "cooldown_remaining_seconds": max(
                        0.0, st.cooldown_until_ts - now
                    ),
                    "last_duration_seconds": float(st.last_duration_seconds),
                    "trigger_count": float(st.trigger_count),
                    "last_trigger_reason": st.last_trigger_reason,  # type: ignore[dict-item]
                }
        return out
