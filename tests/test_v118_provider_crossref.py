"""v1.1.8 extended Phase 3 (Q4) — Crossref provider tests.

Coverage:

* Early returns (empty query, limit<=0).
* HTTP error swallowed.
* Response shape variations (message.items missing / not a list).
* Title extraction from list shape.
* DOI URL construction.
* Author / year / abstract assembly into snippet.
* JATS XML tag stripping in abstract.
"""

from __future__ import annotations

from unittest.mock import patch

from crucible.web_research.providers.crossref import (
    _clean_abstract,
    _entry_to_citation,
    _extract_year,
    _format_authors,
    _USER_AGENT,
    search_crossref,
)


class TestEarlyReturns:
    def test_empty_query(self) -> None:
        assert search_crossref("") == []
        assert search_crossref("   ") == []

    def test_zero_limit(self) -> None:
        assert search_crossref("test", limit=0) == []


class TestHttpFailureSwallowed:
    def test_http_error(self) -> None:
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            side_effect=ConnectionError("net"),
        ):
            assert search_crossref("test") == []

    def test_non_dict_response(self) -> None:
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            return_value="bad",
        ):
            assert search_crossref("test") == []

    def test_missing_message(self) -> None:
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            return_value={"status": "ok"},
        ):
            assert search_crossref("test") == []

    def test_missing_items(self) -> None:
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            return_value={"message": {"total-results": 0}},
        ):
            assert search_crossref("test") == []


class TestResultParsing:
    def test_basic(self) -> None:
        mock = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1234/example",
                        "title": ["Hidden Markov Models for Regime Detection"],
                        "author": [
                            {"family": "Hamilton", "given": "James"}
                        ],
                        "published-print": {"date-parts": [[2024, 1, 1]]},
                        "abstract": "<jats:p>This paper investigates ...</jats:p>",
                    }
                ]
            }
        }
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            return_value=mock,
        ):
            results = search_crossref("HMM regime")
        assert len(results) == 1
        cit = results[0]
        assert cit.provider == "crossref"
        assert "Regime Detection" in cit.title
        assert cit.url == "https://doi.org/10.1234/example"
        assert "Hamilton" in cit.snippet
        assert "2024" in cit.snippet
        # JATS tags stripped.
        assert "<jats:" not in cit.snippet

    def test_missing_doi_skipped(self) -> None:
        mock = {
            "message": {
                "items": [{"DOI": "", "title": ["No DOI"]}]
            }
        }
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            return_value=mock,
        ):
            assert search_crossref("test") == []

    def test_missing_title_skipped(self) -> None:
        mock = {
            "message": {
                "items": [{"DOI": "10.1/x", "title": []}]
            }
        }
        with patch(
            "crucible.web_research.providers.crossref.safe_http_json",
            return_value=mock,
        ):
            assert search_crossref("test") == []


class TestAuthorFormat:
    def test_single_author(self) -> None:
        assert _format_authors([{"family": "Smith"}]) == "Smith"

    def test_multiple_authors_uses_et_al(self) -> None:
        authors = [
            {"family": "Smith"},
            {"family": "Jones"},
        ]
        assert _format_authors(authors) == "Smith et al."

    def test_empty(self) -> None:
        assert _format_authors([]) == ""
        assert _format_authors(None) == ""


class TestYearExtraction:
    def test_published_print(self) -> None:
        entry = {"published-print": {"date-parts": [[2024, 1, 1]]}}
        assert _extract_year(entry) == "2024"

    def test_fallback_to_online(self) -> None:
        entry = {
            "published-online": {"date-parts": [[2023, 5, 1]]},
        }
        assert _extract_year(entry) == "2023"

    def test_fallback_to_issued(self) -> None:
        entry = {"issued": {"date-parts": [[2022]]}}
        assert _extract_year(entry) == "2022"

    def test_missing(self) -> None:
        assert _extract_year({}) == ""

    def test_malformed_date_parts(self) -> None:
        entry = {"published-print": {"date-parts": "not a list"}}
        assert _extract_year(entry) == ""

    def test_string_year_token(self) -> None:
        entry = {"published-print": {"date-parts": [["not-a-year"]]}}
        assert _extract_year(entry) == ""


class TestAbstractCleaning:
    def test_strips_jats_tags(self) -> None:
        raw = "<jats:p>Hello <jats:italic>world</jats:italic>.</jats:p>"
        cleaned = _clean_abstract(raw)
        assert "<jats:" not in cleaned
        assert "Hello" in cleaned
        assert "world" in cleaned

    def test_collapses_whitespace(self) -> None:
        raw = "<p>foo    bar   \n\n baz</p>"
        cleaned = _clean_abstract(raw)
        assert cleaned == "foo bar baz"

    def test_caps_length(self) -> None:
        raw = "A" * 1000
        cleaned = _clean_abstract(raw)
        assert len(cleaned) == 300

    def test_non_string(self) -> None:
        assert _clean_abstract(None) == ""
        assert _clean_abstract(123) == ""
        assert _clean_abstract({"a": "b"}) == ""


class TestUserAgentContainsMailto:
    def test_polite_pool_marker(self) -> None:
        # Crossref polite-pool same requirement as OpenAlex.
        assert "mailto:" in _USER_AGENT
