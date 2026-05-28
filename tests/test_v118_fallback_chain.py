"""v1.1.8 extended Phase 3 (Q3) — per-query-class fallback chain tests.

Coverage:

* ``classify_query`` heuristics for each class.
* ``build_chain_for_query`` returns providers in the right order.
* Disabled providers filtered out of chain.
* Unknown providers (typos) filtered out.
* Fallback disabled → only primary returned.
* ``known_query_classes`` returns sorted list.
"""

from __future__ import annotations

import pytest

from crucible.web_research.fallback import (
    build_chain_for_query,
    classify_query,
    fallback_enabled,
    known_query_classes,
)


class TestClassifyQuery:
    def test_hint_takes_precedence(self) -> None:
        # An explicit hint overrides heuristic inspection.
        assert classify_query("some random text", hint="code") == "code"
        assert classify_query("some random text", hint="academic") == "academic"
        assert classify_query("some random text", hint="docs") == "docs"
        assert classify_query("some random text", hint="general") == "general"

    def test_unknown_hint_falls_through(self) -> None:
        # Unknown hint is ignored; heuristic kicks in.
        assert classify_query("github query", hint="bogus_class") == "code"

    def test_code_keywords(self) -> None:
        assert classify_query("repo site:github.com auth0") == "code"
        assert classify_query("site:gitlab.com test") == "code"
        assert classify_query("filetype:py async io") == "code"
        assert classify_query(" github repo example") == "code"

    def test_academic_keywords(self) -> None:
        assert classify_query("filetype:pdf neural networks") == "academic"
        assert classify_query("site:arxiv.org generative") == "academic"
        assert classify_query("arxiv permutation test") == "academic"
        assert classify_query("Smith et al 2023 momentum") == "academic"
        assert classify_query("doi:10.1234/x") == "academic"

    def test_docs_keywords(self) -> None:
        assert classify_query("stripe documentation webhooks") == "docs"
        assert classify_query("kubernetes api reference") == "docs"
        assert classify_query("getting started with grpc") == "docs"
        assert classify_query("react tutorial hooks") == "docs"

    def test_general_default(self) -> None:
        assert classify_query("ETH funding rate analysis") == "general"
        assert classify_query("housing market 2024 trends") == "general"

    def test_empty_returns_general(self) -> None:
        assert classify_query("") == "general"


class TestBuildChainForQuery:
    def test_academic_class_uses_academic_chain(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_PROVIDERS",
            "websearch,arxiv,paperswithcode",
        )
        monkeypatch.setenv(
            "LIBRARIAN_EXTRA_PROVIDERS",
            "openalex,crossref",
        )
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        chain = build_chain_for_query("arxiv permutation", "academic")
        # Default academic chain: arxiv, openalex, crossref, websearch.
        assert chain == ["arxiv", "openalex", "crossref", "websearch"]

    def test_general_class_uses_general_chain(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_PROVIDERS",
            "websearch,context7,arxiv",
        )
        monkeypatch.setenv(
            "LIBRARIAN_EXTRA_PROVIDERS",
            "wikipedia,searxng",
        )
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        chain = build_chain_for_query("ETH funding mechanism", "general")
        # General chain: websearch, searxng, wikipedia.
        assert chain == ["websearch", "searxng", "wikipedia"]

    def test_code_class_chain(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_PROVIDERS",
            "websearch,grep_app,github",
        )
        monkeypatch.setenv("LIBRARIAN_EXTRA_PROVIDERS", "")
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        chain = build_chain_for_query(
            "python asyncio site:github.com",
            "code",
        )
        # v1.1.11 (F-G1): grep_app was removed from the default "code" chain
        # template (Vercel PoW).  Even when env-enabled it no longer appears in
        # the class chain; default code flow is github -> websearch.  grep_app
        # remains in _CORE_PROVIDERS for explicit direct dispatch.
        assert chain == ["github", "websearch"]

    def test_docs_class_chain(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_PROVIDERS",
            "websearch,context7",
        )
        monkeypatch.setenv("LIBRARIAN_EXTRA_PROVIDERS", "wikipedia")
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        chain = build_chain_for_query("stripe webhook docs", "docs")
        assert chain == ["context7", "wikipedia", "websearch"]

    def test_disabled_providers_filtered(self, monkeypatch) -> None:
        # Only enable a subset; chain should drop missing providers.
        monkeypatch.setenv("LIBRARIAN_SEARCH_PROVIDERS", "arxiv")
        monkeypatch.setenv("LIBRARIAN_EXTRA_PROVIDERS", "")
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        chain = build_chain_for_query("test", "academic")
        # Only arxiv stays; openalex/crossref/websearch are not enabled.
        assert chain == ["arxiv"]

    def test_typo_providers_filtered(self, monkeypatch) -> None:
        # ``searxn`` (typo) and ``unknown_provider`` should be dropped
        # by the known-provider filter.
        monkeypatch.setenv("LIBRARIAN_SEARCH_PROVIDERS", "websearch")
        monkeypatch.setenv(
            "LIBRARIAN_EXTRA_PROVIDERS",
            "searxn,unknown_provider,wikipedia",
        )
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        chain = build_chain_for_query("test", "general")
        # searxn / unknown_provider dropped; only valid ones in chain.
        assert "searxn" not in chain
        assert "unknown_provider" not in chain
        assert "wikipedia" in chain
        assert "websearch" in chain

    def test_fallback_disabled_returns_primary_only(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_PROVIDERS",
            "websearch,arxiv",
        )
        monkeypatch.setenv("LIBRARIAN_EXTRA_PROVIDERS", "openalex,wikipedia")
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "0")
        chain = build_chain_for_query("test", "academic")
        # Only primary (arxiv for academic) returned.
        assert chain == ["arxiv"]

    def test_classify_when_no_class_provided(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_PROVIDERS",
            "websearch,arxiv,paperswithcode",
        )
        monkeypatch.setenv("LIBRARIAN_EXTRA_PROVIDERS", "openalex,crossref")
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "1")
        # query has "et al" → classified as academic.
        chain = build_chain_for_query("Smith et al 2023 momentum factor")
        assert chain == ["arxiv", "openalex", "crossref", "websearch"]


class TestKnownClasses:
    def test_returns_sorted(self) -> None:
        assert known_query_classes() == ["academic", "code", "docs", "general"]


class TestFallbackEnabledFlag:
    def test_default_enabled(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", raising=False)
        assert fallback_enabled() is True

    def test_explicit_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_PROVIDER_FALLBACK_ENABLED", "0")
        assert fallback_enabled() is False
