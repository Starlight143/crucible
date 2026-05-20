"""v1.1.8 extended Phase 3 (Q8) — Domain pins loader + matcher + prefetch tests.

Coverage:

* JSON loading: missing file, corrupt JSON, valid file.
* Schema validation: drop malformed entries, keep valid ones.
* Matching: mode filter (case-insensitive, ``*`` = all modes),
  any_keyword, all_keywords.
* Prefetch: HTTPS-only enforced (SSRF safety), HTML→text snippet,
  HTTP error skipped without crashing other URLs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from crucible.web_research.domain_pins import (
    _fetch_one,
    _html_to_text,
    domain_pins_enabled,
    load_pins,
    match_pins,
    prefetch_pinned_urls,
)


def _write_pins(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture
def temp_pins(tmp_path, monkeypatch):
    """Create a temp domain_pins.json and point env at it."""
    p = tmp_path / "domain_pins.json"
    monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_PATH", str(p))
    monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_ENABLED", "1")
    return p


class TestEnabledFlag:
    def test_default_enabled(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_DOMAIN_PINS_ENABLED", raising=False)
        assert domain_pins_enabled() is True

    def test_explicit_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_ENABLED", "0")
        assert domain_pins_enabled() is False


class TestLoadPins:
    def test_missing_file_returns_empty(self, temp_pins) -> None:
        # File doesn't exist yet — load_pins returns [].
        assert load_pins() == []

    def test_disabled_returns_empty_even_if_file_exists(
        self, temp_pins, monkeypatch,
    ) -> None:
        _write_pins(
            temp_pins,
            {
                "version": 1,
                "pins": [
                    {
                        "id": "x",
                        "match": {"mode": "quant"},
                        "pre_fetch": [],
                    }
                ],
            },
        )
        monkeypatch.setenv("LIBRARIAN_DOMAIN_PINS_ENABLED", "0")
        assert load_pins() == []

    def test_corrupt_json_returns_empty(self, temp_pins) -> None:
        temp_pins.write_text("{this is not json", encoding="utf-8")
        assert load_pins() == []

    def test_non_dict_root_returns_empty(self, temp_pins) -> None:
        _write_pins(temp_pins, ["not a dict"])  # type: ignore[arg-type]
        assert load_pins() == []

    def test_missing_pins_key_returns_empty(self, temp_pins) -> None:
        _write_pins(temp_pins, {"version": 1})
        assert load_pins() == []

    def test_valid_entries_loaded(self, temp_pins) -> None:
        _write_pins(
            temp_pins,
            {
                "version": 1,
                "pins": [
                    {
                        "id": "test_pin",
                        "match": {"mode": "quant", "any_keyword": ["foo"]},
                        "pre_fetch": [
                            {"url": "https://example.com/", "tier": "Tier-1"}
                        ],
                    },
                ],
            },
        )
        pins = load_pins()
        assert len(pins) == 1
        assert pins[0]["id"] == "test_pin"

    def test_malformed_entries_dropped(self, temp_pins) -> None:
        _write_pins(
            temp_pins,
            {
                "version": 1,
                "pins": [
                    {"id": "good", "match": {"mode": "quant"}, "pre_fetch": []},
                    "not a dict",
                    {"id": "no_match"},  # missing match field
                    {"id": "no_prefetch", "match": {"mode": "quant"}},
                ],
            },
        )
        pins = load_pins()
        assert len(pins) == 1
        assert pins[0]["id"] == "good"


class TestMatchPins:
    @pytest.fixture
    def quant_crypto_pin(self) -> dict:
        return {
            "id": "quant_crypto",
            "match": {
                "mode": "quant",
                "any_keyword": ["binance", "funding rate"],
            },
            "pre_fetch": [
                {"url": "https://binance-docs.github.io/", "tier": "Tier-1"}
            ],
        }

    def test_mode_match_case_insensitive(self, quant_crypto_pin) -> None:
        out = match_pins(
            "ETH funding rate analysis",
            "QUANT",
            pins=[quant_crypto_pin],
        )
        assert len(out) == 1

    def test_mode_mismatch_skipped(self, quant_crypto_pin) -> None:
        out = match_pins(
            "ETH funding rate analysis",
            "saas",
            pins=[quant_crypto_pin],
        )
        assert out == []

    def test_wildcard_mode_matches_all(self) -> None:
        pin = {
            "id": "global",
            "match": {"mode": "*", "any_keyword": ["sharpe"]},
            "pre_fetch": [],
        }
        assert len(match_pins("Sharpe ratio", "quant", pins=[pin])) == 1
        assert len(match_pins("Sharpe ratio", "saas", pins=[pin])) == 1
        assert len(match_pins("Sharpe ratio", "agent", pins=[pin])) == 1

    def test_any_keyword_match(self, quant_crypto_pin) -> None:
        # Any one of the keywords triggers.
        out = match_pins(
            "binance API rate limit",
            "quant",
            pins=[quant_crypto_pin],
        )
        assert len(out) == 1

    def test_any_keyword_none_match_skipped(self, quant_crypto_pin) -> None:
        out = match_pins(
            "stocks portfolio analysis",
            "quant",
            pins=[quant_crypto_pin],
        )
        assert out == []

    def test_all_keywords_all_required(self) -> None:
        pin = {
            "id": "specific",
            "match": {
                "mode": "quant",
                "all_keywords": ["binance", "funding"],
            },
            "pre_fetch": [],
        }
        # Both present → match.
        assert len(match_pins("binance funding rate", "quant", pins=[pin])) == 1
        # Only one → no match.
        assert match_pins("binance trading", "quant", pins=[pin]) == []
        assert match_pins("ETH funding only", "quant", pins=[pin]) == []

    def test_keyword_case_insensitive(self, quant_crypto_pin) -> None:
        out = match_pins(
            "BINANCE exchange",
            "quant",
            pins=[quant_crypto_pin],
        )
        assert len(out) == 1

    def test_empty_user_problem_returns_empty(self) -> None:
        assert match_pins("", "quant") == []
        assert match_pins(None, "quant") == []  # type: ignore[arg-type]

    def test_no_keyword_lists_matches_any_problem_in_mode(self) -> None:
        # A pin with no any_keyword and no all_keywords fires for every
        # problem in the given mode.
        pin = {
            "id": "always_quant",
            "match": {"mode": "quant"},
            "pre_fetch": [],
        }
        assert len(match_pins("anything", "quant", pins=[pin])) == 1
        assert match_pins("anything", "saas", pins=[pin]) == []


class TestPrefetchPinnedUrls:
    def test_no_pins_returns_empty(self) -> None:
        assert prefetch_pinned_urls([]) == []

    def test_basic_fetch(self) -> None:
        pins = [
            {
                "id": "p1",
                "match": {"mode": "*"},
                "pre_fetch": [
                    {
                        "url": "https://docs.example.com/api",
                        "tier": "Tier-1",
                        "label": "Example API docs",
                        "subject_hint": "auth flow",
                    },
                ],
            }
        ]
        with patch(
            "crucible.web_research.domain_pins.safe_http_text",
            return_value="<html><body><h1>API Docs</h1><p>Authenticate via OAuth.</p></body></html>",
        ):
            results = prefetch_pinned_urls(pins)
        assert len(results) == 1
        cit = results[0]
        assert cit.provider == "domain_pin"
        assert cit.url == "https://docs.example.com/api"
        assert "API Docs" in cit.snippet
        assert "auth flow" in cit.snippet  # subject_hint prepended
        assert cit.evidence_type == "docs"
        assert cit.verification_status == "fetched_excerpt"
        assert cit.query == "domain_pin:p1"

    def test_non_https_url_skipped(self) -> None:
        # SSRF safety: only https:// pinned URLs accepted.
        pins = [
            {
                "id": "p1",
                "match": {"mode": "*"},
                "pre_fetch": [
                    {"url": "http://example.com", "tier": "Tier-1"},
                    {"url": "ftp://example.com", "tier": "Tier-1"},
                    {"url": "https://ok.example.com", "tier": "Tier-1"},
                ],
            }
        ]
        with patch(
            "crucible.web_research.domain_pins.safe_http_text",
            return_value="<p>ok</p>",
        ) as mock:
            results = prefetch_pinned_urls(pins)
        # Only the https URL fetched.
        assert mock.call_count == 1
        assert len(results) == 1
        assert results[0].url == "https://ok.example.com"

    def test_fetch_error_skipped_continues_others(self) -> None:
        pins = [
            {
                "id": "p1",
                "match": {"mode": "*"},
                "pre_fetch": [
                    {"url": "https://broken.example.com", "tier": "Tier-1"},
                    {"url": "https://working.example.com", "tier": "Tier-1"},
                ],
            }
        ]

        def fake(url, **kwargs):
            if "broken" in url:
                raise RuntimeError("404")
            return "<p>fine</p>"

        with patch(
            "crucible.web_research.domain_pins.safe_http_text",
            side_effect=fake,
        ):
            results = prefetch_pinned_urls(pins)
        # Broken skipped; working included.
        assert len(results) == 1
        assert "working" in results[0].url

    def test_max_per_pin_cap(self) -> None:
        pins = [
            {
                "id": "p1",
                "match": {"mode": "*"},
                "pre_fetch": [
                    {"url": f"https://x{i}.example.com", "tier": "Tier-1"}
                    for i in range(10)
                ],
            }
        ]
        with patch(
            "crucible.web_research.domain_pins.safe_http_text",
            return_value="<p>x</p>",
        ):
            results = prefetch_pinned_urls(pins, max_per_pin=3)
        assert len(results) == 3


class TestHtmlToText:
    def test_strips_tags(self) -> None:
        assert _html_to_text("<p>hello <b>world</b></p>") == "hello world"

    def test_drops_script_and_style(self) -> None:
        html = "<style>.x{color:red}</style><p>visible</p><script>x=1;</script>"
        cleaned = _html_to_text(html)
        assert "color:red" not in cleaned
        assert "x=1" not in cleaned
        assert "visible" in cleaned

    def test_decodes_entities(self) -> None:
        html = "<p>5 &lt; 10 &amp;&amp; foo &gt; bar &nbsp;baz</p>"
        cleaned = _html_to_text(html)
        assert "5 < 10" in cleaned
        assert "&&" in cleaned
        assert "foo > bar" in cleaned

    def test_collapses_whitespace(self) -> None:
        assert _html_to_text("<p>foo     bar</p>\n\n<p>baz</p>") == "foo bar baz"
