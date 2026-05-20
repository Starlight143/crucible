"""Cross-provider query deduplication for librarian dispatch.

v1.1.8 extended (Phase 4, Q6).  Tracks which (normalised_query, query_class)
pairs have already produced results from some provider during the current
run.  When the dispatcher considers running another provider on the same
pair, it consults the registry and skips if already covered.

Saves typically ~30% of HTTP calls in runs that share queries across
multiple lanes / query classes.  Operator can disable via
``LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED=0``.

Not thread-safe — single-process librarian dispatch is sequential, and
the in-memory map fits cleanly in the existing module-level scope.

Failure modes: an empty / None input never raises (graceful no-op).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# Tri-modal import.
try:
    from .._env import env_bool
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


def _normalize(query: str) -> str:
    """Match SearchCache normalisation (Phase 2) so the dedup key
    aligns with cache lookups."""
    return " ".join((query or "").strip().lower().split())


def dedup_enabled() -> bool:
    """Master toggle for cross-provider dedup."""
    return env_bool("LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED", True)


class QueryDedupRegistry:
    """Per-run cross-provider query dedup tracker.

    Lifecycle: instantiated once at the start of a librarian search and
    discarded at the end.  ``mark_covered`` is called after a provider
    successfully produces results for ``(query, query_class)``;
    ``is_covered`` is queried before dispatching subsequent providers on
    the same pair.

    Implementation: simple dict keyed by ``(normalised_query, class)``.
    Value is the FIRST provider that covered the pair, useful for
    observability (logging which provider "won" the race).
    """

    def __init__(self) -> None:
        self._covered: Dict[Tuple[str, str], str] = {}

    def is_covered(self, query: str, query_class: str) -> bool:
        """True iff *(query, query_class)* already has results from
        some provider."""
        if not query or not query_class:
            return False
        norm = _normalize(query)
        return (norm, query_class) in self._covered

    def covered_by(self, query: str, query_class: str) -> Optional[str]:
        """Return the FIRST provider that covered *(query, query_class)*,
        or None if not covered."""
        if not query or not query_class:
            return None
        norm = _normalize(query)
        return self._covered.get((norm, query_class))

    def mark_covered(
        self,
        query: str,
        query_class: str,
        provider: str,
    ) -> bool:
        """Record that *provider* produced results for
        *(query, query_class)*.

        Returns True if this is the first time the pair is marked
        (i.e. the caller is the "winner"); False if it was already
        covered by a previous provider.
        """
        if not query or not query_class or not provider:
            return False
        norm = _normalize(query)
        key = (norm, query_class)
        if key in self._covered:
            return False
        self._covered[key] = provider
        return True

    def clear(self) -> None:
        """Reset the registry for a new run."""
        self._covered.clear()

    def snapshot(self) -> Dict[Tuple[str, str], str]:
        """Return a copy of the coverage map for observability."""
        return dict(self._covered)

    def __len__(self) -> int:
        return len(self._covered)
