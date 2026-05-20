"""v1.1.8 extended Phase 3 (Q4) — SearXNG provider tests.

Coverage:

* Instance rotation on failure.
* Result parsing from JSON.
* Skips entries with missing title / url / non-http.
* Empty instance list returns [].
* HTTP error on one instance falls through to next.
"""

from __future__ import annotations

from unittest.mock import patch

from crucible.web_research.providers.searxng import (
    _DEFAULT_INSTANCES,
    _entry_to_citation,
    _parse_results,
    _resolve_instances,
    search_searxng,
)


class TestEarlyReturns:
    def test_empty_query(self) -> None:
        assert search_searxng("") == []
        assert search_searxng("  ") == []

    def test_zero_limit(self) -> None:
        assert search_searxng("test", limit=0) == []


class TestInstanceRotation:
    def test_first_instance_failure_tries_next(self) -> None:
        call_urls = []

        def fake(url, **kwargs):
            call_urls.append(url)
            # First instance fails; second succeeds.
            if "searx.be" in url:
                raise RuntimeError("503")
            return {
                "results": [
                    {
                        "title": "Result A",
                        "url": "https://example.com/a",
                        "content": "snippet a",
                    }
                ]
            }

        with patch(
            "crucible.web_research.providers.searxng.safe_http_json",
            side_effect=fake,
        ):
            with patch(
                "crucible.web_research.providers.searxng._resolve_instances",
                return_value=["https://searx.be", "https://searx.tiekoetter.com"],
            ):
                results = search_searxng("test query", limit=3)
        assert len(results) == 1
        assert results[0].provider == "searxng"
        assert results[0].title == "Result A"
        # We tried 2 instances.
        assert len(call_urls) == 2
        assert "searx.be" in call_urls[0]
        assert "tiekoetter" in call_urls[1]

    def test_all_instances_fail_returns_empty(self) -> None:
        with patch(
            "crucible.web_research.providers.searxng.safe_http_json",
            side_effect=RuntimeError("fail"),
        ):
            with patch(
                "crucible.web_research.providers.searxng._resolve_instances",
                return_value=["https://a", "https://b"],
            ):
                assert search_searxng("test") == []

    def test_empty_instance_list_returns_empty(self) -> None:
        with patch(
            "crucible.web_research.providers.searxng._resolve_instances",
            return_value=[],
        ):
            assert search_searxng("test") == []


class TestParseResults:
    def test_basic(self) -> None:
        data = {
            "results": [
                {"title": "T1", "url": "https://a.com/1", "content": "c1"},
                {"title": "T2", "url": "https://b.com/2", "content": "c2"},
            ]
        }
        out = _parse_results(data, "q", limit=10)
        assert len(out) == 2
        assert out[0].title == "T1"
        assert out[1].title == "T2"
        assert all(c.provider == "searxng" for c in out)

    def test_skips_entries_missing_required_fields(self) -> None:
        data = {
            "results": [
                {"title": "", "url": "https://a", "content": "x"},  # no title
                {"title": "T", "url": "", "content": "x"},  # no URL
                {"title": "T", "url": "ftp://bad", "content": "x"},  # non-http
                {"title": "GoodOne", "url": "https://ok.com", "content": "x"},
            ]
        }
        out = _parse_results(data, "q", limit=10)
        assert len(out) == 1
        assert out[0].title == "GoodOne"

    def test_non_dict_data(self) -> None:
        assert _parse_results("not dict", "q", limit=10) == []
        assert _parse_results(None, "q", limit=10) == []

    def test_missing_results_key(self) -> None:
        assert _parse_results({"other": "value"}, "q", limit=10) == []

    def test_limit_respected(self) -> None:
        data = {
            "results": [
                {"title": f"T{i}", "url": f"https://x.com/{i}", "content": "x"}
                for i in range(10)
            ]
        }
        out = _parse_results(data, "q", limit=3)
        assert len(out) == 3


class TestResolveInstances:
    def test_default_when_no_pins_file(self, monkeypatch, tmp_path) -> None:
        # Point to a non-existent file.
        monkeypatch.setenv(
            "LIBRARIAN_DOMAIN_PINS_PATH",
            str(tmp_path / "nonexistent.json"),
        )
        instances = _resolve_instances()
        # Returns hardcoded fallback when file is missing.
        assert instances == list(_DEFAULT_INSTANCES)

    def test_loads_from_pins_file(self, monkeypatch, tmp_path) -> None:
        import json
        pins_file = tmp_path / "pins.json"
        pins_file.write_text(
            json.dumps({
                "version": 1,
                "pins": [],
                "searxng_instances": [
                    "https://custom1.example.com",
                    "https://custom2.example.com",
                ],
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_PATH", str(pins_file))
        instances = _resolve_instances()
        assert instances == [
            "https://custom1.example.com",
            "https://custom2.example.com",
        ]

    def test_trailing_slashes_stripped(self, monkeypatch, tmp_path) -> None:
        import json
        pins_file = tmp_path / "pins.json"
        pins_file.write_text(
            json.dumps({
                "version": 1,
                "pins": [],
                "searxng_instances": ["https://a.com/", "https://b.com//"],
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_PATH", str(pins_file))
        # rstrip removes one trailing slash per call; double-slash stays as single.
        # Our impl uses rstrip("/") which removes ALL trailing slashes.
        instances = _resolve_instances()
        assert instances == ["https://a.com", "https://b.com"]
