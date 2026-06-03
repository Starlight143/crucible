"""Per-query-class fallback chain for librarian provider dispatch.

v1.1.8 extended (Phase 3, Q3).  When a primary provider returns empty
results or enters cooldown (Q2), the dispatcher auto-routes to the next
provider in the same query class instead of falling back to a hard
silo (the v1.1.7 behaviour).

Query classes (CLAUDE.md plan):

* ``general``  — broad web search.  Order: websearch (DDG html→lite)
  → tavily → searxng → wikipedia (definitional baseline).  ``tavily`` is
  opt-in (requires TAVILY_API_KEY + ``tavily`` in LIBRARIAN_EXTRA_PROVIDERS);
  it is filtered out of the chain whenever it is not enabled, so the default
  order collapses back to websearch → searxng → wikipedia.
* ``code``     — code / repository search.  Order: github → websearch
  with ``site:github.com`` (grep_app removed from v1.1.10 defaults — Vercel
  Bot Protection serves a JS PoW challenge to unauthenticated clients; it is
  opt-in via LIBRARIAN_SEARCH_PROVIDERS and remains in _CORE_PROVIDERS).
* ``academic`` — academic papers.  Order: arxiv → openalex → crossref
  → semantic_scholar (future) → websearch with ``filetype:pdf``.
* ``docs``     — documentation lookup.  Order: context7 → wikipedia →
  websearch with documentation-site hints.

The chain dispatcher uses the cooldown registry (Q2) as a skip filter:
a provider in cooldown is silently bypassed.

This module deliberately stays SMALL and FOCUSED — it only orchestrates
the ordering.  Per-provider behaviour (cache check, HTTP call, citation
parsing) lives in the provider modules and the section_04 dispatcher.

Wire-in (Phase 3 dispatcher integration in section_04):

    chain = build_chain_for_query(query, query_class)
    for provider in chain:
        if cooldown.is_cooling_down(provider):
            continue  # try next in chain
        results = dispatch_one_provider(provider, query, ...)
        if results:
            return results
    return []  # entire chain exhausted
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Tri-modal import.
try:
    from .._env import env_bool, env_str_passthrough
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool, env_str_passthrough  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


# Per-query-class fallback ordering.  First entry = primary, then
# fallbacks in decreasing preference.  Operator can disable individual
# providers via ``LIBRARIAN_SEARCH_PROVIDERS`` (core list) or
# ``LIBRARIAN_EXTRA_PROVIDERS`` (extras list) — providers NOT in those
# lists are silently dropped from the chain.
_DEFAULT_CHAIN_BY_CLASS: Dict[str, List[str]] = {
    # v1.1.13: ``tavily`` sits between websearch and searxng — a higher-quality
    # general fallback than public SearXNG instances.  It is opt-in, so
    # ``build_chain_for_query`` drops it from the chain unless the operator
    # adds ``tavily`` to LIBRARIAN_EXTRA_PROVIDERS (and sets TAVILY_API_KEY).
    "general": ["websearch", "tavily", "searxng", "wikipedia"],
    # v1.1.10 removed grep_app from the default provider list (Vercel Bot
    # Protection serves a JS PoW challenge to unauthenticated HTTP clients).
    # It is intentionally absent from the default chain here; operators who
    # re-enable it via LIBRARIAN_SEARCH_PROVIDERS still resolve it because it
    # remains in _CORE_PROVIDERS (the known-set filter).  Default code flow
    # is github -> websearch.
    "code": ["github", "websearch"],
    "academic": ["arxiv", "openalex", "crossref", "websearch"],
    "docs": ["context7", "wikipedia", "websearch"],
}


# Known core providers (must align with LIBRARIAN_SEARCH_PROVIDERS env).
_CORE_PROVIDERS = frozenset(
    [
        "websearch",
        "context7",
        "grep_app",
        "github",
        "arxiv",
        "paperswithcode",
    ]
)

# Known v1.1.8 extra providers (must align with LIBRARIAN_EXTRA_PROVIDERS).
_EXTRA_PROVIDERS = frozenset(
    [
        "openalex",
        "crossref",
        "wikipedia",
        "searxng",
        "tavily",
    ]
)


def fallback_enabled() -> bool:
    """Master toggle for per-class fallback chain."""
    return env_bool("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", True)


def _parse_csv_env(name: str, default: str) -> List[str]:
    """Parse a comma / semicolon-separated env value into a name list.

    Strips whitespace and lowercases.  Empty entries discarded.

    Uses ``env_str_passthrough`` (not ``env_str``) so an explicit
    empty-string env value ``LIBRARIAN_EXTRA_PROVIDERS=`` means
    "no extras", distinct from "unset" which falls back to *default*.
    Without this, operators couldn't disable the extras list.
    """
    raw = env_str_passthrough(name, default)
    if not raw:
        return []
    parts: List[str] = []
    # Accept comma OR semicolon as separator (matches the existing
    # _parse_csv_env precedent in section_04).
    for chunk in raw.replace(";", ",").split(","):
        s = chunk.strip().lower()
        if s:
            parts.append(s)
    return parts


def _enabled_providers() -> List[str]:
    """Resolve the union of core + extras that the operator has
    enabled via env.

    v1.1.10 (S2): the default ``LIBRARIAN_SEARCH_PROVIDERS`` no longer
    includes ``grep_app`` — Vercel Bot Protection now serves a JS PoW
    challenge for every unauthenticated grep.app request, which a pure
    HTTP client cannot solve.  Operators who still want grep_app can
    re-add it explicitly via ``LIBRARIAN_SEARCH_PROVIDERS`` env override
    (the ``_search_grep_app`` helper itself is preserved).
    """
    core = _parse_csv_env(
        "LIBRARIAN_SEARCH_PROVIDERS",
        "websearch,context7,github,arxiv,paperswithcode",
    )
    extras = _parse_csv_env(
        "LIBRARIAN_EXTRA_PROVIDERS",
        "openalex,crossref,wikipedia",
    )
    # Preserve order from env; dedupe.
    seen = set()
    out: List[str] = []
    for p in core + extras:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def classify_query(
    query: str,
    *,
    hint: Optional[str] = None,
) -> str:
    """Classify a query into one of the known classes.

    *hint* (if provided) takes precedence — the dispatcher knows context
    that pure-query inspection cannot derive (e.g. "this query is part
    of a lane that the LLM marked as academic-focused").

    Heuristics (used when *hint* is missing):
    * ``site:github.com`` / ``filetype:py`` / "github" keyword → code
    * ``filetype:pdf`` / "arxiv" / "paper" / "et al." → academic
    * "documentation" / "api reference" / "tutorial" → docs
    * otherwise → general
    """
    import re as _re
    if hint:
        h = hint.strip().lower()
        if h in _DEFAULT_CHAIN_BY_CLASS:
            return h
    if not query:
        return "general"
    q = query.lower()
    # Word-boundary checks so ``github query`` (no flanking spaces) still
    # classifies as code, but ``engithubed`` wouldn't.  ``filetype:`` and
    # ``site:`` directives are exact-substring because they're already
    # delimited by the colon syntax.
    if (
        "site:github.com" in q
        or "site:gitlab.com" in q
        or _re.search(r"\bgithub\b", q)
        or _re.search(r"\bgitlab\b", q)
        or "filetype:py" in q
        or "filetype:js" in q
        or "filetype:go" in q
        or "filetype:rs" in q
    ):
        return "code"
    if (
        "filetype:pdf" in q
        or "site:arxiv.org" in q
        or _re.search(r"\barxiv\b", q)
        or _re.search(r"\bet al\b", q)
        or "doi:" in q
        or _re.search(r"\babstract\b", q)
    ):
        return "academic"
    if (
        _re.search(r"\bdocumentation\b", q)
        or "api reference" in q
        or _re.search(r"\btutorial\b", q)
        or "getting started" in q
    ):
        return "docs"
    return "general"


def build_chain_for_query(
    query: str,
    query_class: Optional[str] = None,
    *,
    enabled_providers: Optional[List[str]] = None,
) -> List[str]:
    """Build the fallback provider chain for *query*.

    Returns providers in priority order.  Filters out:

    * Providers not enabled in the env (LIBRARIAN_SEARCH_PROVIDERS +
      LIBRARIAN_EXTRA_PROVIDERS).
    * Providers not in the known set (defensive — protects against
      typos like ``searxn`` instead of ``searxng``).

    If fallback is disabled (``LIBRARIAN_PROVIDER_FALLBACK_ENABLED=0``),
    returns just the primary for that class (or an empty list when the
    primary is also disabled).
    """
    cls = query_class if query_class in _DEFAULT_CHAIN_BY_CLASS else (
        classify_query(query, hint=query_class)
    )
    chain = list(_DEFAULT_CHAIN_BY_CLASS.get(cls, []))
    enabled = set(enabled_providers if enabled_providers is not None else _enabled_providers())
    known = _CORE_PROVIDERS | _EXTRA_PROVIDERS
    filtered: List[str] = [
        p for p in chain if p in enabled and p in known
    ]
    if not fallback_enabled():
        # Keep only the primary.
        return filtered[:1] if filtered else []
    return filtered


def known_query_classes() -> List[str]:
    """Return the sorted list of supported query classes."""
    return sorted(_DEFAULT_CHAIN_BY_CLASS.keys())
