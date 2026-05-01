"""
crucible/error_budget.py
================================
Structured error budget tracking with JSONL audit log.

Inspired by Claude Code's bounded-error approach: every pipeline stage has an
"error budget" — a maximum number of tolerated failures before the stage is
considered unhealthy and raises ``BudgetExhaustedError``.  Every error event
is appended to an append-only JSONL audit log for post-mortem analysis.

Key design
----------
* ``ErrorBudget`` — per-stage budget object tracking consumed / remaining.
* ``BudgetExhaustedError`` — raised when the budget is depleted.
* ``ErrorBudgetRegistry`` — module-level registry mapping stage names to budgets.
* ``ErrorAuditLog`` — JSONL append-only writer (thread-safe).
* ``record_error(stage, exc)`` — records to audit log AND checks stage budget.

Usage::

    from crucible.error_budget import record_error, configure_budget

    configure_budget("analysis_crew", max_errors=5)

    try:
        result = run_analysis()
    except Exception as exc:
        record_error("analysis_crew", exc, run_dir="/path/to/run")
        raise
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
    from .errors import BudgetExhaustedError as _CanonicalBudgetExhaustedError
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]
    from errors import BudgetExhaustedError as _CanonicalBudgetExhaustedError  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

_DEFAULT_MAX_ERRORS = 10
_DEFAULT_AUDIT_FILENAME = "error_audit.jsonl"


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExhaustedError(_CanonicalBudgetExhaustedError):
    """
    Raised when a stage's error budget is fully consumed.

    Inherits from ``errors.BudgetExhaustedError`` (a ``PermanentError``) so
    that callers catching either import path see the same class hierarchy.

    Attributes
    ----------
    stage:
        The stage that exhausted its budget.
    consumed:
        Number of errors recorded (equals max_errors at the moment of raise).
    max_errors:
        The budget limit that was reached.
    """

    def __init__(self, stage: str, *, consumed: int, max_errors: int) -> None:
        self.stage = stage
        self.consumed = consumed
        self.max_errors = max_errors
        # Call Exception.__init__ directly to set the message; skip
        # _CanonicalBudgetExhaustedError.__init__ which takes no args.
        Exception.__init__(
            self,
            f"Stage '{stage}' error budget exhausted: "
            f"{consumed}/{max_errors} errors recorded.",
        )


# ── ErrorBudget ───────────────────────────────────────────────────────────────

@dataclass
class ErrorBudget:
    """
    Tracks the error budget for a single pipeline stage.

    Thread-safe: all mutations hold ``_lock``.
    """

    stage: str
    max_errors: int = _DEFAULT_MAX_ERRORS
    # compare=False: exclude mutable state from dataclass-generated __eq__
    # so two budgets are equal only when stage + max_errors match.
    _consumed: int = field(default=0, init=False, repr=False, compare=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    @property
    def consumed(self) -> int:
        with self._lock:
            return self._consumed

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_errors - self._consumed)

    @property
    def is_exhausted(self) -> bool:
        with self._lock:
            return self._consumed >= self.max_errors

    def record(self) -> int:
        """
        Increment the consumed count by one.

        Returns the new consumed count.
        Raises ``BudgetExhaustedError`` when the budget is at or over capacity.
        """
        should_raise = False
        with self._lock:
            self._consumed += 1
            consumed = self._consumed
            max_e = self.max_errors
            if consumed >= max_e:
                should_raise = True

        if should_raise:
            log_event(
                LOGGER, 40, "error_budget_exhausted",
                f"Stage '{self.stage}' error budget exhausted ({consumed}/{max_e}).",
                stage=self.stage, consumed=consumed, max_errors=max_e,
            )
            raise BudgetExhaustedError(self.stage, consumed=consumed, max_errors=max_e)
        log_event(
            LOGGER, 30, "error_budget_consumed",
            f"Stage '{self.stage}' error recorded "
            f"({consumed}/{max_e}, remaining: {max_e - consumed}).",
            stage=self.stage, consumed=consumed, max_errors=max_e,
        )
        return consumed

    def reset(self) -> None:
        """Reset the consumed counter to zero (e.g. between runs)."""
        with self._lock:
            self._consumed = 0

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "stage": self.stage,
                "max_errors": self.max_errors,
                "consumed": self._consumed,
                "remaining": max(0, self.max_errors - self._consumed),
                "is_exhausted": self._consumed >= self.max_errors,
            }


# ── ErrorBudgetRegistry ───────────────────────────────────────────────────────

class ErrorBudgetRegistry:
    """
    Process-wide registry mapping stage names to their ``ErrorBudget`` objects.
    Thread-safe via an internal lock.
    """

    def __init__(self) -> None:
        self._budgets: Dict[str, ErrorBudget] = {}
        self._lock = threading.Lock()

    def configure(
        self, stage: str, max_errors: int = _DEFAULT_MAX_ERRORS
    ) -> ErrorBudget:
        """
        Register or update the budget for *stage*.

        Resets the consumed counter when ``max_errors`` changes.
        """
        with self._lock:
            existing = self._budgets.get(stage)
            if existing is not None:
                clamped = max(1, int(max_errors))
                if existing.max_errors != clamped:
                    # Hold the budget's own lock while mutating max_errors so
                    # that concurrent calls to record() — which read max_errors
                    # under the budget lock — never observe a torn or
                    # inconsistent (max_errors, _consumed) pair.
                    # We perform the reset inline (set _consumed = 0) rather
                    # than calling existing.reset() to avoid re-acquiring the
                    # budget lock recursively.
                    with existing._lock:
                        existing.max_errors = clamped
                        existing._consumed = 0
                return existing
            budget = ErrorBudget(stage=stage, max_errors=max(1, int(max_errors)))
            self._budgets[stage] = budget
            return budget

    def get(self, stage: str) -> Optional[ErrorBudget]:
        """Return the budget for *stage*, or None if not configured."""
        with self._lock:
            return self._budgets.get(stage)

    def get_or_create(self, stage: str) -> ErrorBudget:
        """Return the budget for *stage*, creating one with defaults if absent."""
        with self._lock:
            if stage not in self._budgets:
                self._budgets[stage] = ErrorBudget(stage=stage)
            return self._budgets[stage]

    def reset_all(self) -> None:
        """Reset all budget counters (useful between test runs)."""
        # Snapshot under the registry lock, then release it before calling
        # budget.reset() (which acquires each budget's own lock) — avoids
        # holding the registry lock for the full duration of all resets and
        # eliminates spurious contention on get_or_create / configure callers.
        with self._lock:
            budgets = list(self._budgets.values())
        for budget in budgets:
            budget.reset()

    def clear(self) -> None:
        """Remove all registered budgets."""
        with self._lock:
            self._budgets.clear()

    def snapshot(self) -> List[Dict[str, Any]]:
        """Return a list of budget snapshots for all registered stages."""
        with self._lock:
            return [b.to_dict() for b in self._budgets.values()]


# ── ErrorAuditLog ─────────────────────────────────────────────────────────────

class ErrorAuditLog:
    """
    Append-only JSONL audit log for pipeline errors.

    Each call to ``write`` appends one JSON line.  Thread-safe via an
    internal per-instance lock.

    Parameters
    ----------
    log_dir:
        Directory where the audit file is written.  Created automatically.
    filename:
        Override the default ``error_audit.jsonl`` filename.
    """

    def __init__(
        self,
        log_dir: str,
        *,
        filename: str = _DEFAULT_AUDIT_FILENAME,
    ) -> None:
        self._log_dir = str(log_dir)
        self._log_path = os.path.join(log_dir, filename)
        self._lock = threading.Lock()

    def write(
        self,
        stage: str,
        error_type: str,
        message: str,
        *,
        run_dir: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one error record to the audit log."""
        record: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "error_type": error_type,
            "message": message,
            "run_dir": run_dir,
        }
        if extra:
            record.update(extra)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                os.makedirs(self._log_dir, exist_ok=True)
                with open(self._log_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                LOGGER.warning(
                    "ErrorAuditLog: failed to write audit record: %s", exc
                )

    def read_all(self) -> List[Dict[str, Any]]:
        """Return all audit records (oldest first)."""
        records: List[Dict[str, Any]] = []
        with self._lock:
            if not os.path.isfile(self._log_path):
                return records
            try:
                with open(self._log_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict):
                                records.append(obj)
                        except json.JSONDecodeError:
                            pass
            except OSError:
                pass
        return records


# ── Module-level singletons ───────────────────────────────────────────────────

_GLOBAL_REGISTRY = ErrorBudgetRegistry()
_GLOBAL_AUDIT_LOG: Optional[ErrorAuditLog] = None
_GLOBAL_AUDIT_LOCK = threading.Lock()


def _get_or_init_audit_log(run_dir: str = "") -> Optional[ErrorAuditLog]:
    global _GLOBAL_AUDIT_LOG
    with _GLOBAL_AUDIT_LOCK:
        if run_dir:
            if _GLOBAL_AUDIT_LOG is None or _GLOBAL_AUDIT_LOG._log_dir != run_dir:
                _GLOBAL_AUDIT_LOG = ErrorAuditLog(run_dir)
        return _GLOBAL_AUDIT_LOG


# ── Public API ────────────────────────────────────────────────────────────────

def configure_budget(
    stage: str, max_errors: int = _DEFAULT_MAX_ERRORS
) -> ErrorBudget:
    """Configure (or update) the error budget for *stage* in the global registry."""
    return _GLOBAL_REGISTRY.configure(stage, max_errors=max_errors)


def record_error(
    stage: str,
    exc: Optional[BaseException] = None,
    *,
    run_dir: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Record one error against *stage*'s budget and append to the audit log.

    Parameters
    ----------
    stage:
        Pipeline stage name.
    exc:
        The exception that occurred (used for type and message).  May be None.
    run_dir:
        Path to the current run's output directory (written to audit log).
    extra:
        Additional key-value fields for the audit record.

    Returns
    -------
    int
        Updated consumed error count for the stage.

    Raises
    ------
    BudgetExhaustedError
        When the stage's budget is now fully consumed.
    """
    budget = _GLOBAL_REGISTRY.get_or_create(stage)
    error_type = type(exc).__name__ if exc is not None else "UnknownError"
    message = str(exc) if exc is not None else ""

    audit = _get_or_init_audit_log(run_dir)
    if audit is not None:
        audit.write(stage, error_type, message, run_dir=run_dir, extra=extra)
    else:
        LOGGER.debug(
            "record_error: no audit log available for stage '%s' "
            "(run_dir not set); audit record dropped.",
            stage,
        )

    return budget.record()


def get_budget(stage: str) -> Optional[ErrorBudget]:
    """Return the global budget for *stage*, or None if not configured."""
    return _GLOBAL_REGISTRY.get(stage)


def reset_all_budgets() -> None:
    """Reset all stage budget counters (primarily for testing)."""
    _GLOBAL_REGISTRY.reset_all()


def clear_budgets() -> None:
    """Remove all stage budgets from the global registry (primarily for testing)."""
    global _GLOBAL_AUDIT_LOG
    _GLOBAL_REGISTRY.clear()
    with _GLOBAL_AUDIT_LOCK:
        _GLOBAL_AUDIT_LOG = None
