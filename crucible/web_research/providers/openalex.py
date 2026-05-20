"""OpenAlex provider — global academic papers, no authentication.

v1.1.8 extended (Phase 3, Q4).  OpenAlex (https://openalex.org) provides
metadata for 250M+ academic works.  Free tier: 100k requests/day per IP
in the "polite pool" (User-Agent including mailto).

Strong for:
* Cross-discipline academic search (broader than arXiv).
* CJK-language paper metadata (arXiv is English-only).
* DOI resolution.

Weak for:
* Full-text content (abstracts only via inverted index).
* Recent preprints (lag ~1-2 weeks behind arXiv).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
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


_OPENALEX_BASE = "https://api.openalex.org/works"

# Including ``mailto:`` in the User-Agent places us in OpenAlex's
# "polite pool" — much higher rate limit and priority.  Email is a
# generic project address; users can override via ``LIBRARIAN_OPENALEX_MAILTO``
# env var in the future if desired.
_USER_AGENT = (
    "Crucible/1.1.8 (https://github.com/Starlight143/crucible; "
    "mailto:research@crucible.ai)"
)


def search_openalex(
    query: str,
    *,
    limit: int = 3,
    timeout_seconds: float = 15.0,
) -> List[ResearchCitation]:
    """Search OpenAlex for *query*.

    Returns up to *limit* citations.  Empty list on error or no results.
    Never raises (errors are logged at DEBUG and swallowed).
    """
    if not query or not query.strip():
        return []
    if limit <= 0:
        return []
    encoded = quote_plus(query.strip())
    # ``select`` trims response to the fields we need — drops 60-70% of
    # bytes vs the default full-record response.
    url = (
        f"{_OPENALEX_BASE}"
        f"?search={encoded}"
        f"&per-page={int(min(limit, 25))}"
        "&select=id,doi,title,publication_year,authorships,host_venue,abstract_inverted_index"
    )
    try:
        data = safe_http_json(
            url,
            timeout_seconds=float(timeout_seconds),
            max_bytes=1024 * 1024,
            user_agent=_USER_AGENT,
            circuit_breaker_name="librarian_openalex:api.openalex.org",
        )
    except Exception as exc:
        LOGGER.debug("openalex: HTTP error: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    out: List[ResearchCitation] = []
    for entry in (data.get("results") or [])[:limit]:
        cit = _entry_to_citation(entry, query)
        if cit is not None:
            out.append(cit)
    return out


def _entry_to_citation(
    entry: Any,
    query: str,
) -> Optional[ResearchCitation]:
    """Convert a single OpenAlex result dict to a ``ResearchCitation``.

    Returns ``None`` on missing required fields (title, url).
    """
    if not isinstance(entry, dict):
        return None
    title = str(entry.get("title") or "").strip()
    if not title:
        return None
    # Prefer DOI URL (permanent identifier) over OpenAlex ID URL.
    doi = str(entry.get("doi") or "").strip()
    oa_id = str(entry.get("id") or "").strip()
    url_str = doi or oa_id
    if not url_str:
        return None
    snippet = _reconstruct_abstract(entry.get("abstract_inverted_index"))
    # First-author surname makes the snippet more informative.
    auth = _first_author(entry.get("authorships"))
    year = entry.get("publication_year")
    if auth and year:
        snippet = f"({auth} et al., {year}) {snippet}".strip()
    elif year:
        snippet = f"({year}) {snippet}".strip()
    return ResearchCitation(
        provider="openalex",
        title=title[:200],
        url=url_str,
        snippet=snippet[:400],
        query=query,
        evidence_type="paper",
        verification_status="metadata_only",
    )


def _first_author(authorships: Any) -> str:
    """Extract first-author display name from OpenAlex authorships list."""
    if not isinstance(authorships, list) or not authorships:
        return ""
    first = authorships[0]
    if not isinstance(first, dict):
        return ""
    author = first.get("author")
    if not isinstance(author, dict):
        return ""
    name = str(author.get("display_name") or "").strip()
    if not name:
        return ""
    # Return surname (last token) for the citation snippet — most
    # commonly used citation form.
    return name.split()[-1] if " " in name else name


def _reconstruct_abstract(inverted: Any) -> str:
    """Reconstruct OpenAlex abstract from inverted-index format.

    OpenAlex returns abstracts as ``{"word": [pos1, pos2, ...]}``.
    Sorting (position, word) and joining recovers the original text.
    Returns ``""`` on any unexpected shape.
    """
    if not isinstance(inverted, dict):
        return ""
    positions: List[tuple[int, str]] = []
    for word, plist in inverted.items():
        if not isinstance(plist, list):
            continue
        for p in plist:
            try:
                positions.append((int(p), str(word)))
            except (TypeError, ValueError):
                continue
    positions.sort()
    # Cap at 80 words to keep snippets short.
    return " ".join(w for _, w in positions[:80])
