"""Tavily provider — AI-optimised web search via the Tavily Search API.

Added as an optional parallel provider alongside DuckDuckGo 'websearch'.
Opt in via ``LIBRARIAN_EXTRA_PROVIDERS=...,tavily``.

Requires:
* ``tavily-python>=0.3`` (pip install tavily-python)
* ``TAVILY_API_KEY`` environment variable set to a valid API key
  (get one free at https://app.tavily.com).

The implementation follows the unified provider signature and error
contract established by searxng.py / crossref.py: return up to *limit*
``ResearchCitation`` objects, return ``[]`` on any error or empty result,
never raise for routine failures.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Tri-modal import.
try:
    from ...modules.section_03_models_and_context import ResearchCitation
    from ...runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from modules.section_03_models_and_context import ResearchCitation  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


def search_tavily(
    query: str,
    *,
    limit: int = 3,
    timeout_seconds: float = 15.0,
) -> List[ResearchCitation]:
    """Search the web via the Tavily Search API.

    Returns up to *limit* citations.  Empty list on any error or empty
    result.  Never raises for routine failures.
    """
    if not query or not query.strip():
        return []
    if limit <= 0:
        return []

    try:
        from tavily import TavilyClient  # type: ignore[import-untyped]
    except ImportError:
        LOGGER.debug(
            "tavily: tavily-python package not installed — returning []"
        )
        return []

    import os

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        LOGGER.debug("tavily: TAVILY_API_KEY not set — returning []")
        return []

    try:
        client = TavilyClient(api_key=api_key)
        response: Dict[str, Any] = client.search(
            query=query.strip(),
            max_results=limit,
            search_depth="basic",
            topic="general",
        )
    except Exception as exc:
        LOGGER.debug("tavily: search failed: %s", exc)
        return []

    return _parse_results(response, query, limit)


def _parse_results(
    data: Any,
    query: str,
    limit: int,
) -> List[ResearchCitation]:
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []
    out: List[ResearchCitation] = []
    for entry in results[:limit]:
        cit = _entry_to_citation(entry, query)
        if cit is not None:
            out.append(cit)
    return out


def _entry_to_citation(
    entry: Any,
    query: str,
) -> Optional[ResearchCitation]:
    if not isinstance(entry, dict):
        return None
    title = str(entry.get("title") or "").strip()
    url_str = str(entry.get("url") or "").strip()
    if not title or not url_str:
        return None
    if not url_str.startswith(("http://", "https://")):
        return None
    snippet = str(entry.get("content") or "").strip()
    return ResearchCitation(
        provider="tavily",
        title=title[:200],
        url=url_str,
        snippet=snippet[:400],
        query=query,
        evidence_type="web_result",
        verification_status="search_snippet",
    )
