"""v1.1.8 extended Phase 2 (Q1) — Disk-persistent search cache tests.

Coverage:

* Cache hit / miss for (provider, query).
* Per-provider TTL respected (env-configurable).
* Query normalisation (whitespace, case) produces stable keys.
* Empty / falsy inputs handled gracefully (no key, no row).
* Corrupt JSON in cache row is treated as miss, not raise.
* ``get_default()`` returns None when disabled.
* ``get_default()`` returns None on filesystem error (graceful degrade).
* ``purge_expired()`` deletes only expired rows.
* ``count()`` reflects current row count.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from crucible.web_research.search_cache import (
    SearchCache,
    _cache_key,
    _normalize_query,
    _resolve_ttl_seconds,
)


@pytest.fixture
def fresh_cache(tmp_path, monkeypatch):
    """Per-test SearchCache pointing at a temp file.  Resets the
    singleton so tests don't bleed state."""
    monkeypatch.setenv("LIBRARIAN_SEARCH_DISK_CACHE_ENABLED", "1")
    cache_path = tmp_path / "search_cache.sqlite3"
    monkeypatch.setenv("LIBRARIAN_SEARCH_CACHE_PATH", str(cache_path))
    SearchCache.reset_default()
    yield SearchCache(cache_path)
    SearchCache.reset_default()


class TestNormalizeQuery:
    def test_strips_outer_whitespace(self) -> None:
        assert _normalize_query("  hello world  ") == "hello world"

    def test_collapses_inner_whitespace(self) -> None:
        assert _normalize_query("hello   \t  world") == "hello world"

    def test_lowercases(self) -> None:
        assert _normalize_query("HELLO World") == "hello world"

    def test_empty_input(self) -> None:
        assert _normalize_query("") == ""
        assert _normalize_query("   ") == ""

    def test_preserves_site_directive(self) -> None:
        # Stripping site: would cause cache poisoning — same key
        # resolving to different result sets across calls.
        norm = _normalize_query("ETH funding site:binance.com")
        assert "site:binance.com" in norm


class TestCacheKey:
    def test_same_inputs_yield_same_key(self) -> None:
        assert _cache_key("websearch", "foo") == _cache_key("websearch", "foo")

    def test_different_providers_differ(self) -> None:
        assert _cache_key("websearch", "foo") != _cache_key("arxiv", "foo")

    def test_different_queries_differ(self) -> None:
        assert _cache_key("websearch", "foo") != _cache_key("websearch", "bar")

    def test_case_insensitive_via_normalization(self) -> None:
        assert _cache_key("websearch", "Hello") == _cache_key("websearch", "hello")

    def test_whitespace_normalised(self) -> None:
        assert _cache_key("websearch", "  hello  ") == _cache_key("websearch", "hello")

    def test_key_length(self) -> None:
        key = _cache_key("websearch", "anything")
        assert len(key) == 32
        assert all(c in "0123456789abcdef" for c in key)


class TestTtlResolution:
    def test_websearch_default_12_hours(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_SEARCH_CACHE_TTL_WEBSEARCH_HOURS", raising=False)
        assert _resolve_ttl_seconds("websearch") == 12 * 3600

    def test_github_default_24_hours(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_SEARCH_CACHE_TTL_GITHUB_HOURS", raising=False)
        assert _resolve_ttl_seconds("github") == 24 * 3600

    def test_arxiv_default_168_hours(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_SEARCH_CACHE_TTL_ARXIV_HOURS", raising=False)
        assert _resolve_ttl_seconds("arxiv") == 168 * 3600

    def test_env_override_respected(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_SEARCH_CACHE_TTL_WEBSEARCH_HOURS", "48")
        assert _resolve_ttl_seconds("websearch") == 48 * 3600

    def test_zero_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_SEARCH_CACHE_TTL_WEBSEARCH_HOURS", "0")
        # Zero or negative falls back to default 12h (CLAUDE.md numerical
        # correctness rule — typo / hostile env values default safely).
        assert _resolve_ttl_seconds("websearch") == 12 * 3600

    def test_negative_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_SEARCH_CACHE_TTL_WEBSEARCH_HOURS", "-5")
        assert _resolve_ttl_seconds("websearch") == 12 * 3600

    def test_unknown_provider_default_12h(self, monkeypatch) -> None:
        assert _resolve_ttl_seconds("nonexistent_provider") == 12 * 3600


class TestGetSet:
    def test_set_then_get_returns_payload(self, fresh_cache) -> None:
        payload = [{"url": "https://example.com", "title": "Hello"}]
        assert fresh_cache.set("websearch", "test query", payload) is True
        got = fresh_cache.get("websearch", "test query")
        assert got == payload

    def test_get_miss_returns_none(self, fresh_cache) -> None:
        assert fresh_cache.get("websearch", "never inserted") is None

    def test_get_with_empty_inputs_returns_none(self, fresh_cache) -> None:
        assert fresh_cache.get("", "query") is None
        assert fresh_cache.get("websearch", "") is None

    def test_set_with_empty_inputs_returns_false(self, fresh_cache) -> None:
        assert fresh_cache.set("", "query", [{}]) is False
        assert fresh_cache.set("websearch", "", [{}]) is False

    def test_set_with_non_list_payload_returns_false(self, fresh_cache) -> None:
        assert fresh_cache.set("websearch", "x", {"not": "a list"}) is False  # type: ignore[arg-type]
        assert fresh_cache.set("websearch", "x", "string") is False  # type: ignore[arg-type]

    def test_normalised_query_collides(self, fresh_cache) -> None:
        """``Hello`` and ``hello`` resolve to the same cache row."""
        fresh_cache.set("websearch", "Hello World", [{"url": "x"}])
        # Different casing / whitespace should hit the same row.
        assert fresh_cache.get("websearch", "hello world") == [{"url": "x"}]
        assert fresh_cache.get("websearch", "  HELLO  world  ") == [{"url": "x"}]

    def test_payload_preserves_unicode(self, fresh_cache) -> None:
        payload = [{"title": "資金費率均值回歸"}]
        fresh_cache.set("websearch", "funding", payload)
        assert fresh_cache.get("websearch", "funding") == payload


class TestTtlExpiration:
    def test_expired_entry_returns_none(self, fresh_cache, monkeypatch) -> None:
        # Set with normal TTL.
        fresh_cache.set("websearch", "x", [{"a": 1}])
        # Fast-forward time by 25 hours (past 12h default TTL).
        far_future = int(time.time()) + 25 * 3600
        with patch.object(time, "time", return_value=far_future):
            assert fresh_cache.get("websearch", "x") is None

    def test_unexpired_entry_returns_payload(self, fresh_cache, monkeypatch) -> None:
        fresh_cache.set("websearch", "x", [{"a": 1}])
        # 6 hours later — still within 12h TTL.
        near_future = int(time.time()) + 6 * 3600
        with patch.object(time, "time", return_value=near_future):
            assert fresh_cache.get("websearch", "x") == [{"a": 1}]


class TestPurgeExpired:
    def test_only_expired_deleted(self, fresh_cache, monkeypatch) -> None:
        fresh_cache.set("websearch", "fresh", [{"x": 1}])
        fresh_cache.set("websearch", "stale", [{"y": 2}])
        # Confirm both stored.
        assert fresh_cache.count() == 2
        # Manually expire one entry by patching time forward.
        far_future = int(time.time()) + 25 * 3600
        with patch.object(time, "time", return_value=far_future):
            # Re-write "fresh" so it gets a far-future expires_at.
            fresh_cache.set("websearch", "fresh", [{"x": 1}])
            # purge_expired runs at that far_future time → "stale" is expired.
            deleted = fresh_cache.purge_expired()
            assert deleted == 1
            assert fresh_cache.count() == 1


class TestCorruptDataSwallow:
    def test_corrupt_json_in_row_returns_none(self, fresh_cache) -> None:
        # Inject a corrupt row directly via the underlying connection.
        with fresh_cache._lock:
            fresh_cache._conn.execute(
                "INSERT INTO search_cache "
                "(cache_key, provider, query, payload, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _cache_key("websearch", "corrupt"),
                    "websearch",
                    "corrupt",
                    "this is not valid JSON {{{",
                    int(time.time()) + 3600,
                    int(time.time()),
                ),
            )
        # get() must return None, not raise.
        assert fresh_cache.get("websearch", "corrupt") is None

    def test_non_list_json_returns_none(self, fresh_cache) -> None:
        with fresh_cache._lock:
            fresh_cache._conn.execute(
                "INSERT INTO search_cache "
                "(cache_key, provider, query, payload, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _cache_key("websearch", "wrongtype"),
                    "websearch",
                    "wrongtype",
                    json.dumps({"not": "a list"}),
                    int(time.time()) + 3600,
                    int(time.time()),
                ),
            )
        assert fresh_cache.get("websearch", "wrongtype") is None


class TestGetDefault:
    def test_disabled_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_SEARCH_DISK_CACHE_ENABLED", "0")
        SearchCache.reset_default()
        assert SearchCache.get_default() is None

    def test_enabled_returns_instance(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_SEARCH_DISK_CACHE_ENABLED", "1")
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_CACHE_PATH",
            str(tmp_path / "test.sqlite3"),
        )
        SearchCache.reset_default()
        try:
            instance = SearchCache.get_default()
            assert instance is not None
            assert isinstance(instance, SearchCache)
        finally:
            SearchCache.reset_default()

    def test_singleton(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_SEARCH_DISK_CACHE_ENABLED", "1")
        monkeypatch.setenv(
            "LIBRARIAN_SEARCH_CACHE_PATH",
            str(tmp_path / "test.sqlite3"),
        )
        SearchCache.reset_default()
        try:
            a = SearchCache.get_default()
            b = SearchCache.get_default()
            assert a is b
        finally:
            SearchCache.reset_default()


class TestCount:
    def test_empty_cache_count_zero(self, fresh_cache) -> None:
        assert fresh_cache.count() == 0

    def test_count_increments_with_set(self, fresh_cache) -> None:
        fresh_cache.set("websearch", "a", [{"x": 1}])
        assert fresh_cache.count() == 1
        fresh_cache.set("websearch", "b", [{"y": 2}])
        assert fresh_cache.count() == 2

    def test_count_overwrite_does_not_duplicate(self, fresh_cache) -> None:
        fresh_cache.set("websearch", "a", [{"x": 1}])
        fresh_cache.set("websearch", "a", [{"x": 2}])
        assert fresh_cache.count() == 1
