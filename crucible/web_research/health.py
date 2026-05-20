"""Per-provider health tracking + summary emission for the librarian.

v1.1.8 extended (Phase 2, Q7).  Counts requests, 200 OK, 429 / 202,
timeouts, citation yield, and cache hits per provider per librarian-stage
run.  At end-of-stage prints a one-line summary per provider AND emits a
``provider_health_summary`` ledger event for v1.2.0 retrieval.

Failure modes: counter increments never raise.  Summary emission is
best-effort (recorder swallow contract).

Typical usage::

    tracker = HealthTracker.get_default()
    tracker.record_request("websearch")
    try:
        results = _safe_http_text(...)
        tracker.record_ok("websearch")
        tracker.record_citations("websearch", len(results))
    except HTTPStatusError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status == 429:
            tracker.record_rate_limit("websearch")
        elif status == 202:
            tracker.record_bot_detection("websearch")
        else:
            tracker.record_other_error("websearch")
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

try:
    from .._env import env_bool
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


@dataclass
class _ProviderCounters:
    """Per-provider counter bundle.  All increments are guarded by
    ``HealthTracker._lock``."""

    requests: int = 0
    ok_200: int = 0
    rate_limited_429: int = 0
    bot_detected_202: int = 0
    timeouts: int = 0
    other_errors: int = 0
    citations_yielded: int = 0
    cache_hits: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "requests": self.requests,
            "ok_200": self.ok_200,
            "rate_limited_429": self.rate_limited_429,
            "bot_detected_202": self.bot_detected_202,
            "timeouts": self.timeouts,
            "other_errors": self.other_errors,
            "citations_yielded": self.citations_yielded,
            "cache_hits": self.cache_hits,
        }


class HealthTracker:
    """Process-local per-provider counter aggregator.

    Thread-safe via single internal lock.  Singleton via
    ``get_default()``.

    Counter semantics:

    * ``record_request`` is called on EVERY outbound call regardless of
      outcome.  This is the denominator for success-rate calculations.
    * ``record_ok`` is called on HTTP 200 successful response.
    * ``record_rate_limit`` for HTTP 429.
    * ``record_bot_detection`` for HTTP 202 (DDG-specific).
    * ``record_timeout`` for ``httpx.TimeoutException``.
    * ``record_other_error`` for any other failure (5xx, network errors).
    * ``record_citations(n)`` for the number of unique citations
      yielded from this call.  Use after parsing succeeded.
    * ``record_cache_hit`` when a Q1 cache HIT short-circuits an
      outbound call.  Increments cache_hits but NOT requests (since no
      request was actually issued).
    """

    _instance_lock = threading.Lock()
    _instance: Optional["HealthTracker"] = None

    @classmethod
    def get_default(cls) -> "HealthTracker":
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
        self._state: Dict[str, _ProviderCounters] = {}

    def _get(self, provider: str) -> _ProviderCounters:
        """Internal: caller MUST hold ``self._lock``."""
        return self._state.setdefault(provider, _ProviderCounters())

    def record_request(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).requests += 1

    def record_ok(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).ok_200 += 1

    def record_rate_limit(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).rate_limited_429 += 1

    def record_bot_detection(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).bot_detected_202 += 1

    def record_timeout(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).timeouts += 1

    def record_other_error(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).other_errors += 1

    def record_citations(self, provider: str, n: int) -> None:
        if not provider or n <= 0:
            return
        with self._lock:
            self._get(provider).citations_yielded += int(n)

    def record_cache_hit(self, provider: str) -> None:
        if not provider:
            return
        with self._lock:
            self._get(provider).cache_hits += 1

    def reset(self) -> None:
        """Clear all counters (test / end-of-stage cleanup)."""
        with self._lock:
            self._state.clear()

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return a copy of current counters for observability."""
        with self._lock:
            return {p: c.to_dict() for p, c in self._state.items()}

    def format_summary_lines(self) -> List[str]:
        """Render a list of one-line strings suitable for stdout.

        One line per provider, sorted alphabetically.  If no providers
        have been touched, returns a single placeholder line.
        """
        snap = self.snapshot()
        if not snap:
            return ["[librarian] (no provider activity this run)"]
        out: List[str] = []
        for provider in sorted(snap.keys()):
            c = snap[provider]
            out.append(
                f"[librarian] {provider}: "
                f"{c['requests']} req, "
                f"{c['ok_200']} ok, "
                f"{c['rate_limited_429']} 429, "
                f"{c['bot_detected_202']} 202(bot), "
                f"{c['timeouts']} timeout, "
                f"{c['cache_hits']} cache_hit. "
                f"Citations: {c['citations_yielded']}."
            )
        return out


def health_summary_enabled() -> bool:
    """Whether end-of-stage health summary should be emitted."""
    return env_bool("LIBRARIAN_PROVIDER_HEALTH_SUMMARY", True)
