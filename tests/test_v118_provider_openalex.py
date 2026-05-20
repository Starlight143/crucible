"""v1.1.8 extended Phase 3 (Q4) — OpenAlex provider tests.

Coverage:

* Empty / whitespace query returns [] without hitting HTTP.
* limit<=0 returns [].
* HTTP error swallowed → returns [].
* Non-dict response → returns [].
* Result parsing: title, DOI URL preference, abstract reconstruction,
  first-author surname extraction.
* User-Agent contains ``mailto:`` (polite-pool requirement).
"""

from __future__ import annotations

from unittest.mock import patch

from crucible.web_research.providers.openalex import (
    _entry_to_citation,
    _first_author,
    _reconstruct_abstract,
    _USER_AGENT,
    search_openalex,
)


class TestEarlyReturns:
    def test_empty_query(self) -> None:
        assert search_openalex("") == []
        assert search_openalex("   ") == []

    def test_zero_limit(self) -> None:
        assert search_openalex("ETH funding", limit=0) == []

    def test_negative_limit(self) -> None:
        assert search_openalex("ETH funding", limit=-1) == []


class TestHttpFailureSwallowed:
    def test_http_error_returns_empty(self) -> None:
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            side_effect=RuntimeError("network"),
        ):
            assert search_openalex("ETH funding") == []

    def test_non_dict_response_returns_empty(self) -> None:
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value="not a dict",
        ):
            assert search_openalex("ETH funding") == []

    def test_missing_results_key_returns_empty(self) -> None:
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value={"meta": {"count": 0}},
        ):
            assert search_openalex("ETH funding") == []


class TestResultParsing:
    def test_basic_result(self) -> None:
        mock_response = {
            "results": [
                {
                    "id": "https://openalex.org/W12345",
                    "doi": "https://doi.org/10.1000/test",
                    "title": "Funding Rate Mean Reversion in Crypto Perpetual Markets",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Jane Smith"}},
                        {"author": {"display_name": "Bob Jones"}},
                    ],
                },
            ]
        }
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value=mock_response,
        ):
            results = search_openalex("ETH funding")
        assert len(results) == 1
        cit = results[0]
        assert cit.provider == "openalex"
        assert "Mean Reversion" in cit.title
        # DOI is preferred over OpenAlex ID.
        assert cit.url == "https://doi.org/10.1000/test"
        assert cit.evidence_type == "paper"
        assert cit.verification_status == "metadata_only"
        # Snippet should include first-author surname + year.
        assert "Smith" in cit.snippet
        assert "2024" in cit.snippet

    def test_missing_doi_falls_back_to_openalex_id(self) -> None:
        mock_response = {
            "results": [
                {
                    "id": "https://openalex.org/W12345",
                    "doi": "",  # no DOI
                    "title": "Some Paper",
                },
            ]
        }
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value=mock_response,
        ):
            results = search_openalex("test")
        assert len(results) == 1
        assert results[0].url == "https://openalex.org/W12345"

    def test_missing_title_skipped(self) -> None:
        mock_response = {
            "results": [
                {"id": "https://openalex.org/W1", "title": ""},
                {"id": "https://openalex.org/W2", "title": "Valid Title"},
            ]
        }
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value=mock_response,
        ):
            results = search_openalex("test")
        assert len(results) == 1
        assert results[0].title == "Valid Title"

    def test_missing_url_skipped(self) -> None:
        mock_response = {
            "results": [{"id": "", "doi": "", "title": "No URL"}]
        }
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value=mock_response,
        ):
            assert search_openalex("test") == []

    def test_limit_respected(self) -> None:
        mock_response = {
            "results": [
                {"id": f"https://openalex.org/W{i}", "title": f"Paper {i}"}
                for i in range(10)
            ]
        }
        with patch(
            "crucible.web_research.providers.openalex.safe_http_json",
            return_value=mock_response,
        ):
            results = search_openalex("test", limit=3)
        assert len(results) == 3


class TestFirstAuthorExtraction:
    def test_returns_surname(self) -> None:
        authorships = [{"author": {"display_name": "Jane Smith"}}]
        assert _first_author(authorships) == "Smith"

    def test_handles_single_name(self) -> None:
        authorships = [{"author": {"display_name": "Plato"}}]
        assert _first_author(authorships) == "Plato"

    def test_empty_list(self) -> None:
        assert _first_author([]) == ""
        assert _first_author(None) == ""

    def test_malformed(self) -> None:
        assert _first_author([{"not author": "data"}]) == ""


class TestAbstractReconstruction:
    def test_recovers_word_order(self) -> None:
        # OpenAlex inverted index: {"word": [position1, position2, ...]}
        inverted = {
            "hello": [0],
            "world": [1],
            "from": [2],
            "openalex": [3],
        }
        result = _reconstruct_abstract(inverted)
        assert result == "hello world from openalex"

    def test_multiple_positions_for_word(self) -> None:
        inverted = {
            "the": [0, 4],
            "quick": [1],
            "brown": [2],
            "fox": [3, 5],
        }
        result = _reconstruct_abstract(inverted)
        assert result == "the quick brown fox the fox"

    def test_empty_input(self) -> None:
        assert _reconstruct_abstract(None) == ""
        assert _reconstruct_abstract({}) == ""
        assert _reconstruct_abstract("not a dict") == ""

    def test_caps_at_80_words(self) -> None:
        inverted = {f"word{i}": [i] for i in range(120)}
        result = _reconstruct_abstract(inverted)
        assert len(result.split()) == 80


class TestUserAgentContainsMailto:
    def test_polite_pool_marker(self) -> None:
        # OpenAlex prioritises requests that include ``mailto:`` in
        # User-Agent (polite pool).  Don't regress this.
        assert "mailto:" in _USER_AGENT
