# ruff: noqa: E402, I001
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import httpx

from crucible.web_research import http_clients


class TestHttpClients(unittest.TestCase):
    def test_is_public_http_url_accepts_public_https_url(self) -> None:
        self.assertTrue(http_clients._is_public_http_url("https://example.com/data"))

    def test_is_public_http_url_rejects_localhost_and_file_scheme(self) -> None:
        self.assertFalse(http_clients._is_public_http_url("http://localhost:8000/health"))
        self.assertFalse(http_clients._is_public_http_url("file:///etc/passwd"))

    def test_is_public_http_url_rejects_private_and_loopback_ip_literals(self) -> None:
        self.assertFalse(http_clients._is_public_http_url("http://127.0.0.1:8080/health"))
        self.assertFalse(http_clients._is_public_http_url("http://10.0.0.8/internal"))
        self.assertFalse(http_clients._is_public_http_url("http://192.168.1.10/status"))
        self.assertFalse(http_clients._is_public_http_url("http://172.16.0.5/status"))

    def test_is_public_http_url_accepts_global_ip_literal(self) -> None:
        self.assertTrue(http_clients._is_public_http_url("https://8.8.8.8/dns-query"))

    def test_safe_http_text_rejects_non_public_urls_before_network_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "Refusing non-public HTTP"):
            http_clients.safe_http_text(
                "file:///etc/passwd",
                timeout_seconds=1.0,
                max_bytes=1024,
                user_agent="test-agent",
            )

    def test_safe_http_json_rejects_non_public_urls_before_network_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "Refusing non-public HTTP"):
            http_clients.safe_http_json(
                "http://127.0.0.1:8000/private",
                timeout_seconds=1.0,
                max_bytes=1024,
                user_agent="test-agent",
            )

    def test_safe_http_text_raises_for_202_bot_detection_response(self) -> None:
        """HTTP 202 from DuckDuckGo (bot detection) must raise, not return CAPTCHA HTML."""
        fake_request = httpx.Request("GET", "https://html.duckduckgo.com/html/?q=test")
        fake_response = httpx.Response(
            status_code=202,
            content=b"<html><body>Please verify you are a human</body></html>",
            request=fake_request,
        )
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request = MagicMock(return_value=fake_response)

        with patch.object(http_clients, "_http_client", return_value=mock_client):
            with self.assertRaises(httpx.HTTPStatusError) as ctx:
                http_clients.safe_http_text(
                    "https://html.duckduckgo.com/html/?q=test",
                    timeout_seconds=5.0,
                    max_bytes=65536,
                    user_agent="test-agent",
                )
        self.assertIn("202", str(ctx.exception))

    def test_safe_http_text_returns_body_for_200_response(self) -> None:
        fake_request = httpx.Request("GET", "https://example.com/page")
        fake_response = httpx.Response(
            status_code=200,
            content=b"<html><body>Hello</body></html>",
            request=fake_request,
        )
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request = MagicMock(return_value=fake_response)

        with patch.object(http_clients, "_http_client", return_value=mock_client):
            result = http_clients.safe_http_text(
                "https://example.com/page",
                timeout_seconds=5.0,
                max_bytes=65536,
                user_agent="test-agent",
            )
        self.assertIn("Hello", result)

    def test_breaker_name_for_url_uses_hostname(self) -> None:
        """Per-host breaker names prevent one endpoint from opening the breaker for others."""
        self.assertEqual(
            http_clients._breaker_name_for_url("librarian_http_text", "https://html.duckduckgo.com/html/?q=x"),
            "librarian_http_text:html.duckduckgo.com",
        )
        self.assertEqual(
            http_clients._breaker_name_for_url("librarian_http_json", "https://grep.app/api/search?q=x"),
            "librarian_http_json:grep.app",
        )
        self.assertNotEqual(
            http_clients._breaker_name_for_url("librarian_http_json", "https://api.github.com/search/repositories?q=x"),
            http_clients._breaker_name_for_url("librarian_http_json", "https://grep.app/api/search?q=x"),
        )

    def test_safe_http_json_does_not_retry_404_client_error(self) -> None:
        """404 is a permanent client error and must not burn retry budget."""
        from crucible.resilience import reset_circuit_breakers

        reset_circuit_breakers()
        fake_request = httpx.Request("GET", "https://api.example.com/missing")
        fake_response = httpx.Response(
            status_code=404,
            content=b'{"error":"not found"}',
            request=fake_request,
        )
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request = MagicMock(return_value=fake_response)

        with patch.object(http_clients, "_http_client", return_value=mock_client):
            with self.assertRaises(httpx.HTTPStatusError):
                http_clients.safe_http_json(
                    "https://api.example.com/missing",
                    timeout_seconds=5.0,
                    max_bytes=65536,
                    user_agent="test-agent",
                )
        # 404 must NOT be retried: only one request was issued.
        self.assertEqual(mock_client.request.call_count, 1)


class TestSearchHelpers(unittest.TestCase):
    """Cover the librarian search helpers."""

    def test_grep_app_skips_cjk_queries(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _is_grep_app_compatible_query,
        )

        self.assertFalse(_is_grep_app_compatible_query("幣安永續合約 資金費率策略 回測績效"))
        self.assertFalse(_is_grep_app_compatible_query("日本語の自然言語 クエリ"))
        self.assertFalse(_is_grep_app_compatible_query("한국어 자연어 검색"))

    def test_grep_app_skips_long_natural_language(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _is_grep_app_compatible_query,
        )

        # >8 tokens and no code markers -> natural language -> skip
        self.assertFalse(_is_grep_app_compatible_query(
            "how do i backtest a funding rate strategy for perpetual futures contracts reliably"
        ))

    def test_grep_app_accepts_code_queries(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _is_grep_app_compatible_query,
        )

        self.assertTrue(_is_grep_app_compatible_query("def compute_funding_rate"))
        self.assertTrue(_is_grep_app_compatible_query("np.mean(arr)"))
        self.assertTrue(_is_grep_app_compatible_query("portfolio.rebalance"))
        self.assertTrue(_is_grep_app_compatible_query("websocket orderbook"))

    def test_duckduckgo_parser_ignores_captcha_nofollow_links(self) -> None:
        """CAPTCHA pages contain rel=nofollow privacy/about links that must NOT become citations."""
        from crucible.modules.section_04_web_research_and_direction import (
            _extract_websearch_citations_from_html,
        )

        captcha_html = (
            '<html><body>'
            '<h1>Please verify you are a human</h1>'
            '<a rel="nofollow" href="https://duckduckgo.com/privacy">Privacy</a>'
            '<a rel="nofollow" href="https://spreadprivacy.com/about">About</a>'
            '<a rel="nofollow" href="https://duckduckgo.com/settings">Settings</a>'
            '</body></html>'
        )
        citations = _extract_websearch_citations_from_html(captcha_html, query="test")
        self.assertEqual(citations, [])

    def test_duckduckgo_parser_extracts_real_results(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _extract_websearch_citations_from_html,
        )

        html = (
            '<html><body>'
            '<a class="result__a" href="https://example.com/page1">Example Title</a>'
            '<div class="result__snippet">This is a real snippet.</div>'
            '</body></html>'
        )
        citations = _extract_websearch_citations_from_html(html, query="test")
        self.assertEqual(len(citations), 1)
        self.assertEqual(citations[0].url, "https://example.com/page1")

    def test_github_code_search_skipped_without_token(self) -> None:
        """search/code requires auth; skip silently when no token is configured."""
        from crucible.modules.section_04_web_research_and_direction import (
            _search_github_code,
        )

        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_API_TOKEN": ""},
            clear=False,
        ):
            self.assertEqual(_search_github_code("def foo()"), [])

    def test_github_token_placeholder_treated_as_absent(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _resolve_github_token,
        )

        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "your_github_token_here",
                "GH_TOKEN": "",
                "GITHUB_API_TOKEN": "",
            },
            clear=False,
        ):
            self.assertEqual(_resolve_github_token(), "")

    def test_github_api_headers_include_auth_when_token_present(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _github_api_headers,
        )

        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ghp_fakefakefake1234567890", "GH_TOKEN": "", "GITHUB_API_TOKEN": ""},
            clear=False,
        ):
            headers = _github_api_headers(accept="application/vnd.github+json")
        self.assertEqual(headers["Authorization"], "Bearer ghp_fakefakefake1234567890")
        self.assertEqual(headers["X-GitHub-Api-Version"], "2022-11-28")

    def test_github_api_headers_omit_auth_when_token_absent(self) -> None:
        from crucible.modules.section_04_web_research_and_direction import (
            _github_api_headers,
        )

        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "", "GH_TOKEN": "", "GITHUB_API_TOKEN": ""},
            clear=False,
        ):
            headers = _github_api_headers(accept="application/vnd.github+json")
        self.assertNotIn("Authorization", headers)

    def test_arxiv_returns_empty_on_malformed_xml(self) -> None:
        """arxiv sometimes returns truncated/HTML instead of Atom XML.

        Must not propagate a ParseError to the librarian loop (which
        would cause a fatal traceback up the stack in some edge cases).
        """
        from crucible.modules.section_04_web_research_and_direction import (
            _search_arxiv,
        )

        for bad_xml in (
            "<html><body>503 Service Unavailable</body></html>",  # HTML error page
            "<?xml version='1.0'?><feed xmlns='bad'",            # truncated
            "",                                                    # empty body
            "not xml at all",                                     # garbage
        ):
            with patch(
                "crucible.modules.section_04_web_research_and_direction._safe_http_text",
                return_value=bad_xml,
            ):
                self.assertEqual(_search_arxiv("funding rate backtest"), [])


if __name__ == "__main__":
    unittest.main()
