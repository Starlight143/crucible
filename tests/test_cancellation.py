"""Tests for crucible/cancellation.py"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.cancellation import (
    CancellationToken,
    OperationCancelledError,
    cancellation_scope,
    current_token,
    raise_if_cancelled,
)


# ── CancellationToken ─────────────────────────────────────────────────────────

class TestCancellationToken:
    def test_starts_not_cancelled(self) -> None:
        token = CancellationToken()
        assert token.is_cancelled is False

    def test_cancel_sets_is_cancelled(self) -> None:
        token = CancellationToken()
        token.cancel()
        assert token.is_cancelled is True

    def test_cancel_is_idempotent(self) -> None:
        token = CancellationToken()
        token.cancel("first")
        token.cancel("second")  # must not raise
        assert token.is_cancelled is True

    def test_raise_if_cancelled_raises_when_cancelled(self) -> None:
        token = CancellationToken()
        token.cancel()
        with pytest.raises(OperationCancelledError):
            token.raise_if_cancelled()

    def test_raise_if_cancelled_noop_when_not_cancelled(self) -> None:
        token = CancellationToken()
        token.raise_if_cancelled()  # should not raise

    def test_wait_returns_true_when_cancelled(self) -> None:
        token = CancellationToken()
        token.cancel()
        result = token.wait(timeout=0.01)
        assert result is True

    def test_wait_returns_false_on_timeout(self) -> None:
        token = CancellationToken()
        result = token.wait(timeout=0.01)
        assert result is False

    def test_cancel_with_reason(self) -> None:
        token = CancellationToken()
        token.cancel(reason="test reason")
        assert token.is_cancelled is True

    def test_thread_safety_multiple_cancel_calls(self) -> None:
        """Multiple threads calling cancel simultaneously should not raise."""
        token = CancellationToken()
        errors: list = []

        def try_cancel() -> None:
            try:
                token.cancel("from thread")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=try_cancel) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert token.is_cancelled is True

    def test_wait_unblocks_on_cancel_from_other_thread(self) -> None:
        token = CancellationToken()
        results: list = []

        def canceller() -> None:
            time.sleep(0.05)
            token.cancel()

        threading.Thread(target=canceller, daemon=True).start()
        result = token.wait(timeout=2.0)
        assert result is True


# ── Module-level raise_if_cancelled ──────────────────────────────────────────

class TestRaiseIfCancelled:
    def test_noop_when_no_token(self) -> None:
        # Ensure no token is set in this context
        raise_if_cancelled()  # should not raise

    def test_noop_when_token_not_cancelled(self) -> None:
        with cancellation_scope() as token:
            raise_if_cancelled()  # token not cancelled, should not raise

    def test_raises_when_token_cancelled(self) -> None:
        with cancellation_scope() as token:
            token.cancel()
            with pytest.raises(OperationCancelledError):
                raise_if_cancelled()

    def test_noop_outside_scope(self) -> None:
        with cancellation_scope() as token:
            token.cancel()

        # Outside scope, no token active
        raise_if_cancelled()  # should not raise


# ── current_token ─────────────────────────────────────────────────────────────

class TestCurrentToken:
    def test_returns_none_outside_scope(self) -> None:
        assert current_token() is None

    def test_returns_active_token_inside_scope(self) -> None:
        with cancellation_scope() as token:
            assert current_token() is token

    def test_returns_none_after_scope_exits(self) -> None:
        with cancellation_scope():
            pass
        assert current_token() is None


# ── cancellation_scope ────────────────────────────────────────────────────────

class TestCancellationScope:
    def test_yields_a_token(self) -> None:
        with cancellation_scope() as token:
            assert isinstance(token, CancellationToken)

    def test_accepts_explicit_token(self) -> None:
        explicit = CancellationToken()
        with cancellation_scope(explicit) as token:
            assert token is explicit

    def test_restores_no_token_on_exit(self) -> None:
        with cancellation_scope():
            pass
        assert current_token() is None

    def test_nested_scopes_restore_outer(self) -> None:
        with cancellation_scope() as outer:
            with cancellation_scope() as inner:
                assert current_token() is inner
            assert current_token() is outer
        assert current_token() is None

    def test_restores_on_exception(self) -> None:
        try:
            with cancellation_scope():
                raise ValueError("test")
        except ValueError:
            pass
        assert current_token() is None

    def test_cancelled_token_propagates_raise_if_cancelled(self) -> None:
        with cancellation_scope() as token:
            token.cancel()
            with pytest.raises(OperationCancelledError):
                raise_if_cancelled()


# ── OperationCancelledError ───────────────────────────────────────────────────

class TestOperationCancelledError:
    def test_default_message(self) -> None:
        exc = OperationCancelledError()
        assert "cancelled" in str(exc).lower()

    def test_custom_message(self) -> None:
        exc = OperationCancelledError("custom message")
        assert "custom message" in str(exc)

    def test_is_runtime_error(self) -> None:
        exc = OperationCancelledError()
        assert isinstance(exc, RuntimeError)

    def test_is_exception(self) -> None:
        exc = OperationCancelledError()
        assert isinstance(exc, Exception)
