"""Tests for crucible.error_budget"""
from __future__ import annotations

import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.error_budget import (
    BudgetExhaustedError,
    ErrorAuditLog,
    ErrorBudget,
    ErrorBudgetRegistry,
    clear_budgets,
    configure_budget,
    get_budget,
    record_error,
    reset_all_budgets,
)


@pytest.fixture(autouse=True)
def _clean_global():
    clear_budgets()
    yield
    clear_budgets()


# ── BudgetExhaustedError ──────────────────────────────────────────────────────

class TestBudgetExhaustedError:
    def test_attributes(self):
        exc = BudgetExhaustedError("stage1", consumed=5, max_errors=5)
        assert exc.stage == "stage1"
        assert exc.consumed == 5
        assert exc.max_errors == 5

    def test_message_contains_stage(self):
        exc = BudgetExhaustedError("mystage", consumed=3, max_errors=3)
        assert "mystage" in str(exc)


# ── ErrorBudget ───────────────────────────────────────────────────────────────

class TestErrorBudget:
    def test_initial_state(self):
        b = ErrorBudget(stage="s", max_errors=5)
        assert b.consumed == 0
        assert b.remaining == 5
        assert not b.is_exhausted

    def test_record_increments(self):
        b = ErrorBudget(stage="s", max_errors=5)
        count = b.record()
        assert count == 1
        assert b.consumed == 1
        assert b.remaining == 4

    def test_record_raises_at_max(self):
        b = ErrorBudget(stage="s", max_errors=2)
        b.record()  # 1
        with pytest.raises(BudgetExhaustedError) as exc_info:
            b.record()  # 2 — raises
        assert exc_info.value.stage == "s"
        assert exc_info.value.consumed == 2

    def test_is_exhausted_after_max(self):
        b = ErrorBudget(stage="s", max_errors=1)
        with pytest.raises(BudgetExhaustedError):
            b.record()
        assert b.is_exhausted

    def test_reset_clears_counter(self):
        b = ErrorBudget(stage="s", max_errors=5)
        b.record()
        b.record()
        b.reset()
        assert b.consumed == 0
        assert b.remaining == 5

    def test_to_dict(self):
        b = ErrorBudget(stage="s", max_errors=5)
        b.record()
        d = b.to_dict()
        assert d["stage"] == "s"
        assert d["consumed"] == 1
        assert d["remaining"] == 4
        assert d["max_errors"] == 5
        assert d["is_exhausted"] is False

    def test_thread_safe_record(self):
        b = ErrorBudget(stage="s", max_errors=100)
        errors: list = []

        def run():
            try:
                b.record()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert b.consumed == 50


# ── ErrorBudgetRegistry ───────────────────────────────────────────────────────

class TestErrorBudgetRegistry:
    def test_configure_creates_budget(self):
        reg = ErrorBudgetRegistry()
        b = reg.configure("stage1", max_errors=5)
        assert b.stage == "stage1"
        assert b.max_errors == 5

    def test_configure_same_stage_returns_same_object(self):
        reg = ErrorBudgetRegistry()
        b1 = reg.configure("s", max_errors=5)
        b2 = reg.configure("s", max_errors=5)
        assert b1 is b2

    def test_configure_changed_max_resets_counter(self):
        reg = ErrorBudgetRegistry()
        b = reg.configure("s", max_errors=5)
        b.record()
        assert b.consumed == 1
        reg.configure("s", max_errors=10)
        assert b.consumed == 0
        assert b.max_errors == 10

    def test_get_returns_none_for_unknown(self):
        reg = ErrorBudgetRegistry()
        assert reg.get("unknown") is None

    def test_get_or_create(self):
        reg = ErrorBudgetRegistry()
        b = reg.get_or_create("new_stage")
        assert b.stage == "new_stage"
        assert b.max_errors == 10  # default

    def test_reset_all(self):
        reg = ErrorBudgetRegistry()
        b = reg.configure("s1", max_errors=5)
        b.record()
        reg.reset_all()
        assert b.consumed == 0

    def test_clear_removes_all(self):
        reg = ErrorBudgetRegistry()
        reg.configure("s1")
        reg.configure("s2")
        reg.clear()
        assert reg.get("s1") is None
        assert reg.get("s2") is None

    def test_snapshot(self):
        reg = ErrorBudgetRegistry()
        reg.configure("a", max_errors=3)
        reg.configure("b", max_errors=7)
        snap = reg.snapshot()
        stages = {s["stage"] for s in snap}
        assert {"a", "b"} == stages


# ── ErrorAuditLog ─────────────────────────────────────────────────────────────

class TestErrorAuditLog:
    def test_write_and_read(self, tmp_path):
        log = ErrorAuditLog(str(tmp_path))
        log.write("stage1", "ValueError", "bad value", run_dir="/run/1")
        records = log.read_all()
        assert len(records) == 1
        r = records[0]
        assert r["stage"] == "stage1"
        assert r["error_type"] == "ValueError"
        assert r["message"] == "bad value"
        assert r["run_dir"] == "/run/1"

    def test_multiple_writes_all_readable(self, tmp_path):
        log = ErrorAuditLog(str(tmp_path))
        for i in range(5):
            log.write(f"stage{i}", "Error", f"msg{i}")
        records = log.read_all()
        assert len(records) == 5

    def test_extra_fields_included(self, tmp_path):
        log = ErrorAuditLog(str(tmp_path))
        log.write("s", "E", "m", extra={"custom_key": "custom_val"})
        records = log.read_all()
        assert records[0]["custom_key"] == "custom_val"

    def test_empty_file_returns_empty_list(self, tmp_path):
        log = ErrorAuditLog(str(tmp_path))
        assert log.read_all() == []

    def test_each_record_has_timestamp(self, tmp_path):
        log = ErrorAuditLog(str(tmp_path))
        log.write("s", "E", "m")
        records = log.read_all()
        assert "timestamp" in records[0]

    def test_concurrent_writes(self, tmp_path):
        log = ErrorAuditLog(str(tmp_path))
        errors: list = []

        def write():
            try:
                log.write("s", "E", "msg")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        records = log.read_all()
        assert len(records) == 20


# ── Public API (global registry) ──────────────────────────────────────────────

class TestPublicAPI:
    def test_configure_budget(self):
        b = configure_budget("stage_api", max_errors=7)
        assert b.stage == "stage_api"
        assert b.max_errors == 7

    def test_get_budget_none_before_configure(self):
        assert get_budget("unknown_stage") is None

    def test_get_budget_after_configure(self):
        configure_budget("s", max_errors=3)
        b = get_budget("s")
        assert b is not None
        assert b.max_errors == 3

    def test_record_error_increments(self, tmp_path):
        configure_budget("s", max_errors=10)
        count = record_error("s", ValueError("oops"), run_dir=str(tmp_path))
        assert count == 1

    def test_record_error_raises_when_exhausted(self, tmp_path):
        configure_budget("s", max_errors=2)
        record_error("s", run_dir=str(tmp_path))  # 1
        with pytest.raises(BudgetExhaustedError):
            record_error("s", run_dir=str(tmp_path))  # 2

    def test_reset_all_budgets(self):
        configure_budget("s", max_errors=5)
        b = get_budget("s")
        assert b is not None
        b.record()
        reset_all_budgets()
        assert b.consumed == 0

    def test_clear_budgets(self):
        configure_budget("s", max_errors=5)
        clear_budgets()
        assert get_budget("s") is None


# ── Concurrent configure + record ─────────────────────────────────────────────

class TestConcurrentConfigureAndRecord:
    """
    Regression tests for the max_errors synchronization race condition:

        Problem (pre-fix): ErrorBudgetRegistry.configure() wrote
        existing.max_errors while holding the registry lock but NOT the
        budget's own _lock.  Concurrent calls to ErrorBudget.record() read
        max_errors under the budget lock, creating a window where one thread
        writes max_errors and another reads a torn (partially updated) value.

        Fix: configure() now holds both registry lock *and* budget._lock when
        mutating max_errors and resetting _consumed.
    """

    def test_concurrent_configure_and_record_no_deadlock(self):
        """
        Hammer configure() and record() concurrently from many threads.
        No deadlock, no exception (other than BudgetExhaustedError which is
        expected), and the budget is in a consistent state at the end.
        """
        reg = ErrorBudgetRegistry()
        budget = reg.configure("stress", max_errors=1000)
        errors: list = []
        exhausted_count: list = []

        def do_record():
            try:
                reg.get_or_create("stress").record()
            except BudgetExhaustedError:
                exhausted_count.append(1)
            except Exception as e:
                errors.append(e)

        def do_configure():
            try:
                reg.configure("stress", max_errors=999)
                reg.configure("stress", max_errors=1000)
            except Exception as e:
                errors.append(e)

        threads = (
            [threading.Thread(target=do_record) for _ in range(50)]
            + [threading.Thread(target=do_configure) for _ in range(10)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"
        # consumed + remaining must equal max_errors (internal consistency)
        b = reg.get("stress")
        assert b is not None
        assert b.consumed + b.remaining == b.max_errors

    def test_configure_max_errors_visible_under_budget_lock(self):
        """
        After configure() changes max_errors, record() must immediately see
        the updated value without stale reads — verified by checking that the
        budget raises BudgetExhaustedError exactly at the new limit.
        """
        reg = ErrorBudgetRegistry()
        b = reg.configure("vis", max_errors=10)
        for _ in range(5):
            b.record()  # consume 5 of 10

        # Shrink budget to 6 — next record should still work (consumed = 5 < 6)
        reg.configure("vis", max_errors=6)
        # configure with new max resets counter; consumed is now 0
        assert b.consumed == 0
        assert b.max_errors == 6

        # Fill up to the new limit
        for _ in range(5):
            b.record()  # 1..5
        with pytest.raises(BudgetExhaustedError) as exc_info:
            b.record()  # 6 — should exhaust
        assert exc_info.value.max_errors == 6
