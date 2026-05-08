from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Type

if __package__ == "crucible":
    from . import _env
    from .cancellation import OperationCancelledError as _OperationCancelledError
    from .runtime_logging import get_logger, log_event
else:  # pragma: no cover - direct script fallback
    import _env  # type: ignore[no-redef]
    from cancellation import (  # type: ignore[no-redef]
        OperationCancelledError as _OperationCancelledError,
    )
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# ---------------------------------------------------------------------------
# Retryable-error callback hooks
# ---------------------------------------------------------------------------
# External modules can register callables here.
# They are called with the exception whenever ``is_transient_retryable_error``
# returns True — i.e. a genuine transient 429 / rate-limit / network error.
_RETRYABLE_ERROR_HOOKS: list[Callable[[BaseException], None]] = []
# Guards both _RETRYABLE_ERROR_HOOKS reads and writes to prevent
# "list changed size during iteration" under concurrent register/fire.
_RETRYABLE_ERROR_HOOKS_LOCK = threading.Lock()


def register_retryable_error_hook(fn: Callable[[BaseException], None]) -> None:
    """Register a callable that will be invoked on every transient retryable error."""
    with _RETRYABLE_ERROR_HOOKS_LOCK:
        if fn not in _RETRYABLE_ERROR_HOOKS:
            _RETRYABLE_ERROR_HOOKS.append(fn)


def _fire_retryable_error_hooks(exc: BaseException) -> None:
    # Snapshot the list under the lock to avoid "list changed size during
    # iteration" if register_retryable_error_hook() runs concurrently.
    with _RETRYABLE_ERROR_HOOKS_LOCK:
        hooks = list(_RETRYABLE_ERROR_HOOKS)
    for fn in hooks:
        try:
            fn(exc)
        except _OperationCancelledError:
            raise  # cooperative cancellation must always propagate out of hooks
        except Exception as _hook_exc:
            LOGGER.debug("retryable_error_hook %r raised: %s", fn, _hook_exc)


DEFAULT_KICKOFF_RETRY_ATTEMPTS = 20
DEFAULT_KICKOFF_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_KICKOFF_RETRY_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_KICKOFF_RETRY_JITTER_RATIO = 0.15


class CircuitBreakerOpenError(RuntimeError):
    pass


@dataclass
class BreakerState:
    name: str
    failure_threshold: int
    recovery_timeout_seconds: float
    failure_count: int = 0
    opened_at: Optional[float] = None
    # _open_count tracks how many times the breaker has transitioned into the
    # open (or re-opened from half-open) state.  Excluded from __eq__ and
    # __repr__ so equality checks remain deterministic in tests.
    _open_count: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self.opened_at is None:
                return "closed"
            if (time.monotonic() - self.opened_at) >= self.recovery_timeout_seconds:
                return "half_open"
            return "open"

    def get_stats(self) -> Dict[str, Any]:
        """
        Return a snapshot of the breaker's current state and metrics.

        Thread-safe.  Does not acquire ``_lock`` on ``state`` (re-entrant
        deadlock risk); instead computes state inline from raw fields.

        Returns
        -------
        dict with keys:
            name, state, failure_count, failure_threshold,
            open_count, recovery_timeout_seconds, opened_at
        """
        with self._lock:
            if self.opened_at is None:
                current_state = "closed"
            elif (time.monotonic() - self.opened_at) >= self.recovery_timeout_seconds:
                current_state = "half_open"
            else:
                current_state = "open"
            return {
                "name": self.name,
                "state": current_state,
                "failure_count": self.failure_count,
                "failure_threshold": self.failure_threshold,
                "open_count": self._open_count,
                "recovery_timeout_seconds": self.recovery_timeout_seconds,
                "opened_at": self.opened_at,
            }

    def before_call(self) -> None:
        # Acquire _lock for the full check so that failure_count is read under
        # the same lock that governs opened_at.  Reading failure_count outside
        # the lock is a data race even though the property self.state already
        # acquires it internally (after the property releases the lock, a
        # concurrent record_failure() could mutate failure_count before we read it).
        with self._lock:
            if self.opened_at is None:
                return
            if (time.monotonic() - self.opened_at) < self.recovery_timeout_seconds:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker '{self.name}' is open after "
                    f"{self.failure_count} failures."
                )

    def record_success(self) -> None:
        with self._lock:
            self.failure_count = 0
            self.opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            if self.failure_count >= self.failure_threshold:
                # Count a new open event only when transitioning from "closed"
                # or "half_open" into "open".  Ongoing failures while already
                # open must not double-count the open event.
                now = time.monotonic()
                was_truly_open = (
                    self.opened_at is not None
                    and (now - self.opened_at) < self.recovery_timeout_seconds
                )
                self.opened_at = now
                if not was_truly_open:
                    self._open_count += 1


_BREAKER_LOCK = threading.Lock()
_BREAKERS: Dict[str, BreakerState] = {}


def get_circuit_breaker(
    name: str,
    *,
    failure_threshold: int,
    recovery_timeout_seconds: float,
) -> BreakerState:
    with _BREAKER_LOCK:
        existing = _BREAKERS.get(name)
        if existing is not None:
            # Acquire the per-instance lock before mutating fields that
            # record_failure() and before_call() also read under that lock.
            # Without this, updating failure_threshold while a concurrent thread
            # is mid-read in record_failure() is a data race.
            with existing._lock:
                existing.failure_threshold = max(1, int(failure_threshold))
                existing.recovery_timeout_seconds = max(1.0, float(recovery_timeout_seconds))
            return existing
        state = BreakerState(
            name=name,
            failure_threshold=max(1, int(failure_threshold)),
            recovery_timeout_seconds=max(1.0, float(recovery_timeout_seconds)),
        )
        _BREAKERS[name] = state
        return state


def reset_circuit_breakers() -> None:
    with _BREAKER_LOCK:
        _BREAKERS.clear()


def _compute_backoff_seconds(
    *,
    attempt: int,
    base_seconds: float,
    max_backoff_seconds: float,
    jitter_ratio: float,
) -> float:
    # Cap the exponent at 62 to prevent 2**exp overflowing to float Inf for
    # very large attempt counts (Python float overflows silently to Inf above
    # 2**1024).  The min(max_backoff_seconds, ...) clamp is still applied, but
    # an intermediate Inf would produce a spurious Inf jitter value.
    _exp = min(max(0, attempt - 1), 62)
    capped_base = min(max_backoff_seconds, max(0.0, base_seconds) * (2 ** _exp))
    if capped_base <= 0:
        return 0.0
    jitter = capped_base * max(0.0, jitter_ratio) * random.random()
    return min(max_backoff_seconds, capped_base + jitter)


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default)


def _resolve_kickoff_retry_defaults(
    *,
    default_max_attempts: Optional[int] = None,
    default_backoff_seconds: Optional[float] = None,
) -> Dict[str, float]:
    resolved_attempts = max(
        1,
        _env_int(
            "AGENT_KICKOFF_RETRY_ATTEMPTS",
            int(
                DEFAULT_KICKOFF_RETRY_ATTEMPTS
                if default_max_attempts is None
                else default_max_attempts
            ),
        ),
    )
    resolved_backoff = max(
        0.0,
        _env_float(
            "AGENT_KICKOFF_RETRY_BACKOFF_SECONDS",
            float(
                DEFAULT_KICKOFF_RETRY_BACKOFF_SECONDS
                if default_backoff_seconds is None
                else default_backoff_seconds
            ),
        ),
    )
    resolved_max_backoff = max(
        resolved_backoff,
        _env_float(
            "AGENT_KICKOFF_RETRY_MAX_BACKOFF_SECONDS",
            DEFAULT_KICKOFF_RETRY_MAX_BACKOFF_SECONDS,
        ),
    )
    resolved_jitter = min(
        1.0,
        max(
            0.0,
            _env_float(
                "AGENT_KICKOFF_RETRY_JITTER_RATIO",
                DEFAULT_KICKOFF_RETRY_JITTER_RATIO,
            ),
        ),
    )
    return {
        "max_attempts": int(resolved_attempts),
        "backoff_seconds": float(resolved_backoff),
        "max_backoff_seconds": float(resolved_max_backoff),
        "jitter_ratio": float(resolved_jitter),
    }


def _walk_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _exception_text(exc: BaseException) -> str:
    parts = []
    for item in _walk_exception_chain(exc):
        parts.append(f"{type(item).__name__}: {item}")
    return " | ".join(parts).lower()


# Billing / account errors that look like 429s but are NEVER transient.
# Matching any of these markers short-circuits the retryable check and
# returns False immediately, preventing multi-minute retry loops on
# non-recoverable account/quota issues.
#
# Known cases:
#   OpenAI billing hard limit — "billing_hard_limit_reached"
#   OpenAI quota exhausted — "insufficient_quota"
_NON_RETRYABLE_BILLING_MARKERS = (
    "insufficient_quota",
    "billing_hard_limit_reached",
    "quota has been exhausted",
    "you exceeded your current quota",
    "account has been deactivated",
    "account is not active",
    "invalid api key",
    "incorrect api key",
    "no api key provided",
    "organization has been disabled",
    "payment required",
)


def is_transient_retryable_error(exc: BaseException) -> bool:
    # --- Phase 1: billing / auth errors are NEVER retryable ---
    # Check this before any transient markers because billing errors are
    # often returned with HTTP 429 / "Too Many Requests" status codes,
    # which would otherwise be misclassified as transient rate limits.
    text = _exception_text(exc)
    if any(marker in text for marker in _NON_RETRYABLE_BILLING_MARKERS):
        return False

    # --- Phase 2: known transient error class names ---
    # NOTE: intentionally narrow.  Earlier revisions included the generic
    # markers "apierror" and "servererror", which caused false positives:
    # OpenAI/anthropic `APIError` is the parent of `BadRequestError`,
    # `AuthenticationError`, `NotFoundError` (4xx — permanent), and any
    # provider class whose name contains "servererror" regardless of
    # whether the underlying status code is actually 5xx.  Those spurious
    # retries burned through retry budget on permanent failures.
    # Callers that need HTTP-status-aware classification should use
    # `http_retry.is_http_retryable`, which checks `response.status_code`.
    transient_name_markers = (
        "timeout",
        "timedout",
        "rate",
        "429",
        "connection",
        "network",
        "apiconnection",
        "serviceunavailable",
        "overloaded",
        "temporar",
        "remoteprotocol",
        "protocolerror",
        # Mid-response body-read failures: these were previously unretried
        # because the class names don't contain "connection"/"network"/"timeout".
        # They are classic transient failures (e.g. Cloudflare closed the
        # chunked-gzip stream before the client finished reading) and MUST
        # be retried — otherwise a single upstream hiccup aborts the pipeline.
        "readerror",
        "writeerror",
        "closederror",
        "chunkedencoding",
        "incompleteread",
        "streamconsumed",
        "streamclosed",
        "contentdecoding",
    )
    transient_text_markers = (
        "timed out",
        "timeout",
        "deadline exceeded",
        "rate limit",
        "too many requests",
        "429",
        "connection reset",
        "connection aborted",
        "connection refused",
        "connection error",
        "network error",
        "service unavailable",
        "temporarily unavailable",
        "overloaded",
        "gateway timeout",
        "remote end closed connection",
        "server disconnected",
        # NOTE: "server error" was intentionally removed from this list.  It
        # matched 4xx response bodies that include the substring "server error"
        # (common in upstream error JSON), causing non-retryable 4xx failures
        # to be treated as transient and burn the retry budget.  Status-code
        # classification (`http_retry.is_http_retryable`) handles real 5xx
        # cases authoritatively.
        "read timed out",
        "api connection error",
        "request timeout",
        "temporary failure",
        # Mid-response body-read failure phrases (httpx / requests / urllib3).
        "incomplete read",
        "chunked",
        "content decoding",
        "peer closed connection",
        "response ended prematurely",
        "stream truncated",
        "connection broken",
        "transfer-encoding",
        # Reasoning-model empty-response failure (kimi-k2/deepseek-r1/o1 class).
        # When the model exhausts completion budget on reasoning tokens it
        # returns {content: None}; CrewAI's _validate_and_finalize_llm_response
        # raises `ValueError("Invalid response from LLM call - None or empty.")`.
        # This is a classic transient condition — retrying with a fresh request
        # typically yields a shorter reasoning chain that does emit content.
        "invalid response from llm",
        "none or empty",
        "empty response from llm",
        "empty model response",
        "no content in response",
    )
    for item in _walk_exception_chain(exc):
        name = type(item).__name__.lower()
        if any(marker in name for marker in transient_name_markers):
            return True
    return any(marker in text for marker in transient_text_markers)


def is_context_length_error(exc: BaseException) -> bool:
    """Return True when *exc* indicates the model's context window was exceeded."""
    text = _exception_text(exc)
    markers = (
        "context_length_exceeded",
        "context length exceeded",
        "maximum context length",
        "prompt is too long",
        "input is too long",
        "tokens in your prompt",
    )
    return any(marker in text for marker in markers)


def execute_with_retry(
    operation: Callable[[], Any],
    *,
    operation_name: str,
    max_attempts: int,
    backoff_seconds: float,
    retryable_exceptions: Tuple[Type[BaseException], ...],
    retryable_exception_filter: Optional[Callable[[BaseException], bool]] = None,
    circuit_breaker_name: Optional[str] = None,
    circuit_failure_threshold: int = 3,
    circuit_recovery_seconds: float = 30.0,
    max_backoff_seconds: float = 15.0,
    jitter_ratio: float = 0.15,
    sleep_fn: Callable[[float], None] = time.sleep,
    logger: Any = LOGGER,
    log_fields: Optional[Dict[str, Any]] = None,
) -> Any:
    # Guard None explicitly: `max_attempts or 1` would also suppress a caller
    # passing max_attempts=0 (which is a valid "disable retries" intent, clamped
    # to 1 by max(1, ...)).  Use is-None check to preserve 0 semantics.
    attempts = max(1, int(max_attempts if max_attempts is not None else 1))
    # Sanitise caller-supplied log_fields: strip keys that this function
    # passes explicitly to log_event() to avoid "got multiple values for
    # keyword argument" TypeError at runtime.
    _INTERNAL_LOG_KEYS = frozenset({
        "operation", "attempt", "max_attempts",
        "delay_seconds", "breaker_state", "error_type",
    })
    if log_fields:
        log_fields = {k: v for k, v in log_fields.items() if k not in _INTERNAL_LOG_KEYS}
    breaker = None
    if circuit_breaker_name:
        breaker = get_circuit_breaker(
            circuit_breaker_name,
            failure_threshold=circuit_failure_threshold,
            recovery_timeout_seconds=circuit_recovery_seconds,
        )
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        # Re-check the circuit breaker on every attempt so that a breaker
        # opened by record_failure() mid-loop blocks subsequent retries.
        # Previously before_call() was only called once before the loop,
        # meaning attempts 2..N could proceed even with an open breaker.
        if breaker is not None:
            breaker.before_call()
        try:
            result = operation()
        except retryable_exceptions as exc:
            # Cooperative cancellation must propagate immediately without being
            # logged as a "non_retryable_failure" — that event is reserved for
            # genuine errors, not intentional user-initiated cancellation.
            if isinstance(exc, _OperationCancelledError):
                raise
            if retryable_exception_filter is not None and not retryable_exception_filter(exc):
                # WARNING (30) rather than ERROR (40): the exception is
                # re-raised immediately, so the caller decides the ultimate
                # severity.  Librarian/search providers treat non-retryable
                # failures as fallback triggers — not crashes — and must
                # not surface to the user as ERROR.
                log_event(
                    logger,
                    30,
                    "non_retryable_failure",
                    f"{operation_name} failed with non-retryable error.",
                    operation=operation_name,
                    attempt=attempt,
                    max_attempts=attempts,
                    error_type=type(exc).__name__,
                    **(log_fields or {}),
                )
                raise
            last_error = exc
            # Notify registered hooks (e.g. adaptive backoff)
            _fire_retryable_error_hooks(exc)
            if breaker is not None:
                breaker.record_failure()
            should_retry = attempt < attempts
            delay_seconds = 0.0
            if should_retry:
                delay_seconds = _compute_backoff_seconds(
                    attempt=attempt,
                    base_seconds=backoff_seconds,
                    max_backoff_seconds=max_backoff_seconds,
                    jitter_ratio=jitter_ratio,
                )
            # Use WARNING (30) for both mid-loop retries and final exhaustion.
            # The exception is re-raised on the final attempt so the caller
            # decides the ultimate log level — in the librarian search flow,
            # for example, provider failures are non-fatal (fallback providers
            # cover the gap) and must NOT surface to the user as ERROR.
            log_event(
                logger,
                30,
                "retryable_failure",
                f"{operation_name} failed with retryable error.",
                operation=operation_name,
                attempt=attempt,
                max_attempts=attempts,
                delay_seconds=f"{delay_seconds:.3f}",
                breaker_state=(breaker.state if breaker is not None else "disabled"),
                error_type=type(exc).__name__,
                **(log_fields or {}),
            )
            if not should_retry:
                raise
            if delay_seconds > 0:
                sleep_fn(delay_seconds)
            continue
        else:
            if breaker is not None:
                breaker.record_success()
            if attempt > 1:
                log_event(
                    logger,
                    20,
                    "retry_recovered",
                    f"{operation_name} recovered after retry.",
                    operation=operation_name,
                    attempt=attempt,
                    breaker_state=(breaker.state if breaker is not None else "disabled"),
                    **(log_fields or {}),
                )
            return result
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{operation_name} exhausted retry attempts without result.")


def retry_policy_settings(
    policy: Any,
    *,
    default_max_attempts: int = 1,
    default_backoff_seconds: float = 1.0,
) -> Dict[str, Any]:
    raw_max_attempts = getattr(policy, "max_attempts", default_max_attempts)
    raw_backoff_seconds = getattr(policy, "backoff_seconds", default_backoff_seconds)
    return {
        # Use explicit None-and-zero check: policy.max_attempts=0 is the "unset"
        # sentinel (consistent with HttpRetryConfig), but `is None` alone would
        # fall through for 0 and use max(1, 0)=1 instead of the default.
        "max_attempts": max(
            1,
            int(
                default_max_attempts
                if (raw_max_attempts is None or raw_max_attempts == 0)
                else raw_max_attempts
            ),
        ),
        # Use None-only check for backoff_seconds: unlike max_attempts where 0
        # is a meaningless sentinel (can't have zero attempts), backoff_seconds=0.0
        # is a valid explicit value meaning "no delay between retries".  Only
        # fall back to default_backoff_seconds when the attribute is absent
        # (getattr returns default_backoff_seconds) or explicitly None.
        "backoff_seconds": max(
            0.0,
            float(
                default_backoff_seconds
                if raw_backoff_seconds is None
                else raw_backoff_seconds
            ),
        ),
        "retry_on_json_fail": bool(getattr(policy, "retry_on_json_fail", False)),
        "retry_on_low_confidence": bool(getattr(policy, "retry_on_low_confidence", False)),
    }


def kickoff_crew_with_retry(
    crew: Any,
    *,
    crew_name: Optional[str] = None,
    retry_policy: Optional[Any] = None,
    default_max_attempts: Optional[int] = None,
    default_backoff_seconds: Optional[float] = None,
    logger: Any = LOGGER,
    log_fields: Optional[Dict[str, Any]] = None,
) -> Any:
    kickoff_defaults = _resolve_kickoff_retry_defaults(
        default_max_attempts=default_max_attempts,
        default_backoff_seconds=default_backoff_seconds,
    )
    # Use explicit None-check: a non-None but falsy policy object (e.g. a Pydantic
    # model whose __bool__ returns False) must not be silently discarded.
    resolved_policy = retry_policy if retry_policy is not None else getattr(crew, "_retry_policy", None)
    settings = retry_policy_settings(
        resolved_policy,
        default_max_attempts=int(kickoff_defaults["max_attempts"]),
        default_backoff_seconds=float(kickoff_defaults["backoff_seconds"]),
    )
    settings["max_attempts"] = max(
        settings["max_attempts"], int(kickoff_defaults["max_attempts"])
    )
    settings["backoff_seconds"] = max(
        float(settings["backoff_seconds"]),
        float(kickoff_defaults["backoff_seconds"]),
    )
    resolved_name = (
        str(
            crew_name
            or getattr(crew, "_crew_name", "")
            or getattr(crew, "__class__", type("Crew", (), {})).__name__
        ).strip()
        or "crew"
    )
    breaker_name = _resolve_kickoff_circuit_breaker_name(
        resolved_name,
        crew,
        log_fields=log_fields,
    )
    result = execute_with_retry(
        crew.kickoff,
        operation_name=f"{resolved_name}.kickoff",
        max_attempts=settings["max_attempts"],
        backoff_seconds=settings["backoff_seconds"],
        retryable_exceptions=(Exception,),
        retryable_exception_filter=is_transient_retryable_error,
        circuit_breaker_name=breaker_name,
        max_backoff_seconds=float(kickoff_defaults["max_backoff_seconds"]),
        jitter_ratio=float(kickoff_defaults["jitter_ratio"]),
        logger=logger,
        log_fields=log_fields,
    )
    if __package__ == "crucible":
        # Usage extraction uses a relative import that only works inside the package.
        # Skip entirely in direct-script mode rather than relying on except ImportError
        # to silently swallow real import errors from inside get_runtime() itself.
        try:
            from .module_runtime import get_runtime

            rt = get_runtime()
            model_id = _resolve_crew_model_id(crew)
            rt.extract_and_set_usage_from_crew(crew, model_id=model_id)
        except _OperationCancelledError:
            # Cooperative cancellation must propagate — do not swallow it as a
            # usage-extraction failure.
            raise
        except Exception as _usage_exc:
            LOGGER.debug("Failed to extract usage after kickoff: %s", _usage_exc)
    return result


def _resolve_crew_model_id(crew: Any) -> str:
    try:
        agents = getattr(crew, "agents", None) or []
    except Exception:
        agents = []
    for agent in agents:
        try:
            llm = getattr(agent, "llm", None)
        except Exception:
            llm = None
        if llm is None:
            continue
        for attr in ("model", "model_name", "model_id"):
            try:
                value = getattr(llm, attr, None)
            except Exception:
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _resolve_crew_provider_id(crew: Any) -> str:
    try:
        agents = getattr(crew, "agents", None) or []
    except Exception:
        agents = []
    for agent in agents:
        try:
            llm = getattr(agent, "llm", None)
        except Exception:
            llm = None
        if llm is None:
            continue
        for attr in ("_quant_llm_provider", "provider", "provider_name"):
            try:
                value = getattr(llm, attr, None)
            except Exception:
                value = None
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _sanitize_breaker_component(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9._-]+", "-", text).strip("-.")


def _resolve_kickoff_circuit_breaker_name(
    resolved_name: str,
    crew: Any,
    *,
    log_fields: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Build a stable circuit-breaker key for a crew kickoff operation.

    The key is scoped by: crew name → kickoff → run_id (if given) OR
    provider → model.  Object identity (``id(crew)``) is intentionally
    excluded so that re-creating the crew object does not silently reset
    the failure counter, which would defeat the circuit-breaker pattern.
    """
    parts = [_sanitize_breaker_component(resolved_name), "kickoff"]
    run_id = ""
    if isinstance(log_fields, dict):
        run_id = _sanitize_breaker_component(log_fields.get("run_id"))
    provider = _sanitize_breaker_component(_resolve_crew_provider_id(crew))
    model_id = _sanitize_breaker_component(_resolve_crew_model_id(crew))
    if run_id:
        parts.append(run_id)
    elif provider:
        parts.append(provider)
    if model_id:
        parts.append(model_id)
    # NOTE: id(crew) deliberately removed — using object address as part of
    # the key caused a fresh circuit breaker on every crew instantiation,
    # meaning repeated failures with newly-constructed crew objects would
    # never accumulate enough failures to open the breaker.
    return ".".join(part for part in parts if part)
