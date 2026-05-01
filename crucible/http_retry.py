"""
crucible/http_retry.py
==============================
HTTP retry decorator and helpers for ad-hoc HTTP calls in the pipeline.

The existing ``web_research/http_clients.py`` already wraps ``_safe_http_text``
and ``_safe_http_json`` with ``execute_with_retry``.  This module provides:

1. ``@with_http_retry`` — a decorator for any function that makes HTTP calls
   using *requests* or *httpx* that are NOT already routed through the safe
   wrappers, so new code gets retry behaviour without rewriting the call site.

2. ``is_http_retryable(exc)`` — explicit predicate recognising transient HTTP
   errors across requests/httpx/urllib3 error classes.

3. ``HttpRetryConfig`` — typed retry configuration dataclass, env-var backed.

4. ``safe_get(url, ...)`` / ``safe_post(url, ...)`` — thin wrappers around
   *httpx* for one-off calls that need retry + timeout + size guard without
   depending on the full ``http_clients.py`` machinery.

Design
------
* Builds on ``resilience.execute_with_retry`` so retry semantics are
  consistent across the whole codebase.
* Zero new mandatory dependencies — falls back gracefully if httpx is absent.
* Thread-safe: each decorated call gets its own retry state.

Usage::

    from crucible.http_retry import with_http_retry, safe_get

    # Decorate an existing function:
    @with_http_retry(max_attempts=3, operation_name="context7_fetch")
    def fetch_context7(url: str) -> dict:
        import httpx
        return httpx.get(url, timeout=30).json()

    # One-shot retry-aware GET:
    text = safe_get("https://api.example.com/data", timeout=30)
"""
from __future__ import annotations

import functools
import json as _json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, Type

if __package__ == "crucible":
    from .resilience import execute_with_retry, is_transient_retryable_error
    from .runtime_logging import get_logger
    from .cancellation import OperationCancelledError as _OperationCancelledError
else:  # pragma: no cover
    from resilience import execute_with_retry, is_transient_retryable_error  # type: ignore
    from runtime_logging import get_logger  # type: ignore
    from cancellation import OperationCancelledError as _OperationCancelledError  # type: ignore

LOGGER = get_logger(__name__)

# ── Default config ─────────────────────────────────────────────────────────────

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_SECONDS = 2.0
_DEFAULT_MAX_BACKOFF_SECONDS = 30.0
_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024   # 2 MB


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name, "")
        # Do not clamp to max(1, ...) here: callers that require positive values
        # (e.g. max_attempts) clamp independently.  Clamping unconditionally blocks
        # 0 as a valid "no-limit" or "disabled" env override (e.g. max_bytes=0).
        return int(v) if v.strip() else default
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "")
        return float(v) if v.strip() else default
    except (ValueError, TypeError):
        return default


# ── Retry predicate ───────────────────────────────────────────────────────────

def is_http_retryable(exc: BaseException) -> bool:
    """
    Return True when *exc* is a transient HTTP / network error worth retrying.

    Recognises error classes from: requests, httpx, urllib3.
    Delegates to ``resilience.is_transient_retryable_error`` for text-based
    matching after checking class hierarchy.
    """
    class_name = type(exc).__name__.lower()
    http_class_markers = (
        "timeout", "connecttimeout", "readtimeout",
        "connectionerror", "networkerror", "remotedisconnected",
        "protocolerror", "chunkedencodingerror",
        "httpstatuserror",  # httpx: non-2xx; check status code below
        "httperror",
        "requestexception",
    )
    if any(marker in class_name for marker in http_class_markers):
        # For httpx.HTTPStatusError / requests.HTTPError, only retry on 5xx,
        # 429 (rate limit), and 408 (request timeout — server-side transient
        # condition that is explicitly retryable per RFC 7231 §6.5.7).
        # If the status code is readable, use it as the authoritative decision.
        status = getattr(exc, "response", None)
        if status is not None:
            status_code = getattr(status, "status_code", None)
            if status_code is not None:
                return status_code in (408, 429, 500, 502, 503, 504)
        # For status-carrying error classes (HTTPStatusError, HTTPError) where
        # we cannot read the status code, default to NOT retrying.  These
        # classes indicate a completed HTTP response whose status was
        # non-2xx; without a status code we must be conservative rather than
        # blindly retrying a potentially permanent failure (e.g. 404, 403).
        _status_error_markers = ("httpstatuserror", "httperror")
        if any(m in class_name for m in _status_error_markers):
            return False
        # Non-status-carrying matches (timeout, connection error, etc.):
        # treat as transient and retry.
        return True
    return is_transient_retryable_error(exc)


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class HttpRetryConfig:
    """
    Retry configuration for HTTP calls.

    All fields default to the corresponding ``HTTP_RETRY_*`` env var.
    """
    max_attempts: int = 0           # 0 → use env/default
    backoff_seconds: float = 0.0    # 0 → use env/default
    max_backoff_seconds: float = 0.0
    timeout_seconds: int = 0        # 0 → use env/default for safe_get/post
    max_bytes: int = 0              # 0 → use env/default for safe_get/post

    def resolved_max_attempts(self) -> int:
        # 0 is the sentinel meaning "use env/default"; use explicit == 0 check
        # rather than `or` so that a future caller passing a non-zero value is
        # never silently overridden by a falsy short-circuit.
        return (
            _env_int("HTTP_RETRY_MAX_ATTEMPTS", _DEFAULT_MAX_ATTEMPTS)
            if self.max_attempts == 0
            else self.max_attempts
        )

    def resolved_backoff(self) -> float:
        return (
            _env_float("HTTP_RETRY_BACKOFF_SECONDS", _DEFAULT_BACKOFF_SECONDS)
            if self.backoff_seconds == 0.0
            else self.backoff_seconds
        )

    def resolved_max_backoff(self) -> float:
        return (
            _env_float("HTTP_RETRY_MAX_BACKOFF_SECONDS", _DEFAULT_MAX_BACKOFF_SECONDS)
            if self.max_backoff_seconds == 0.0
            else self.max_backoff_seconds
        )

    def resolved_timeout(self) -> int:
        return (
            _env_int("HTTP_RETRY_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
            if self.timeout_seconds == 0
            else self.timeout_seconds
        )

    def resolved_max_bytes(self) -> int:
        return (
            _env_int("HTTP_RETRY_MAX_BYTES", _DEFAULT_MAX_BYTES)
            if self.max_bytes == 0
            else self.max_bytes
        )


_DEFAULT_CONFIG = HttpRetryConfig()


# ── Decorator ─────────────────────────────────────────────────────────────────

def with_http_retry(
    fn: Optional[Callable] = None,
    *,
    max_attempts: int = 0,
    backoff_seconds: float = 0.0,
    max_backoff_seconds: float = 0.0,
    operation_name: str = "",
    config: Optional[HttpRetryConfig] = None,
) -> Any:
    """
    Decorator that wraps a function with HTTP-aware retry logic.

    Can be used with or without arguments::

        @with_http_retry
        def fetch(): ...

        @with_http_retry(max_attempts=5, operation_name="my_fetch")
        def fetch(): ...

    Parameters
    ----------
    max_attempts:
        Override max retry attempts. 0 → env var / default (3).
    backoff_seconds:
        Override initial backoff. 0 → env var / default (2.0s).
    max_backoff_seconds:
        Override max backoff cap. 0 → env var / default (30.0s).
    operation_name:
        Name for log messages. Defaults to the wrapped function's ``__name__``.
    config:
        Full ``HttpRetryConfig`` override (takes precedence over individual args).
    """
    cfg = config or HttpRetryConfig(
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )

    def decorator(func: Callable) -> Callable:
        op_name = operation_name or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return execute_with_retry(
                lambda: func(*args, **kwargs),
                operation_name=op_name,
                max_attempts=cfg.resolved_max_attempts(),
                backoff_seconds=cfg.resolved_backoff(),
                max_backoff_seconds=cfg.resolved_max_backoff(),
                retryable_exceptions=(Exception,),
                retryable_exception_filter=is_http_retryable,
                logger=LOGGER,
            )

        return wrapper

    if fn is not None:
        # Called without arguments: @with_http_retry
        return decorator(fn)
    return decorator


# ── safe_get / safe_post ──────────────────────────────────────────────────────

def safe_get(
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: Optional[int] = None,
    max_bytes: Optional[int] = None,
    config: Optional[HttpRetryConfig] = None,
    as_json: bool = False,
) -> Any:
    """
    Retry-aware HTTP GET returning text (or parsed JSON when ``as_json=True``).

    Falls back to ``None`` on permanent failure so callers can handle
    missing data gracefully instead of crashing.

    Parameters
    ----------
    url:
        Target URL.
    headers:
        Optional extra request headers.
    timeout:
        Per-attempt timeout in seconds. Defaults to ``HttpRetryConfig.resolved_timeout()``.
    max_bytes:
        Maximum response body size. Excess is silently truncated.
    config:
        Optional full config override.
    as_json:
        When True, parse response as JSON and return the decoded object.
    """
    try:
        import httpx  # type: ignore
    except ImportError:  # pragma: no cover
        LOGGER.warning("safe_get: httpx not installed — install httpx to use safe_get.")
        return None

    # Explicit None-check: a caller may pass config=HttpRetryConfig(...) whose
    # __bool__ could be overridden (e.g. Pydantic model).  Use `is not None`.
    cfg = config if config is not None else _DEFAULT_CONFIG
    # Explicit None-check: a caller may pass timeout=0 ("no timeout") or
    # max_bytes=0 ("no size cap"); `or` would silently discard those.
    resolved_timeout = cfg.resolved_timeout() if timeout is None else timeout
    resolved_max_bytes = cfg.resolved_max_bytes() if max_bytes is None else max_bytes

    def _do_get() -> Any:
        resp = httpx.get(url, headers=headers or {}, timeout=resolved_timeout, follow_redirects=True)
        resp.raise_for_status()
        # Always apply max_bytes guard; for JSON, parse the (possibly truncated) bytes
        # directly so the size cap is consistently enforced.
        # resolved_max_bytes=0 means "no cap" (caller passed max_bytes=0).
        # content[:0] would return empty bytes, discarding the entire response.
        body = resp.content if resolved_max_bytes == 0 else resp.content[:resolved_max_bytes]
        if as_json:
            return _json.loads(body)
        return body.decode("utf-8", errors="replace")

    try:
        return execute_with_retry(
            _do_get,
            operation_name=f"safe_get:{url[:80]}",
            max_attempts=cfg.resolved_max_attempts(),
            backoff_seconds=cfg.resolved_backoff(),
            max_backoff_seconds=cfg.resolved_max_backoff(),
            retryable_exceptions=(Exception,),
            retryable_exception_filter=is_http_retryable,
            logger=LOGGER,
        )
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — do not convert it to None.
        raise
    except Exception as exc:
        LOGGER.warning("safe_get: permanent failure for '%s': %s", url[:80], exc)
        return None


def safe_post(
    url: str,
    payload: Any = None,
    *,
    headers: Optional[dict] = None,
    timeout: Optional[int] = None,
    config: Optional[HttpRetryConfig] = None,
    as_json: bool = True,
) -> Any:
    """
    Retry-aware HTTP POST returning parsed JSON (default) or text.

    Falls back to ``None`` on permanent failure.
    """
    try:
        import httpx  # type: ignore
    except ImportError:  # pragma: no cover
        LOGGER.warning("safe_post: httpx not installed.")
        return None

    cfg = config if config is not None else _DEFAULT_CONFIG
    # Explicit None-check: timeout=0 means "no timeout", not "use default".
    resolved_timeout = cfg.resolved_timeout() if timeout is None else timeout

    def _do_post() -> Any:
        resp = httpx.post(
            url,
            json=payload,
            headers=headers or {},
            timeout=resolved_timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json() if as_json else resp.text

    try:
        return execute_with_retry(
            _do_post,
            operation_name=f"safe_post:{url[:80]}",
            max_attempts=cfg.resolved_max_attempts(),
            backoff_seconds=cfg.resolved_backoff(),
            max_backoff_seconds=cfg.resolved_max_backoff(),
            retryable_exceptions=(Exception,),
            retryable_exception_filter=is_http_retryable,
            logger=LOGGER,
        )
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — do not convert it to None.
        raise
    except Exception as exc:
        LOGGER.warning("safe_post: permanent failure for '%s': %s", url[:80], exc)
        return None
