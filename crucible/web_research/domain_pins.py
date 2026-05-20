"""Domain authoritative-source pinning — loader + pre-fetch executor.

v1.1.8 extended (Phase 3, Q8).  Reads ``crucible/config/domain_pins.json``
(path overridable via ``LIBRARIAN_DOMAIN_PINS_PATH``) and exposes:

* ``load_pins()`` — load and validate the JSON file.
* ``match_pins(user_problem, mode)`` — return all pins matching the
  given problem and mode (case-insensitive substring on any_keyword,
  and / or all_keywords).
* ``prefetch_pinned_urls(matched_pins)`` — fetch each pinned URL and
  return as a list of ``ResearchCitation`` entries tagged
  ``provider="domain_pin"`` and ``evidence_type="docs"``.

The dispatcher (section_04) calls ``match_pins`` + ``prefetch_pinned_urls``
BEFORE the regular provider loop, so pinned citations enter the pool as
Tier-1 anchors that the evidence auditor can later attribute direction-
specific claims to.

Failure modes (all logged + swallowed; never raises):
* Missing / malformed JSON file → return empty list, log warning.
* URL fetch error → skip that URL, continue with others.
* No match → return empty list (caller falls through to regular search).

JSON format: see ``crucible/config/domain_pins.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

# Tri-modal import.
try:
    from .._env import env_bool, env_str
    from ..modules.section_03_models_and_context import ResearchCitation
    from ..runtime_logging import get_logger
    from .http_clients import safe_http_text
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool, env_str  # type: ignore[no-redef]
    from modules.section_03_models_and_context import ResearchCitation  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]
    from web_research.http_clients import safe_http_text  # type: ignore[no-redef]


LOGGER = get_logger(__name__)

_USER_AGENT = (
    "Crucible/1.1.8 librarian (https://github.com/Starlight143/crucible)"
)


def domain_pins_enabled() -> bool:
    """Master toggle (env-gated)."""
    return env_bool("LIBRARIAN_DOMAIN_PINS_ENABLED", True)


def _resolve_path() -> Path:
    """Resolve ``LIBRARIAN_DOMAIN_PINS_PATH`` to an absolute Path.

    Repo-relative paths are resolved against the repo root.
    """
    raw = env_str(
        "LIBRARIAN_DOMAIN_PINS_PATH",
        "crucible/config/domain_pins.json",
    )
    p = Path(raw)
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parents[2]
        p = repo_root / raw
    return p


def load_pins() -> List[Dict[str, Any]]:
    """Load and validate the pin definitions.

    Returns the ``pins`` list from the JSON file, or an empty list on
    any error.  Logs a warning on schema problems but does not raise.
    """
    if not domain_pins_enabled():
        return []
    path = _resolve_path()
    if not path.exists():
        LOGGER.debug("domain_pins: file not found at %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning(
            "domain_pins: failed to load %s: %s",
            path, exc,
        )
        return []
    if not isinstance(data, dict):
        LOGGER.warning("domain_pins: %s root is not an object", path)
        return []
    pins = data.get("pins")
    if not isinstance(pins, list):
        LOGGER.warning("domain_pins: %s 'pins' is not a list", path)
        return []
    # Light validation — drop malformed entries.  We keep going even if
    # SOME entries are bad.
    valid: List[Dict[str, Any]] = []
    for i, pin in enumerate(pins):
        if not isinstance(pin, dict):
            continue
        match = pin.get("match")
        pre_fetch = pin.get("pre_fetch")
        if not isinstance(match, dict) or not isinstance(pre_fetch, list):
            LOGGER.debug("domain_pins: pin %d malformed; skipping", i)
            continue
        valid.append(pin)
    return valid


def match_pins(
    user_problem: str,
    mode: str,
    pins: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Return all pins matching *user_problem* + *mode*.

    Match semantics:

    * ``match.mode`` must equal *mode* (case-insensitive).  A pin with
      ``mode = "*"`` (or missing mode field) matches every mode.
    * If ``match.any_keyword`` is present, the pin fires when ANY
      keyword appears as a substring of the lower-cased
      *user_problem*.
    * If ``match.all_keywords`` is present, the pin fires only when
      ALL listed keywords appear.
    * A pin with no ``any_keyword`` AND no ``all_keywords`` matches
      every problem in the given mode (used for "global" pins; rare).
    """
    if not user_problem or not isinstance(user_problem, str):
        return []
    if pins is None:
        pins = load_pins()
    if not pins:
        return []
    problem_lower = user_problem.lower()
    mode_lower = (mode or "").strip().lower()
    matched: List[Dict[str, Any]] = []
    for pin in pins:
        match = pin.get("match") or {}
        pin_mode = str(match.get("mode") or "").strip().lower()
        if pin_mode and pin_mode != "*" and pin_mode != mode_lower:
            continue
        any_kw = match.get("any_keyword") or []
        all_kw = match.get("all_keywords") or []
        if not isinstance(any_kw, list):
            any_kw = []
        if not isinstance(all_kw, list):
            all_kw = []
        # ANY-keyword test: fires if list is non-empty AND none match.
        if any_kw:
            if not any(
                isinstance(k, str) and k.lower() in problem_lower
                for k in any_kw
            ):
                continue
        # ALL-keywords test: fires if list is non-empty AND any missing.
        if all_kw:
            if not all(
                isinstance(k, str) and k.lower() in problem_lower
                for k in all_kw
            ):
                continue
        matched.append(pin)
    return matched


def prefetch_pinned_urls(
    matched_pins: List[Dict[str, Any]],
    *,
    timeout_seconds: float = 15.0,
    max_per_pin: int = 4,
) -> List[ResearchCitation]:
    """Pre-fetch the URLs from each matched pin.

    Returns one ``ResearchCitation`` per successfully fetched URL.
    Failed fetches are silently skipped (logged at DEBUG).  The
    citation snippet is the first 400 chars of the response body —
    enough to give the auditor context without bloating the prompt.
    """
    if not matched_pins:
        return []
    out: List[ResearchCitation] = []
    for pin in matched_pins:
        pin_id = str(pin.get("id") or "")
        pre_fetch = pin.get("pre_fetch") or []
        if not isinstance(pre_fetch, list):
            continue
        for fetch in pre_fetch[:max_per_pin]:
            cit = _fetch_one(fetch, pin_id, timeout_seconds)
            if cit is not None:
                out.append(cit)
    return out


def _fetch_one(
    fetch: Any,
    pin_id: str,
    timeout_seconds: float,
) -> Optional[ResearchCitation]:
    if not isinstance(fetch, dict):
        return None
    url = str(fetch.get("url") or "").strip()
    if not url or not url.startswith("https://"):
        # SSRF safety: only HTTPS pinned URLs accepted (matches the
        # structural test in ``test_v118_extended_4layer_sync.py``).
        return None
    label = str(fetch.get("label") or "").strip()
    subject_hint = str(fetch.get("subject_hint") or "").strip()
    try:
        body = safe_http_text(
            url,
            timeout_seconds=float(timeout_seconds),
            max_bytes=256 * 1024,
            user_agent=_USER_AGENT,
            circuit_breaker_name=f"librarian_domain_pin:{pin_id}",
        )
    except Exception as exc:
        LOGGER.debug(
            "domain_pins: pre-fetch failed for %s (%s): %s",
            url, pin_id, exc,
        )
        return None
    # Extract a short snippet — strip HTML tags and collapse whitespace.
    snippet = _html_to_text(body or "")[:400]
    # Title falls back to label or pin_id when not derivable from HTML.
    title = label or pin_id or url
    if subject_hint:
        snippet = f"({subject_hint}) {snippet}".strip()
    return ResearchCitation(
        provider="domain_pin",
        title=title[:200],
        url=url,
        snippet=snippet,
        query=f"domain_pin:{pin_id}",
        evidence_type="docs",
        verification_status="fetched_excerpt",
    )


def _html_to_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace.

    Lightweight — does not try to render JS-rendered SPAs (those are
    bad pin candidates anyway; pick API doc pages that render
    server-side).
    """
    import re
    # Drop <script> / <style> blocks and their contents entirely.
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip all other tags.
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode common HTML entities.
    cleaned = (
        cleaned.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # Collapse runs of whitespace.
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
