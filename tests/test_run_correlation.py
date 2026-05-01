"""Tests for crucible/run_correlation.py"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.run_correlation import (
    _RUN_ID,
    get_run_id,
    run_context,
    set_run_id,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_run_id() -> None:
    """Reset the ContextVar to its default so tests are isolated."""
    _RUN_ID.set("")


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestGetRunId:
    def test_default_empty_string(self) -> None:
        _reset_run_id()
        assert get_run_id() == ""

    def test_returns_set_value(self) -> None:
        _reset_run_id()
        set_run_id("explicit-id-123")
        assert get_run_id() == "explicit-id-123"
        _reset_run_id()


class TestSetRunId:
    def test_explicit_run_id(self) -> None:
        _reset_run_id()
        returned = set_run_id("my-run-id")
        assert returned == "my-run-id"
        assert get_run_id() == "my-run-id"
        _reset_run_id()

    def test_auto_uuid_when_none(self) -> None:
        _reset_run_id()
        returned = set_run_id(None)
        assert len(returned) == 36  # UUID4 format: 8-4-4-4-12
        assert returned == get_run_id()
        _reset_run_id()

    def test_auto_uuid_when_no_arg(self) -> None:
        _reset_run_id()
        returned = set_run_id()
        assert len(returned) > 0
        assert get_run_id() == returned
        _reset_run_id()


class TestRunContext:
    def test_sets_run_id_within_block(self) -> None:
        _reset_run_id()
        with run_context("test-run-id") as rid:
            assert rid == "test-run-id"
            assert get_run_id() == "test-run-id"

    def test_restores_empty_on_exit(self) -> None:
        _reset_run_id()
        with run_context("test-run-id"):
            pass
        assert get_run_id() == ""

    def test_restores_previous_run_id_on_exit(self) -> None:
        _reset_run_id()
        set_run_id("outer-run")
        with run_context("inner-run") as rid:
            assert rid == "inner-run"
            assert get_run_id() == "inner-run"
        assert get_run_id() == "outer-run"
        _reset_run_id()

    def test_nested_contexts_restore_correctly(self) -> None:
        _reset_run_id()
        with run_context("level-1") as rid1:
            assert get_run_id() == "level-1"
            with run_context("level-2") as rid2:
                assert get_run_id() == "level-2"
                with run_context("level-3") as rid3:
                    assert get_run_id() == "level-3"
                assert get_run_id() == "level-2"
            assert get_run_id() == "level-1"
        assert get_run_id() == ""

    def test_auto_uuid_when_no_arg(self) -> None:
        _reset_run_id()
        with run_context() as rid:
            assert len(rid) == 36
            assert get_run_id() == rid
        assert get_run_id() == ""

    def test_yields_the_run_id(self) -> None:
        _reset_run_id()
        with run_context("explicit") as rid:
            assert rid == "explicit"

    def test_restores_on_exception(self) -> None:
        _reset_run_id()
        try:
            with run_context("exception-test"):
                raise ValueError("test error")
        except ValueError:
            pass
        assert get_run_id() == ""

    def test_thread_isolation(self) -> None:
        """Each thread gets its own context var value."""
        _reset_run_id()
        results: dict = {}

        def thread_fn(name: str) -> None:
            with run_context(name):
                import time
                time.sleep(0.01)
                results[name] = get_run_id()

        threads = [threading.Thread(target=thread_fn, args=(f"run-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(5):
            assert results[f"run-{i}"] == f"run-{i}"


class TestTelemetryIntegration:
    def test_telemetry_event_carries_run_id(self) -> None:
        from crucible.telemetry import TelemetryEvent

        _reset_run_id()
        with run_context("telem-test-run") as rid:
            event = TelemetryEvent(name="test.event")
            assert event.run_id == rid

        # After context, run_id resets
        event_outside = TelemetryEvent(name="test.outside")
        assert event_outside.run_id == ""
