"""Tests for crucible.hooks"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.hooks import (
    HookContext,
    HookRegistry,
    HookResult,
    clear_hooks,
    execute_stage_hooks,
    hook_for,
    register_stage_hook,
    unregister_stage_hook,
    GLOBAL_REGISTRY,
)
from crucible.cancellation import (
    CancellationToken,
    OperationCancelledError,
    cancellation_scope,
)


@pytest.fixture(autouse=True)
def _clean_global_registry():
    """Ensure global registry is clean before/after each test."""
    clear_hooks()
    yield
    clear_hooks()


def _make_ctx(stage: str = "test_stage") -> HookContext:
    return HookContext(stage=stage, run_dir="/tmp/test", elapsed_seconds=1.0)


# ── HookContext ────────────────────────────────────────────────────────────────

class TestHookContext:
    def test_defaults(self):
        ctx = HookContext(stage="s")
        assert ctx.stage == "s"
        assert ctx.run_dir == ""
        assert ctx.elapsed_seconds == 0.0
        assert ctx.payload is None
        assert ctx.extra == {}

    def test_with_payload(self):
        ctx = HookContext(stage="x", payload={"key": "val"})
        assert ctx.payload == {"key": "val"}


# ── HookResult ────────────────────────────────────────────────────────────────

class TestHookResult:
    def test_to_dict(self):
        r = HookResult(
            hook_name="mymod.fn", stage="s", success=True, duration_seconds=0.123
        )
        d = r.to_dict()
        assert d["hook"] == "mymod.fn"
        assert d["stage"] == "s"
        assert d["success"] is True
        assert d["duration_seconds"] == pytest.approx(0.123, abs=0.001)
        assert d["error"] is None

    def test_to_dict_failure(self):
        r = HookResult(
            hook_name="fn", stage="s", success=False, error="ValueError: oops"
        )
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "ValueError: oops"


# ── HookRegistry ─────────────────────────────────────────────────────────────

class TestHookRegistry:
    def test_register_and_execute(self):
        reg = HookRegistry()
        received: list = []

        def hook(ctx: HookContext) -> None:
            received.append(ctx.stage)

        reg.register("stage1", hook)
        ctx = _make_ctx("stage1")
        results = reg.execute("stage1", ctx)

        assert len(results) == 1
        assert results[0].success is True
        assert "stage1" in received

    def test_no_hooks_returns_empty(self):
        reg = HookRegistry()
        results = reg.execute("nonexistent", _make_ctx("nonexistent"))
        assert results == []

    def test_multiple_hooks_all_run(self):
        reg = HookRegistry()
        log: list = []

        reg.register("s", lambda ctx: log.append("a"))
        reg.register("s", lambda ctx: log.append("b"))
        reg.execute("s", _make_ctx("s"))

        assert log == ["a", "b"]  # registration order

    def test_exception_isolated(self):
        reg = HookRegistry()
        log: list = []

        def bad_hook(ctx: HookContext) -> None:
            raise RuntimeError("boom")

        def good_hook(ctx: HookContext) -> None:
            log.append("ok")

        reg.register("s", bad_hook)
        reg.register("s", good_hook)
        results = reg.execute("s", _make_ctx("s"))

        assert len(results) == 2
        assert results[0].success is False
        assert results[0].error is not None
        assert results[1].success is True
        assert "ok" in log

    def test_unregister_removes_hook(self):
        reg = HookRegistry()
        log: list = []

        def fn(ctx: HookContext) -> None:
            log.append("ran")

        reg.register("s", fn)
        reg.unregister("s", fn)
        reg.execute("s", _make_ctx("s"))
        assert log == []

    def test_unregister_nonexistent_no_error(self):
        reg = HookRegistry()

        def fn(ctx: HookContext) -> None:
            pass

        reg.unregister("ghost", fn)  # must not raise

    def test_clear_single_stage(self):
        reg = HookRegistry()
        log: list = []

        reg.register("a", lambda ctx: log.append("a"))
        reg.register("b", lambda ctx: log.append("b"))
        reg.clear("a")
        reg.execute("a", _make_ctx("a"))
        reg.execute("b", _make_ctx("b"))

        assert "a" not in log
        assert "b" in log

    def test_clear_all(self):
        reg = HookRegistry()
        log: list = []

        reg.register("a", lambda ctx: log.append("a"))
        reg.register("b", lambda ctx: log.append("b"))
        reg.clear()
        reg.execute("a", _make_ctx("a"))
        reg.execute("b", _make_ctx("b"))

        assert log == []

    def test_hooks_for_snapshot(self):
        reg = HookRegistry()
        fn1 = lambda ctx: None
        fn2 = lambda ctx: None
        reg.register("s", fn1)
        reg.register("s", fn2)
        snapshot = reg.hooks_for("s")
        assert fn1 in snapshot
        assert fn2 in snapshot

    def test_result_duration_positive(self):
        import time as _time
        reg = HookRegistry()
        # Use a hook with a measurable delay to guarantee > 0 on low-res timers.
        reg.register("s", lambda ctx: _time.sleep(0.050))
        results = reg.execute("s", _make_ctx("s"))
        assert isinstance(results[0].duration_seconds, float)
        assert results[0].duration_seconds > 0.0


# ── Thread-safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_register_and_execute(self):
        reg = HookRegistry()
        log: list = []
        errors: list = []
        lock = threading.Lock()

        def register_and_run():
            try:
                def h(ctx: HookContext) -> None:
                    with lock:
                        log.append(1)
                reg.register("s", h)
                reg.execute("s", _make_ctx("s"))
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=register_and_run) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


# ── Public API ────────────────────────────────────────────────────────────────

class TestPublicAPI:
    def test_register_stage_hook_and_execute(self):
        received: list = []

        def fn(ctx: HookContext) -> None:
            received.append(ctx.stage)

        register_stage_hook("pub_stage", fn)
        results = execute_stage_hooks("pub_stage", _make_ctx("pub_stage"))

        assert len(results) == 1
        assert received == ["pub_stage"]

    def test_unregister_stage_hook(self):
        received: list = []

        def fn(ctx: HookContext) -> None:
            received.append(1)

        register_stage_hook("u_stage", fn)
        unregister_stage_hook("u_stage", fn)
        execute_stage_hooks("u_stage", _make_ctx("u_stage"))
        assert received == []

    def test_hook_for_decorator(self):
        received: list = []

        @hook_for("deco_stage")
        def on_deco(ctx: HookContext) -> None:
            received.append(ctx.stage)

        execute_stage_hooks("deco_stage", _make_ctx("deco_stage"))
        assert received == ["deco_stage"]

    def test_clear_hooks_all(self):
        register_stage_hook("x", lambda ctx: None)
        clear_hooks()
        results = execute_stage_hooks("x", _make_ctx("x"))
        assert results == []

    def test_clear_hooks_single_stage(self):
        received: list = []
        register_stage_hook("aa", lambda ctx: received.append("aa"))
        register_stage_hook("bb", lambda ctx: received.append("bb"))
        clear_hooks("aa")
        execute_stage_hooks("aa", _make_ctx("aa"))
        execute_stage_hooks("bb", _make_ctx("bb"))
        assert "aa" not in received
        assert "bb" in received

    def test_registry_override(self):
        custom = HookRegistry()
        received: list = []
        custom.register("s", lambda ctx: received.append("custom"))

        execute_stage_hooks("s", _make_ctx("s"), registry=custom)
        assert received == ["custom"]


# ── Hook timeout ──────────────────────────────────────────────────────────────

class TestHookTimeout:
    def test_fast_hook_within_timeout_succeeds(self):
        reg = HookRegistry()
        log: list = []

        def fast_hook(ctx: HookContext) -> None:
            log.append("ran")

        reg.register("s", fast_hook)
        results = reg.execute("s", _make_ctx("s"), hook_timeout_seconds=5.0)

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].timed_out is False
        assert "ran" in log

    def test_slow_hook_exceeds_timeout_is_marked_timed_out(self):
        reg = HookRegistry()

        def slow_hook(ctx: HookContext) -> None:
            import time
            time.sleep(10.0)  # will be interrupted by timeout

        reg.register("s", slow_hook)
        results = reg.execute("s", _make_ctx("s"), hook_timeout_seconds=0.05)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].timed_out is True
        assert "timed out" in (results[0].error or "").lower()

    def test_exception_in_hook_within_timeout_produces_failure(self):
        reg = HookRegistry()

        def boom(ctx: HookContext) -> None:
            raise ValueError("unexpected error")

        reg.register("s", boom)
        results = reg.execute("s", _make_ctx("s"), hook_timeout_seconds=5.0)

        assert len(results) == 1
        assert results[0].success is False
        assert results[0].timed_out is False
        assert "ValueError" in (results[0].error or "")

    def test_to_dict_includes_timed_out_key(self):
        r = HookResult(hook_name="fn", stage="s", success=False, timed_out=True,
                       error="Hook timed out after 1.0s")
        d = r.to_dict()
        assert "timed_out" in d
        assert d["timed_out"] is True

    def test_to_dict_timed_out_false_by_default(self):
        r = HookResult(hook_name="fn", stage="s", success=True)
        assert r.to_dict()["timed_out"] is False

    def test_execute_stage_hooks_passes_timeout(self):
        log: list = []

        def fast(ctx: HookContext) -> None:
            log.append("ok")

        register_stage_hook("timeout_stage", fast)
        results = execute_stage_hooks(
            "timeout_stage", _make_ctx("timeout_stage"), hook_timeout_seconds=5.0
        )
        assert results[0].success is True
        assert results[0].timed_out is False
        assert "ok" in log

    def test_second_hook_runs_after_first_times_out(self):
        """Pipeline continues: next hook runs even when the previous one timed out."""
        reg = HookRegistry()
        log: list = []

        def slow(ctx: HookContext) -> None:
            import time
            time.sleep(10.0)

        def fast(ctx: HookContext) -> None:
            log.append("ran")

        reg.register("s", slow)
        reg.register("s", fast)
        results = reg.execute("s", _make_ctx("s"), hook_timeout_seconds=0.05)

        assert len(results) == 2
        assert results[0].timed_out is True
        assert results[1].success is True
        assert "ran" in log

    def test_operation_cancelled_error_propagates_in_timed_path(self):
        """
        Regression: OperationCancelledError raised inside a hook running in
        the timed path (_run_hook_with_timeout) was silently swallowed and
        converted to HookResult(success=False), allowing subsequent hooks to
        still run.  It must now propagate and stop the execution loop —
        identical behaviour to the no-timeout path.
        """
        reg = HookRegistry()
        ran: list = []

        def cancelling_hook(ctx: HookContext) -> None:
            raise OperationCancelledError("cancelled from inside hook")

        def should_not_run(ctx: HookContext) -> None:
            ran.append("ran")

        reg.register("s", cancelling_hook)
        reg.register("s", should_not_run)

        with pytest.raises(OperationCancelledError):
            reg.execute("s", _make_ctx("s"), hook_timeout_seconds=5.0)

        assert ran == [], "second hook must not have run after OperationCancelledError"

    def test_operation_cancelled_error_propagates_in_no_timeout_path(self):
        """Verify the no-timeout path also propagates OperationCancelledError (parity test)."""
        reg = HookRegistry()
        ran: list = []

        def cancelling_hook(ctx: HookContext) -> None:
            raise OperationCancelledError("cancelled")

        def should_not_run(ctx: HookContext) -> None:
            ran.append("ran")

        reg.register("s", cancelling_hook)
        reg.register("s", should_not_run)

        with pytest.raises(OperationCancelledError):
            reg.execute("s", _make_ctx("s"))  # no timeout

        assert ran == [], "second hook must not run after OperationCancelledError"
