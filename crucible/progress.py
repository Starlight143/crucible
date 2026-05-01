"""
crucible/progress.py
============================
Streaming progress callbacks for the multi-stage analysis pipeline.

Inspired by Claude Code's ``StreamingToolExecutor`` pattern: each pipeline
stage emits typed ``ProgressEvent`` objects to registered listeners so that
long-running (5-15 min) pipelines have visible intermediate progress without
requiring callers to poll or wait for full completion.

Architecture
------------
* ``ProgressEvent``       — immutable event record (stage, type, message, pct).
* ``ProgressReporter``    — emits events; stages call ``reporter.update(...)``
                             or use the ``stage_context(...)`` context manager.
* ``ProgressListener``    — protocol / callable interface for receiving events.
* Built-in listeners: ``ConsoleProgressListener`` (stdout), ``LogProgressListener``
                       (routes to ``runtime_logging``).
* Thread-safe: listeners are called under a lock; slow listeners don't block
  stage execution because dispatch is synchronous but exception-isolated.

Usage::

    from crucible.progress import ProgressReporter, ConsoleProgressListener

    reporter = ProgressReporter()
    reporter.add_listener(ConsoleProgressListener())

    # Inside a pipeline stage:
    with reporter.stage_context("research_swarm", total_steps=3) as stage:
        data = fetch_web(...)
        stage.step("Web search complete")

        results = run_crew(...)
        stage.step("Crew analysis complete")

        stage.step("Saving results")
        stage.done()

    # Or manual events:
    reporter.update("direction_debate", "Debating 7 directions...", pct=0.1)
    reporter.finish("direction_debate", "Direction selected: momentum breakout")
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
    from .cancellation import OperationCancelledError as _OperationCancelledError
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]
    from cancellation import OperationCancelledError as _OperationCancelledError  # type: ignore[no-redef]

LOGGER = get_logger(__name__)


# ── Event types ───────────────────────────────────────────────────────────────

class EventType(str, Enum):
    STARTED = "started"
    PROGRESS = "progress"
    STEP = "step"
    WARNING = "warning"
    FINISHED = "finished"
    FAILED = "failed"


# ── Core data model ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProgressEvent:
    """
    Immutable progress event emitted by a pipeline stage.

    Attributes
    ----------
    stage:
        Canonical stage name (e.g. ``"research_swarm"``).
    event_type:
        One of the ``EventType`` enum values.
    message:
        Human-readable status message.
    pct:
        Completion percentage in [0.0, 1.0], or ``None`` when unknown.
    elapsed_seconds:
        Monotonic time since the stage was started (0.0 if not tracked).
    extra:
        Optional freeform metadata dict.
    """
    stage: str
    event_type: EventType
    message: str
    pct: Optional[float] = None
    elapsed_seconds: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        pct_str = f" {self.pct * 100:.0f}%" if self.pct is not None else ""
        elapsed_str = f" [{self.elapsed_seconds:.1f}s]" if self.elapsed_seconds > 0 else ""
        return f"[{self.stage}]{pct_str}{elapsed_str} {self.message}"


# ── Listener protocol ─────────────────────────────────────────────────────────

ProgressListener = Callable[[ProgressEvent], None]


class ConsoleProgressListener:
    """Writes progress events to stdout."""

    def __init__(self, *, prefix: str = "") -> None:
        self._prefix = prefix

    def __call__(self, event: ProgressEvent) -> None:
        icon = {
            EventType.STARTED:  "▶",
            EventType.PROGRESS: "·",
            EventType.STEP:     "✓",
            EventType.WARNING:  "⚠",
            EventType.FINISHED: "✔",
            EventType.FAILED:   "✗",
        }.get(event.event_type, "·")
        print(f"{self._prefix}{icon} {event.format()}", flush=True)


class LogProgressListener:
    """Routes progress events to ``runtime_logging``."""

    def __call__(self, event: ProgressEvent) -> None:
        level = 30 if event.event_type in (EventType.WARNING, EventType.FAILED) else 20
        log_event(
            LOGGER,
            level,
            f"progress_{event.event_type.value}",
            event.message,
            stage=event.stage,
            pct=event.pct,
            elapsed_seconds=round(event.elapsed_seconds, 2),
            **event.extra,
        )


# ── Stage context helper ──────────────────────────────────────────────────────

class _StageContext:
    """
    Helper returned by ``ProgressReporter.stage_context()``.

    Tracks step count within a stage and emits STEP events with auto-computed
    completion percentages when ``total_steps`` is provided.
    """

    def __init__(
        self,
        reporter: "ProgressReporter",
        stage: str,
        total_steps: Optional[int],
        start_time: float,
    ) -> None:
        self._reporter = reporter
        self._stage = stage
        self._total = total_steps
        self._start = start_time
        self._current_step = 0

    def _elapsed(self) -> float:
        return time.monotonic() - self._start

    def _pct(self) -> Optional[float]:
        if self._total is None or self._total <= 0:
            return None
        return min(1.0, self._current_step / self._total)

    def step(self, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a STEP event and advance the step counter."""
        self._current_step += 1
        self._reporter._emit(ProgressEvent(
            stage=self._stage,
            event_type=EventType.STEP,
            message=message,
            pct=self._pct(),
            elapsed_seconds=self._elapsed(),
            extra=extra or {},
        ))

    def update(self, message: str, *, pct: Optional[float] = None,
               extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a PROGRESS event with optional explicit percentage."""
        self._reporter._emit(ProgressEvent(
            stage=self._stage,
            event_type=EventType.PROGRESS,
            message=message,
            pct=pct,
            elapsed_seconds=self._elapsed(),
            extra=extra or {},
        ))

    def warn(self, message: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a WARNING event."""
        self._reporter._emit(ProgressEvent(
            stage=self._stage,
            event_type=EventType.WARNING,
            message=message,
            pct=self._pct(),
            elapsed_seconds=self._elapsed(),
            extra=extra or {},
        ))

    def done(self, message: str = "Stage complete", *,
             extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a FINISHED event."""
        self._reporter._emit(ProgressEvent(
            stage=self._stage,
            event_type=EventType.FINISHED,
            message=message,
            pct=1.0,
            elapsed_seconds=self._elapsed(),
            extra=extra or {},
        ))


# ── ProgressReporter ─────────────────────────────────────────────────────────

class ProgressReporter:
    """
    Central event bus for pipeline progress notifications.

    Thread-safe: all listener dispatch happens under an internal lock.
    Listener exceptions are caught and logged so that one broken listener
    never silences other listeners or disrupts pipeline execution.

    Listeners are invoked synchronously in registration order.
    """

    def __init__(self, *, listeners: Optional[List[ProgressListener]] = None) -> None:
        self._listeners: List[ProgressListener] = list(listeners or [])
        self._lock = threading.Lock()
        self._stage_starts: Dict[str, float] = {}

    # ── Listener management ──────────────────────────────────────────────────

    def add_listener(self, listener: ProgressListener) -> None:
        """Register a new listener."""
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: ProgressListener) -> None:
        """Unregister a listener (no-op if not found)."""
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    # ── Internal dispatch ────────────────────────────────────────────────────

    def _emit(self, event: ProgressEvent) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except _OperationCancelledError:
                # Cooperative cancellation must propagate out of the listener
                # dispatch loop — do not swallow it with the isolation handler.
                raise
            except Exception as exc:  # pragma: no cover
                LOGGER.warning("ProgressReporter: listener %r raised: %s", listener, exc)

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self, stage: str, message: str = "Starting…",
              *, extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a STARTED event and record the stage start time."""
        with self._lock:
            self._stage_starts[stage] = time.monotonic()
        self._emit(ProgressEvent(
            stage=stage,
            event_type=EventType.STARTED,
            message=message,
            pct=0.0,
            elapsed_seconds=0.0,
            extra=extra or {},
        ))

    def update(self, stage: str, message: str, *,
               pct: Optional[float] = None,
               extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a PROGRESS event for *stage*."""
        with self._lock:
            stage_start = self._stage_starts.get(stage)
        now = time.monotonic()
        elapsed = max(0.0, now - stage_start) if stage_start is not None else 0.0
        self._emit(ProgressEvent(
            stage=stage,
            event_type=EventType.PROGRESS,
            message=message,
            pct=pct,
            elapsed_seconds=elapsed,
            extra=extra or {},
        ))

    def finish(self, stage: str, message: str = "Done",
               *, extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a FINISHED event for *stage*."""
        with self._lock:
            stage_start = self._stage_starts.pop(stage, None)
        elapsed = max(0.0, time.monotonic() - stage_start) if stage_start is not None else 0.0
        self._emit(ProgressEvent(
            stage=stage,
            event_type=EventType.FINISHED,
            message=message,
            pct=1.0,
            elapsed_seconds=elapsed,
            extra=extra or {},
        ))

    def fail(self, stage: str, message: str,
             *, extra: Optional[Dict[str, Any]] = None) -> None:
        """Emit a FAILED event for *stage*."""
        with self._lock:
            stage_start = self._stage_starts.pop(stage, None)
        elapsed = max(0.0, time.monotonic() - stage_start) if stage_start is not None else 0.0
        self._emit(ProgressEvent(
            stage=stage,
            event_type=EventType.FAILED,
            message=message,
            elapsed_seconds=elapsed,
            extra=extra or {},
        ))

    @contextlib.contextmanager
    def stage_context(
        self,
        stage: str,
        *,
        start_message: str = "Starting…",
        total_steps: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[_StageContext]:
        """
        Context manager that wraps a pipeline stage with automatic
        STARTED / FAILED lifecycle events.

        On normal exit, the caller is expected to call ``ctx.done()`` explicitly
        so that the FINISHED message is meaningful.  If the block raises,
        a FAILED event is emitted automatically.

        Example::

            with reporter.stage_context("research_swarm", total_steps=3) as ctx:
                data = fetch(...)
                ctx.step("Fetch complete")
                result = analyse(data)
                ctx.step("Analysis complete")
                ctx.done("Research complete")
        """
        self.start(stage, start_message, extra=extra)
        with self._lock:
            start_time = self._stage_starts.get(stage, time.monotonic())
        ctx = _StageContext(self, stage, total_steps, start_time)
        try:
            yield ctx
        except _OperationCancelledError:
            # Cooperative cancellation must propagate without emitting a FAILED
            # progress event (which would misclassify an intentional cancellation
            # as a stage failure in downstream listeners and monitoring systems).
            raise
        except Exception as exc:
            self.fail(stage, f"Stage failed: {type(exc).__name__}: {exc}")
            raise
        finally:
            # Clean up start time if still present (caller may not have called done())
            with self._lock:
                self._stage_starts.pop(stage, None)


# ── Module-level singleton ────────────────────────────────────────────────────

_DEFAULT_REPORTER: Optional[ProgressReporter] = None
_REPORTER_LOCK = threading.Lock()


def get_reporter() -> ProgressReporter:
    """
    Return the process-wide default ``ProgressReporter`` (lazy-init).

    Automatically adds a ``LogProgressListener`` so all events are captured
    in structured logs even when no explicit console listener is registered.
    """
    global _DEFAULT_REPORTER
    with _REPORTER_LOCK:
        if _DEFAULT_REPORTER is None:
            reporter = ProgressReporter()
            reporter.add_listener(LogProgressListener())
            _DEFAULT_REPORTER = reporter
    return _DEFAULT_REPORTER


def reset_reporter() -> None:
    """Reset the process-wide reporter (mainly for tests)."""
    global _DEFAULT_REPORTER
    with _REPORTER_LOCK:
        _DEFAULT_REPORTER = None
