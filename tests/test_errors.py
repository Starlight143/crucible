"""Tests for crucible/errors.py"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.errors import (
    AuthenticationError,
    BudgetExhaustedError,
    LLMTimeoutError,
    NetworkError,
    PermissionDeniedError,
    PermanentError,
    PipelineConfigError,
    CrucibleError,
    RateLimitError,
    ServiceUnavailableError,
    TransientError,
    classify_exception,
)


# ── Instantiation ─────────────────────────────────────────────────────────────

class TestExceptionInstantiation:
    def test_crucible_error(self) -> None:
        exc = CrucibleError("base error")
        assert str(exc) == "base error"
        assert isinstance(exc, Exception)

    def test_crucible_error_with_cause(self) -> None:
        cause = ValueError("original")
        exc = CrucibleError("wrapped", cause=cause)
        assert exc.__cause__ is cause

    def test_transient_error(self) -> None:
        exc = TransientError("transient")
        assert isinstance(exc, CrucibleError)

    def test_rate_limit_error(self) -> None:
        exc = RateLimitError("429 too many requests")
        assert isinstance(exc, TransientError)
        assert isinstance(exc, CrucibleError)

    def test_service_unavailable_error(self) -> None:
        exc = ServiceUnavailableError("503 overloaded")
        assert isinstance(exc, TransientError)

    def test_network_error(self) -> None:
        exc = NetworkError("connection reset")
        assert isinstance(exc, TransientError)

    def test_llm_timeout_error(self) -> None:
        exc = LLMTimeoutError("timed out after 30s")
        assert isinstance(exc, TransientError)

    def test_permanent_error(self) -> None:
        exc = PermanentError("permanent")
        assert isinstance(exc, CrucibleError)

    def test_authentication_error(self) -> None:
        exc = AuthenticationError("401 unauthorized")
        assert isinstance(exc, PermanentError)
        assert isinstance(exc, CrucibleError)

    def test_permission_denied_error(self) -> None:
        exc = PermissionDeniedError("403 forbidden")
        assert isinstance(exc, PermanentError)

    def test_budget_exhausted_error(self) -> None:
        exc = BudgetExhaustedError("budget depleted")
        assert isinstance(exc, PermanentError)

    def test_pipeline_config_error(self) -> None:
        exc = PipelineConfigError("missing feature")
        assert isinstance(exc, PermanentError)


# ── isinstance hierarchy ──────────────────────────────────────────────────────

class TestInstanceofHierarchy:
    def test_rate_limit_is_transient(self) -> None:
        exc = RateLimitError()
        assert isinstance(exc, TransientError)

    def test_rate_limit_is_crucible(self) -> None:
        exc = RateLimitError()
        assert isinstance(exc, CrucibleError)

    def test_rate_limit_is_exception(self) -> None:
        exc = RateLimitError()
        assert isinstance(exc, Exception)

    def test_auth_error_is_permanent(self) -> None:
        exc = AuthenticationError()
        assert isinstance(exc, PermanentError)

    def test_auth_error_is_crucible(self) -> None:
        exc = AuthenticationError()
        assert isinstance(exc, CrucibleError)

    def test_llm_timeout_is_transient(self) -> None:
        exc = LLMTimeoutError()
        assert isinstance(exc, TransientError)

    def test_transient_not_permanent(self) -> None:
        exc = NetworkError()
        assert not isinstance(exc, PermanentError)

    def test_permanent_not_transient(self) -> None:
        exc = AuthenticationError()
        assert not isinstance(exc, TransientError)


# ── classify_exception ────────────────────────────────────────────────────────

class TestClassifyException:
    def test_already_classified_returned_as_is(self) -> None:
        exc = RateLimitError("already typed")
        result = classify_exception(exc)
        assert result is exc

    def test_returns_none_for_unrecognized(self) -> None:
        exc = ValueError("completely unknown error type xyz123")
        result = classify_exception(exc)
        assert result is None

    def test_classifies_401_as_auth(self) -> None:
        exc = Exception("HTTP 401 unauthorized")
        result = classify_exception(exc)
        assert isinstance(result, AuthenticationError)
        assert result.__cause__ is exc

    def test_classifies_invalid_api_key_as_auth(self) -> None:
        exc = Exception("Invalid API key provided")
        result = classify_exception(exc)
        assert isinstance(result, AuthenticationError)

    def test_classifies_403_as_permission(self) -> None:
        exc = Exception("403 forbidden access")
        result = classify_exception(exc)
        assert isinstance(result, PermissionDeniedError)

    def test_classifies_429_as_rate_limit(self) -> None:
        exc = Exception("429 too many requests")
        result = classify_exception(exc)
        assert isinstance(result, RateLimitError)

    def test_classifies_rate_limit_text_as_rate_limit(self) -> None:
        exc = Exception("rate limit exceeded, please wait")
        result = classify_exception(exc)
        assert isinstance(result, RateLimitError)

    def test_classifies_503_as_service_unavailable(self) -> None:
        exc = Exception("503 service unavailable")
        result = classify_exception(exc)
        assert isinstance(result, ServiceUnavailableError)

    def test_classifies_overloaded_as_service_unavailable(self) -> None:
        exc = Exception("The API is currently overloaded")
        result = classify_exception(exc)
        assert isinstance(result, ServiceUnavailableError)

    def test_classifies_timeout_as_llm_timeout(self) -> None:
        class TimeoutError(Exception):
            pass

        exc = TimeoutError("request timed out")
        result = classify_exception(exc)
        # Either LLMTimeoutError or NetworkError depending on name/text matching
        assert isinstance(result, TransientError)

    def test_cause_set_on_classified_exception(self) -> None:
        exc = Exception("429 rate limit")
        result = classify_exception(exc)
        assert result is not None
        assert result.__cause__ is exc

    def test_classifies_context_length_exceeded_as_pipeline_config(self) -> None:
        # Regression: is_context_length_error was imported but never used,
        # so context-length API errors fell through classify_exception and
        # returned None instead of PipelineConfigError.
        exc = Exception("context_length_exceeded: maximum context length is 4096 tokens")
        result = classify_exception(exc)
        assert isinstance(result, PipelineConfigError), \
            f"Expected PipelineConfigError, got {type(result)}"
        assert result.__cause__ is exc

    def test_classifies_prompt_too_long_as_pipeline_config(self) -> None:
        exc = Exception("prompt is too long: 5000 tokens exceeds limit of 4096")
        result = classify_exception(exc)
        assert isinstance(result, PipelineConfigError)

    def test_classifies_maximum_context_length_as_pipeline_config(self) -> None:
        exc = Exception("This model's maximum context length is 8192 tokens.")
        result = classify_exception(exc)
        assert isinstance(result, PipelineConfigError)
