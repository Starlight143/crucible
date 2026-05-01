"""
crucible/hooks.py
=========================
Post-stage hook system for the analysis pipeline.

Inspired by Claude Code's ``registerPostSamplingHook`` / ``executePostSamplingHooks``
pattern: instead of per-feature ``try/except`` blocks copy-pasted 75+ times
across the pipeline, each stage declares hooks that fire after stage completion.
Hooks are:

* **Exception-isolated**: one broken hook never blocks others.
* **Sequential**: hooks for the same stage run one-at-a-time (no concurrency).
* **Logging-integrated**: every hook invocation is recorded via
  ``runtime_logging``, so the audit trail is always available.
* **Context-aware**: hooks receive a ``HookContext`` with stage name,
  run_dir, elapsed time, and optional per-stage payload.

Architecture
------------
* ``register_stage_hook(stage, fn)`` — register a callable for a stage.
* ``execute_stage_hooks(stage, context)`` — run all hooks for *stage*
  in registration order.  Returns a list of ``HookResult``.
* ``hook_for(stage)`` — decorator sugar for registering hooks.
* ``GLOBAL_REGISTRY`` — module-level registry (can be replaced for testing).

Usage::

    from crucible.hooks import register_stage_hook, execute_stage_hooks, HookContext

    # Register a hook:
    def after_codegen(ctx: HookContext) -> None:
        if ctx.payload:
            run_security_scan(ctx.payload.get("code_bundle"))

    register_stage_hook("codegen", after_codegen)

    # Or use the decorator:
    from crucible.hooks import hook_for

    @hook_for("analysis_crew")
    def notify_slack(ctx: HookContext) -> None:
        send_slack(f"Analysis complete in {ctx.elapsed_seconds:.1f}s")

    # Execute (called by the pipeline runner after each stage):
    results = execute_stage_hooks("codegen", HookContext(
        stage="codegen",
        run_dir="/path/to/run",
        elapsed_seconds=42.0,
        payload={"code_bundle": bundle},
    ))
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event, log_exception
    from .cancellation import raise_if_cancelled, OperationCancelledError
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event, log_exception  # type: ignore[no-redef]
    from cancellation import raise_if_cancelled, OperationCancelledError  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# Hook callable signature:  fn(HookContext) -> None
StageHook = Callable[["HookContext"], None]


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class HookContext:
    """
    Context passed to every hook invocation.

    Attributes
    ----------
    stage:
        Name of the completed stage (e.g. ``"codegen"``).
    run_dir:
        Path to the current run's output directory.
    elapsed_seconds:
        Time the stage took to complete.
    payload:
        Optional free-form data from the stage (e.g. code bundle, analysis
        report).  Individual hooks are responsible for knowing what to expect.
    extra:
        Additional key-value metadata.
    """
    stage: str
    run_dir: str = ""
    elapsed_seconds: float = 0.0
    payload: Optional[Any] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """
    Result of a single hook execution.

    Attributes
    ----------
    hook_name:
        Display name of the hook (its ``__name__`` or a label).
    stage:
        Stage the hook was registered for.
    success:
        True if the hook completed without raising.
    duration_seconds:
        Wall time for this hook invocation.
    error:
        Exception message if ``success is False``.
    timed_out:
        True when the hook exceeded its ``hook_timeout_seconds`` budget.
        The hook's background thread continues running until completion,
        but the pipeline moves on without waiting for it.
    """
    hook_name: str
    stage: str
    success: bool
    duration_seconds: float = 0.0
    error: Optional[str] = None
    timed_out: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hook": self.hook_name,
            "stage": self.stage,
            "success": self.success,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "timed_out": self.timed_out,
        }


# ── Registry ───────────────────────────────────────────────────────────────────

class HookRegistry:
    """
    Thread-safe registry mapping stage names to ordered lists of hooks.

    A single registry instance is typically shared process-wide via
    ``GLOBAL_REGISTRY``.  Tests can instantiate their own registry to avoid
    cross-test pollution.
    """

    def __init__(self) -> None:
        self._hooks: Dict[str, List[StageHook]] = {}
        # Per-stage sequential execution locks: prevent re-entrant / concurrent
        # execution of hooks for the same stage (mirrors Claude Code's
        # ``sequential()`` wrapper pattern).
        self._stage_locks: Dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def register(self, stage: str, fn: StageHook) -> None:
        """Register *fn* as a hook for *stage*."""
        with self._registry_lock:
            if stage not in self._hooks:
                self._hooks[stage] = []
                self._stage_locks[stage] = threading.Lock()
            self._hooks[stage].append(fn)
        log_event(
            LOGGER, 10, "hook_registered",
            f"Hook '{_hook_name(fn)}' registered for stage '{stage}'.",
            stage=stage, hook=_hook_name(fn),
        )

    def unregister(self, stage: str, fn: StageHook) -> None:
        """Remove *fn* from *stage*'s hook list (no-op if not found)."""
        with self._registry_lock:
            hooks = self._hooks.get(stage, [])
            try:
                hooks.remove(fn)
            except ValueError:
                pass

    def clear(self, stage: Optional[str] = None) -> None:
        """Remove all hooks for *stage*, or all hooks when *stage* is None.

        Stage locks are intentionally NOT removed.  ``execute()`` snapshots a
        lock reference *before* releasing ``_registry_lock``, then acquires it
        *after*.  Removing the lock from ``_stage_locks`` between those two
        steps (TOCTOU) would cause a subsequent re-register + execute cycle to
        create a fresh lock, so the two concurrent ``execute()`` calls would
        synchronise on *different* objects — defeating mutual exclusion.  Keeping
        the lock alive eliminates the race at the cost of a small permanent
        entry per stage, which is acceptable (stages are finite and known).
        """
        with self._registry_lock:
            if stage is None:
                self._hooks.clear()
                # _stage_locks deliberately left intact — see docstring.
            else:
                self._hooks.pop(stage, None)
                # _stage_locks[stage] deliberately left intact — see docstring.

    def hooks_for(self, stage: str) -> List[StageHook]:
        """Return a snapshot of hooks registered for *stage*."""
        with self._registry_lock:
            return list(self._hooks.get(stage, []))

    def execute(
        self,
        stage: str,
        context: HookContext,
        *,
        hook_timeout_seconds: Optional[float] = None,
    ) -> List[HookResult]:
        """
        Execute all hooks for *stage* sequentially, exception-isolated.

        Acquires the per-stage lock so concurrent calls for the same stage
        wait rather than interleave.  Hooks for *different* stages can run
        concurrently.

        Parameters
        ----------
        stage:
            Stage name whose hooks to run.
        context:
            ``HookContext`` passed to each hook.
        hook_timeout_seconds:
            Optional per-hook wall-clock budget.  When set and > 0, each hook
            runs in a daemon thread.  If the thread has not completed within
            *hook_timeout_seconds*, the result is marked ``timed_out=True`` and
            ``success=False``, and execution continues with the next hook.
            The timed-out hook's thread continues in the background and will
            complete eventually (Python threads cannot be forcibly killed).

        Returns a list of ``HookResult`` (one per registered hook, in
        registration order).  Never raises.
        """
        with self._registry_lock:
            hooks = list(self._hooks.get(stage, []))
            lock = self._stage_locks.get(stage)
            if lock is None and hooks:
                self._stage_locks[stage] = threading.Lock()
                lock = self._stage_locks[stage]
        if not hooks:
            return []

        use_timeout = hook_timeout_seconds is not None and hook_timeout_seconds > 0

        results: List[HookResult] = []
        with lock:
            for fn in hooks:
                raise_if_cancelled()
                name = _hook_name(fn)
                log_event(
                    LOGGER, 10, "hook_executing",
                    f"Executing hook '{name}' for stage '{stage}'.",
                    stage=stage, hook=name,
                )
                if use_timeout:
                    result = _run_hook_with_timeout(
                        fn, context, name, stage, hook_timeout_seconds,  # type: ignore[arg-type]
                    )
                else:
                    start = time.monotonic()
                    try:
                        fn(context)
                        duration = time.monotonic() - start
                        result = HookResult(
                            hook_name=name, stage=stage,
                            success=True, duration_seconds=duration,
                        )
                        log_event(
                            LOGGER, 20, "hook_completed",
                            f"Hook '{name}' completed in {duration:.3f}s.",
                            stage=stage, hook=name,
                            duration_seconds=round(duration, 3),
                        )
                    except OperationCancelledError:
                        raise
                    except Exception as exc:
                        duration = time.monotonic() - start
                        err_msg = f"{type(exc).__name__}: {exc}"
                        result = HookResult(
                            hook_name=name, stage=stage,
                            success=False, duration_seconds=duration,
                            error=err_msg,
                        )
                        log_exception(
                            LOGGER, "hook_failed",
                            f"Hook '{name}' for stage '{stage}' raised: {err_msg}",
                            stage=stage, hook=name,
                            duration_seconds=round(duration, 3),
                        )
                results.append(result)
        return results


# ── Timeout helper ────────────────────────────────────────────────────────────

def _run_hook_with_timeout(
    fn: StageHook,
    context: HookContext,
    name: str,
    stage: str,
    timeout: float,
) -> HookResult:
    """
    Run *fn* in a daemon thread and wait up to *timeout* seconds.

    If the thread is still alive after *timeout*, returns a ``HookResult``
    with ``timed_out=True`` and ``success=False``.  The thread continues
    running in the background (Python threads cannot be forcibly terminated).

    Exception isolation mirrors the non-timeout path: a raised exception
    produces ``success=False`` with the exception message in ``error``.
    """
    exc_container: List[Optional[BaseException]] = [None]

    def _target() -> None:
        try:
            fn(context)
        except BaseException as exc:  # noqa: BLE001
            # Catch BaseException (not just Exception) so that
            # OperationCancelledError (and any other BaseException subclass
            # that does NOT inherit from Exception) is stored in
            # exc_container and correctly propagated by the caller.
            # Without this, OperationCancelledError escapes the thread
            # silently and the caller sees success=True (false positive).
            exc_container[0] = exc

    thread = threading.Thread(
        target=_target,
        name=f"hook-{name}",
        daemon=True,
    )
    start = time.monotonic()
    thread.start()
    thread.join(timeout=timeout)
    duration = time.monotonic() - start

    # Read exc_container BEFORE the is_alive() check to close a TOCTOU race:
    # the thread may finish (storing OperationCancelledError) in the window
    # between join() returning and is_alive() being evaluated.  Reading first
    # guarantees we see any cancellation signal the thread stored.
    exc = exc_container[0]

    if thread.is_alive():
        # Even in the timed-out branch, propagate cooperative cancellation.
        # If the hook raised OperationCancelledError just before the timeout
        # expired, we must not silently drop it and let the pipeline continue.
        if exc is not None and isinstance(exc, OperationCancelledError):
            raise exc
        log_event(
            LOGGER, 30, "hook_timeout",
            f"Hook '{name}' for stage '{stage}' timed out after {timeout:.1f}s "
            f"— continuing without its result.",
            stage=stage, hook=name,
            timeout_seconds=round(timeout, 3),
        )
        return HookResult(
            hook_name=name,
            stage=stage,
            success=False,
            duration_seconds=duration,
            error=f"Hook timed out after {timeout:.1f}s",
            timed_out=True,
        )

    if exc is not None:
        # OperationCancelledError must propagate — do not convert it to a
        # failed HookResult.  This keeps timed-path behaviour consistent with
        # the no-timeout path (which has an explicit `except OperationCancelledError: raise`).
        if isinstance(exc, OperationCancelledError):
            raise exc
        err_msg = f"{type(exc).__name__}: {exc}"
        # Pass exc directly so logger.error() derives (type, value, __traceback__)
        # from the captured exception rather than calling sys.exc_info() (which
        # returns (None, None, None) here — we are not inside an except block).
        log_exception(
            LOGGER, "hook_failed",
            f"Hook '{name}' for stage '{stage}' raised: {err_msg}",
            exc_info=exc,
            stage=stage, hook=name,
            duration_seconds=round(duration, 3),
        )
        return HookResult(
            hook_name=name,
            stage=stage,
            success=False,
            duration_seconds=duration,
            error=err_msg,
        )

    log_event(
        LOGGER, 20, "hook_completed",
        f"Hook '{name}' completed in {duration:.3f}s.",
        stage=stage, hook=name,
        duration_seconds=round(duration, 3),
    )
    return HookResult(
        hook_name=name,
        stage=stage,
        success=True,
        duration_seconds=duration,
    )


# ── Module-level singleton ────────────────────────────────────────────────────

GLOBAL_REGISTRY = HookRegistry()


# ── Public API (delegates to GLOBAL_REGISTRY) ─────────────────────────────────

def register_stage_hook(stage: str, fn: StageHook) -> None:
    """Register *fn* as a post-stage hook in the global registry."""
    GLOBAL_REGISTRY.register(stage, fn)


def unregister_stage_hook(stage: str, fn: StageHook) -> None:
    """Unregister *fn* from *stage* in the global registry."""
    GLOBAL_REGISTRY.unregister(stage, fn)


def execute_stage_hooks(
    stage: str,
    context: HookContext,
    *,
    registry: Optional[HookRegistry] = None,
    hook_timeout_seconds: Optional[float] = None,
) -> List[HookResult]:
    """
    Execute all post-stage hooks for *stage*.

    Parameters
    ----------
    stage:
        Stage name whose hooks to run.
    context:
        ``HookContext`` passed to each hook.
    registry:
        Optional override registry (defaults to ``GLOBAL_REGISTRY``).
    hook_timeout_seconds:
        Optional per-hook wall-clock budget.  When set and > 0, each hook
        runs in a daemon thread; timed-out hooks produce ``timed_out=True``
        results without blocking the pipeline.

    Returns
    -------
    List[HookResult]
        One entry per registered hook.
    """
    return (registry or GLOBAL_REGISTRY).execute(
        stage, context, hook_timeout_seconds=hook_timeout_seconds
    )


def hook_for(stage: str) -> Callable[[StageHook], StageHook]:
    """
    Decorator that registers a function as a post-stage hook.

    Usage::

        @hook_for("codegen")
        def on_codegen_complete(ctx: HookContext) -> None:
            ...
    """
    def decorator(fn: StageHook) -> StageHook:
        GLOBAL_REGISTRY.register(stage, fn)
        return fn
    return decorator


def clear_hooks(stage: Optional[str] = None) -> None:
    """Remove hooks from the global registry (mainly for tests)."""
    GLOBAL_REGISTRY.clear(stage)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hook_name(fn: Callable) -> str:
    """Return a display name for a hook callable."""
    name = getattr(fn, "__name__", None) or getattr(fn, "__class__", type(fn)).__name__
    module = getattr(fn, "__module__", "") or ""
    if module and not module.startswith("__"):
        short_module = module.split(".")[-1]
        return f"{short_module}.{name}"
    return str(name)
