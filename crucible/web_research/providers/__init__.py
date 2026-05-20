"""Librarian provider clients added in v1.1.8 extended (Phase 3, Q4).

These are zero-auth additional providers that supplement the core
list (websearch / context7 / grep_app / github / arxiv / paperswithcode).
Each provider exposes a single ``search_<provider>`` function with a
unified signature::

    def search_<provider>(
        query: str,
        *,
        limit: int = 3,
        timeout_seconds: float = 15.0,
    ) -> List[ResearchCitation]

Behaviour contract:

* Return up to ``limit`` citations.
* Return ``[]`` on any error or empty result — the dispatcher handles
  the fallback chain; providers themselves do not raise for routine
  failures.
* HTTP requests MUST go through ``safe_http_*`` helpers from
  ``crucible.web_research.http_clients`` to inherit SSRF protection.
* Cache / cooldown / health observability is wired by the dispatcher
  (``section_04``) — provider modules deliberately do NOT touch those
  modules.  This keeps provider implementations focused and testable
  in isolation.

The provider registry is the dict ``PROVIDERS`` at the bottom of this
module — call sites look up by lowercase name (``"openalex"``,
``"crossref"``, etc.).
"""

from __future__ import annotations

from typing import Callable, Dict, List

# Tri-modal import — see ``crucible/features/run_insights/recorder.py``.
try:
    from ...modules.section_03_models_and_context import ResearchCitation
except ImportError:  # pragma: no cover - flat-launcher fallback
    from modules.section_03_models_and_context import ResearchCitation  # type: ignore[no-redef]

from .crossref import search_crossref
from .openalex import search_openalex
from .searxng import search_searxng
from .wikipedia import search_wikipedia


SearchFunction = Callable[..., List[ResearchCitation]]


PROVIDERS: Dict[str, SearchFunction] = {
    "openalex": search_openalex,
    "crossref": search_crossref,
    "wikipedia": search_wikipedia,
    "searxng": search_searxng,
}


def get_provider(name: str) -> SearchFunction | None:
    """Look up a v1.1.8 extended provider by name.

    Returns ``None`` if the name is unknown.  Case-insensitive.
    """
    if not name:
        return None
    return PROVIDERS.get(name.strip().lower())


def known_provider_names() -> List[str]:
    """Return the sorted list of registered provider names."""
    return sorted(PROVIDERS.keys())


__all__ = [
    "PROVIDERS",
    "get_provider",
    "known_provider_names",
    "search_crossref",
    "search_openalex",
    "search_searxng",
    "search_wikipedia",
]
