"""Tavily provider — AI-optimised web search via the Tavily Search API.

Added in v1.1.13 as an optional, opt-in general-web provider alongside the
zero-auth DuckDuckGo ``websearch`` and the federated ``searxng`` providers.

Design (v1.1.13 — clean reimplementation, NOT the upstream SDK approach):

* **No new third-party dependency.**  The request is a plain ``POST`` to the
  Tavily REST endpoint issued through ``crucible.web_research.http_clients.
  safe_http_json`` — the same SSRF-checked, redirect-validating, circuit-
  broken helper every other provider in this package uses.  This honours the
  package contract in ``providers/__init__.py`` ("HTTP requests MUST go
  through ``safe_http_*`` helpers ... to inherit SSRF protection") and avoids
  pulling in ``tavily-python``.
* **The operator timeout is respected.**  ``timeout_seconds`` (sourced from
  ``LIBRARIAN_HTTP_TIMEOUT_SECONDS`` by the section_04 dispatcher) is forwarded
  to ``safe_http_json`` so a hung Tavily call cannot blow the stage budget.
* **Credentials are resolved with the same placeholder hygiene** as the other
  auth-tier providers (mirrors ``section_04._resolve_context7_token``): the
  ``.env.example`` placeholder ``replace_with_tavily_api_key`` is treated as
  "not configured", so a fresh checkout never sends a bogus key.

Activation requires BOTH:

* ``TAVILY_API_KEY`` set to a real key (free tier: https://app.tavily.com), and
* ``tavily`` present in ``LIBRARIAN_EXTRA_PROVIDERS``.

When either is missing the provider no-ops (returns ``[]``), so default
behaviour is unchanged bit-for-bit.

Behaviour contract (shared by every provider in this package):

* Return up to *limit* :class:`ResearchCitation` objects.
* Return ``[]`` on any error or empty result — never raise for routine
  failures; the dispatcher owns the fallback chain.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# Tri-modal import — mirrors the sibling providers (searxng.py / crossref.py)
# so the module loads both as ``python -m crucible`` (package) and
# ``python crucible/__main__.py`` (flat launcher).
try:
    from ...modules.section_03_models_and_context import ResearchCitation
    from ...runtime_logging import get_logger
    from ..http_clients import safe_http_json
except ImportError:  # pragma: no cover - flat-launcher fallback
    from modules.section_03_models_and_context import ResearchCitation  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]
    from web_research.http_clients import safe_http_json  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


# Fixed vendor endpoint.  ``safe_http_json`` re-validates this (and any
# redirect target) against ``_is_public_http_url`` before every request, so an
# attacker who somehow influenced this constant could not redirect the call to
# a private/metadata address.
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"

_USER_AGENT = (
    "Crucible/1.1.13 librarian (https://github.com/Starlight143/crucible)"
)

# Cap the response body.  Tavily ``basic`` search returns small JSON; 1 MiB is
# generous and prevents a misbehaving endpoint from streaming unbounded data.
_MAX_RESPONSE_BYTES = 1024 * 1024

# Placeholder prefixes treated as "no key configured".  Mirrors
# ``section_04._resolve_context7_token`` so a copied ``.env.example`` never
# sends a sentinel value to the live API.
_PLACEHOLDER_PREFIXES = ("your_", "xxxx", "placeholder", "changeme", "replace_")


def _resolve_tavily_api_key() -> str:
    """Return a real Tavily API key from the environment, or ``""`` when absent.

    Whitespace-stripped; placeholder sentinels (``your_*``, ``xxxx*``,
    ``placeholder*``, ``changeme*``, ``replace_*``) are ignored so the
    ``.env.example`` default value behaves exactly like an unset key.
    """
    raw = str(os.environ.get("TAVILY_API_KEY") or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return raw


def search_tavily(
    query: str,
    *,
    limit: int = 3,
    timeout_seconds: float = 15.0,
) -> List[ResearchCitation]:
    """Search the web via the Tavily Search API.

    Returns up to *limit* citations.  Returns ``[]`` on any error, empty
    result, missing/placeholder API key, or empty query.  Never raises for
    routine failures.
    """
    if not query or not query.strip():
        return []
    if limit <= 0:
        return []

    api_key = _resolve_tavily_api_key()
    if not api_key:
        LOGGER.debug(
            "tavily: TAVILY_API_KEY not set (or placeholder) — returning []"
        )
        return []

    # ``api_key`` is sent in the JSON body (Tavily's documented + SDK contract).
    # ``safe_http_json`` only logs the URL and method — never the request body —
    # so the key is not exposed in logs.
    payload: Dict[str, Any] = {
        "api_key": api_key,
        "query": query.strip(),
        "max_results": int(limit),
        "search_depth": "basic",
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }

    try:
        data = safe_http_json(
            _TAVILY_SEARCH_URL,
            timeout_seconds=float(timeout_seconds),
            max_bytes=_MAX_RESPONSE_BYTES,
            user_agent=_USER_AGENT,
            method="POST",
            payload=payload,
            circuit_breaker_name="librarian_tavily",
        )
    except Exception as exc:
        # Includes ValueError (SSRF refusal / byte-budget), httpx errors,
        # JSON decode errors, retry exhaustion — all routine for a provider.
        LOGGER.debug("tavily: search failed: %s", exc)
        return []

    return _parse_results(data, query, limit)


def _parse_results(
    data: Any,
    query: str,
    limit: int,
) -> List[ResearchCitation]:
    """Convert a Tavily search response into citations.

    Tavily returns ``{"results": [{"title", "url", "content", ...}, ...]}``.
    Non-dict payloads, missing ``results``, or non-list ``results`` all yield
    ``[]`` rather than raising.
    """
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
    """Map one Tavily result entry to a :class:`ResearchCitation`.

    Drops entries missing a title or URL, or whose URL is not ``http(s)``
    (defensive: a citation URL is later surfaced to the LLM / operator, so a
    ``ftp://`` / ``file://`` / ``javascript:`` value must not leak through).
    """
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
