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
import os
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

    Notes
    -----
    v1.1.2 (audit fix G1-2): inputs are ``.strip()``-ed before the truthiness
    check, so whitespace-only strings (e.g. ``"   "`` from a misconfigured
    CI / Settings UI / blank-padded env value) trigger the fresh-UUID
    fallback rather than silently pinning a three-space run_id that fails to
    match the WebUI's ``_runs[run_id]`` dict.
    """
    candidate = (run_id or "").strip() if isinstance(run_id, str) else (run_id or "")
    rid = candidate or str(uuid.uuid4())
    _RUN_ID.set(rid)
    update_log_context(run_id=rid)
    return rid


def init_run_correlation_from_env(env_var: str = "CRUCIBLE_RUN_ID") -> str:
    """Bootstrap helper: bind the run-correlation ContextVar from an env var.

    Reads *env_var* (default ``CRUCIBLE_RUN_ID``), strips it, and passes
    it through :func:`set_run_id` so whitespace-only values fall back to
    a fresh UUID4 rather than silently pinning a blank-looking run_id.
    Never raises — correlation-id binding must not abort the pipeline boot.

    v1.1.9 (L1): factored out of the three identical try/except blocks at
    ``crucible/__main__.py``, ``run_crucible.py`` and
    ``run_crucible_enhanced.py:main()`` so they stay in lockstep.  Each
    entry point now calls this helper exactly once at process start.
    """
    try:
        return set_run_id((os.environ.get(env_var) or "").strip() or None)
    except Exception:
        return ""


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

    Notes
    -----
    v1.1.2 (audit fix G1-2): mirrors ``set_run_id``'s whitespace-stripping
    so a blank-padded explicit ``run_id`` is treated as "unset" and a fresh
    UUID is generated instead.
    """
    candidate = (run_id or "").strip() if isinstance(run_id, str) else (run_id or "")
    rid = candidate or str(uuid.uuid4())
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
