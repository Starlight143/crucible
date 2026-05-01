"""Tests for crucible.progress"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.progress import (
    ProgressReporter,
    ProgressEvent,
    EventType,
    ConsoleProgressListener,
    LogProgressListener,
    get_reporter,
    reset_reporter,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_reporter()
    yield
    reset_reporter()


# ── ProgressEvent ─────────────────────────────────────────────────────────────

class TestProgressEvent:
    def test_format_with_pct(self):
        e = ProgressEvent(stage="s", event_type=EventType.PROGRESS,
                          message="working", pct=0.5)
        text = e.format()
        assert "[s]" in text
        assert "50%" in text
        assert "working" in text

    def test_format_without_pct(self):
        e = ProgressEvent(stage="s", event_type=EventType.STARTED,
                          message="started")
        text = e.format()
        assert "%" not in text
        assert "started" in text

    def test_format_with_elapsed(self):
        e = ProgressEvent(stage="s", event_type=EventType.FINISHED,
                          message="done", elapsed_seconds=5.2)
        text = e.format()
        assert "[5.2s]" in text


# ── ProgressReporter ──────────────────────────────────────────────────────────

class TestProgressReporter:
    def _collected(self):
        events = []
        reporter = ProgressReporter(listeners=[events.append])
        return reporter, events

    def test_start_emits_started_event(self):
        reporter, events = self._collected()
        reporter.start("stage_x", "Starting...")
        assert len(events) == 1
        assert events[0].event_type == EventType.STARTED
        assert events[0].stage == "stage_x"

    def test_update_emits_progress_event(self):
        reporter, events = self._collected()
        reporter.start("s")
        reporter.update("s", "halfway", pct=0.5)
        prog = [e for e in events if e.event_type == EventType.PROGRESS]
        assert len(prog) == 1
        assert prog[0].pct == 0.5

    def test_finish_emits_finished_event(self):
        reporter, events = self._collected()
        reporter.start("s")
        reporter.finish("s", "Done!")
        finished = [e for e in events if e.event_type == EventType.FINISHED]
        assert len(finished) == 1
        assert finished[0].pct == 1.0

    def test_fail_emits_failed_event(self):
        reporter, events = self._collected()
        reporter.start("s")
        reporter.fail("s", "Boom")
        failed = [e for e in events if e.event_type == EventType.FAILED]
        assert len(failed) == 1

    def test_add_remove_listener(self):
        reporter = ProgressReporter()
        events = []
        reporter.add_listener(events.append)
        reporter.start("s")
        assert len(events) == 1
        reporter.remove_listener(events.append)
        reporter.start("s2")
        assert len(events) == 1  # no new events after removal

    def test_listener_exception_does_not_propagate(self):
        def bad_listener(e):
            raise RuntimeError("listener crash")

        reporter = ProgressReporter(listeners=[bad_listener])
        # Should not raise
        reporter.start("s", "msg")

    def test_elapsed_seconds_non_negative(self):
        import time as _time
        reporter, events = self._collected()
        reporter.start("s")
        _time.sleep(0.050)  # ensure measurable elapsed time on low-res Windows timers
        reporter.finish("s")
        finished = [e for e in events if e.event_type == EventType.FINISHED][0]
        assert finished.elapsed_seconds > 0.0


# ── stage_context ─────────────────────────────────────────────────────────────

class TestStageContext:
    def _collected(self):
        events = []
        reporter = ProgressReporter(listeners=[events.append])
        return reporter, events

    def test_emits_started_on_enter(self):
        reporter, events = self._collected()
        with reporter.stage_context("s") as ctx:
            ctx.done()
        started = [e for e in events if e.event_type == EventType.STARTED]
        assert len(started) == 1

    def test_emits_failed_on_exception(self):
        reporter, events = self._collected()
        with pytest.raises(ValueError):
            with reporter.stage_context("s") as ctx:
                raise ValueError("fail")
        failed = [e for e in events if e.event_type == EventType.FAILED]
        assert len(failed) == 1

    def test_step_increments_count(self):
        reporter, events = self._collected()
        with reporter.stage_context("s", total_steps=3) as ctx:
            ctx.step("step1")
            ctx.step("step2")
            ctx.done()
        steps = [e for e in events if e.event_type == EventType.STEP]
        assert len(steps) == 2

    def test_pct_computed_from_steps(self):
        reporter, events = self._collected()
        with reporter.stage_context("s", total_steps=4) as ctx:
            ctx.step("s1")  # 1/4 = 0.25
            ctx.done()
        step_event = next(e for e in events if e.event_type == EventType.STEP)
        assert step_event.pct == pytest.approx(0.25)

    def test_warn_emits_warning(self):
        reporter, events = self._collected()
        with reporter.stage_context("s") as ctx:
            ctx.warn("something is off")
            ctx.done()
        warnings = [e for e in events if e.event_type == EventType.WARNING]
        assert len(warnings) == 1

    def test_update_inside_context(self):
        reporter, events = self._collected()
        with reporter.stage_context("s") as ctx:
            ctx.update("working...", pct=0.3)
            ctx.done()
        progress = [e for e in events if e.event_type == EventType.PROGRESS]
        assert len(progress) == 1
        assert progress[0].pct == pytest.approx(0.3)


# ── ConsoleProgressListener ───────────────────────────────────────────────────

class TestConsoleProgressListener:
    def test_does_not_raise(self, capsys):
        listener = ConsoleProgressListener()
        event = ProgressEvent(stage="s", event_type=EventType.STARTED,
                              message="hello", pct=0.0)
        listener(event)
        captured = capsys.readouterr()
        assert "s" in captured.out
        assert "hello" in captured.out

    def test_prefix_applied(self, capsys):
        listener = ConsoleProgressListener(prefix=">> ")
        event = ProgressEvent(stage="s", event_type=EventType.FINISHED,
                              message="done", pct=1.0)
        listener(event)
        assert capsys.readouterr().out.startswith(">> ")


# ── Singleton ─────────────────────────────────────────────────────────────────

class TestGetReporter:
    def test_returns_same_instance(self):
        r1 = get_reporter()
        r2 = get_reporter()
        assert r1 is r2

    def test_reset_creates_new_instance(self):
        r1 = get_reporter()
        reset_reporter()
        r2 = get_reporter()
        assert r1 is not r2

    def test_default_reporter_has_log_listener(self):
        # Default reporter should not crash even without explicit console listener
        r = get_reporter()
        r.start("test_stage", "testing")
        r.finish("test_stage", "done")


# ── Cancellation propagation ───────────────────────────────────────────────────

class TestProgressListenerCancellationPropagation:
    """
    Regression tests: OperationCancelledError raised inside a progress listener
    was previously swallowed by the `except Exception` isolation handler in
    ProgressReporter._emit(), allowing subsequent listeners and the calling code
    to continue executing.  It must now propagate unconditionally.
    """

    def test_cancellation_in_listener_propagates_from_emit(self):
        """
        OperationCancelledError from a listener must propagate out of _emit()
        (and therefore out of start(), update(), finish(), fail()).
        """
        from crucible.cancellation import OperationCancelledError

        reporter = ProgressReporter()
        subsequent_ran: list = []

        def cancelling_listener(event: ProgressEvent) -> None:
            raise OperationCancelledError("cancelled inside listener")

        def subsequent_listener(event: ProgressEvent) -> None:
            subsequent_ran.append("ran")

        reporter.add_listener(cancelling_listener)
        reporter.add_listener(subsequent_listener)

        with pytest.raises(OperationCancelledError):
            reporter.start("stage", "Starting")

        assert subsequent_ran == [], (
            "subsequent listener must not run after OperationCancelledError"
        )

    def test_cancellation_in_listener_propagates_through_stage_context(self):
        """
        OperationCancelledError from a listener inside stage_context must
        propagate out (not be converted to a FAILED event and suppressed).
        """
        from crucible.cancellation import OperationCancelledError

        reporter = ProgressReporter()

        def cancelling_listener(event: ProgressEvent) -> None:
            raise OperationCancelledError("cancelled")

        reporter.add_listener(cancelling_listener)

        with pytest.raises(OperationCancelledError):
            with reporter.stage_context("stage"):
                pass  # stage_context calls start() which calls _emit()

    def test_ordinary_listener_exception_still_isolated(self):
        """Non-cancellation exceptions from listeners are still silently swallowed."""
        reporter = ProgressReporter()
        ran: list = []

        def bad_listener(event: ProgressEvent) -> None:
            raise RuntimeError("listener bug")

        def good_listener(event: ProgressEvent) -> None:
            ran.append("ok")

        reporter.add_listener(bad_listener)
        reporter.add_listener(good_listener)

        # Must NOT raise; the bad listener's RuntimeError is isolated
        reporter.start("stage", "Starting")

        assert "ok" in ran, "good listener must still run after bad listener's RuntimeError"


# ── stage_context cancellation ────────────────────────────────────────────────

class TestStageContextCancellationPropagation:
    """
    Regression tests: OperationCancelledError raised inside a stage_context
    body previously matched `except Exception as exc:` which called self.fail()
    (emitting a spurious FAILED progress event) before re-raising.  The fix
    adds an explicit `except _OperationCancelledError: raise` guard so
    cancellation propagates immediately without misclassifying it as a failure.
    """

    def test_cancelled_body_does_not_emit_failed_event(self):
        """
        OperationCancelledError from user code must NOT trigger self.fail() and
        therefore must NOT emit a FAILED progress event.
        """
        from crucible.cancellation import OperationCancelledError

        reporter = ProgressReporter()
        events: list = []

        def listener(event: ProgressEvent) -> None:
            events.append(event.event_type)

        reporter.add_listener(listener)

        with pytest.raises(OperationCancelledError):
            with reporter.stage_context("stage"):
                raise OperationCancelledError("cancelled")

        # STARTED must have been emitted (from stage_context entry)
        assert EventType.STARTED in events, "STARTED event must have been emitted"

        # FAILED must NOT have been emitted — cancellation is not a failure
        assert EventType.FAILED not in events, (
            "FAILED event must not be emitted for OperationCancelledError; "
            "that would misclassify an intentional cancellation as a stage failure"
        )

    def test_cancelled_body_propagates_correctly(self):
        """OperationCancelledError from stage body must propagate to the caller."""
        from crucible.cancellation import OperationCancelledError

        reporter = ProgressReporter()

        with pytest.raises(OperationCancelledError):
            with reporter.stage_context("stage"):
                raise OperationCancelledError("cancelled")

    def test_ordinary_exception_still_emits_failed_event(self):
        """Non-cancellation exceptions must still emit a FAILED event (existing contract)."""
        reporter = ProgressReporter()
        events: list = []

        def listener(event: ProgressEvent) -> None:
            events.append(event.event_type)

        reporter.add_listener(listener)

        with pytest.raises(RuntimeError):
            with reporter.stage_context("stage"):
                raise RuntimeError("genuine failure")

        assert EventType.FAILED in events, "FAILED event must be emitted for RuntimeError"

