"""Wikipedia provider — definitional baselines and Tier-1 anchors.

v1.1.8 extended (Phase 3, Q4).  Wikipedia REST API
(https://en.wikipedia.org/api/rest_v1/) is unlimited and unauthenticated.

Strong for:
* Definitional Tier-1 anchors (Sharpe ratio, Bayesian inference, etc.).
* Cross-lingual coverage via the opensearch endpoint.
* Quickly establishing what a term means before deeper research.

Weak for:
* Niche / specialist topics (use OpenAlex + arXiv for those).
* Cutting-edge research (Wikipedia lags real research by months).
"""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import quote, quote_plus

# Tri-modal import.
try:
    from ...modules.section_03_models_and_context import ResearchCitation
    from ...runtime_logging import get_logger
    from ..http_clients import safe_http_json
except ImportError:  # pragma: no cover - flat-launcher fallback
    from modules.section_03_models_and_context import ResearchCitation  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]
    from web_research.http_clients import safe_http_json  # type: ignore[no-redef]


LOGGER = get_logger(__name__)

_OPENSEARCH_BASE = "https://en.wikipedia.org/w/api.php"
_SUMMARY_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"

_USER_AGENT = (
    "Crucible/1.1.8 librarian (https://github.com/Starlight143/crucible)"
)


def search_wikipedia(
    query: str,
    *,
    limit: int = 3,
    timeout_seconds: float = 15.0,
) -> List[ResearchCitation]:
    """Search Wikipedia for *query*.

    Two-step:
    1. ``opensearch`` to find matching page titles.
    2. ``page/summary`` for each top result to get the abstract.

    Returns up to *limit* citations.  Empty list on error or no
    results.  Never raises.
    """
    if not query or not query.strip():
        return []
    if limit <= 0:
        return []
    titles = _opensearch_titles(query.strip(), limit, timeout_seconds)
    if not titles:
        return []
    out: List[ResearchCitation] = []
    for title in titles[:limit]:
        cit = _summary_to_citation(title, query, timeout_seconds)
        if cit is not None:
            out.append(cit)
    return out


def _opensearch_titles(
    query: str,
    limit: int,
    timeout_seconds: float,
) -> List[str]:
    """Step 1: opensearch returns matching titles."""
    encoded = quote_plus(query)
    url = (
        f"{_OPENSEARCH_BASE}"
        f"?action=opensearch"
        f"&search={encoded}"
        f"&limit={int(min(limit, 10))}"
        f"&namespace=0"
        f"&format=json"
    )
    try:
        data = safe_http_json(
            url,
            timeout_seconds=float(timeout_seconds),
            max_bytes=256 * 1024,
            user_agent=_USER_AGENT,
            circuit_breaker_name="librarian_wikipedia:en.wikipedia.org",
        )
    except Exception as exc:
        LOGGER.debug("wikipedia opensearch: HTTP error: %s", exc)
        return []
    # opensearch returns [query, [titles], [descriptions], [urls]].
    if not isinstance(data, list) or len(data) < 2:
        return []
    titles = data[1]
    if not isinstance(titles, list):
        return []
    return [str(t).strip() for t in titles if str(t).strip()]


def _summary_to_citation(
    title: str,
    query: str,
    timeout_seconds: float,
) -> Optional[ResearchCitation]:
    """Step 2: ``page/summary`` returns abstract + canonical URL."""
    # The summary endpoint takes the title in the path.  quote() is
    # used (not quote_plus) because Wikipedia uses underscores for
    # spaces and ``quote`` handles them more naturally.
    title_encoded = quote(title.replace(" ", "_"), safe="")
    url = f"{_SUMMARY_BASE}/{title_encoded}"
    try:
        data = safe_http_json(
            url,
            timeout_seconds=float(timeout_seconds),
            max_bytes=256 * 1024,
            user_agent=_USER_AGENT,
            circuit_breaker_name="librarian_wikipedia:en.wikipedia.org",
        )
    except Exception as exc:
        LOGGER.debug("wikipedia summary: HTTP error for %r: %s", title, exc)
        return None
    if not isinstance(data, dict):
        return None
    extract = str(data.get("extract") or "").strip()
    canonical_url = str(
        ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
        or ""
    ).strip()
    if not canonical_url:
        # Construct fallback URL from title.
        canonical_url = f"https://en.wikipedia.org/wiki/{title_encoded}"
    return ResearchCitation(
        provider="wikipedia",
        title=title[:200],
        url=canonical_url,
        snippet=extract[:400],
        query=query,
        evidence_type="docs",
        verification_status="fetched_excerpt",
    )
