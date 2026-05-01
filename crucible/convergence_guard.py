"""
crucible/convergence_guard.py
======================================
Loop convergence guard for pipeline feedback loops.

Inspired by Claude Code's bounded turn-loop pattern: every loop that can
interact with an LLM must have both an iteration cap AND a wall-clock
timeout so that a stuck model response never causes the pipeline to hang
indefinitely.

The existing `section_05` selective-rerun loop has per-iteration guards
(``rerun_attempt >= max_reruns``) but no **total elapsed timeout**.  If each
LLM call takes 90 seconds and ``max_reruns=20``, the loop can legitimately
run for 30+ minutes.  A timeout cap lets operators bound worst-case latency.

Additionally, a **signature-based stale-detection** mechanism flags when the
loop is cycling through identical states — a sign that the feedback mechanism
is not converging — so the guard can surface a warning before hard-stopping.

Key design
----------
* Zero-dependency: stdlib only, integrates with ``runtime_logging``.
* Additive: wrap any existing loop with ``with LoopConvergenceGuard(...) as g:``
  without restructuring the loop body.
* ``ConvergenceError`` is raised on hard stop; ``StaleLoopWarning`` on soft
  stale-detection (configurable: warn-only or raise).
* All thresholds env-var configurable so operators can tune without code edits.
* Thread-safe: each ``LoopConvergenceGuard`` instance tracks its own state.

Usage::

    from crucible.convergence_guard import LoopConvergenceGuard

    with LoopConvergenceGuard(
        name="selective_rerun",
        max_iterations=20,
        timeout_seconds=1800,           # 30 min hard cap
        stale_threshold=3,              # warn after 3 identical signatures
    ) as guard:
        while True:
            guard.tick(signature=f"{rerun_attempt}:{len(agents_to_rerun)}")
            # ... loop body ...
            if done:
                break
"""
from __future__ import annotations

import hashlib
import os
import time
import warnings
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]

# Optional: import OperationCancelledError so __exit__ can classify cooperative
# cancellation as expected (not a WARNING-level guard failure).  Imported here
# (not lazily) so the symbol is resolvable via globals().get() at __exit__ time.
# Wrapped in try/except to keep convergence_guard usable even if cancellation
# module is absent (e.g. partial installations / older test fixtures).
try:
    if __package__ == "crucible":
        from .cancellation import OperationCancelledError  # noqa: F401
    else:  # pragma: no cover
        from cancellation import OperationCancelledError  # type: ignore[no-redef]  # noqa: F401
except ImportError:  # pragma: no cover
    OperationCancelledError = None  # type: ignore[assignment]

LOGGER = get_logger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_MAX_ITERATIONS = 50
_DEFAULT_TIMEOUT_SECONDS = 3600.0   # 1 hour hard cap
_DEFAULT_STALE_THRESHOLD = 5        # warn after N identical signatures
_DEFAULT_STALE_RAISES = False       # warn-only by default


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name, "")
        # Allow 0 — callers use 0 to disable caps (e.g. CONVERGENCE_MAX_ITERATIONS=0).
        return max(0, int(v)) if v.strip() else default
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "")
        return float(v) if v.strip() else default
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


# ── Exceptions ────────────────────────────────────────────────────────────────

class ConvergenceError(RuntimeError):
    """
    Raised when a loop exceeds its iteration cap or elapsed timeout.

    Attributes
    ----------
    name:
        Loop name from the ``LoopConvergenceGuard`` constructor.
    iterations:
        Number of iterations completed before stop.
    elapsed_seconds:
        Elapsed wall time before stop.
    reason:
        ``"max_iterations"`` or ``"timeout"``.
    """

    def __init__(
        self,
        name: str,
        *,
        iterations: int,
        elapsed_seconds: float,
        reason: str,
    ) -> None:
        self.name = name
        self.iterations = iterations
        self.elapsed_seconds = elapsed_seconds
        self.reason = reason
        super().__init__(
            f"Loop '{name}' did not converge: {reason} "
            f"(iterations={iterations}, elapsed={elapsed_seconds:.1f}s)."
        )


class StaleLoopWarning(UserWarning):
    """Emitted when the same loop signature repeats above the stale threshold."""


# ── Statistics snapshot ───────────────────────────────────────────────────────

@dataclass
class ConvergenceStats:
    """Runtime statistics from a LoopConvergenceGuard instance."""
    name: str
    iterations: int = 0
    elapsed_seconds: float = 0.0
    unique_signatures: int = 0
    most_common_signature: Optional[str] = None
    most_common_count: int = 0
    stopped_by: Optional[str] = None   # "max_iterations" | "timeout" | "stale" | None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "iterations": self.iterations,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "unique_signatures": self.unique_signatures,
            "most_common_signature": self.most_common_signature,
            "most_common_count": self.most_common_count,
            "stopped_by": self.stopped_by,
        }


# ── Core guard ────────────────────────────────────────────────────────────────

class LoopConvergenceGuard:
    """
    Context manager that enforces convergence bounds on a feedback loop.

    Parameters
    ----------
    name:
        Descriptive identifier for the loop (used in log messages and errors).
    max_iterations:
        Hard cap on the number of ``tick()`` calls.  0 disables iteration cap.
        Defaults to ``CONVERGENCE_MAX_ITERATIONS`` env var → 50.
    timeout_seconds:
        Wall-clock timeout in seconds.  0.0 disables timeout.
        Defaults to ``CONVERGENCE_TIMEOUT_SECONDS`` env var → 3600.
    stale_threshold:
        Emit ``StaleLoopWarning`` when the same signature is seen this many
        times consecutively.  0 disables stale detection.
        Defaults to ``CONVERGENCE_STALE_THRESHOLD`` env var → 5.
    stale_raises:
        If True, stale detection raises ``ConvergenceError`` instead of
        emitting a warning.
        Defaults to ``CONVERGENCE_STALE_RAISES`` env var → False.
    """

    def __init__(
        self,
        name: str,
        *,
        max_iterations: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        stale_threshold: Optional[int] = None,
        stale_raises: Optional[bool] = None,
    ) -> None:
        self.name = name
        self.max_iterations: int = int(
            max_iterations
            if max_iterations is not None
            else _env_int("CONVERGENCE_MAX_ITERATIONS", _DEFAULT_MAX_ITERATIONS)
        )
        self.timeout_seconds: float = float(
            timeout_seconds
            if timeout_seconds is not None
            else _env_float("CONVERGENCE_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
        )
        self.stale_threshold: int = int(
            stale_threshold
            if stale_threshold is not None
            else _env_int("CONVERGENCE_STALE_THRESHOLD", _DEFAULT_STALE_THRESHOLD)
        )
        self.stale_raises: bool = bool(
            stale_raises
            if stale_raises is not None
            else _env_bool("CONVERGENCE_STALE_RAISES", _DEFAULT_STALE_RAISES)
        )

        # Runtime state (reset on __enter__)
        self._iterations: int = 0
        self._start: Optional[float] = None
        self._sig_counter: Counter[str] = Counter()
        self._last_sig: Optional[str] = None
        self._consecutive_same: int = 0
        self._stopped_by: Optional[str] = None
        self._stale_warned: bool = False

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "LoopConvergenceGuard":
        self._iterations = 0
        self._start = time.monotonic()
        self._sig_counter = Counter()
        self._last_sig = None
        self._consecutive_same = 0
        self._stopped_by = None
        self._stale_warned = False
        log_event(
            LOGGER, 20, "convergence_guard_started",
            f"Loop '{self.name}' started. "
            f"max_iter={self.max_iterations or '∞'}  "
            f"timeout={self.timeout_seconds or '∞'}s  "
            f"stale_threshold={self.stale_threshold or 'off'}",
            loop=self.name,
            max_iterations=self.max_iterations,
            timeout_seconds=self.timeout_seconds,
        )
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        elapsed = 0.0 if self._start is None else time.monotonic() - self._start
        stats = self.stats()
        # Guard with isinstance(exc_type, type) before issubclass: Python always
        # passes a class (or None) here, but mocking frameworks or C-extension hooks
        # may pass a non-class value that would cause issubclass() to raise TypeError,
        # swallowing the original exception with a confusing secondary error.
        # Cooperative cancellation (OperationCancelledError) is also expected — a
        # caller-driven stop is not a guard failure and must not pollute logs /
        # alerts with WARNING-level "convergence_guard_exited" events.  Resolved
        # lazily via globals().get() to avoid a circular import at module load.
        _expected_cls: Any = globals().get("OperationCancelledError")
        _expected: tuple = (ConvergenceError,)
        if isinstance(_expected_cls, type):
            _expected = _expected + (_expected_cls,)
        _is_unexpected = (
            exc_type is not None
            and isinstance(exc_type, type)
            and not issubclass(exc_type, _expected)
        )
        log_event(
            LOGGER,
            30 if _is_unexpected else 20,
            "convergence_guard_exited",
            f"Loop '{self.name}' exited after {self._iterations} iterations "
            f"({elapsed:.1f}s). stopped_by={self._stopped_by or 'normal'}",
            **stats.to_dict(),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def tick(self, *, signature: Optional[str] = None) -> None:
        """
        Advance the guard by one loop iteration.

        Call once at the **start** of each loop iteration (before the body).
        Raises ``ConvergenceError`` if any convergence bound is exceeded.

        Parameters
        ----------
        signature:
            An optional hashable string that encodes the loop's current state
            (e.g. a tuple of agent names, attempt count, key results).
            Used for stale-detection: if the same signature repeats
            ``stale_threshold`` times consecutively, a warning is emitted.
        """
        self._iterations += 1
        if self._start is None:
            raise RuntimeError(
                f"LoopConvergenceGuard '{self.name}': tick() called before __enter__. "
                "Use the guard as a context manager: 'with guard: ...'."
            )
        elapsed = time.monotonic() - self._start

        # ── Hard stop: iteration cap ─────────────────────────────────────────
        if self.max_iterations > 0 and self._iterations > self.max_iterations:
            self._stopped_by = "max_iterations"
            log_event(
                LOGGER, 40, "convergence_guard_max_iterations",
                f"Loop '{self.name}' exceeded max_iterations={self.max_iterations}.",
                loop=self.name, iterations=self._iterations, elapsed_seconds=round(elapsed, 2),
            )
            raise ConvergenceError(
                self.name,
                iterations=self._iterations,
                elapsed_seconds=elapsed,
                reason="max_iterations",
            )

        # ── Hard stop: timeout ───────────────────────────────────────────────
        if self.timeout_seconds > 0 and elapsed >= self.timeout_seconds:
            self._stopped_by = "timeout"
            log_event(
                LOGGER, 40, "convergence_guard_timeout",
                f"Loop '{self.name}' exceeded timeout={self.timeout_seconds}s.",
                loop=self.name, iterations=self._iterations, elapsed_seconds=round(elapsed, 2),
            )
            raise ConvergenceError(
                self.name,
                iterations=self._iterations,
                elapsed_seconds=elapsed,
                reason="timeout",
            )

        # ── Soft / hard stop: stale signature ────────────────────────────────
        if signature is not None and self.stale_threshold > 0:
            sig_key = _hash_signature(signature)
            self._sig_counter[sig_key] += 1
            if sig_key == self._last_sig:
                self._consecutive_same += 1
            else:
                self._consecutive_same = 1
                self._last_sig = sig_key
                self._stale_warned = False  # new signature resets the stale-warn gate

            if self._consecutive_same >= self.stale_threshold and not self._stale_warned:
                msg = (
                    f"Loop '{self.name}' appears stale: same signature "
                    f"repeated {self._consecutive_same} times consecutively "
                    f"(iteration {self._iterations}, elapsed {elapsed:.1f}s)."
                )
                log_event(
                    LOGGER, 30, "convergence_guard_stale",
                    msg,
                    loop=self.name,
                    iterations=self._iterations,
                    consecutive_same=self._consecutive_same,
                    elapsed_seconds=round(elapsed, 2),
                )
                if self.stale_raises:
                    self._stopped_by = "stale"
                    raise ConvergenceError(
                        self.name,
                        iterations=self._iterations,
                        elapsed_seconds=elapsed,
                        reason="stale_signature",
                    )
                self._stale_warned = True  # suppress repeat warnings for same signature
                warnings.warn(msg, StaleLoopWarning, stacklevel=2)

        # ── Progress log (every 5 iterations) ────────────────────────────────
        if self._iterations % 5 == 0:
            log_event(
                LOGGER, 10, "convergence_guard_tick",
                f"Loop '{self.name}': iteration {self._iterations} "
                f"({elapsed:.1f}s elapsed)",
                loop=self.name, iterations=self._iterations,
                elapsed_seconds=round(elapsed, 2),
            )

    @property
    def iterations(self) -> int:
        """Number of ``tick()`` calls so far."""
        return self._iterations

    @property
    def elapsed_seconds(self) -> float:
        """Elapsed seconds since ``__enter__``; 0.0 if not yet entered.

        ``_start`` is initialised to 0.0 in ``__init__`` and set to
        ``time.monotonic()`` on ``__enter__``.  Calling this property before
        ``__enter__`` would otherwise return a spuriously large value
        (time.monotonic() is seconds since an arbitrary epoch, typically
        machine boot, not since object creation).
        """
        if self._start is None:
            return 0.0
        return time.monotonic() - self._start

    def stats(self) -> ConvergenceStats:
        """Return a snapshot of current convergence statistics."""
        # Mirror the same guard used by elapsed_seconds: when _start is still
        # the __init__ sentinel (0.0) the guard prevents a spuriously large
        # elapsed value (time since machine boot) from appearing in stats.
        elapsed = 0.0 if self._start is None else time.monotonic() - self._start
        most_common = self._sig_counter.most_common(1)
        mc_sig, mc_count = (most_common[0] if most_common else (None, 0))
        return ConvergenceStats(
            name=self.name,
            iterations=self._iterations,
            elapsed_seconds=elapsed,
            unique_signatures=len(self._sig_counter),
            most_common_signature=mc_sig,
            most_common_count=mc_count,
            stopped_by=self._stopped_by,
        )


# ── Helper ────────────────────────────────────────────────────────────────────

def _hash_signature(sig: str) -> str:
    """Return a short stable hash of *sig* for signature tracking."""
    return hashlib.md5(sig.encode("utf-8", errors="replace")).hexdigest()[:12]
