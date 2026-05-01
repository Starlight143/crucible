"""
crucible/cancellation.py
================================
Cooperative cancellation for the analysis pipeline.

Inspired by Claude Code's AbortController / AbortSignal pattern: instead of
hard-killing threads (impossible in Python), each pipeline stage checks a
shared ``CancellationToken`` at well-defined checkpoints.  Cancellation is
cooperative — the token is checked at the start of every hook and feature
execution loop iteration.

Design
------
* ``CancellationToken`` — thread-safe event wrapper; ``cancel()`` is
  idempotent; ``is_cancelled`` is a cheap property.
* ``current_token()`` — retrieve the active token from a contextvars.ContextVar
  so callers don't pass the token through every function signature.
* ``cancellation_scope()`` — context manager that creates a fresh token,
  installs it as the current token, and ensures cleanup on exit.
* ``raise_if_cancelled()`` — helper that raises ``OperationCancelledError``
  if the current token is cancelled; call this at checkpoints.
* Integrates with hooks.py and feature_registry.py via check points.

Usage::

    from crucible.cancellation import cancellation_scope, raise_if_cancelled

    with cancellation_scope() as token:
        # In another thread:  token.cancel()
        for stage in stages:
            raise_if_cancelled()    # checkpoint
            run_stage(stage)
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
from typing import Iterator, Optional

if __package__ == "crucible":
    from .runtime_logging import get_logger, log_event
else:  # pragma: no cover
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

_CURRENT_TOKEN: contextvars.ContextVar[Optional["CancellationToken"]] = \
    contextvars.ContextVar("crucible_cancel_token", default=None)


class OperationCancelledError(RuntimeError):
    """Raised at a cancellation checkpoint when the active token is cancelled."""

    def __init__(self, message: str = "Operation was cancelled.") -> None:
        super().__init__(message)


class CancellationToken:
    """
    Thread-safe cooperative cancellation token.

    Call ``cancel()`` from any thread to signal cancellation.
    All threads that call ``raise_if_cancelled()`` or check ``is_cancelled``
    will observe the change immediately (threading.Event guarantees visibility).

    The token is one-shot: once cancelled it cannot be reset.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._cancel_lock = threading.Lock()

    def cancel(self, reason: str = "") -> None:
        """
        Signal cancellation.  Idempotent — subsequent calls are no-ops.

        Parameters
        ----------
        reason:
            Optional human-readable reason (logged once on first cancel).
        """
        with self._cancel_lock:
            if self._event.is_set():
                return
            self._event.set()
        # Log outside the lock to avoid holding it during I/O.
        log_event(
            LOGGER, 30, "cancellation_requested",
            f"Cancellation requested: {reason or '(no reason given)'}",
            reason=reason,
        )

    @property
    def is_cancelled(self) -> bool:
        """True if ``cancel()`` has been called."""
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until cancelled or *timeout* seconds elapse.  Returns is_cancelled."""
        return self._event.wait(timeout=timeout)

    def raise_if_cancelled(self) -> None:
        """
        Raise ``OperationCancelledError`` if this token is cancelled.

        Call this at safe checkpoints (between stages, between hooks) to
        allow graceful cooperative shutdown.
        """
        if self._event.is_set():
            raise OperationCancelledError()


def current_token() -> Optional[CancellationToken]:
    """Return the ``CancellationToken`` active in the current context, or None."""
    return _CURRENT_TOKEN.get(None)


def raise_if_cancelled() -> None:
    """
    Raise ``OperationCancelledError`` if the current context has an active
    cancelled token.  No-op if no token is set or token is not cancelled.

    Call this at stage/hook checkpoints.
    """
    token = _CURRENT_TOKEN.get(None)
    if token is not None:
        token.raise_if_cancelled()


@contextlib.contextmanager
def cancellation_scope(
    token: Optional[CancellationToken] = None,
) -> Iterator[CancellationToken]:
    """
    Context manager that installs a ``CancellationToken`` as the active token
    for the current context.

    Parameters
    ----------
    token:
        Explicit token to install.  If None, a fresh ``CancellationToken``
        is created.

    Yields
    ------
    CancellationToken
        The active token.  Call ``.cancel()`` on it to trigger cooperative
        cancellation in code running inside this scope.

    Example::

        with cancellation_scope() as token:
            threading.Timer(30.0, token.cancel).start()  # auto-cancel after 30s
            run_pipeline()
    """
    active = token or CancellationToken()
    cv_token = _CURRENT_TOKEN.set(active)
    try:
        yield active
    finally:
        _CURRENT_TOKEN.reset(cv_token)
