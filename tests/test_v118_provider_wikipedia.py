"""v1.1.8 extended Phase 3 (Q4) — Wikipedia REST provider tests.

Coverage:

* Empty query early-returns [].
* Two-step fetch (opensearch → summary) sequence.
* Mock opensearch returns titles; mock summary returns abstract.
* opensearch HTTP failure → [].
* summary HTTP failure for one title → other titles still tried.
* URL fallback when content_urls missing.
"""

from __future__ import annotations

from unittest.mock import patch

from crucible.web_research.providers.wikipedia import search_wikipedia


class TestEarlyReturns:
    def test_empty_query(self) -> None:
        assert search_wikipedia("") == []
        assert search_wikipedia("  ") == []

    def test_zero_limit(self) -> None:
        assert search_wikipedia("Sharpe ratio", limit=0) == []


class TestOpensearchFailure:
    def test_opensearch_error_returns_empty(self) -> None:
        # When opensearch fails, no titles → no citations.  Don't call
        # summary at all.
        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            side_effect=RuntimeError("opensearch error"),
        ):
            assert search_wikipedia("Sharpe ratio") == []

    def test_opensearch_returns_non_list(self) -> None:
        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            return_value={"error": "bad format"},
        ):
            assert search_wikipedia("Sharpe ratio") == []

    def test_opensearch_returns_short_list(self) -> None:
        # opensearch is supposed to return [query, [titles], [descs], [urls]].
        # Anything shorter than 2 items is malformed.
        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            return_value=["just the query"],
        ):
            assert search_wikipedia("Sharpe ratio") == []


class TestTwoStepFetch:
    def test_basic_flow(self) -> None:
        # First call (opensearch) → titles.  Subsequent calls (summary
        # per title) → abstracts.
        call_count = {"n": 0}

        def fake_safe_http_json(url, **kwargs):
            call_count["n"] += 1
            if "action=opensearch" in url:
                # opensearch response
                return [
                    "Sharpe ratio",
                    ["Sharpe ratio", "Information ratio"],
                    ["Descriptions"],
                    ["Urls"],
                ]
            # summary response
            if "Sharpe_ratio" in url:
                return {
                    "title": "Sharpe ratio",
                    "extract": "The Sharpe ratio measures risk-adjusted return ...",
                    "content_urls": {
                        "desktop": {"page": "https://en.wikipedia.org/wiki/Sharpe_ratio"},
                    },
                }
            if "Information_ratio" in url:
                return {
                    "title": "Information ratio",
                    "extract": "The information ratio (IR) is a measure ...",
                    "content_urls": {
                        "desktop": {"page": "https://en.wikipedia.org/wiki/Information_ratio"},
                    },
                }
            return None

        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            side_effect=fake_safe_http_json,
        ):
            results = search_wikipedia("Sharpe ratio", limit=2)

        # 1 opensearch + 2 summaries = 3 calls.
        assert call_count["n"] == 3
        assert len(results) == 2
        assert results[0].provider == "wikipedia"
        assert results[0].title == "Sharpe ratio"
        assert "risk-adjusted" in results[0].snippet
        assert "Sharpe_ratio" in results[0].url
        assert results[0].verification_status == "fetched_excerpt"

    def test_summary_failure_skips_just_that_title(self) -> None:
        def fake(url, **kwargs):
            if "action=opensearch" in url:
                return [
                    "q",
                    ["Title One", "Title Two"],
                    ["d"],
                    ["u"],
                ]
            if "Title_One" in url:
                raise RuntimeError("summary error")
            # Title Two works.
            return {
                "title": "Title Two",
                "extract": "Two extract",
                "content_urls": {
                    "desktop": {"page": "https://en.wikipedia.org/wiki/Title_Two"},
                },
            }

        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            side_effect=fake,
        ):
            results = search_wikipedia("q", limit=2)
        assert len(results) == 1
        assert results[0].title == "Title Two"


class TestUrlFallback:
    def test_constructed_url_when_content_urls_missing(self) -> None:
        def fake(url, **kwargs):
            if "action=opensearch" in url:
                return ["q", ["My Page"], [""], [""]]
            # Summary missing content_urls entirely.
            return {"title": "My Page", "extract": "ext"}

        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            side_effect=fake,
        ):
            results = search_wikipedia("q", limit=1)
        assert len(results) == 1
        # Fallback URL constructed from title.
        assert "en.wikipedia.org/wiki/" in results[0].url
        assert "My_Page" in results[0].url


class TestLimit:
    def test_only_top_N_summaries_fetched(self) -> None:
        def fake(url, **kwargs):
            if "action=opensearch" in url:
                # Return 10 titles even though limit=3.
                return ["q", [f"Title {i}" for i in range(10)], [], []]
            # Each summary returns trivially.
            return {
                "title": "Title X",
                "extract": "ext",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/X"}},
            }

        with patch(
            "crucible.web_research.providers.wikipedia.safe_http_json",
            side_effect=fake,
        ) as mock:
            results = search_wikipedia("q", limit=3)
        assert len(results) == 3
        # 1 opensearch + 3 summaries = 4 total calls.
        assert mock.call_count == 4
