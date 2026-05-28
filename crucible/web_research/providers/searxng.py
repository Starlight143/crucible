"""SearXNG provider — federated meta-search via public instances.

v1.1.8 extended (Phase 3, Q4).  SearXNG (https://docs.searxng.org)
aggregates results from Google / Bing / Brave / etc.  This module
queries one or more PUBLIC instances configured in
``crucible/config/domain_pins.json`` under ``searxng_instances``.

Default disabled — public instance reliability is variable.  Opt in via
``LIBRARIAN_EXTRA_PROVIDERS=...,searxng``.

The implementation rotates through configured instances on consecutive
failures — if instance A returns 503, try instance B, etc.  Per-instance
failure does NOT count against the cooldown registry (that's for
provider-level failures); only when EVERY instance fails do we treat it
as a provider-level error and let the dispatcher route to the next
fallback.

Strong for:
* Cross-engine aggregation when a single search engine misses something.
* SaaS / general-web queries that don't have an obvious specialist
  provider.

Weak for:
* Reliability — public instances rate-limit aggressively.
* Trust — operator should pin instances they trust.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote_plus

# Tri-modal import.
try:
    from ..._env import env_str
    from ...modules.section_03_models_and_context import ResearchCitation
    from ...runtime_logging import get_logger
    from ..http_clients import safe_http_json
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_str  # type: ignore[no-redef]
    from modules.section_03_models_and_context import ResearchCitation  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]
    from web_research.http_clients import safe_http_json  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


# No hard-coded public instances (v1.1.11).  Querying arbitrary third-party
# public SearXNG hosts by default contradicts the "operator should pin
# instances they trust" contract (see module docstring "Weak for: Trust").
# When this list is empty AND domain_pins.json supplies no
# ``searxng_instances``, the provider no-ops (search_searxng returns []), so
# it never silently routes operator queries through an untrusted aggregator.
# Operators opt in by adding trusted hosts to ``searxng_instances`` in
# domain_pins.json (or via the LIBRARIAN_DOMAIN_PINS_PATH override).
_DEFAULT_INSTANCES: List[str] = []


_USER_AGENT = (
    "Crucible/1.1.8 librarian (https://github.com/Starlight143/crucible)"
)


def _resolve_instances() -> List[str]:
    """Resolve SearXNG instance list from the domain pins file or
    the hardcoded fallback."""
    pins_path = env_str(
        "LIBRARIAN_DOMAIN_PINS_PATH",
        "crucible/config/domain_pins.json",
    )
    p = Path(pins_path)
    if not p.is_absolute():
        # Repo-root relative.
        repo_root = Path(__file__).resolve().parents[3]
        p = repo_root / pins_path
    if not p.exists():
        return list(_DEFAULT_INSTANCES)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.debug(
            "searxng: failed to read instance list from %s: %s",
            p, exc,
        )
        return list(_DEFAULT_INSTANCES)
    instances = data.get("searxng_instances")
    if not isinstance(instances, list) or not instances:
        return list(_DEFAULT_INSTANCES)
    # SSRF safety (v1.1.11): only accept https:// instances (mirrors
    # domain_pins _fetch_one).  A pin entry such as "http://10.0.0.1" or
    # "file:///etc/passwd" is dropped rather than queried.
    resolved: List[str] = []
    for i in instances:
        if not isinstance(i, str):
            continue
        cleaned = i.strip().rstrip("/")
        if cleaned.startswith("https://"):
            resolved.append(cleaned)
    return resolved


def search_searxng(
    query: str,
    *,
    limit: int = 3,
    timeout_seconds: float = 15.0,
) -> List[ResearchCitation]:
    """Search SearXNG via configured instances.

    Rotates through instances on failure.  Returns up to *limit*
    citations from the first instance that responds successfully.
    Empty list if every instance fails.  Never raises.
    """
    if not query or not query.strip():
        return []
    if limit <= 0:
        return []
    instances = _resolve_instances()
    if not instances:
        return []
    encoded = quote_plus(query.strip())
    for instance in instances:
        url = f"{instance}/search?q={encoded}&format=json"
        try:
            data = safe_http_json(
                url,
                timeout_seconds=float(timeout_seconds),
                max_bytes=1024 * 1024,
                user_agent=_USER_AGENT,
                circuit_breaker_name=f"librarian_searxng:{instance}",
            )
        except Exception as exc:
            LOGGER.debug(
                "searxng instance %s failed: %s — trying next",
                instance, exc,
            )
            continue
        results = _parse_results(data, query, limit)
        if results:
            return results
        # Empty result from instance — try next (might be that
        # instance's search engine availability is bad).
    return []


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
        provider="searxng",
        title=title[:200],
        url=url_str,
        snippet=snippet[:400],
        query=query,
        evidence_type="web_result",
        verification_status="search_snippet",
    )
