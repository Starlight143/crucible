"""v1.1.8 extended Phase 4 (Q6) — Cross-provider query dedup registry tests.

Coverage:

* mark_covered then is_covered round-trip.
* Same query different class is independent (not deduped).
* Different query same class is independent.
* mark_covered idempotent on repeat — returns False second time.
* Normalisation: case + whitespace insensitive.
* Empty inputs handled gracefully.
* clear / snapshot / len.
* dedup_enabled env toggle.
"""

from __future__ import annotations

import pytest

from crucible.web_research.dedup import (
    QueryDedupRegistry,
    _normalize,
    dedup_enabled,
)


@pytest.fixture
def registry():
    return QueryDedupRegistry()


class TestNormalize:
    def test_strips_outer_whitespace(self) -> None:
        assert _normalize("  hello  ") == "hello"

    def test_collapses_inner_whitespace(self) -> None:
        assert _normalize("hello   \t  world") == "hello world"

    def test_lowercases(self) -> None:
        assert _normalize("Hello WORLD") == "hello world"

    def test_empty(self) -> None:
        assert _normalize("") == ""
        assert _normalize(None) == ""


class TestMarkAndQuery:
    def test_mark_first_returns_true(self, registry) -> None:
        assert registry.mark_covered("ETH funding", "general", "websearch") is True

    def test_mark_second_returns_false(self, registry) -> None:
        registry.mark_covered("ETH funding", "general", "websearch")
        # Second provider trying same (query, class) — already covered.
        assert registry.mark_covered("ETH funding", "general", "wikipedia") is False

    def test_is_covered_after_mark(self, registry) -> None:
        registry.mark_covered("ETH funding", "general", "websearch")
        assert registry.is_covered("ETH funding", "general") is True

    def test_is_covered_before_mark(self, registry) -> None:
        assert registry.is_covered("ETH funding", "general") is False

    def test_covered_by_returns_first_provider(self, registry) -> None:
        registry.mark_covered("ETH funding", "general", "websearch")
        # Second mark is a no-op.
        registry.mark_covered("ETH funding", "general", "wikipedia")
        # covered_by reports the WINNER (first).
        assert registry.covered_by("ETH funding", "general") == "websearch"

    def test_covered_by_missing_returns_none(self, registry) -> None:
        assert registry.covered_by("anything", "general") is None


class TestNormalisationOnLookup:
    def test_case_insensitive(self, registry) -> None:
        registry.mark_covered("ETH Funding", "general", "websearch")
        assert registry.is_covered("eth funding", "general") is True
        assert registry.is_covered("ETH FUNDING", "general") is True

    def test_whitespace_insensitive(self, registry) -> None:
        registry.mark_covered("ETH funding rate", "general", "websearch")
        assert registry.is_covered("  ETH  funding  rate  ", "general") is True


class TestClassIndependence:
    def test_same_query_different_class_independent(self, registry) -> None:
        registry.mark_covered("ETH funding", "general", "websearch")
        # Same query but different class is NOT covered.
        assert registry.is_covered("ETH funding", "academic") is False
        assert registry.is_covered("ETH funding", "code") is False

    def test_different_query_same_class_independent(self, registry) -> None:
        registry.mark_covered("ETH funding", "general", "websearch")
        assert registry.is_covered("BTC volume", "general") is False


class TestEmptyInputsHandled:
    def test_mark_empty_query_returns_false(self, registry) -> None:
        assert registry.mark_covered("", "general", "websearch") is False
        assert registry.mark_covered(None, "general", "websearch") is False  # type: ignore[arg-type]

    def test_mark_empty_class_returns_false(self, registry) -> None:
        assert registry.mark_covered("ETH", "", "websearch") is False

    def test_mark_empty_provider_returns_false(self, registry) -> None:
        assert registry.mark_covered("ETH", "general", "") is False

    def test_is_covered_empty_returns_false(self, registry) -> None:
        assert registry.is_covered("", "general") is False
        assert registry.is_covered("ETH", "") is False


class TestClearAndLen:
    def test_clear_resets(self, registry) -> None:
        registry.mark_covered("a", "general", "p1")
        registry.mark_covered("b", "code", "p2")
        assert len(registry) == 2
        registry.clear()
        assert len(registry) == 0
        assert registry.is_covered("a", "general") is False

    def test_snapshot_is_copy(self, registry) -> None:
        registry.mark_covered("a", "general", "p1")
        snap = registry.snapshot()
        # Mutating snapshot does not affect registry.
        snap[("a", "general")] = "evil"
        assert registry.covered_by("a", "general") == "p1"


class TestDedupEnabledFlag:
    def test_default_enabled(self, monkeypatch) -> None:
        monkeypatch.delenv(
            "LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED", raising=False,
        )
        assert dedup_enabled() is True

    def test_explicit_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED", "0")
        assert dedup_enabled() is False

    def test_typo_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED", "ture")
        # CLAUDE.md whitelist rule — typos return default (True).
        assert dedup_enabled() is True
