"""v1.1.13 — Tavily web-search provider (clean reimplementation) tests.

Context
-------
Two automated bot PRs (#5, #6) proposed adding Tavily via the ``tavily-python``
SDK.  That approach (a) bypassed the package's SSRF-checked ``safe_http_json``
helper, (b) accepted ``timeout_seconds`` but never forwarded it, (c) added a
third-party dependency, and (d) shipped no tests / no Settings-UI sync.  This
module pins the clean reimplementation that replaced both PRs.

Coverage
--------
* Behaviour contract: empty query / zero limit / missing key → ``[]``; never
  raises for routine failures; result parsing + field filtering.
* API-key hygiene: placeholder sentinels filtered exactly like
  ``_resolve_context7_token``.
* **Clean-version pins** (the differentiators from the bot PRs):
  - routes through ``safe_http_json`` (POST), NOT the ``tavily-python`` SDK;
  - ``timeout_seconds`` is forwarded;
  - no ``tavily-python`` dependency added to requirements.txt.
* Wiring (CLAUDE.md § 9.6 producer→consumer): registry, fallback chain,
  ``_EXTRA_PROVIDERS``, opt-in filtering, section_04 dispatch branch + import.
* Settings 3-layer sync: ``.env.example`` (uncommented + filterable
  placeholder), ``SETTINGS_SCHEMA`` group membership, bilingual ``KEY_META``.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from crucible.web_research.providers.tavily import (
    _TAVILY_SEARCH_URL,
    _entry_to_citation,
    _parse_results,
    _resolve_tavily_api_key,
    search_tavily,
)


_REAL_KEY = "tvly-realtestkey0123456789"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_env_example() -> str:
    return (_repo_root() / ".env.example").read_text(encoding="utf-8")


def _read_app_js() -> str:
    return (_repo_root() / "webui" / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )


def _read_requirements() -> str:
    return (_repo_root() / "requirements.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Behaviour contract
# ---------------------------------------------------------------------------
class TestEarlyReturns:
    def test_empty_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        assert search_tavily("") == []
        assert search_tavily("   ") == []

    def test_zero_or_negative_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        assert search_tavily("test", limit=0) == []
        assert search_tavily("test", limit=-1) == []

    def test_missing_key_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        # Must NOT reach the network — patch to prove it is never called.
        with patch(
            "crucible.web_research.providers.tavily.safe_http_json"
        ) as mock_http:
            assert search_tavily("test query") == []
            mock_http.assert_not_called()

    def test_placeholder_key_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", "replace_with_tavily_api_key")
        with patch(
            "crucible.web_research.providers.tavily.safe_http_json"
        ) as mock_http:
            assert search_tavily("test query") == []
            mock_http.assert_not_called()


# ---------------------------------------------------------------------------
# API-key resolution hygiene (mirrors _resolve_context7_token)
# ---------------------------------------------------------------------------
class TestApiKeyResolution:
    def test_unset_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        assert _resolve_tavily_api_key() == ""

    def test_all_placeholder_prefixes_filtered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for placeholder in (
            "replace_with_tavily_api_key",
            "your_tavily_key",
            "xxxxxxxx",
            "placeholder",
            "changeme",
            "REPLACE_WITH_KEY",  # case-insensitive
        ):
            monkeypatch.setenv("TAVILY_API_KEY", placeholder)
            assert _resolve_tavily_api_key() == "", (
                f"placeholder {placeholder!r} should be filtered out"
            )

    def test_real_value_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        assert _resolve_tavily_api_key() == _REAL_KEY

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", f"  {_REAL_KEY}  \n")
        assert _resolve_tavily_api_key() == _REAL_KEY


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------
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
        assert all(c.provider == "tavily" for c in out)

    def test_skips_entries_missing_required_fields(self) -> None:
        data = {
            "results": [
                {"title": "", "url": "https://a", "content": "x"},  # no title
                {"title": "T", "url": "", "content": "x"},  # no URL
                {"title": "T", "url": "ftp://bad", "content": "x"},  # non-http
                {"title": "T", "url": "javascript:alert(1)", "content": "x"},  # scheme
                {"title": "GoodOne", "url": "https://ok.com", "content": "x"},
            ]
        }
        out = _parse_results(data, "q", limit=10)
        assert len(out) == 1
        assert out[0].title == "GoodOne"

    def test_non_dict_data(self) -> None:
        assert _parse_results("not dict", "q", limit=10) == []
        assert _parse_results(None, "q", limit=10) == []
        assert _parse_results(["list"], "q", limit=10) == []

    def test_missing_results_key(self) -> None:
        assert _parse_results({"answer": "value"}, "q", limit=10) == []

    def test_results_not_a_list(self) -> None:
        assert _parse_results({"results": "nope"}, "q", limit=10) == []

    def test_limit_respected(self) -> None:
        data = {
            "results": [
                {"title": f"T{i}", "url": f"https://x.com/{i}", "content": "x"}
                for i in range(10)
            ]
        }
        out = _parse_results(data, "q", limit=3)
        assert len(out) == 3

    def test_title_and_snippet_truncated(self) -> None:
        entry = {
            "title": "T" * 500,
            "url": "https://ok.com",
            "content": "C" * 1000,
        }
        cit = _entry_to_citation(entry, "q")
        assert cit is not None
        assert len(cit.title) == 200
        assert len(cit.snippet) == 400

    def test_non_dict_entry_returns_none(self) -> None:
        assert _entry_to_citation("not a dict", "q") is None
        assert _entry_to_citation(None, "q") is None


# ---------------------------------------------------------------------------
# Search end-to-end (safe_http_json mocked)
# ---------------------------------------------------------------------------
class TestSearchTavily:
    def test_success_returns_citations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)

        def fake(url, **kwargs):
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
            "crucible.web_research.providers.tavily.safe_http_json",
            side_effect=fake,
        ):
            results = search_tavily("test query", limit=3)
        assert len(results) == 1
        assert results[0].provider == "tavily"
        assert results[0].title == "Result A"
        assert results[0].url == "https://example.com/a"

    def test_http_failure_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        with patch(
            "crucible.web_research.providers.tavily.safe_http_json",
            side_effect=RuntimeError("502 Bad Gateway"),
        ):
            assert search_tavily("test query") == []

    def test_ssrf_refusal_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """safe_http_json raises ValueError on SSRF refusal / byte-budget;
        the provider must swallow it (never raise)."""
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        with patch(
            "crucible.web_research.providers.tavily.safe_http_json",
            side_effect=ValueError("Refusing non-public HTTP(S) URL"),
        ):
            assert search_tavily("test query") == []


# ---------------------------------------------------------------------------
# Clean-version pins — the differentiators from the bot PRs
# ---------------------------------------------------------------------------
class TestCleanReimplementationContract:
    def test_request_shape_is_post_with_expected_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        captured: dict = {}

        def fake(url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return {"results": []}

        with patch(
            "crucible.web_research.providers.tavily.safe_http_json",
            side_effect=fake,
        ):
            search_tavily("hello world", limit=4, timeout_seconds=7.5)

        assert captured["url"] == _TAVILY_SEARCH_URL == "https://api.tavily.com/search"
        assert captured["method"] == "POST"
        # timeout is FORWARDED (the bot PR dropped it).
        assert captured["timeout_seconds"] == 7.5
        assert captured["max_bytes"] == 1024 * 1024
        assert captured["circuit_breaker_name"] == "librarian_tavily"
        payload = captured["payload"]
        assert payload["api_key"] == _REAL_KEY
        assert payload["query"] == "hello world"
        assert payload["max_results"] == 4
        assert payload["search_depth"] == "basic"
        assert payload["topic"] == "general"

    def test_timeout_forwarded_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TAVILY_API_KEY", _REAL_KEY)
        captured: dict = {}

        def fake(url, **kwargs):
            captured.update(kwargs)
            return {"results": []}

        with patch(
            "crucible.web_research.providers.tavily.safe_http_json",
            side_effect=fake,
        ):
            search_tavily("q", timeout_seconds=22.0)
        assert captured["timeout_seconds"] == 22.0

    def test_routes_through_safe_http_json_not_sdk(self) -> None:
        """Source must use safe_http_json and must NOT import the
        tavily-python SDK (the bot PR's approach)."""
        src = inspect.getsource(
            __import__(
                "crucible.web_research.providers.tavily",
                fromlist=["_"],
            )
        )
        assert "safe_http_json" in src
        assert "from tavily import" not in src
        assert "TavilyClient" not in src

    def test_no_tavily_python_dependency_added(self) -> None:
        """The clean reimplementation adds ZERO new dependencies — the
        request goes through the existing safe_http_json helper."""
        reqs = _read_requirements().lower()
        assert "tavily-python" not in reqs
        assert "tavily_python" not in reqs


# ---------------------------------------------------------------------------
# Wiring (CLAUDE.md § 9.6 producer→consumer)
# ---------------------------------------------------------------------------
class TestRegistryWiring:
    def test_registered_in_providers_dict(self) -> None:
        from crucible.web_research.providers import (
            PROVIDERS,
            get_provider,
            known_provider_names,
            search_tavily as exported,
        )

        assert PROVIDERS.get("tavily") is exported
        assert get_provider("TAVILY") is exported  # case-insensitive
        assert "tavily" in known_provider_names()

    def test_in_extra_providers_frozenset(self) -> None:
        from crucible.web_research.fallback import _EXTRA_PROVIDERS

        assert "tavily" in _EXTRA_PROVIDERS

    def test_in_general_default_chain(self) -> None:
        from crucible.web_research.fallback import _DEFAULT_CHAIN_BY_CLASS

        chain = _DEFAULT_CHAIN_BY_CLASS["general"]
        assert "tavily" in chain
        # Positioned between websearch and searxng.
        assert chain.index("websearch") < chain.index("tavily") < chain.index("searxng")

    def test_chain_includes_tavily_when_enabled(self) -> None:
        from crucible.web_research.fallback import build_chain_for_query

        chain = build_chain_for_query(
            "broad web query",
            "general",
            enabled_providers=["websearch", "tavily", "searxng", "wikipedia"],
        )
        assert "tavily" in chain

    def test_chain_excludes_tavily_when_not_enabled(self) -> None:
        """Opt-in proof: default extras (no tavily) → tavily dropped from the
        chain even though it is in the default ordering."""
        from crucible.web_research.fallback import build_chain_for_query

        chain = build_chain_for_query(
            "broad web query",
            "general",
            enabled_providers=["websearch", "searxng", "wikipedia"],
        )
        assert "tavily" not in chain

    def test_chain_excludes_tavily_under_default_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.web_research.fallback import build_chain_for_query

        # Default extras do not include tavily.
        monkeypatch.delenv("LIBRARIAN_EXTRA_PROVIDERS", raising=False)
        monkeypatch.delenv("LIBRARIAN_SEARCH_PROVIDERS", raising=False)
        chain = build_chain_for_query("broad web query", "general")
        assert "tavily" not in chain
        assert "websearch" in chain  # the rest of the chain is intact

    def test_chain_includes_tavily_via_env_optin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from crucible.web_research.fallback import build_chain_for_query

        monkeypatch.setenv(
            "LIBRARIAN_EXTRA_PROVIDERS", "openalex,crossref,wikipedia,tavily"
        )
        monkeypatch.delenv("LIBRARIAN_SEARCH_PROVIDERS", raising=False)
        chain = build_chain_for_query("broad web query", "general")
        assert "tavily" in chain


class TestSection04DispatchWiring:
    def test_dispatch_branch_present(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as sec04

        src = inspect.getsource(sec04)
        assert 'elif provider_name == "tavily":' in src
        assert "_v118_search_tavily(" in src

    def test_import_alias_in_both_tri_modal_blocks(self) -> None:
        from crucible.modules import section_04_web_research_and_direction as sec04

        src = inspect.getsource(sec04)
        # One in the package block, one in the flat-launcher block.
        assert src.count("search_tavily as _v118_search_tavily") == 2


# ---------------------------------------------------------------------------
# Settings 3-layer sync (.env.example + SETTINGS_SCHEMA + KEY_META)
# ---------------------------------------------------------------------------
class TestEnvExampleSync:
    def test_tavily_api_key_uncommented(self) -> None:
        content = _read_env_example()
        assert re.search(
            r"^TAVILY_API_KEY\s*=", content, flags=re.MULTILINE
        ), "TAVILY_API_KEY must be uncommented so the Settings UI surfaces it"

    def test_tavily_placeholder_is_filterable(self) -> None:
        content = _read_env_example()
        match = re.search(
            r"^TAVILY_API_KEY\s*=\s*(\S+)", content, flags=re.MULTILINE
        )
        assert match, "TAVILY_API_KEY assignment missing"
        value = match.group(1).strip().lower()
        assert value.startswith(
            ("replace_", "your_", "xxxx", "placeholder", "changeme")
        ), (
            f"TAVILY_API_KEY placeholder must be filterable by "
            f"_resolve_tavily_api_key, got: {match.group(1)!r}"
        )


class TestSettingsUiSync:
    def test_librarian_auth_group_lists_tavily(self) -> None:
        js = _read_app_js()
        match = re.search(
            r"id:'librarian_auth'[^}]*?keys:\[([^\]]+)\]",
            js,
            flags=re.DOTALL,
        )
        assert match, "could not locate librarian_auth group's keys array"
        keys_text = match.group(1)
        assert "'TAVILY_API_KEY'" in keys_text
        # Existing keys must remain (no accidental removal).
        assert "'CONTEXT7_API_KEY'" in keys_text
        assert "'GITHUB_TOKEN'" in keys_text

    def test_key_meta_entry_is_bilingual_password(self) -> None:
        js = _read_app_js()
        m = re.search(
            r"TAVILY_API_KEY\s*:\s*\{(.*?)\},\s*$",
            js,
            flags=re.DOTALL | re.MULTILINE,
        )
        assert m, "TAVILY_API_KEY KEY_META entry missing"
        entry = m.group(1)
        desc = re.search(r"desc:\s*(\{.*?\})", entry, flags=re.DOTALL)
        assert desc, "TAVILY_API_KEY must have a desc field"
        assert "en:" in desc.group(1) and "zh:" in desc.group(1), (
            "TAVILY_API_KEY desc must be bilingual {en, zh}"
        )
        assert "type:'password'" in entry.replace(" ", ""), (
            "TAVILY_API_KEY must be type:'password' so the value is masked"
        )
