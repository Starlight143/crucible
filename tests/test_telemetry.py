"""Tests for crucible.telemetry"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.telemetry import (
    JsonlFileSink,
    TelemetryEvent,
    TelemetryQueue,
    add_sink,
    clear_sinks,
    emit,
    flush,
    get_dropped_count,
    reset_for_testing,
    shutdown,
)


@pytest.fixture(autouse=True)
def _reset_global():
    reset_for_testing()
    yield
    reset_for_testing()


# ── TelemetryEvent ────────────────────────────────────────────────────────────

class TestTelemetryEvent:
    def test_defaults(self):
        e = TelemetryEvent(name="test.event")
        assert e.name == "test.event"
        assert e.payload == {}
        assert e.source == ""
        assert e.timestamp != ""

    def test_to_dict(self):
        e = TelemetryEvent(
            name="stage.complete",
            payload={"elapsed": 1.5},
            source="analysis",
            timestamp="2025-01-01T00:00:00+00:00",
        )
        d = e.to_dict()
        assert d["name"] == "stage.complete"
        assert d["payload"] == {"elapsed": 1.5}
        assert d["source"] == "analysis"
        assert d["timestamp"] == "2025-01-01T00:00:00+00:00"


# ── TelemetryQueue ────────────────────────────────────────────────────────────

class TestTelemetryQueue:
    def test_emit_calls_sink(self):
        q = TelemetryQueue(maxsize=10)
        received: list = []

        def sink(event: TelemetryEvent) -> None:
            received.append(event.name)

        q.add_sink(sink)
        q.emit(TelemetryEvent(name="test"))
        q.flush(timeout=2.0)
        q.shutdown(timeout=2.0)

        assert "test" in received

    def test_multiple_sinks_all_called(self):
        q = TelemetryQueue(maxsize=10)
        log1: list = []
        log2: list = []
        q.add_sink(lambda e: log1.append(e.name))
        q.add_sink(lambda e: log2.append(e.name))
        q.emit(TelemetryEvent(name="ev"))
        q.flush(timeout=2.0)
        q.shutdown(timeout=2.0)

        assert log1 == ["ev"]
        assert log2 == ["ev"]

    def test_broken_sink_doesnt_block_others(self):
        q = TelemetryQueue(maxsize=10)
        log: list = []

        def bad_sink(e: TelemetryEvent) -> None:
            raise RuntimeError("broken")

        def good_sink(e: TelemetryEvent) -> None:
            log.append(e.name)

        q.add_sink(bad_sink)
        q.add_sink(good_sink)
        q.emit(TelemetryEvent(name="x"))
        q.flush(timeout=2.0)
        q.shutdown(timeout=2.0)

        assert "x" in log

    def test_full_queue_drops_without_blocking(self):
        q = TelemetryQueue(maxsize=2)
        # Block the worker with a slow sink
        barrier = threading.Event()

        def slow_sink(e: TelemetryEvent) -> None:
            barrier.wait(timeout=1.0)

        q.add_sink(slow_sink)
        # Fill queue + overflow
        for _ in range(10):
            q.emit(TelemetryEvent(name="ev"))
        dropped = q.dropped
        barrier.set()
        q.shutdown(timeout=2.0)
        assert dropped > 0, f"Expected at least one dropped event from overflow; got {dropped}"

    def test_remove_sink(self):
        q = TelemetryQueue(maxsize=10)
        log: list = []

        def sink(e: TelemetryEvent) -> None:
            log.append(1)

        q.add_sink(sink)
        q.remove_sink(sink)
        q.emit(TelemetryEvent(name="after_remove"))
        q.flush(timeout=1.0)
        q.shutdown(timeout=1.0)
        assert log == []

    def test_clear_sinks(self):
        q = TelemetryQueue(maxsize=10)
        log: list = []
        q.add_sink(lambda e: log.append(1))
        q.clear_sinks()
        q.emit(TelemetryEvent(name="after_clear"))
        q.flush(timeout=1.0)
        q.shutdown(timeout=1.0)
        assert log == []

    def test_flush_waits_for_queue(self):
        q = TelemetryQueue(maxsize=100)
        processed: list = []

        def slow_sink(e: TelemetryEvent) -> None:
            time.sleep(0.02)
            processed.append(e.name)

        q.add_sink(slow_sink)
        q.emit(TelemetryEvent(name="a"))
        q.emit(TelemetryEvent(name="b"))
        q.flush(timeout=3.0)
        q.shutdown(timeout=2.0)
        assert len(processed) == 2

    def test_same_sink_registered_once(self):
        q = TelemetryQueue(maxsize=10)
        log: list = []

        def sink(e: TelemetryEvent) -> None:
            log.append(1)

        q.add_sink(sink)
        q.add_sink(sink)  # duplicate — should be ignored
        q.emit(TelemetryEvent(name="x"))
        q.flush(timeout=2.0)
        q.shutdown(timeout=2.0)
        assert log == [1]  # only one call, not two


# ── JsonlFileSink ─────────────────────────────────────────────────────────────

class TestJsonlFileSink:
    def test_writes_events_to_file(self, tmp_path):
        log_path = str(tmp_path / "telemetry.jsonl")
        sink = JsonlFileSink(log_path)
        e = TelemetryEvent(
            name="test.event",
            payload={"key": "val"},
            timestamp="2025-01-01T00:00:00+00:00",
        )
        sink(e)

        assert os.path.isfile(log_path)
        with open(log_path, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["name"] == "test.event"
        assert obj["payload"] == {"key": "val"}

    def test_appends_multiple_events(self, tmp_path):
        log_path = str(tmp_path / "multi.jsonl")
        sink = JsonlFileSink(log_path)
        for i in range(3):
            sink(TelemetryEvent(name=f"ev{i}"))

        with open(log_path, encoding="utf-8") as fh:
            lines = [l.strip() for l in fh if l.strip()]
        assert len(lines) == 3

    def test_creates_parent_dirs(self, tmp_path):
        log_path = str(tmp_path / "deep" / "nested" / "telemetry.jsonl")
        sink = JsonlFileSink(log_path)
        sink(TelemetryEvent(name="x"))
        assert os.path.isfile(log_path)


# ── Global public API ─────────────────────────────────────────────────────────

class TestGlobalAPI:
    def test_emit_and_flush(self):
        received: list = []
        add_sink(lambda e: received.append(e.name))
        emit("global.test", payload={"x": 1})
        flush(timeout=2.0)
        assert "global.test" in received

    def test_emit_with_source(self):
        received: list = []
        add_sink(lambda e: received.append(e.source))
        emit("ev", source="my_module")
        flush(timeout=2.0)
        assert "my_module" in received

    def test_clear_sinks_from_global(self):
        log: list = []
        add_sink(lambda e: log.append(1))
        clear_sinks()
        emit("ev")
        flush(timeout=1.0)
        assert log == []

    def test_reset_for_testing(self):
        log: list = []
        add_sink(lambda e: log.append(1))
        reset_for_testing()
        emit("after_reset")
        flush(timeout=1.0)
        assert log == []  # sink was cleared with old queue

    def test_concurrent_emit(self):
        log: list = []
        lock = threading.Lock()
        add_sink(lambda e: (lock.acquire(), log.append(1), lock.release()))
        threads = [threading.Thread(target=lambda: emit("t")) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        flush(timeout=3.0)
        assert len(log) == 20


# ── emit() bool return + get_dropped_count ────────────────────────────────────

class TestEmitBoolReturn:
    def test_emit_returns_true_when_space_available(self):
        result = emit("test.bool_return", payload={"x": 1})
        assert result is True

    def test_queue_emit_returns_true_on_success(self):
        q = TelemetryQueue(maxsize=10)
        result = q.emit(TelemetryEvent(name="ok"))
        q.shutdown(timeout=2.0)
        assert result is True

    def test_queue_emit_returns_false_when_full(self):
        q = TelemetryQueue(maxsize=2)
        barrier = threading.Event()

        def blocking_sink(e: TelemetryEvent) -> None:
            barrier.wait(timeout=2.0)

        q.add_sink(blocking_sink)
        # Fill the queue fully so put_nowait raises queue.Full
        results = [q.emit(TelemetryEvent(name="ev")) for _ in range(20)]
        dropped_result = any(r is False for r in results)
        barrier.set()
        q.shutdown(timeout=2.0)
        assert dropped_result, "Expected at least one False return when queue is full"

    def test_get_dropped_count_reflects_full_queue_drops(self):
        q = TelemetryQueue(maxsize=2)
        barrier = threading.Event()

        def blocking_sink(e: TelemetryEvent) -> None:
            barrier.wait(timeout=2.0)

        q.add_sink(blocking_sink)
        for _ in range(20):
            q.emit(TelemetryEvent(name="ev"))
        dropped = q.dropped
        barrier.set()
        q.shutdown(timeout=2.0)
        assert dropped > 0

    def test_global_get_dropped_count(self):
        # Without filling the queue this should be 0 for a fresh queue
        count = get_dropped_count()
        assert isinstance(count, int)
        assert count >= 0


# ── shutdown() thread-exit guarantees ────────────────────────────────────────

class TestFlushCompletionGuarantee:
    """
    Regression tests: flush() must not return while a sink is still executing
    for the last dequeued item.  The old implementation polled queue.empty()
    which returns True as soon as the worker dequeues the item — before the
    sink call completes.  The fix uses queue.join() so flush() only unblocks
    after task_done() is called, which happens after all sinks finish.
    """

    def test_flush_waits_for_slow_sink_before_returning(self):
        """
        flush() must not return until the slow sink has appended to 'processed'.
        This test asserts BEFORE shutdown() — so if flush() returns prematurely
        the assertion would catch the race.
        """
        q = TelemetryQueue(maxsize=100)
        processed: list = []
        sink_started = threading.Event()

        def slow_sink(e: TelemetryEvent) -> None:
            sink_started.set()
            time.sleep(0.05)   # sink is mid-processing when queue becomes empty
            processed.append(e.name)

        q.add_sink(slow_sink)
        q.emit(TelemetryEvent(name="only_item"))

        # Wait until the sink has started so the worker has dequeued the item
        # (queue.empty() would now return True with the old implementation).
        sink_started.wait(timeout=2.0)

        # flush() must block until the sink actually completes.
        q.flush(timeout=3.0)

        # Assert BEFORE shutdown() — the item must already be in processed.
        assert "only_item" in processed, (
            "flush() returned before the slow sink finished processing the item"
        )

        q.shutdown(timeout=2.0)

    def test_flush_zero_timeout_returns_immediately(self):
        """flush(timeout=0) must return immediately without blocking."""
        q = TelemetryQueue(maxsize=100)
        gate = threading.Event()

        def blocking_sink(e: TelemetryEvent) -> None:
            gate.wait(timeout=5.0)

        q.add_sink(blocking_sink)
        q.emit(TelemetryEvent(name="ev"))

        start = time.monotonic()
        q.flush(timeout=0.0)
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"flush(timeout=0) blocked for {elapsed:.3f}s"

        gate.set()
        q.shutdown(timeout=2.0)


class TestShutdownThreadExit:
    def test_shutdown_thread_exits_cleanly(self):
        """Worker thread must stop after shutdown() completes."""
        q = TelemetryQueue(maxsize=10)
        for i in range(5):
            q.emit(TelemetryEvent(name=f"ev{i}"))
        q.shutdown(timeout=3.0)
        assert not q._thread.is_alive(), "Worker thread must exit after shutdown"

    def test_shutdown_with_full_queue_thread_exits(self):
        """Sentinel must be deliverable even when queue was full at shutdown time."""
        q = TelemetryQueue(maxsize=2)
        gate = threading.Event()

        def blocking_sink(e: TelemetryEvent) -> None:
            gate.wait(timeout=5.0)

        q.add_sink(blocking_sink)
        for _ in range(10):
            q.emit(TelemetryEvent(name="ev"))

        def _release():
            time.sleep(0.1)
            gate.set()

        releaser = threading.Thread(target=_release, daemon=True)
        releaser.start()
        q.shutdown(timeout=3.0)
        assert not q._thread.is_alive(), "Worker thread must exit cleanly even after full-queue shutdown"
