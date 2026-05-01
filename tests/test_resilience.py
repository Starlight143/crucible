# ruff: noqa: E402, I001
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.resilience import (  # noqa: E402
    CircuitBreakerOpenError,
    DEFAULT_KICKOFF_RETRY_ATTEMPTS,
    DEFAULT_KICKOFF_RETRY_BACKOFF_SECONDS,
    _compute_backoff_seconds,
    execute_with_retry,
    get_circuit_breaker,
    is_transient_retryable_error,
    kickoff_crew_with_retry,
    retry_policy_settings,
    reset_circuit_breakers,
)


class _FakeCrew:
    def __init__(self) -> None:
        self._retry_policy = type(
            "RetryPolicy",
            (),
            {"max_attempts": 3, "backoff_seconds": 0.0},
        )()
        self._crew_name = "fake_crew"
        self.calls = 0

    def kickoff(self) -> str:
        self.calls += 1
        if self.calls < 3:
            raise RuntimeError("temporary failure")
        return "ok"


class _TimeoutCrew:
    def __init__(self) -> None:
        self.calls = 0

    def kickoff(self) -> str:
        self.calls += 1
        if self.calls < 3:
            raise RuntimeError("request timed out while contacting upstream api")
        return "ok"


class _BuggyCrew:
    def __init__(self) -> None:
        self.calls = 0

    def kickoff(self) -> str:
        self.calls += 1
        raise ValueError("schema mismatch in output contract")


class _EventuallyHealthyCrew:
    def __init__(self, success_after: int, *, backoff_seconds: float = 0.5) -> None:
        self.calls = 0
        self._retry_policy = type(
            "RetryPolicy",
            (),
            {"max_attempts": 2, "backoff_seconds": backoff_seconds},
        )()
        self._crew_name = "eventually_healthy_crew"
        self._success_after = success_after

    def kickoff(self) -> str:
        self.calls += 1
        if self.calls < self._success_after:
            raise RuntimeError("request timed out while contacting upstream api")
        return "ok"


class _AlwaysTimeoutCrew:
    def __init__(self, name: str) -> None:
        self.calls = 0
        self._crew_name = name

    def kickoff(self) -> str:
        self.calls += 1
        raise RuntimeError("request timed out while contacting upstream api")


class _HealthyCrewWithName:
    def __init__(self, name: str) -> None:
        self.calls = 0
        self._crew_name = name

    def kickoff(self) -> str:
        self.calls += 1
        return "ok"


class TestResilience(unittest.TestCase):
    def setUp(self) -> None:
        reset_circuit_breakers()

    def test_backoff_without_jitter_is_deterministic(self) -> None:
        self.assertEqual(
            _compute_backoff_seconds(
                attempt=1,
                base_seconds=1.5,
                max_backoff_seconds=10.0,
                jitter_ratio=0.0,
            ),
            1.5,
        )
        self.assertEqual(
            _compute_backoff_seconds(
                attempt=3,
                base_seconds=1.5,
                max_backoff_seconds=10.0,
                jitter_ratio=0.0,
            ),
            6.0,
        )

    def test_execute_with_retry_recovers_before_budget_exhausted(self) -> None:
        attempts = {"count": 0}
        sleeps: list[float] = []

        def flaky_operation() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("retry me")
            return "done"

        result = execute_with_retry(
            flaky_operation,
            operation_name="flaky_operation",
            max_attempts=3,
            backoff_seconds=0.5,
            retryable_exceptions=(RuntimeError,),
            sleep_fn=sleeps.append,
            jitter_ratio=0.0,
        )

        self.assertEqual(result, "done")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_circuit_breaker_opens_after_threshold(self) -> None:
        breaker = get_circuit_breaker(
            "demo_breaker",
            failure_threshold=2,
            recovery_timeout_seconds=60.0,
        )
        breaker.record_failure()
        breaker.record_failure()

        with self.assertRaises(CircuitBreakerOpenError):
            breaker.before_call()

    def test_retry_policy_settings_preserves_explicit_zero_backoff(self) -> None:
        policy = type(
            "RetryPolicy",
            (),
            {"max_attempts": 2, "backoff_seconds": 0.0, "retry_on_json_fail": True},
        )()

        settings = retry_policy_settings(policy)

        self.assertEqual(settings["max_attempts"], 2)
        self.assertEqual(settings["backoff_seconds"], 0.0)
        self.assertTrue(settings["retry_on_json_fail"])

    def test_global_kickoff_defaults_are_raised_for_attempts_and_backoff(self) -> None:
        self.assertEqual(DEFAULT_KICKOFF_RETRY_ATTEMPTS, 20)
        self.assertEqual(DEFAULT_KICKOFF_RETRY_BACKOFF_SECONDS, 2.0)

    def test_kickoff_crew_with_retry_uses_attached_policy(self) -> None:
        crew = _FakeCrew()
        result = kickoff_crew_with_retry(crew)

        self.assertEqual(result, "ok")
        self.assertEqual(crew.calls, 3)

    def test_transient_timeout_classifier_matches_request_timeout_errors(self) -> None:
        self.assertTrue(
            is_transient_retryable_error(
                RuntimeError("request timed out while contacting upstream api")
            )
        )
        self.assertFalse(
            is_transient_retryable_error(
                ValueError("schema mismatch in output contract")
            )
        )

    def test_body_read_failures_classified_as_transient(self) -> None:
        """Mid-response body-read failures must retry (v16.9.48).

        Before v16.9.48, an OpenRouter 200 OK response whose chunked-gzip
        body was truncated by Cloudflare raised httpx.ReadError /
        requests.exceptions.ChunkedEncodingError, which matched NO
        transient marker and was re-raised as a fatal error, killing the
        pipeline with exit code 1.
        """
        # Simulated exception instances whose class names match the new markers.
        class ReadError(OSError):
            pass

        class WriteError(OSError):
            pass

        class ChunkedEncodingError(Exception):
            pass

        class IncompleteRead(Exception):
            pass

        class ContentDecodingError(Exception):
            pass

        class StreamClosed(Exception):
            pass

        for cls in (
            ReadError,
            WriteError,
            ChunkedEncodingError,
            IncompleteRead,
            ContentDecodingError,
            StreamClosed,
        ):
            self.assertTrue(
                is_transient_retryable_error(cls("body truncated")),
                msg=f"{cls.__name__} should be transient",
            )

    def test_body_read_failure_text_markers(self) -> None:
        for phrase in (
            "incomplete read",
            "peer closed connection",
            "response ended prematurely",
            "stream truncated",
            "connection broken",
        ):
            self.assertTrue(
                is_transient_retryable_error(RuntimeError(phrase)),
                msg=f"phrase {phrase!r} should be transient",
            )

    def test_non_transient_client_errors_still_not_retried(self) -> None:
        """Guard against over-broad matching — 4xx client errors and
        schema errors must remain non-transient."""
        self.assertFalse(is_transient_retryable_error(ValueError("invalid schema")))
        self.assertFalse(is_transient_retryable_error(KeyError("missing field")))
        self.assertFalse(
            is_transient_retryable_error(RuntimeError("404 not found"))
        )
        self.assertFalse(
            is_transient_retryable_error(RuntimeError("400 bad request"))
        )

    def test_reasoning_model_empty_response_classified_as_transient(self) -> None:
        """CrewAI raises ValueError('Invalid response from LLM call - None or empty.')
        when a reasoning model (kimi-k2/deepseek-r1/o1 class) exhausts its completion
        budget on reasoning tokens and returns {content: None}.  A fresh retry
        typically produces a shorter reasoning chain that does emit content, so
        this must be classified as transient.
        """
        self.assertTrue(
            is_transient_retryable_error(
                ValueError("Invalid response from LLM call - None or empty.")
            )
        )
        # Case-insensitive and phrase-variant coverage.
        self.assertTrue(
            is_transient_retryable_error(
                RuntimeError("Received None or empty response from LLM call.")
            )
        )
        self.assertTrue(
            is_transient_retryable_error(RuntimeError("empty response from llm"))
        )
        self.assertTrue(
            is_transient_retryable_error(RuntimeError("no content in response"))
        )
        self.assertTrue(
            is_transient_retryable_error(RuntimeError("empty model response"))
        )

    def test_kickoff_crew_with_retry_retries_timeout_failures_without_policy(self) -> None:
        crew = _TimeoutCrew()

        result = kickoff_crew_with_retry(
            crew,
            crew_name="timeout_crew",
            default_max_attempts=3,
            default_backoff_seconds=0.0,
        )

        self.assertEqual(result, "ok")
        self.assertEqual(crew.calls, 3)

    def test_kickoff_crew_with_retry_does_not_retry_non_transient_failures(self) -> None:
        crew = _BuggyCrew()

        with self.assertRaises(ValueError):
            kickoff_crew_with_retry(
                crew,
                crew_name="buggy_crew",
                default_max_attempts=3,
                default_backoff_seconds=0.0,
            )

        self.assertEqual(crew.calls, 1)

    def test_kickoff_crew_with_retry_floors_attached_policy_attempts_to_global_default(self) -> None:
        # success_after=3 requires 2 failures before succeeding on call 3.
        # The attached policy (max_attempts=2) would stop at call 2, but the
        # global default (20) allows the 3rd call — verifying the flooring.
        # success_after must stay below circuit_failure_threshold (3) so the
        # breaker, which now correctly re-checks before every retry attempt,
        # does not open and block the final success call.
        crew = _EventuallyHealthyCrew(success_after=3, backoff_seconds=0.0)
        with mock.patch(
            "crucible.resilience._compute_backoff_seconds",
            return_value=0.0,
        ):
            result = kickoff_crew_with_retry(crew)

        self.assertEqual(result, "ok")
        self.assertEqual(crew.calls, 3)

    def test_kickoff_crew_with_retry_floors_attached_policy_backoff_to_global_default(self) -> None:
        crew = _EventuallyHealthyCrew(success_after=2, backoff_seconds=0.5)
        observed: list[float] = []

        def _fake_backoff(
            *,
            attempt: int,
            base_seconds: float,
            max_backoff_seconds: float,
            jitter_ratio: float,
        ) -> float:
            observed.append(base_seconds)
            return 0.0

        with mock.patch("crucible.resilience._compute_backoff_seconds", _fake_backoff):
            result = kickoff_crew_with_retry(crew)

        self.assertEqual(result, "ok")
        self.assertEqual(observed, [2.0])

    def test_kickoff_circuit_breaker_shared_by_name_recovers_after_reset(self) -> None:
        # After removing id(crew) from the circuit-breaker key, crews with the
        # same name intentionally share the breaker so that failure counts
        # accumulate across re-instantiations.  After an explicit reset (which
        # models the recovery-timeout path), the healthy crew can proceed.
        from crucible.resilience import reset_circuit_breakers
        reset_circuit_breakers()

        failing = _AlwaysTimeoutCrew("shared_name2")
        healthy = _HealthyCrewWithName("shared_name2")

        with mock.patch("crucible.resilience._compute_backoff_seconds", return_value=0.0):
            with self.assertRaises(RuntimeError):
                kickoff_crew_with_retry(
                    failing,
                    default_max_attempts=3,
                    default_backoff_seconds=0.0,
                )

            # Breaker is now open; reset simulates the recovery timeout elapsing.
            reset_circuit_breakers()

            result = kickoff_crew_with_retry(
                healthy,
                default_max_attempts=3,
                default_backoff_seconds=0.0,
            )

        self.assertEqual(failing.calls, 3)
        self.assertEqual(healthy.calls, 1)
        self.assertEqual(result, "ok")


class TestBreakerStateGetStats(unittest.TestCase):
    def setUp(self) -> None:
        reset_circuit_breakers()

    def test_get_stats_closed_state(self) -> None:
        breaker = get_circuit_breaker("stats_closed", failure_threshold=3, recovery_timeout_seconds=60.0)
        stats = breaker.get_stats()
        self.assertEqual(stats["name"], "stats_closed")
        self.assertEqual(stats["state"], "closed")
        self.assertEqual(stats["failure_count"], 0)
        self.assertEqual(stats["open_count"], 0)
        self.assertEqual(stats["failure_threshold"], 3)

    def test_get_stats_open_state_after_threshold(self) -> None:
        breaker = get_circuit_breaker("stats_open", failure_threshold=2, recovery_timeout_seconds=60.0)
        breaker.record_failure()
        breaker.record_failure()
        stats = breaker.get_stats()
        self.assertEqual(stats["state"], "open")
        self.assertEqual(stats["failure_count"], 2)
        self.assertEqual(stats["open_count"], 1)

    def test_open_count_increments_on_closed_to_open_transition(self) -> None:
        breaker = get_circuit_breaker("oc_closed_open", failure_threshold=2, recovery_timeout_seconds=60.0)
        self.assertEqual(breaker.get_stats()["open_count"], 0)
        breaker.record_failure()
        self.assertEqual(breaker.get_stats()["open_count"], 0)  # not open yet
        breaker.record_failure()
        self.assertEqual(breaker.get_stats()["open_count"], 1)  # now open

    def test_open_count_does_not_increment_while_already_open(self) -> None:
        breaker = get_circuit_breaker("oc_no_double", failure_threshold=2, recovery_timeout_seconds=60.0)
        breaker.record_failure()
        breaker.record_failure()  # opens → open_count = 1
        # More failures while already open must NOT increment open_count
        breaker.record_failure()
        breaker.record_failure()
        self.assertEqual(breaker.get_stats()["open_count"], 1)

    def test_open_count_increments_on_half_open_to_open_re_open(self) -> None:
        # Use mock to fast-forward time past recovery_timeout without sleeping.
        from unittest.mock import patch

        breaker = get_circuit_breaker(
            "oc_reopen", failure_threshold=2, recovery_timeout_seconds=1.0
        )
        breaker.record_failure()
        breaker.record_failure()  # open_count = 1, state = open

        # Simulate the recovery timeout having elapsed by patching time.monotonic
        # to return a value 2 seconds after the breaker was opened.
        opened_at = breaker.opened_at
        assert opened_at is not None
        future_time = opened_at + 2.0  # 2 s > recovery_timeout_seconds (1.0)

        with patch("crucible.resilience.time") as mock_time:
            mock_time.monotonic.return_value = future_time
            # now = future_time = original_opened_at + 2.0
            # was_truly_open = (now - self.opened_at) < recovery_timeout_seconds
            #                 = (original_opened_at + 2.0 - original_opened_at) < 1.0
            #                 = 2.0 < 1.0 → False
            # → not was_truly_open is True → _open_count increments from 1 to 2
            breaker.record_failure()

        stats = breaker.get_stats()
        self.assertEqual(stats["open_count"], 2)

    def test_get_stats_half_open_state(self) -> None:
        from unittest.mock import patch

        breaker = get_circuit_breaker(
            "stats_half_open", failure_threshold=2, recovery_timeout_seconds=1.0
        )
        breaker.record_failure()
        breaker.record_failure()

        # Simulate time past recovery_timeout so get_stats sees "half_open"
        opened_at = breaker.opened_at
        assert opened_at is not None
        future_time = opened_at + 2.0  # 2 s > recovery_timeout (1.0)

        with patch("crucible.resilience.time") as mock_time:
            mock_time.monotonic.return_value = future_time
            stats = breaker.get_stats()

        self.assertEqual(stats["state"], "half_open")


class TestExecuteWithRetryCancellation(unittest.TestCase):
    """
    Regression: OperationCancelledError raised inside execute_with_retry was
    caught by `except retryable_exceptions` (which uses Exception as a catch-all),
    then logged at ERROR level as "non_retryable_failure" before being re-raised.
    Cancellation is cooperative shutdown — it must propagate immediately without
    false-alarm error logs.
    """

    def setUp(self) -> None:
        reset_circuit_breakers()

    def test_cancellation_propagates_without_retry(self) -> None:
        from crucible.cancellation import OperationCancelledError

        call_count = 0

        def operation() -> None:
            nonlocal call_count
            call_count += 1
            raise OperationCancelledError("user cancelled")

        with self.assertRaises(OperationCancelledError):
            execute_with_retry(
                operation,
                operation_name="test_op",
                max_attempts=5,  # must NOT retry
                backoff_seconds=0.0,
                retryable_exceptions=(Exception,),
                retryable_exception_filter=is_transient_retryable_error,
            )

        # Cancellation must NOT trigger retries
        self.assertEqual(call_count, 1, "operation must not be retried after cancellation")

    def test_cancellation_not_logged_as_non_retryable_failure(self) -> None:
        """The non_retryable_failure log event must NOT fire for OperationCancelledError."""
        from crucible.cancellation import OperationCancelledError
        from unittest.mock import patch

        logged_events: list = []

        def capturing_log(logger, level, event, msg, **fields) -> None:
            logged_events.append(event)

        with patch("crucible.resilience.log_event", side_effect=capturing_log):
            with self.assertRaises(OperationCancelledError):
                execute_with_retry(
                    lambda: (_ for _ in ()).throw(OperationCancelledError("cancelled")),
                    operation_name="test_op",
                    max_attempts=3,
                    backoff_seconds=0.0,
                    retryable_exceptions=(Exception,),
                    retryable_exception_filter=is_transient_retryable_error,
                )

        self.assertNotIn(
            "non_retryable_failure", logged_events,
            "OperationCancelledError must not be logged as non_retryable_failure"
        )

    def test_kickoff_crew_with_retry_propagates_cancellation(self) -> None:
        """kickoff_crew_with_retry must propagate OperationCancelledError without retrying."""
        from crucible.cancellation import OperationCancelledError

        call_count = 0

        class _CancellingCrew:
            def kickoff(self) -> None:
                nonlocal call_count
                call_count += 1
                raise OperationCancelledError("cancelled")

        with self.assertRaises(OperationCancelledError):
            kickoff_crew_with_retry(_CancellingCrew(), default_max_attempts=5)

        self.assertEqual(call_count, 1, "kickoff must not be retried after cancellation")

    def test_kickoff_crew_with_retry_propagates_cancellation_from_usage_extraction(
        self,
    ) -> None:
        """
        OperationCancelledError raised inside the post-kickoff usage-extraction
        block must propagate out of kickoff_crew_with_retry.

        Previously `except Exception as _usage_exc: LOGGER.debug(...)` silently
        swallowed any OperationCancelledError from extract_and_set_usage_from_crew,
        causing the caller to receive the crew result as if cancellation never
        occurred.  The fix adds an explicit `except _OperationCancelledError: raise`
        guard before the broad handler.
        """
        import sys
        from crucible.cancellation import OperationCancelledError
        from unittest.mock import MagicMock, patch

        class _SuccessfulCrew:
            def kickoff(self) -> str:
                return "done"

        # Simulate extract_and_set_usage_from_crew raising OperationCancelledError.
        # The function is imported dynamically inside a try block via
        # `from .module_runtime import get_runtime`, so we inject a mock into
        # sys.modules so the import resolves to our controlled object.
        mock_rt = MagicMock()
        mock_rt.extract_and_set_usage_from_crew.side_effect = OperationCancelledError(
            "cancelled during usage extraction"
        )
        mock_module_runtime = MagicMock()
        mock_module_runtime.get_runtime.return_value = mock_rt

        with patch.dict(
            sys.modules,
            {"crucible.module_runtime": mock_module_runtime},
        ):
            with self.assertRaises(OperationCancelledError):
                kickoff_crew_with_retry(_SuccessfulCrew(), default_max_attempts=1)


class TestLogFieldsSanitisation(unittest.TestCase):
    """Regression: callers passing 'attempt' inside log_fields caused
    ``TypeError: log_event() got multiple values for keyword argument 'attempt'``
    because execute_with_retry also passes ``attempt=attempt`` explicitly.
    """

    def test_log_fields_with_attempt_key_does_not_raise(self) -> None:
        """log_fields containing 'attempt' must not cause a TypeError."""
        call_count = 0

        def failing_op() -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            execute_with_retry(
                failing_op,
                operation_name="test_dup_attempt",
                max_attempts=1,
                backoff_seconds=0.0,
                retryable_exceptions=(ValueError,),
                retryable_exception_filter=lambda _exc: False,  # non-retryable
                log_fields={"attempt": 42, "stage": "test"},
            )

        self.assertEqual(call_count, 1)

    def test_log_fields_attempt_stripped_internal_keys_preserved(self) -> None:
        """Internal keys in log_fields are stripped; other keys survive."""
        from unittest.mock import patch

        captured: list[dict] = []

        def spy_log(_logger: object, _level: int, _event: str, _msg: str, **fields: object) -> None:
            captured.append(dict(fields))

        with patch("crucible.resilience.log_event", side_effect=spy_log):
            with self.assertRaises(RuntimeError):
                execute_with_retry(
                    lambda: (_ for _ in ()).throw(RuntimeError("fail")),
                    operation_name="test_strip",
                    max_attempts=1,
                    backoff_seconds=0.0,
                    retryable_exceptions=(RuntimeError,),
                    retryable_exception_filter=lambda _exc: False,
                    log_fields={
                        "attempt": 99,       # collides — must be stripped
                        "operation": "dup",   # collides — must be stripped
                        "stage": "kept",      # safe — must survive
                    },
                )

        self.assertTrue(len(captured) >= 1, "at least one log_event call expected")
        fields = captured[0]
        # Internal 'attempt' must come from the retry loop, not from log_fields
        self.assertEqual(fields["attempt"], 1)
        self.assertEqual(fields["operation"], "test_strip")
        # Non-colliding key must survive
        self.assertEqual(fields["stage"], "kept")

    def test_log_fields_with_retryable_attempt_key(self) -> None:
        """Same collision bug on the retryable_failure code path."""
        call_count = 0

        def failing_op() -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError("retry me")

        with self.assertRaises(ValueError):
            execute_with_retry(
                failing_op,
                operation_name="test_retryable_dup",
                max_attempts=2,
                backoff_seconds=0.0,
                retryable_exceptions=(ValueError,),
                retryable_exception_filter=lambda _exc: True,  # retryable
                log_fields={"attempt": 42, "max_attempts": 99},
            )

        self.assertEqual(call_count, 2)


if __name__ == "__main__":
    unittest.main()
