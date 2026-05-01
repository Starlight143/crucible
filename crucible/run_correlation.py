"""
crucible/run_correlation.py
====================================
Run-scoped correlation ID propagation.

Borrowed from Claude Code's requestLogID pattern: every pipeline execution
gets a unique run_id that threads through all telemetry events, log lines,
and hook contexts — making it trivial to reconstruct one full execution's
trace from a JSONL log file.

Key design
----------
* Uses contextvars.ContextVar so the run_id is isolated per-thread/task
  with no explicit passing between callers.
* ``run_context()`` context manager handles set / reset automatically
  (reset via token, not re-set, so nested contexts restore the outer id).
* Also calls ``update_log_context(run_id=...)`` so every log line from
  runtime_logging automatically carries the run_id without extra wiring.

Usage::

    from crucible.run_correlation import run_context, get_run_id

    with run_context() as run_id:          # auto-generates UUID
        emit("pipeline.start")             # TelemetryEvent.run_id = run_id
        LOGGER.info("starting")            # log line includes run_id=run_id
        ...
"""
from __future__ import annotations

import contextlib
import uuid
from typing import Iterator, Optional
import contextvars

if __package__ == "crucible":
    from .runtime_logging import update_log_context, clear_log_context
else:  # pragma: no cover
    from runtime_logging import update_log_context, clear_log_context  # type: ignore[no-redef]

_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("crucible_run_id", default="")


def get_run_id() -> str:
    """Return the active run_id for the current context, or '' if none set."""
    return _RUN_ID.get("")


def set_run_id(run_id: Optional[str] = None) -> str:
    """
    Set the run_id for the current context.

    Parameters
    ----------
    run_id:
        Explicit run_id.  If None, a fresh UUID4 is generated.

    Returns
    -------
    str
        The run_id that was set.

    Warning
    -------
    This does NOT return a reset token, so the change persists for the rest
    of the current context.  Prefer ``run_context()`` for scoped usage.
    """
    rid = run_id or str(uuid.uuid4())
    _RUN_ID.set(rid)
    update_log_context(run_id=rid)
    return rid


@contextlib.contextmanager
def run_context(run_id: Optional[str] = None) -> Iterator[str]:
    """
    Context manager that sets a run_id for the duration of the block and
    restores the previous value on exit.

    Parameters
    ----------
    run_id:
        Explicit run_id.  If None, a fresh UUID4 is generated.

    Yields
    ------
    str
        The active run_id for this context.

    Example::

        with run_context() as rid:
            emit("pipeline.started", payload={"run_id": rid})
    """
    rid = run_id or str(uuid.uuid4())
    token = _RUN_ID.set(rid)
    update_log_context(run_id=rid)
    try:
        yield rid
    finally:
        _RUN_ID.reset(token)
        # Restore prior run_id in log context ('' means clear it)
        prior = _RUN_ID.get("")
        if prior:
            update_log_context(run_id=prior)
        else:
            clear_log_context("run_id")
