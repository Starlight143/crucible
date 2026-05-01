"""
crucible/errors.py
===========================
Structured exception hierarchy for the Crucible pipeline.

Inspired by Claude Code's error.ts: instead of string-matching exception
messages throughout the codebase, callers can catch specific exception types
and make branching decisions based on them.

Hierarchy
---------
CrucibleError
├── TransientError         (safe to retry)
│   ├── RateLimitError     (429 / token bucket exceeded)
│   ├── ServiceUnavailableError  (503 / overloaded)
│   ├── NetworkError       (connection reset, timeout)
│   └── LLMTimeoutError    (LLM call wall-clock exceeded)
└── PermanentError         (retry will not help)
    ├── AuthenticationError    (401 / bad API key)
    ├── PermissionDeniedError  (403)
    ├── BudgetExhaustedError   (cost / error budget depleted)
    └── PipelineConfigError    (bad configuration, missing required feature)

Related (defined in their own modules):
    output_validation.OutputValidationError — raised by ValidationResult.raise_on_failure()
    cancellation.OperationCancelledError    — raised at cancellation checkpoints
    context_pressure.ContextWindowCriticalError — raised at 95% context utilisation

Integration
-----------
``classify_exception()`` maps an arbitrary exception to the closest member
of this hierarchy so that ``execute_with_retry()`` in resilience.py can be
driven by isinstance() checks instead of string matching.
"""
from __future__ import annotations

from typing import Optional


# ── Base ──────────────────────────────────────────────────────────────────────

class CrucibleError(Exception):
    """Base class for all Crucible-specific exceptions."""

    def __init__(self, message: str = "", *, cause: Optional[BaseException] = None) -> None:
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


# ── Transient (retryable) ─────────────────────────────────────────────────────

class TransientError(CrucibleError):
    """Errors that may resolve on retry without code or config changes."""


class RateLimitError(TransientError):
    """API rate limit exceeded (HTTP 429 or token-bucket depleted)."""


class ServiceUnavailableError(TransientError):
    """Upstream service temporarily unavailable (HTTP 503, overloaded)."""


class NetworkError(TransientError):
    """Network-level failure: connection reset, refused, or timed out."""


class LLMTimeoutError(TransientError):
    """LLM call exceeded its wall-clock timeout budget."""


# ── Permanent (non-retryable) ─────────────────────────────────────────────────

class PermanentError(CrucibleError):
    """Errors that will not resolve on retry without external intervention."""


class AuthenticationError(PermanentError):
    """Invalid or expired API credentials (HTTP 401)."""


class PermissionDeniedError(PermanentError):
    """Caller lacks permission for the requested operation (HTTP 403)."""


class BudgetExhaustedError(PermanentError):
    """Cost or error budget for the current stage/session is depleted."""


class PipelineConfigError(PermanentError):
    """Pipeline is misconfigured (missing feature, invalid parameter, etc.)."""


# ── Classification ────────────────────────────────────────────────────────────

def classify_exception(exc: BaseException) -> Optional[CrucibleError]:
    """
    Map an arbitrary exception to the nearest ``CrucibleError`` subclass.

    Returns a new wrapped exception, or None if the exception does not match
    any known pattern.

    Parameters
    ----------
    exc:
        The exception to classify.

    Returns
    -------
    Optional[CrucibleError]
        A ``CrucibleError`` whose ``__cause__`` is *exc*, or None if
        classification fails.

    Usage::

        try:
            crew.kickoff()
        except Exception as raw_exc:
            typed = classify_exception(raw_exc) or raw_exc
            if isinstance(typed, TransientError):
                retry_later()
    """
    if isinstance(exc, CrucibleError):
        return exc  # already classified

    from crucible.resilience import (  # local import to avoid circular
        is_transient_retryable_error, is_context_length_error,
    )

    text = (str(exc) + " " + type(exc).__name__).lower()

    # Authentication
    if "401" in text or "authentication" in text or "invalid api key" in text:
        return AuthenticationError(str(exc), cause=exc)

    # Permission
    if "403" in text or "permission" in text or "forbidden" in text:
        return PermissionDeniedError(str(exc), cause=exc)

    # Rate limit
    if "429" in text or "rate limit" in text or "ratelimit" in text:
        return RateLimitError(str(exc), cause=exc)

    # Service unavailable
    if "503" in text or "overloaded" in text or "service unavailable" in text:
        return ServiceUnavailableError(str(exc), cause=exc)

    # Context length exceeded — permanent: retrying the identical prompt will
    # not succeed; the caller must reduce context before retrying.
    if is_context_length_error(exc):
        return PipelineConfigError(str(exc), cause=exc)

    # Transient (network/timeout) — delegate to existing classifier
    if is_transient_retryable_error(exc):
        name = type(exc).__name__.lower()
        if "timeout" in name or "timed out" in text:
            return LLMTimeoutError(str(exc), cause=exc)
        return NetworkError(str(exc), cause=exc)

    return None
