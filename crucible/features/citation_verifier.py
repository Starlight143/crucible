from __future__ import annotations
"""features/citation_verifier.py
================================
Verifies that citations in analysis reports are reachable URLs and that
quoted text approximately matches source content.

Reads analysis_result.json, extracts all URLs from all string fields,
checks each URL with an HTTP HEAD request, and optionally fetches snippets
to verify quoted text appears in the source.

Usage::

    from crucible.feature_registry import run_features, FeatureConfig
    import crucible.features.citation_verifier  # auto-registers

    config = FeatureConfig()
    results = run_features(
        "/path/to/run_dir",
        enabled_features=["citation_verifier"],
        config=config,
    )

Environment variables
---------------------
CITATION_VERIFY_ENABLED     Master switch; 0 = skip entirely (default: 1).
CITATION_VERIFY_TIMEOUT_S   HTTP timeout in seconds (default: 5).
CITATION_VERIFY_MAX_URLS    Max URLs to check per run (default: 20).
CITATION_FETCH_SNIPPET      Fetch content to verify quotes (default: 1).
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


# Match against the PARSED HOSTNAME (not the raw URL) to prevent bypasses via
# URL userinfo (e.g. http://safe.com@169.254.169.254/ — the regex would match
# "safe.com" while urllib connects to the IMDS endpoint).
_PRIVATE_HOST_RE = re.compile(
    r"^("
    r"localhost$|"
    r"127\.|"
    r"0\.0\.0\.0$|"              # unspecified / wildcard address
    r"10\.|"
    r"192\.168\.|"
    r"172\.(1[6-9]|2[0-9]|3[01])\.|"
    r"169\.254\.|"               # link-local / AWS IMDS (169.254.169.254)
    r"::1$|"                     # IPv6 loopback (urlparse strips brackets)
    r"::ffff:127\.|"             # IPv4-mapped loopback
    r"fe80:"                     # IPv6 link-local
    r")"
)


def _is_private_or_local(url: str) -> bool:
    """Return True if *url*'s parsed hostname is localhost or private/link-local.

    Uses urllib.parse to extract the real hostname so that URL userinfo tricks
    (e.g. ``http://legit@169.254.169.254/``) are not bypassed by matching on
    the raw URL string.  Any parse failure is treated as private (safe default).
    """
    try:
        hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return True
    if not hostname:
        return True
    return bool(_PRIVATE_HOST_RE.match(hostname))


class _SSRFBlockingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that blocks 3xx redirects to private/local addresses.

    Without this, an attacker-controlled server can respond with a redirect to
    ``http://169.254.169.254/latest/meta-data/`` and bypass the SSRF blocklist.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if _is_private_or_local(newurl):
            raise urllib.error.URLError(
                f"SSRF blocked: redirect to private address {newurl!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Build once at module level; shared by all _check_url calls.
_SAFE_OPENER = urllib.request.build_opener(_SSRFBlockingRedirectHandler)


def _extract_urls(text: str) -> List[str]:
    """Extract all URLs from *text* using a simple regex.

    Trailing punctuation characters (.,;:!?)>"') that commonly surround URLs in
    prose (e.g. ``see (https://example.com/path).``) are stripped from each
    match to avoid false 404s caused by the closing delimiter being included in
    the URL string.
    """
    _URL_PAT = r"https?://[a-zA-Z0-9_.~:/?#@!$&'()*+,;=%-]+"
    _TRAIL_STRIP = frozenset(".,;:!?)>\"'")
    results: List[str] = []
    for url in re.findall(_URL_PAT, text):
        while url and url[-1] in _TRAIL_STRIP:
            url = url[:-1]
        if url:
            results.append(url)
    return results

def _walk_strings(obj: Any, acc: List[str]) -> None:
    """Recursively walk *obj* and append all string values to *acc*."""
    if isinstance(obj, str):
        acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, acc)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_strings(item, acc)


def _strip_html(html: str) -> str:
    """Remove HTML tags from *html* using a simple regex."""
    return re.sub(r"<[^>]+>", "", html)


def _check_url(
    url: str,
    timeout: float,
    fetch_snippet: bool,
) -> Dict[str, Any]:
    """Perform a HEAD (and optionally GET) request against *url*.

    Returns a dict with: url, valid_format, reachable, status_code,
    redirect_url, check_ms, snippet (if fetched).
    """
    result: Dict[str, Any] = {
        "url": url,
        "valid_format": False,
        "reachable": False,
        "status_code": None,
        "redirect_url": None,
        "check_ms": 0.0,
        "snippet": None,
    }

    # Validate format — accept both https:// and http:// (regex extracts both)
    if not (url.startswith("https://") or url.startswith("http://")):
        result["valid_format"] = False
        return result
    if _is_private_or_local(url):
        result["valid_format"] = False
        return result
    result["valid_format"] = True

    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": "Crucible-CitationVerifier/1.0"},
        )
        # Use _SAFE_OPENER so redirects to private addresses are blocked.
        with _SAFE_OPENER.open(req, timeout=timeout) as resp:
            result["status_code"] = resp.status
            result["reachable"] = 200 <= resp.status < 400
            final_url = resp.url
            if final_url and final_url != url:
                result["redirect_url"] = final_url
    except urllib.error.HTTPError as exc:
        result["status_code"] = exc.code
        result["reachable"] = False
    except Exception as exc:
        result["reachable"] = False
        result["error"] = str(exc)
    finally:
        result["check_ms"] = round((time.monotonic() - t0) * 1000, 2)

    if fetch_snippet and result["reachable"]:
        try:
            req_get = urllib.request.Request(
                url,
                headers={"User-Agent": "Crucible-CitationVerifier/1.0"},
            )
            # Use _SAFE_OPENER to block redirect-based SSRF on the GET too.
            with _SAFE_OPENER.open(req_get, timeout=timeout) as resp_get:
                raw = resp_get.read(4096).decode("utf-8", errors="replace")
                result["snippet"] = _strip_html(raw)[:2000]
        except Exception:
            pass

    return result


@register("citation_verifier")
class CitationVerifierFeature(BaseFeature):
    """Verify that citations in analysis reports are reachable URLs."""

    name = "citation_verifier"
    label = "Citation Verifier"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        """Execute citation verification and write the report."""
        env: Dict[str, str] = config.env if config.env is not None else dict(os.environ)
        _cv_enabled = env.get("CITATION_VERIFY_ENABLED", "1").strip().lower()
        # Disable when the env var is set to any recognised falsy sentinel.
        # Previously only "0" was checked; using the full set is consistent
        # with every other boolean env var in this codebase (_env_bool pattern).
        if _cv_enabled in ("0", "false", "no", "off"):
            return FeatureResult(
                feature=self.name, success=True, skipped=True,
                skip_reason="CITATION_VERIFY_ENABLED is set to 0.",
            )

        t0 = time.monotonic()
        warnings: List[str] = []

        try:
            timeout = float(env.get("CITATION_VERIFY_TIMEOUT_S", "5"))
        except ValueError:
            timeout = 5.0
            warnings.append("CITATION_VERIFY_TIMEOUT_S invalid; defaulting to 5s.")
        if timeout <= 0:
            timeout = 5.0
            warnings.append("CITATION_VERIFY_TIMEOUT_S must be > 0; defaulting to 5s.")

        try:
            max_urls = int(env.get("CITATION_VERIFY_MAX_URLS", "20"))
        except ValueError:
            max_urls = 20
            warnings.append("CITATION_VERIFY_MAX_URLS invalid; defaulting to 20.")
        if max_urls <= 0:
            max_urls = 20
            warnings.append("CITATION_VERIFY_MAX_URLS must be > 0; defaulting to 20.")

        fetch_snippet = env.get("CITATION_FETCH_SNIPPET", "1").strip().lower() not in ("0", "false", "no", "off")
        rdp = Path(run_dir).resolve()
        ar_path = rdp / "analysis_result.json"

        data: Any = {}
        try:
            data = json.loads(ar_path.read_text(encoding="utf-8"))
        except Exception as exc:
            warnings.append(f"Could not read analysis_result.json: {exc}")

        all_strings: List[str] = []
        _walk_strings(data, all_strings)
        full_text = " ".join(all_strings)

        raw_urls: List[str] = []
        for s in all_strings:
            raw_urls.extend(_extract_urls(s))
        seen: set = set()
        urls: List[str] = []
        for u in raw_urls:
            if u not in seen:
                seen.add(u)
                urls.append(u)
        urls = urls[:max_urls]

        _Q_PAT = chr(34) + r"([^" + chr(34) + r"]{10,200})" + chr(34)
        quotes: List[str] = re.findall(_Q_PAT, full_text)

        citation_results: List[Dict[str, Any]] = []
        for url in urls:
            try:
                cr = _check_url(url, timeout=timeout, fetch_snippet=fetch_snippet)
            except Exception as exc:
                cr = {
                    "url": url, "valid_format": False, "reachable": False,
                    "status_code": None, "redirect_url": None,
                    "check_ms": 0.0, "snippet": None,
                    "error": str(exc),
                }
                warnings.append(f"Error checking {url}: {exc}")
            citation_results.append(cr)

        reachable_snippets: Dict[str, str] = {
            cr["url"]: cr["snippet"]
            for cr in citation_results
            if cr.get("reachable") and cr.get("snippet")
        }
        quote_checks: List[Dict[str, Any]] = []
        for q in quotes:
            found_in: Optional[str] = None
            for url, snippet in reachable_snippets.items():
                if q.lower() in snippet.lower():
                    found_in = url
                    break
            quote_checks.append({"quote": q, "found_in_url": found_in})

        n_reachable = sum(1 for cr in citation_results if cr.get("reachable"))
        n_verified = sum(1 for qc in quote_checks if qc["found_in_url"] is not None)

        report: Dict[str, Any] = {
            "total_citations": len(citation_results),
            "reachable": n_reachable,
            "unreachable": len(citation_results) - n_reachable,
            "verified_quotes": n_verified,
            "unverifiable_quotes": len(quote_checks) - n_verified,
            "citations": citation_results,
            "quote_checks": quote_checks,
        }

        out_path = rdp / "citation_verification_report.json"
        artifacts: List[str] = []
        try:
            from .._atomic_io import atomic_write_text
        except ImportError:  # flat-launcher mode
            from _atomic_io import atomic_write_text  # type: ignore[no-redef]
        try:
            # v1.1.11: shared atomic writer (parent-dir fsync, CLAUDE.md §13.1).
            atomic_write_text(
                out_path,
                json.dumps(report, indent=2, default=str),
            )
            artifacts.append(str(out_path))
        except Exception as exc:
            warnings.append(f"Failed to write citation report: {exc}")

        return FeatureResult(
            feature=self.name,
            success=True,
            summary=(
                f"Checked {len(citation_results)} URLs: "
                f"{n_reachable} reachable, "
                f"{len(citation_results) - n_reachable} unreachable; "
                f"{n_verified}/{len(quote_checks)} quotes verified."
            ),
            details={
                "citation_verifier": report,
                "artifacts": artifacts,
                "warnings": warnings,
            },
            duration_seconds=time.monotonic() - t0,
        )
