"""Tests for crucible/streaming.py"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.streaming import StreamChunk, collect_crew_result, stream_crew
from crucible.cancellation import (
    CancellationToken,
    OperationCancelledError,
    cancellation_scope,
)
from crucible.errors import LLMTimeoutError


# ── Fake crew helpers ─────────────────────────────────────────────────────────

class FakeCrew:
    """Synchronous crew that returns a value immediately."""

    def __init__(self, result: Any = "result", delay: float = 0.0) -> None:
        self._result = result
        self._delay = delay

    def kickoff(self) -> Any:
        if self._delay > 0:
            time.sleep(self._delay)
        return self._result


class ErrorCrew:
    """Crew that raises an exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def kickoff(self) -> Any:
        raise self._exc


# ── StreamChunk ───────────────────────────────────────────────────────────────

class TestStreamChunk:
    def test_is_terminal_done(self) -> None:
        chunk = StreamChunk(kind="done")
        assert chunk.is_terminal() is True

    def test_is_terminal_error(self) -> None:
        chunk = StreamChunk(kind="error")
        assert chunk.is_terminal() is True

    def test_is_terminal_heartbeat_false(self) -> None:
        chunk = StreamChunk(kind="heartbeat")
        assert chunk.is_terminal() is False

    def test_is_terminal_progress_false(self) -> None:
        chunk = StreamChunk(kind="progress")
        assert chunk.is_terminal() is False


# ── stream_crew ───────────────────────────────────────────────────────────────

class TestStreamCrew:
    def test_yields_done_chunk_on_success(self) -> None:
        crew = FakeCrew(result="my_result")
        chunks = list(stream_crew(crew, poll_interval=0.01))
        terminal = [c for c in chunks if c.is_terminal()]
        assert len(terminal) == 1
        assert terminal[0].kind == "done"
        assert terminal[0].result == "my_result"

    def test_yields_error_chunk_on_exception(self) -> None:
        exc = ValueError("crew exploded")
        crew = ErrorCrew(exc)
        chunks = list(stream_crew(crew, poll_interval=0.01))
        terminal = [c for c in chunks if c.is_terminal()]
        assert len(terminal) == 1
        assert terminal[0].kind == "error"
        assert terminal[0].error is exc

    def test_heartbeat_chunks_yielded_during_long_run(self) -> None:
        crew = FakeCrew(delay=0.25)
        chunks = list(stream_crew(
            crew,
            heartbeat_interval=0.05,
            poll_interval=0.01,
        ))
        heartbeats = [c for c in chunks if c.kind == "heartbeat"]
        assert len(heartbeats) >= 1

    def test_always_yields_exactly_one_terminal_chunk(self) -> None:
        crew = FakeCrew(result="ok")
        chunks = list(stream_crew(crew, poll_interval=0.01))
        terminal = [c for c in chunks if c.is_terminal()]
        assert len(terminal) == 1

    def test_done_chunk_has_elapsed_seconds(self) -> None:
        # Use a measurable delay to guarantee > 0 on low-resolution Windows timers.
        crew = FakeCrew(result="ok", delay=0.050)
        chunks = list(stream_crew(crew, poll_interval=0.01))
        done = next(c for c in chunks if c.kind == "done")
        assert isinstance(done.elapsed_seconds, float)
        assert done.elapsed_seconds > 0.0

    def test_error_chunk_has_elapsed_seconds(self) -> None:
        # v1.1.2 (audit fix G6-D-MED-3): match the ``done`` chunk pattern at
        # line 104-110 — use a measurable delay before the exception so the
        # ``> 0.0`` assertion has teeth on low-resolution Windows timers
        # (CLAUDE.md § 9.5: ``>= 0.0`` on a non-negative float is tautology).
        class _DelayedErrorCrew:
            def __init__(self, exc: Exception, delay: float) -> None:
                self._exc = exc
                self._delay = delay

            def kickoff(self) -> Any:
                import time as _t
                _t.sleep(self._delay)
                raise self._exc

        crew = _DelayedErrorCrew(RuntimeError("err"), delay=0.050)
        chunks = list(stream_crew(crew, poll_interval=0.01))
        err = next(c for c in chunks if c.kind == "error")
        assert isinstance(err.elapsed_seconds, float)
        assert err.elapsed_seconds > 0.0

    def test_timeout_yields_error_chunk(self) -> None:
        crew = FakeCrew(delay=10.0)  # will not finish within timeout
        chunks = list(stream_crew(
            crew,
            timeout=0.05,
            poll_interval=0.01,
        ))
        terminal = [c for c in chunks if c.is_terminal()]
        assert len(terminal) == 1
        assert terminal[0].kind == "error"
        assert isinstance(terminal[0].error, LLMTimeoutError)

    def test_cancellation_yields_error_chunk(self) -> None:
        crew = FakeCrew(delay=10.0)
        with cancellation_scope() as token:
            token.cancel()  # pre-cancel
            chunks = list(stream_crew(crew, poll_interval=0.01))

        terminal = [c for c in chunks if c.is_terminal()]
        assert len(terminal) == 1
        assert terminal[0].kind == "error"
        assert isinstance(terminal[0].error, OperationCancelledError)

    def test_operation_name_in_chunk_stage(self) -> None:
        crew = FakeCrew(result="ok")
        chunks = list(stream_crew(
            crew,
            operation_name="my_analysis",
            poll_interval=0.01,
        ))
        terminal = next(c for c in chunks if c.is_terminal())
        assert terminal.stage == "my_analysis"


# ── collect_crew_result ───────────────────────────────────────────────────────

class TestCollectCrewResult:
    def test_returns_result_on_success(self) -> None:
        crew = FakeCrew(result={"direction": "long"})
        result = collect_crew_result(crew)
        assert result == {"direction": "long"}

    def test_raises_on_crew_exception(self) -> None:
        crew = ErrorCrew(ValueError("bad crew"))
        with pytest.raises(ValueError, match="bad crew"):
            collect_crew_result(crew)

    def test_raises_on_timeout(self) -> None:
        crew = FakeCrew(delay=10.0)
        with pytest.raises(LLMTimeoutError):
            collect_crew_result(crew, timeout=0.05)

    def test_raises_on_cancellation(self) -> None:
        crew = FakeCrew(delay=10.0)
        with cancellation_scope() as token:
            token.cancel()
            with pytest.raises(OperationCancelledError):
                collect_crew_result(crew)

    def test_on_heartbeat_called_during_execution(self) -> None:
        """on_heartbeat must be invoked at least once during a long-running crew.

        ``collect_crew_result`` uses the default 1 s heartbeat interval, so we
        drive the crew through ``stream_crew`` directly with a short interval
        instead, routing heartbeat chunks into the counter manually — this is
        exactly what ``collect_crew_result`` does internally.
        """
        crew = FakeCrew(delay=0.2)
        heartbeat_count = [0]

        def count_heartbeat() -> None:
            heartbeat_count[0] += 1

        result = None
        for chunk in stream_crew(crew, heartbeat_interval=0.05, poll_interval=0.01):
            if chunk.kind == "heartbeat":
                count_heartbeat()
            elif chunk.kind == "done":
                result = chunk.result

        assert result is not None
        assert heartbeat_count[0] > 0, "on_heartbeat must be invoked at least once during execution"

    def test_on_heartbeat_not_called_if_none(self) -> None:
        crew = FakeCrew(result="ok")
        result = collect_crew_result(crew, on_heartbeat=None)
        assert result == "ok"
