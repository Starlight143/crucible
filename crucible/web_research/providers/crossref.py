"""Crossref provider — DOI metadata, cross-discipline references.

v1.1.8 extended (Phase 3, Q4).  Crossref (https://www.crossref.org)
exposes a free unlimited "polite pool" REST API as long as requests
include a contact mailto in the User-Agent.

Strong for:
* DOI resolution and canonical citation metadata.
* Cross-discipline (medicine, social sciences, etc. that arXiv lacks).
* Bibliographic completeness (authors, year, journal, page numbers).

Weak for:
* Full-text content (Crossref is metadata-only).
* Preprints (focuses on published, DOI-assigned works).
"""

from __future__ import annotations

from typing import Any, List, Optional
from urllib.parse import quote_plus

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


_CROSSREF_BASE = "https://api.crossref.org/works"

_USER_AGENT = (
    "Crucible/1.1.8 (https://github.com/Starlight143/crucible; "
    "mailto:research@crucible.ai)"
)


def search_crossref(
    query: str,
    *,
    limit: int = 3,
    timeout_seconds: float = 15.0,
) -> List[ResearchCitation]:
    """Search Crossref for *query*.

    Returns up to *limit* citations.  Empty list on error or no
    results.  Never raises.
    """
    if not query or not query.strip():
        return []
    if limit <= 0:
        return []
    encoded = quote_plus(query.strip())
    url = (
        f"{_CROSSREF_BASE}"
        f"?query={encoded}"
        f"&rows={int(min(limit, 20))}"
        "&select=DOI,title,author,published-print,published-online,container-title,abstract"
    )
    try:
        data = safe_http_json(
            url,
            timeout_seconds=float(timeout_seconds),
            max_bytes=1024 * 1024,
            user_agent=_USER_AGENT,
            circuit_breaker_name="librarian_crossref:api.crossref.org",
        )
    except Exception as exc:
        LOGGER.debug("crossref: HTTP error: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    message = data.get("message")
    if not isinstance(message, dict):
        return []
    items = message.get("items")
    if not isinstance(items, list):
        return []
    out: List[ResearchCitation] = []
    for entry in items[:limit]:
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
    # ``title`` is a list of strings in Crossref schema.
    title_list = entry.get("title")
    title = ""
    if isinstance(title_list, list) and title_list:
        title = str(title_list[0] or "").strip()
    if not title:
        return None
    doi = str(entry.get("DOI") or "").strip()
    if not doi:
        return None
    url_str = f"https://doi.org/{doi}"
    # Build snippet from authors + year + abstract.
    authors = _format_authors(entry.get("author"))
    year = _extract_year(entry)
    abstract = _clean_abstract(entry.get("abstract"))
    parts: List[str] = []
    if authors:
        parts.append(authors)
    if year:
        parts.append(f"({year})")
    if abstract:
        parts.append(abstract)
    snippet = " ".join(parts).strip()
    return ResearchCitation(
        provider="crossref",
        title=title[:200],
        url=url_str,
        snippet=snippet[:400],
        query=query,
        evidence_type="paper",
        verification_status="metadata_only",
    )


def _format_authors(authors: Any) -> str:
    if not isinstance(authors, list) or not authors:
        return ""
    first = authors[0]
    if not isinstance(first, dict):
        return ""
    family = str(first.get("family") or "").strip()
    if not family:
        return ""
    if len(authors) == 1:
        return family
    return f"{family} et al."


def _extract_year(entry: dict) -> str:
    # Try published-print then published-online; each holds
    # ``date-parts: [[year, month, day]]``.
    for key in ("published-print", "published-online", "issued"):
        val = entry.get(key)
        if not isinstance(val, dict):
            continue
        dp = val.get("date-parts")
        if not isinstance(dp, list) or not dp:
            continue
        first = dp[0]
        if not isinstance(first, list) or not first:
            continue
        try:
            return str(int(first[0]))
        except (TypeError, ValueError):
            continue
    return ""


def _clean_abstract(raw: Any) -> str:
    """Crossref abstracts often include JATS XML tags.  Strip them."""
    if not isinstance(raw, str):
        return ""
    import re
    # Strip XML/HTML tags (Crossref uses JATS: <jats:p>, <jats:italic>...).
    cleaned = re.sub(r"<[^>]+>", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:300]
