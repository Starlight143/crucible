"""v1.1.8 extended Phase 2 (Q7) — Per-provider health tracker tests.

Coverage:

* Each record_* method increments the right counter.
* Multiple calls accumulate.
* Reset clears all counters.
* Snapshot returns a copy (safe for observability).
* Empty provider name short-circuits all increments.
* ``format_summary_lines`` renders one line per provider sorted.
* ``health_summary_enabled`` honours the env toggle.
"""

from __future__ import annotations

import pytest

from crucible.web_research.health import (
    HealthTracker,
    health_summary_enabled,
)


@pytest.fixture
def tracker():
    HealthTracker.reset_default()
    yield HealthTracker()
    HealthTracker.reset_default()


class TestSingleton:
    def test_get_default_returns_same_instance(self) -> None:
        HealthTracker.reset_default()
        try:
            a = HealthTracker.get_default()
            b = HealthTracker.get_default()
            assert a is b
        finally:
            HealthTracker.reset_default()

    def test_reset_yields_new_instance(self) -> None:
        HealthTracker.reset_default()
        a = HealthTracker.get_default()
        HealthTracker.reset_default()
        b = HealthTracker.get_default()
        assert a is not b
        HealthTracker.reset_default()


class TestCounterIncrement:
    def test_request_count(self, tracker) -> None:
        tracker.record_request("websearch")
        tracker.record_request("websearch")
        assert tracker.snapshot()["websearch"]["requests"] == 2

    def test_ok_count(self, tracker) -> None:
        tracker.record_ok("websearch")
        assert tracker.snapshot()["websearch"]["ok_200"] == 1

    def test_rate_limit_count(self, tracker) -> None:
        tracker.record_rate_limit("websearch")
        tracker.record_rate_limit("websearch")
        tracker.record_rate_limit("websearch")
        assert tracker.snapshot()["websearch"]["rate_limited_429"] == 3

    def test_bot_detection_count(self, tracker) -> None:
        tracker.record_bot_detection("websearch")
        assert tracker.snapshot()["websearch"]["bot_detected_202"] == 1

    def test_timeout_count(self, tracker) -> None:
        tracker.record_timeout("arxiv")
        assert tracker.snapshot()["arxiv"]["timeouts"] == 1

    def test_other_error_count(self, tracker) -> None:
        tracker.record_other_error("github")
        assert tracker.snapshot()["github"]["other_errors"] == 1

    def test_citations_count(self, tracker) -> None:
        tracker.record_citations("websearch", 5)
        tracker.record_citations("websearch", 3)
        assert tracker.snapshot()["websearch"]["citations_yielded"] == 8

    def test_citations_zero_or_negative_skipped(self, tracker) -> None:
        tracker.record_citations("websearch", 0)
        tracker.record_citations("websearch", -5)
        assert "websearch" not in tracker.snapshot()

    def test_cache_hit_count(self, tracker) -> None:
        tracker.record_cache_hit("websearch")
        assert tracker.snapshot()["websearch"]["cache_hits"] == 1


class TestEmptyProviderShortCircuits:
    def test_record_request_empty_provider(self, tracker) -> None:
        tracker.record_request("")
        tracker.record_request(None)  # type: ignore[arg-type]
        assert tracker.snapshot() == {}

    def test_all_methods_handle_empty_provider(self, tracker) -> None:
        for method in (
            "record_request",
            "record_ok",
            "record_rate_limit",
            "record_bot_detection",
            "record_timeout",
            "record_other_error",
            "record_cache_hit",
        ):
            getattr(tracker, method)("")
        tracker.record_citations("", 5)
        assert tracker.snapshot() == {}


class TestReset:
    def test_reset_clears_state(self, tracker) -> None:
        tracker.record_request("websearch")
        tracker.record_ok("websearch")
        assert tracker.snapshot() != {}
        tracker.reset()
        assert tracker.snapshot() == {}


class TestSnapshotIsCopy:
    def test_snapshot_mutation_doesnt_leak(self, tracker) -> None:
        tracker.record_request("websearch")
        snap = tracker.snapshot()
        snap["websearch"]["requests"] = 9999
        snap2 = tracker.snapshot()
        assert snap2["websearch"]["requests"] == 1


class TestFormatSummaryLines:
    def test_empty_tracker_placeholder(self, tracker) -> None:
        lines = tracker.format_summary_lines()
        assert len(lines) == 1
        assert "no provider activity" in lines[0]

    def test_sorted_alphabetical(self, tracker) -> None:
        tracker.record_request("websearch")
        tracker.record_request("arxiv")
        tracker.record_request("github")
        lines = tracker.format_summary_lines()
        # Lines are sorted by provider name.
        order = []
        for line in lines:
            for p in ("arxiv", "github", "websearch"):
                if f"[librarian] {p}:" in line:
                    order.append(p)
                    break
        assert order == ["arxiv", "github", "websearch"]

    def test_summary_includes_all_counters(self, tracker) -> None:
        tracker.record_request("websearch")
        tracker.record_ok("websearch")
        tracker.record_rate_limit("websearch")
        tracker.record_bot_detection("websearch")
        tracker.record_timeout("websearch")
        tracker.record_cache_hit("websearch")
        tracker.record_citations("websearch", 7)
        lines = tracker.format_summary_lines()
        line = lines[0]
        assert "1 req" in line
        assert "1 ok" in line
        assert "1 429" in line
        assert "1 202(bot)" in line
        assert "1 timeout" in line
        assert "1 cache_hit" in line
        assert "Citations: 7" in line


class TestHealthSummaryEnabled:
    def test_default_enabled(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_PROVIDER_HEALTH_SUMMARY", raising=False)
        assert health_summary_enabled() is True

    def test_explicit_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_PROVIDER_HEALTH_SUMMARY", "0")
        assert health_summary_enabled() is False

    def test_explicit_enabled(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_PROVIDER_HEALTH_SUMMARY", "1")
        assert health_summary_enabled() is True

    def test_typo_falls_back_to_default(self, monkeypatch) -> None:
        # CLAUDE.md whitelist rule: typos return default.
        monkeypatch.setenv("LIBRARIAN_PROVIDER_HEALTH_SUMMARY", "ture")
        assert health_summary_enabled() is True
