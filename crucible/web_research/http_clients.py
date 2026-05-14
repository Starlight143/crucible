from __future__ import annotations

import ipaddress
import json
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx

if __package__ == "crucible.web_research":
    from ..http_retry import is_http_retryable
    from ..resilience import execute_with_retry
    from ..runtime_logging import get_logger
else:  # pragma: no cover - direct script fallback
    from http_retry import is_http_retryable  # type: ignore[no-redef]
    from resilience import execute_with_retry  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]

LOGGER = get_logger(__name__)


# v1.1.2 (sixth-pass H-2): maximum redirect hops for the SSRF-checked redirect
# handler.  Matches the WebUI ``_safe_urlopen`` default.
_MAX_REDIRECTS: int = 3


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


def _ipv6_embedded_v4(addr: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    """Extract an embedded IPv4 address from various IPv6 forms.

    Mirrors ``webui.app._ipv6_embedded_v4`` (v1.1.0 fourth-pass F-2) so the
    LLM-driven citation-fetch path applies the same SSRF unwrapping as the
    WebUI's user-input outbound paths.  Catches:

    1. ``::ffff:w.x.y.z`` (IPv4-mapped, RFC 4291 §2.5.5.2).
    2. ``::w.x.y.z``      (IPv4-compatible, deprecated RFC 4291 §2.5.5.1).
    3. ``2002:wxyz:abcd::`` (6to4, RFC 3056).
    4. ``64:ff9b::w.x.y.z`` (NAT64 well-known, RFC 6052).

    Returns ``None`` when *addr* does not embed an IPv4 address.
    """
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    packed = addr.packed  # 16 bytes
    if packed[:12] == b"\x00" * 12 and packed[12:] != b"\x00\x00\x00\x00":
        try:
            return ipaddress.IPv4Address(packed[12:])
        except (ValueError, ipaddress.AddressValueError):
            return None
    if packed[:2] == b"\x20\x02":
        try:
            return ipaddress.IPv4Address(packed[2:6])
        except (ValueError, ipaddress.AddressValueError):
            return None
    if packed[:12] == b"\x00\x64\xff\x9b" + b"\x00" * 8:
        try:
            return ipaddress.IPv4Address(packed[12:])
        except (ValueError, ipaddress.AddressValueError):
            return None
    return None


def _addr_is_safe(addr: "ipaddress._BaseAddress") -> bool:
    """Return True iff *addr* is a globally-reachable unicast address.

    Recursively unwraps IPv4-embedded-in-IPv6 forms.  Rejects multicast,
    reserved, unspecified, loopback and link-local — mirrors
    ``webui.app._addr_is_safe`` (v1.1.0 fifth-pass G-2).

    ``is_global`` is necessary but not sufficient: Python reports
    ``is_global=True`` for multicast (224.0.0.0/4 IPv4 + ff00::/8 IPv6),
    the unspecified ranges, and several reserved blocks.  The additional
    predicates close that gap.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _ipv6_embedded_v4(addr)
        if embedded is not None:
            return _addr_is_safe(embedded)
    if (
        addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_loopback
        or addr.is_link_local
    ):
        return False
    return bool(addr.is_global)


def _is_public_http_url(url: str) -> bool:
    """Return True iff *url* points to a public HTTPS/HTTP host.

    v1.1.2 (sixth-pass H-2): hardened to add the structural SSRF checks the
    WebUI's ``_is_safe_url`` already enforces.  Specifically:

    * Rejects ``userinfo`` smuggling (``http://victim@evil.com/``).
    * Rejects IPv6 scope-id syntax (``fe80::1%eth0``).
    * Unwraps IPv4-embedded IPv6 forms whose underlying v4 is private
      (``::ffff:10.0.0.1``, ``::10.0.0.1``, ``2002:...``, ``64:ff9b::...``).
    * Rejects multicast / reserved / unspecified / loopback / link-local
      (Python's ``is_global`` mis-reports True for these).

    DNS-rebinding hardening (resolving the hostname and checking each
    answer) is intentionally NOT applied here.  The pre-v1.1.2 contract
    accepts public-looking hostnames without resolution; adding strict
    DNS would break legitimate offline-test behaviour (``api.example.com``
    used as a synthetic name in mocked-httpx unit tests).  The manual-
    redirect helper below still re-validates every hop against this
    function, so attacker-controlled redirects to literal IPs are
    caught; full DNS hardening for hostname-only literals is an opt-in
    follow-up gated on a future env flag.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    # Userinfo smuggling: urlparse returns hostname="evil.com" for
    # ``http://victim@evil.com/`` but httpx will honour the userinfo via
    # Authorization headers.  Treat any userinfo presence as untrusted.
    if parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return False
    # IPv6 scope-id (``fe80::1%eth0``) is link-local by definition.
    if "%" in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return _addr_is_safe(ip)
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
    """Construct an httpx client with auto-redirect-following DISABLED.

    v1.1.2 (sixth-pass H-2): the prior ``follow_redirects=True`` setting
    bypassed ``_is_public_http_url`` on redirect — an attacker-controlled
    public URL could respond ``302 Location: http://169.254.169.254/...``
    (AWS IMDS) and httpx would dutifully follow.  Redirects are now handled
    manually by ``_request_with_safe_redirects`` which re-checks every hop.
    """
    return httpx.Client(
        timeout=timeout_seconds,
        headers={"User-Agent": user_agent},
        follow_redirects=False,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )


def _request_with_safe_redirects(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    payload: Optional[Dict[str, Any]],
    max_redirects: int = _MAX_REDIRECTS,
) -> httpx.Response:
    """Issue *method* *url* via *client* with SSRF-checked manual redirects.

    v1.1.2 (sixth-pass H-2): mirrors ``webui.app._safe_urlopen``.  Validates
    every hop through ``_is_public_http_url``, follows at most
    ``max_redirects`` hops, and per RFC 7231 §6.4 demotes 301/302/303 +
    POST/PUT/PATCH to GET-without-body so the original request body cannot
    be replayed at a different endpoint.

    Raises ``ValueError`` on a redirect to a non-public host, on a redirect
    loop, on a 30x response without a ``Location`` header, or on exceeding
    the redirect budget.  ``ValueError`` is intentionally NOT in the
    retryable exception list — these are policy refusals, not transient
    failures.
    """
    seen: set[str] = set()
    current_url = url
    current_method = method.upper()
    current_payload = payload
    for _hop in range(max_redirects + 1):
        if current_url in seen:
            raise ValueError(
                f"Refusing redirect loop at {current_url!r}"
            )
        seen.add(current_url)
        if not _is_public_http_url(current_url):
            raise ValueError(
                f"Refusing non-public HTTP(S) URL via redirect: {current_url!r}"
            )
        kwargs: Dict[str, Any] = {"headers": headers}
        if current_payload is not None and current_method in {"POST", "PUT", "PATCH"}:
            kwargs["json"] = current_payload
        response = client.request(current_method, current_url, **kwargs)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        loc = response.headers.get("location")
        try:
            response.close()
        except Exception:
            pass
        if not loc:
            raise ValueError(
                f"Refusing {response.status_code} redirect without Location: {current_url!r}"
            )
        next_url = urljoin(current_url, loc)
        # Per RFC 7231 §6.4: on 301/302/303 demote method to GET and clear
        # the body to prevent body-replay against a different endpoint.
        if response.status_code in (301, 302, 303):
            current_method = "GET"
            current_payload = None
        current_url = next_url
    raise ValueError(
        f"Refusing redirect chain longer than {max_redirects} hops"
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
            response = _request_with_safe_redirects(
                client,
                method,
                url,
                headers=request_headers,
                payload=payload,
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
            response = _request_with_safe_redirects(
                client,
                method,
                url,
                headers=request_headers,
                payload=None,
            )
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
