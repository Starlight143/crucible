from __future__ import annotations

import ipaddress
import json
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

if __package__ == "crucible.web_research":
    from ..resilience import execute_with_retry
    from ..http_retry import is_http_retryable
    from ..runtime_logging import get_logger
else:  # pragma: no cover - direct script fallback
    from resilience import execute_with_retry  # type: ignore[no-redef]
    from http_retry import is_http_retryable  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]

LOGGER = get_logger(__name__)


def _breaker_name_for_url(prefix: str, url: str) -> str:
    """Derive a stable per-host circuit-breaker name.

    Shared breaker names cause one misbehaving endpoint to open the breaker
    for every other endpoint that happens to route through the same helper.
    Scoping the breaker to the target hostname isolates failures.
    """
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:
        host = ""
    return f"{prefix}:{host}" if host else prefix


def _is_public_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return ip.is_global
    if "." not in host:
        return False
    if host.endswith((".local", ".internal", ".lan", ".home", ".corp", ".localdomain")):
        return False
    return True


def _coerce_query_value(v: Any) -> Optional[str]:
    """Convert a query-parameter value to a URL-safe string, or None to omit it.

    None values are dropped entirely (the key is excluded from the query).
    Booleans are serialised as lowercase "true"/"false" rather than Python's
    capitalised "True"/"False" which confuse most server-side parsers.
    All other types are converted via str().
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def append_url_query(base_url: str, params: Dict[str, Any], *, doseq: bool = False) -> str:
    parsed = urlparse(base_url)
    existing = parse_qsl(parsed.query, keep_blank_values=True)
    merged = list(existing)
    for key, value in params.items():
        if doseq and isinstance(value, (list, tuple)):
            for item in value:
                coerced = _coerce_query_value(item)
                if coerced is not None:
                    merged.append((key, coerced))
        else:
            coerced = _coerce_query_value(value)
            if coerced is not None:
                merged.append((key, coerced))
    return urlunparse(parsed._replace(query=urlencode(merged, doseq=False)))


def _http_client(*, timeout_seconds: float, user_agent: str) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_seconds,
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )


def safe_http_json(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    user_agent: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
    circuit_breaker_name: Optional[str] = None,
) -> Any:
    if not _is_public_http_url(url):
        raise ValueError(f"Refusing non-public HTTP(S) URL: {url!r}")
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)

    resolved_breaker = (
        circuit_breaker_name
        if circuit_breaker_name
        else _breaker_name_for_url("librarian_http_json", url)
    )

    with _http_client(timeout_seconds=timeout_seconds, user_agent=user_agent) as client:

        def _execute() -> Any:
            response = client.request(
                method.upper(),
                url,
                headers=request_headers,
                json=payload,
            )
            response.raise_for_status()
            raw_bytes = response.content
            if len(raw_bytes) > max_bytes:
                raise ValueError(f"HTTP JSON response exceeded byte budget ({max_bytes} bytes).")
            # JSON payloads are UTF-8 per RFC 8259 regardless of Content-Type charset.
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            return json.loads(raw_text)

        return execute_with_retry(
            _execute,
            operation_name="safe_http_json",
            max_attempts=3,
            backoff_seconds=1.0,
            retryable_exceptions=(
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.HTTPStatusError,
            ),
            # Use the status-code-aware classifier: only 408/429/5xx are retried.
            # JSONDecodeError is intentionally excluded from retryable_exceptions
            # — a malformed body returned with 200 OK is a permanent upstream bug
            # that will not self-heal on retry, so we fail fast.
            retryable_exception_filter=is_http_retryable,
            circuit_breaker_name=resolved_breaker,
            logger=LOGGER,
            log_fields={"url": url, "method": method.upper()},
        )


def safe_http_text(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    user_agent: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    circuit_breaker_name: Optional[str] = None,
) -> str:
    if not _is_public_http_url(url):
        raise ValueError(f"Refusing non-public HTTP(S) URL: {url!r}")
    request_headers = {
        "Accept": "text/html, text/plain, application/xhtml+xml, application/xml;q=0.9, text/xml;q=0.9"
    }
    if headers:
        request_headers.update(headers)

    resolved_breaker = (
        circuit_breaker_name
        if circuit_breaker_name
        else _breaker_name_for_url("librarian_http_text", url)
    )

    with _http_client(timeout_seconds=timeout_seconds, user_agent=user_agent) as client:

        def _execute() -> str:
            response = client.request(method.upper(), url, headers=request_headers)
            response.raise_for_status()
            if response.status_code != 200:
                # 2xx codes other than 200 (e.g. 202 from DuckDuckGo bot-detection)
                # are not valid HTML responses — raise so callers can fall back.
                raise httpx.HTTPStatusError(
                    f"Non-200 success status: {response.status_code}",
                    request=response.request,
                    response=response,
                )
            raw_bytes = response.content
            if len(raw_bytes) > max_bytes:
                raw_bytes = raw_bytes[:max_bytes]
            return raw_bytes.decode(response.encoding or "utf-8", errors="replace")

        return execute_with_retry(
            _execute,
            operation_name="safe_http_text",
            max_attempts=3,
            backoff_seconds=1.0,
            retryable_exceptions=(httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError),
            retryable_exception_filter=is_http_retryable,
            circuit_breaker_name=resolved_breaker,
            logger=LOGGER,
            log_fields={"url": url, "method": method.upper()},
        )
