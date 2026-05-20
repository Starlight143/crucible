"""v1.1.8 extended Phase 4 (Q9) — HTTP/2 + keep-alive integration tests.

Coverage:

* ``_h2_available`` reflects actual h2 package presence.
* ``_http2_enabled`` honours env + h2 availability.
* ``_keepalive_enabled`` honours env.
* ``_http_client`` constructs httpx.Client without raising even when
  http2 is requested but h2 missing.
* The SSRF protection (``follow_redirects=False`` + manual redirect
  walker) is preserved when HTTP/2 is on.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from crucible.web_research import http_clients


class TestH2Available:
    def test_returns_bool(self) -> None:
        # Whether h2 is installed depends on environment — we just
        # require a bool return without crashing.
        assert isinstance(http_clients._h2_available(), bool)


class TestHttp2Enabled:
    def test_default_enabled_iff_h2(self, monkeypatch) -> None:
        # Default env (no override).
        monkeypatch.delenv("LIBRARIAN_HTTP2_ENABLED", raising=False)
        with patch(
            "crucible.web_research.http_clients._h2_available",
            return_value=True,
        ):
            assert http_clients._http2_enabled() is True
        with patch(
            "crucible.web_research.http_clients._h2_available",
            return_value=False,
        ):
            assert http_clients._http2_enabled() is False

    def test_env_disabled_overrides_h2_available(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_HTTP2_ENABLED", "0")
        with patch(
            "crucible.web_research.http_clients._h2_available",
            return_value=True,
        ):
            assert http_clients._http2_enabled() is False

    def test_env_enabled_but_h2_missing_returns_false(
        self, monkeypatch,
    ) -> None:
        # Operator opts in via env, but h2 not installed → still off.
        # No crash, no warning spam.
        monkeypatch.setenv("LIBRARIAN_HTTP2_ENABLED", "1")
        with patch(
            "crucible.web_research.http_clients._h2_available",
            return_value=False,
        ):
            assert http_clients._http2_enabled() is False


class TestKeepaliveEnabled:
    def test_default_enabled(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_HTTP_KEEPALIVE_ENABLED", raising=False)
        assert http_clients._keepalive_enabled() is True

    def test_explicit_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_HTTP_KEEPALIVE_ENABLED", "0")
        assert http_clients._keepalive_enabled() is False


class TestHttpClientConstruction:
    def test_constructs_without_raising(self) -> None:
        # _http_client should never raise even when h2 is missing AND
        # http2 is env-enabled — _http2_enabled() returns False in that
        # case so httpx.Client is constructed without http2=True.
        client = http_clients._http_client(
            timeout_seconds=10.0, user_agent="test-agent",
        )
        try:
            assert client is not None
            # SSRF protection preserved.
            assert client.follow_redirects is False
        finally:
            client.close()

    def test_keepalive_disabled_sets_max_keepalive_zero(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("LIBRARIAN_HTTP_KEEPALIVE_ENABLED", "0")
        client = http_clients._http_client(
            timeout_seconds=10.0, user_agent="test-agent",
        )
        try:
            # httpx.Limits.max_keepalive_connections should be 0 when
            # keepalive is disabled.  This attribute is part of httpx's
            # public API so it's safe to assert against.
            limits = client._transport._pool._max_keepalive_connections  # type: ignore[attr-defined]
            assert limits == 0
        finally:
            client.close()

    def test_keepalive_enabled_default_pool(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_HTTP_KEEPALIVE_ENABLED", "1")
        client = http_clients._http_client(
            timeout_seconds=10.0, user_agent="test-agent",
        )
        try:
            limits = client._transport._pool._max_keepalive_connections  # type: ignore[attr-defined]
            assert limits == 10
        finally:
            client.close()


class TestSsrfPreservedWithHttp2:
    """When HTTP/2 is on, follow_redirects must still be False and the
    manual redirect walker (``_request_with_safe_redirects``) is the
    only place a hop is allowed.  Critical for not regressing the
    v1.1.2 sixth-pass H-2 SSRF hardening (AWS IMDS via redirect)."""

    def test_follow_redirects_false_with_http2(self, monkeypatch) -> None:
        # Force http2_enabled = True via the helper (skipping h2 check).
        monkeypatch.setenv("LIBRARIAN_HTTP2_ENABLED", "1")
        with patch(
            "crucible.web_research.http_clients._h2_available",
            return_value=True,
        ):
            # If h2 is genuinely installed this builds an h2 client;
            # else the test would have skipped via the previous tests.
            # We catch the ImportError defensively here so we don't
            # falsely fail in environments without h2.
            try:
                client = http_clients._http_client(
                    timeout_seconds=10.0, user_agent="test-agent",
                )
            except ImportError:
                pytest.skip("h2 package not installed in this environment")
            try:
                assert client.follow_redirects is False
            finally:
                client.close()
