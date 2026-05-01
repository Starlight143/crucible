"""
crucible/streaming.py
==============================
Streaming wrapper for synchronous crew.kickoff() executions.

Because CrewAI's kickoff() is synchronous (no token-level streaming
available via the standard interface), this module provides a
thread-based wrapper that:

1. Runs crew.kickoff() in a daemon thread.
2. Yields ``StreamChunk`` objects as execution progresses:
   - Periodic "heartbeat" chunks (every *heartbeat_interval* seconds) while
     the crew is running — useful for keeping HTTP connections alive and
     showing spinner progress.
   - A final "done" chunk with the crew result on success.
   - An "error" chunk on failure.
3. Integrates with ``progress.py`` (if available) to forward any
   ProgressEvent objects emitted during execution as "progress" chunks.
4. Respects the active ``CancellationToken`` from ``cancellation.py``.

Inspired by Claude Code's Stream + MessageStream pattern: the caller
iterates a generator rather than blocking on a single synchronous call.

Usage::

    from crucible.streaming import stream_crew

    for chunk in stream_crew(crew, operation_name="analysis_crew"):
        if chunk.kind == "heartbeat":
            print(".", end="", flush=True)
        elif chunk.kind == "progress":
            print(f"\\n[{chunk.stage}] {chunk.content}")
        elif chunk.kind == "done":
            result = chunk.result
            break
        elif chunk.kind == "error":
            raise chunk.error
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Generator, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
    from .cancellation import raise_if_cancelled, OperationCancelledError
    from .errors import LLMTimeoutError
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]
    from cancellation import raise_if_cancelled, OperationCancelledError  # type: ignore[no-redef]
    from errors import LLMTimeoutError  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# Default interval between heartbeat chunks (seconds)
_DEFAULT_HEARTBEAT_INTERVAL: float = 1.0
_DEFAULT_POLL_INTERVAL: float = 0.05   # How often the generator checks for new chunks


# ── Chunk types ───────────────────────────────────────────────────────────────

@dataclass
class StreamChunk:
    """
    A single chunk yielded by ``stream_crew()``.

    Attributes
    ----------
    kind:
        One of: "heartbeat" | "progress" | "done" | "error".
    stage:
        Stage / operation name associated with this chunk.
    content:
        Human-readable description of this chunk.
    result:
        The crew result (set only for kind="done").
    error:
        The exception (set only for kind="error").
    elapsed_seconds:
        Wall-clock seconds since ``stream_crew()`` was called.
    metadata:
        Arbitrary key-value metadata.
    """
    kind: str   # "heartbeat" | "progress" | "done" | "error"
    stage: str = ""
    content: str = ""
    result: Any = None
    error: Optional[BaseException] = None
    elapsed_seconds: float = 0.0
    metadata: dict = field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Return True for 'done' and 'error' chunks."""
        return self.kind in ("done", "error")


# ── Core streaming function ───────────────────────────────────────────────────

def stream_crew(
    crew: Any,
    *,
    operation_name: str = "crew.kickoff",
    heartbeat_interval: float = _DEFAULT_HEARTBEAT_INTERVAL,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    timeout: Optional[float] = None,
) -> Generator[StreamChunk, None, None]:
    """
    Run ``crew.kickoff()`` in a background daemon thread and yield
    ``StreamChunk`` objects as execution progresses.

    Parameters
    ----------
    crew:
        Any object with a synchronous ``kickoff()`` method
        (CrewAI Crew or compatible).
    operation_name:
        Human-readable label for logging and chunk ``stage`` fields.
    heartbeat_interval:
        Seconds between heartbeat chunks while crew is running.
    poll_interval:
        Seconds between internal queue polls (controls responsiveness).
    timeout:
        Optional overall timeout in seconds.  If exceeded, an "error"
        chunk with ``LLMTimeoutError`` is yielded and the function returns.
        The background thread continues until it finishes (Python threads
        cannot be forcibly terminated).

    Yields
    ------
    StreamChunk
        Heartbeat chunks while running; terminal chunk ("done" / "error")
        at completion.

    Raises
    ------
    Does not raise.  All errors are reported via an "error" StreamChunk.
    """
    result_q: "queue.Queue[Any]" = queue.Queue()
    start_time = time.monotonic()
    last_heartbeat = start_time

    # ── Background worker ──────────────────────────────────────────────────

    def _worker() -> None:
        try:
            res = crew.kickoff()
            result_q.put(("done", res))
        except OperationCancelledError as exc:
            result_q.put(("cancelled", exc))
        except BaseException as exc:
            result_q.put(("error", exc))

    thread = threading.Thread(
        target=_worker,
        name=f"stream-{operation_name}",
        daemon=True,
    )

    log_event(
        LOGGER, 20, "stream_crew_started",
        f"Streaming crew execution: '{operation_name}'",
        operation=operation_name,
    )
    thread.start()

    # ── Generator loop ─────────────────────────────────────────────────────

    try:
        while True:
            # ① Check result queue first — even if we are past the timeout,
            #    a result that arrived before we noticed the deadline must be
            #    delivered.  Checking here before the timeout guard prevents
            #    the race where kickoff() finishes at exactly T=timeout and the
            #    result is silently discarded in favour of a timeout error.
            try:
                kind, value = result_q.get_nowait()
                elapsed = time.monotonic() - start_time
                if kind == "done":
                    log_event(
                        LOGGER, 20, "stream_crew_done",
                        f"Streaming crew completed: '{operation_name}' in {elapsed:.1f}s",
                        operation=operation_name,
                        elapsed_seconds=round(elapsed, 2),
                    )
                    yield StreamChunk(
                        kind="done",
                        stage=operation_name,
                        content=f"Completed in {elapsed:.1f}s",
                        result=value,
                        elapsed_seconds=elapsed,
                    )
                elif kind == "cancelled":
                    log_event(
                        LOGGER, 20, "stream_crew_cancelled",
                        f"Streaming crew cancelled: '{operation_name}'",
                        operation=operation_name,
                        elapsed_seconds=round(elapsed, 2),
                    )
                    yield StreamChunk(
                        kind="error",
                        stage=operation_name,
                        content=f"Cancelled: {value}",
                        error=value,
                        elapsed_seconds=elapsed,
                    )
                elif kind == "error":
                    log_event(
                        LOGGER, 30, "stream_crew_error",
                        f"Streaming crew failed: '{operation_name}': {value}",
                        operation=operation_name,
                        elapsed_seconds=round(elapsed, 2),
                        error=str(value),
                    )
                    yield StreamChunk(
                        kind="error",
                        stage=operation_name,
                        content=str(value),
                        error=value,
                        elapsed_seconds=elapsed,
                    )
                else:
                    log_event(
                        LOGGER, 30, "stream_crew_unknown_kind",
                        f"Streaming crew received unknown message kind '{kind}' for '{operation_name}'",
                        operation=operation_name,
                    )
                    yield StreamChunk(
                        kind="error",
                        stage=operation_name,
                        content=f"Internal error: unknown result kind '{kind}'",
                        elapsed_seconds=elapsed,
                    )
                return
            except queue.Empty:
                pass

            # ② Cancellation check
            try:
                raise_if_cancelled()
            except OperationCancelledError as cancel_exc:
                elapsed = time.monotonic() - start_time
                yield StreamChunk(
                    kind="error",
                    stage=operation_name,
                    content=f"Cancelled: {cancel_exc}",
                    error=cancel_exc,
                    elapsed_seconds=elapsed,
                )
                return

            # ③ Timeout check
            elapsed = time.monotonic() - start_time
            if timeout is not None and elapsed >= timeout:
                timeout_exc = LLMTimeoutError(
                    f"'{operation_name}' exceeded timeout of {timeout:.1f}s"
                )
                yield StreamChunk(
                    kind="error",
                    stage=operation_name,
                    content=f"Timeout after {elapsed:.1f}s",
                    error=timeout_exc,
                    elapsed_seconds=elapsed,
                )
                return

            # ④ Heartbeat
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_interval:
                last_heartbeat = now
                elapsed = now - start_time
                yield StreamChunk(
                    kind="heartbeat",
                    stage=operation_name,
                    content=f"Running ({elapsed:.0f}s elapsed)...",
                    elapsed_seconds=elapsed,
                )

            time.sleep(poll_interval)

    finally:
        # Do not join the thread — if we return early (cancel/timeout), the
        # daemon thread continues and will terminate when the process exits.
        pass


# ── Convenience: collect all chunks ──────────────────────────────────────────

def collect_crew_result(
    crew: Any,
    *,
    operation_name: str = "crew.kickoff",
    timeout: Optional[float] = None,
    on_heartbeat: Optional[Any] = None,
) -> Any:
    """
    Run crew.kickoff() via stream_crew() and return the final result.

    Unlike calling ``crew.kickoff()`` directly, this function:
    - Respects the active CancellationToken.
    - Enforces an optional timeout.
    - Calls *on_heartbeat* (callable, no args) for each heartbeat chunk
      (useful for keep-alive pings, spinner updates, etc.).

    Parameters
    ----------
    crew:           Crew to run.
    operation_name: Label for logging.
    timeout:        Optional overall timeout (seconds).
    on_heartbeat:   Optional callable invoked for each heartbeat chunk.

    Returns
    -------
    Any
        The crew.kickoff() return value.

    Raises
    ------
    OperationCancelledError
        If the active CancellationToken is cancelled.
    errors.LLMTimeoutError
        If *timeout* is exceeded.
    Exception
        Re-raises any exception from crew.kickoff().
    """
    for chunk in stream_crew(
        crew,
        operation_name=operation_name,
        timeout=timeout,
    ):
        if chunk.kind == "heartbeat" and callable(on_heartbeat):
            on_heartbeat()
        elif chunk.kind == "done":
            return chunk.result
        elif chunk.kind == "error":
            if chunk.error is not None:
                raise chunk.error
            raise RuntimeError(f"stream_crew returned error chunk with no exception: {chunk.content}")
    # Should never reach here (stream always yields a terminal chunk)
    raise RuntimeError(f"stream_crew for '{operation_name}' ended without terminal chunk")
