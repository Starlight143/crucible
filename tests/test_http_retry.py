"""Tests for crucible.http_retry"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.http_retry import (
    HttpRetryConfig,
    is_http_retryable,
    with_http_retry,
)


# ── is_http_retryable ─────────────────────────────────────────────────────────

class TestIsHttpRetryable:
    def test_timeout_exception(self):
        class TimeoutError(Exception):
            pass
        assert is_http_retryable(TimeoutError("timed out"))

    def test_connection_error(self):
        class ConnectionError(Exception):
            pass
        assert is_http_retryable(ConnectionError("connection refused"))

    def test_network_error(self):
        class NetworkError(Exception):
            pass
        assert is_http_retryable(NetworkError("network down"))

    def test_generic_value_error_not_retryable(self):
        assert not is_http_retryable(ValueError("bad input"))

    def test_http_status_error_429_retryable(self):
        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 429})()
        assert is_http_retryable(HTTPStatusError("rate limit"))

    def test_http_status_error_503_retryable(self):
        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 503})()
        assert is_http_retryable(HTTPStatusError("service unavailable"))

    def test_http_status_error_404_not_retryable(self):
        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 404})()
        assert not is_http_retryable(HTTPStatusError("not found"))

    def test_http_status_error_500_retryable(self):
        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 500})()
        assert is_http_retryable(HTTPStatusError("server error"))

    def test_transient_text_fallback(self):
        assert is_http_retryable(RuntimeError("deadline exceeded"))

    def test_chained_exception_retryable(self):
        inner = OSError("connection reset")
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert is_http_retryable(outer)


# ── HttpRetryConfig ───────────────────────────────────────────────────────────

class TestHttpRetryConfig:
    def test_defaults(self):
        cfg = HttpRetryConfig()
        assert cfg.resolved_max_attempts() >= 1
        assert cfg.resolved_backoff() >= 0.0
        assert cfg.resolved_max_backoff() >= 0.0
        assert cfg.resolved_timeout() >= 1
        assert cfg.resolved_max_bytes() >= 1

    def test_explicit_override(self):
        cfg = HttpRetryConfig(max_attempts=7, backoff_seconds=1.5, timeout_seconds=60)
        assert cfg.resolved_max_attempts() == 7
        assert cfg.resolved_backoff() == pytest.approx(1.5)
        assert cfg.resolved_timeout() == 60

    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("HTTP_RETRY_MAX_ATTEMPTS", "9")
        cfg = HttpRetryConfig()  # 0 → use env
        assert cfg.resolved_max_attempts() == 9

    def test_zero_fields_use_env_or_default(self, monkeypatch):
        monkeypatch.delenv("HTTP_RETRY_MAX_ATTEMPTS", raising=False)
        cfg = HttpRetryConfig(max_attempts=0)
        assert cfg.resolved_max_attempts() >= 1


# ── @with_http_retry decorator ────────────────────────────────────────────────

class TestWithHttpRetryDecorator:
    def test_successful_call_returns_value(self):
        @with_http_retry
        def fetch():
            return "ok"

        assert fetch() == "ok"

    def test_no_arg_usage(self):
        @with_http_retry
        def fn():
            return 42

        assert fn() == 42

    def test_with_arg_usage(self):
        @with_http_retry(max_attempts=2, operation_name="test_op")
        def fn():
            return "result"

        assert fn() == "result"

    def test_retries_transient_error(self):
        call_count = [0]

        class TimeoutError(Exception):
            pass

        @with_http_retry(max_attempts=3)
        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise TimeoutError("timed out")
            return "success"

        result = flaky()
        assert result == "success"
        assert call_count[0] == 3

    def test_raises_non_retryable_immediately(self):
        call_count = [0]

        @with_http_retry(max_attempts=5)
        def fn():
            call_count[0] += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError):
            fn()
        assert call_count[0] == 1

    def test_exhausts_retries_raises(self):
        class TimeoutError(Exception):
            pass

        @with_http_retry(max_attempts=2)
        def always_fails():
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            always_fails()

    def test_preserves_function_metadata(self):
        @with_http_retry
        def my_named_fn():
            """My docstring."""
            return 1

        assert my_named_fn.__name__ == "my_named_fn"
        assert my_named_fn.__doc__ == "My docstring."

    def test_passes_args_and_kwargs(self):
        @with_http_retry
        def add(a, b, *, extra=0):
            return a + b + extra

        assert add(1, 2, extra=10) == 13

    def test_config_override(self):
        cfg = HttpRetryConfig(max_attempts=1)

        @with_http_retry(config=cfg)
        def fn():
            return "cfg_override"

        assert fn() == "cfg_override"


# ── safe_get / safe_post cancellation propagation ─────────────────────────────

class TestSafeGetSafePostCancellation:
    """
    Regression tests: safe_get() and safe_post() previously caught ALL
    exceptions — including OperationCancelledError — via a bare
    `except Exception` and silently converted them to `return None`.
    Cancellation must now propagate unconditionally.
    """

    def test_safe_get_propagates_cancellation(self, monkeypatch):
        """OperationCancelledError raised during safe_get must not be swallowed."""
        from crucible.cancellation import OperationCancelledError
        from crucible import http_retry

        def _raising_execute(*args, **kwargs):
            raise OperationCancelledError("cancelled during GET")

        monkeypatch.setattr(http_retry, "execute_with_retry", _raising_execute)

        with pytest.raises(OperationCancelledError):
            http_retry.safe_get("https://example.com/test")

    def test_safe_post_propagates_cancellation(self, monkeypatch):
        """OperationCancelledError raised during safe_post must not be swallowed."""
        from crucible.cancellation import OperationCancelledError
        from crucible import http_retry

        # Also need to make httpx importable so the function doesn't bail early.
        # We monkeypatch execute_with_retry so httpx doesn't need to be installed.
        def _raising_execute(*args, **kwargs):
            raise OperationCancelledError("cancelled during POST")

        monkeypatch.setattr(http_retry, "execute_with_retry", _raising_execute)

        with pytest.raises(OperationCancelledError):
            http_retry.safe_post("https://example.com/test", payload={"k": "v"})

    def test_safe_get_still_returns_none_on_ordinary_failure(self, monkeypatch):
        """Non-cancellation permanent failures must still produce None (existing contract)."""
        from crucible import http_retry

        def _raising_execute(*args, **kwargs):
            raise ConnectionError("host unreachable")

        monkeypatch.setattr(http_retry, "execute_with_retry", _raising_execute)

        result = http_retry.safe_get("https://example.com/test")
        assert result is None
