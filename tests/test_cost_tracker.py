"""Tests for crucible.cost_tracker"""
from __future__ import annotations

import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.cost_tracker import (
    CostTracker,
    StageUsage,
    CostSummary,
    StageBudgetExceededError,
    cost_context,
    get_tracker,
    reset_tracker,
)


@pytest.fixture(autouse=True)
def _reset():
    """Ensure each test starts with a fresh global tracker."""
    reset_tracker()
    yield
    reset_tracker()


# ── StageUsage ─────────────────────────────────────────────────────────────────

class TestStageUsage:
    def test_total_tokens(self):
        u = StageUsage(stage="s", input_tokens=100, output_tokens=50,
                       duration_seconds=1.0)
        assert u.total_tokens == 150

    def test_to_dict(self):
        u = StageUsage(stage="s", input_tokens=10, output_tokens=5,
                       duration_seconds=2.0, model_id="gpt-4", cost_usd=0.001)
        d = u.to_dict()
        assert d["stage"] == "s"
        assert d["total_tokens"] == 15
        assert d["model_id"] == "gpt-4"

    def test_frozen(self):
        u = StageUsage(stage="s", input_tokens=1, output_tokens=1,
                       duration_seconds=0.0)
        with pytest.raises((AttributeError, TypeError)):
            u.input_tokens = 999  # type: ignore[misc]


# ── CostTracker.record ─────────────────────────────────────────────────────────

class TestCostTrackerRecord:
    def test_record_returns_stage_usage(self):
        tracker = CostTracker()
        entry = tracker.record("stage_a", input_tokens=100, output_tokens=50)
        assert isinstance(entry, StageUsage)
        assert entry.stage == "stage_a"

    def test_cost_computed(self):
        # input_price=1.0/M, output_price=2.0/M
        tracker = CostTracker(input_price_per_million=1.0, output_price_per_million=2.0)
        entry = tracker.record("s", input_tokens=1_000_000, output_tokens=1_000_000)
        assert abs(entry.cost_usd - 3.0) < 1e-6

    def test_zero_tokens(self):
        tracker = CostTracker()
        entry = tracker.record("s", input_tokens=0, output_tokens=0)
        assert entry.cost_usd == 0.0
        assert entry.total_tokens == 0

    def test_negative_tokens_clamped(self):
        tracker = CostTracker()
        entry = tracker.record("s", input_tokens=-10, output_tokens=-5)
        assert entry.input_tokens == 0
        assert entry.output_tokens == 0

    def test_model_id_stored(self):
        tracker = CostTracker()
        entry = tracker.record("s", model_id="my-model")
        assert entry.model_id == "my-model"

    def test_multiple_records_accumulate(self):
        tracker = CostTracker(input_price_per_million=1.0, output_price_per_million=1.0)
        tracker.record("s", input_tokens=500_000, output_tokens=500_000)
        tracker.record("s", input_tokens=500_000, output_tokens=500_000)
        summary = tracker.summary()
        assert summary.total_cost_usd == pytest.approx(2.0, abs=1e-4)


# ── CostTracker.summary ────────────────────────────────────────────────────────

class TestCostTrackerSummary:
    def test_empty_summary(self):
        tracker = CostTracker()
        s = tracker.summary()
        assert s.total_tokens == 0
        assert s.total_cost_usd == 0.0
        assert s.by_stage == {}

    def test_by_stage_populated(self):
        tracker = CostTracker()
        tracker.record("alpha", input_tokens=100, output_tokens=50)
        tracker.record("beta", input_tokens=200, output_tokens=100)
        s = tracker.summary()
        assert "alpha" in s.by_stage
        assert "beta" in s.by_stage

    def test_by_stage_calls_count(self):
        tracker = CostTracker()
        tracker.record("s", input_tokens=10)
        tracker.record("s", input_tokens=20)
        s = tracker.summary()
        assert s.by_stage["s"]["calls"] == 2

    def test_format_summary_returns_string(self):
        tracker = CostTracker()
        tracker.record("s", input_tokens=1000, output_tokens=500)
        text = tracker.summary().format_summary()
        assert isinstance(text, str)
        assert "s" in text

    def test_to_dict(self):
        tracker = CostTracker()
        tracker.record("s", input_tokens=100, output_tokens=50)
        d = tracker.summary().to_dict()
        assert "total_tokens" in d
        assert "by_stage" in d


# ── Budget guard ───────────────────────────────────────────────────────────────

class TestStageBudgetGuard:
    def test_budget_exceeded_raises(self):
        # 1 M tokens × $1/M input → $1.00; budget $0.50
        tracker = CostTracker(
            input_price_per_million=1.0,
            output_price_per_million=0.0,
            stage_budget_usd={"expensive_stage": 0.50},
        )
        with pytest.raises(StageBudgetExceededError) as exc_info:
            tracker.record("expensive_stage", input_tokens=1_000_000)
        assert exc_info.value.stage == "expensive_stage"

    def test_budget_not_exceeded_ok(self):
        tracker = CostTracker(
            input_price_per_million=1.0,
            output_price_per_million=0.0,
            stage_budget_usd={"s": 1.00},
        )
        # 500_000 tokens × $1/M = $0.50 — under budget
        entry = tracker.record("s", input_tokens=500_000)
        assert entry.input_tokens == 500_000

    def test_cumulative_budget_guard(self):
        tracker = CostTracker(
            input_price_per_million=1.0,
            output_price_per_million=0.0,
            stage_budget_usd={"s": 0.75},
        )
        tracker.record("s", input_tokens=500_000)  # $0.50 — ok
        with pytest.raises(StageBudgetExceededError):
            tracker.record("s", input_tokens=500_000)  # cumulative $1.00 > $0.75


# ── reset ──────────────────────────────────────────────────────────────────────

class TestCostTrackerReset:
    def test_reset_clears_entries(self):
        tracker = CostTracker()
        tracker.record("s", input_tokens=100)
        tracker.reset()
        assert tracker.summary().total_tokens == 0


# ── cost_context ───────────────────────────────────────────────────────────────

class TestCostContext:
    def test_records_on_exit(self):
        tracker = CostTracker()
        with cost_context("ctx_stage", tracker=tracker) as ctx:
            ctx.add_tokens(input_tokens=300, output_tokens=100)
        s = tracker.summary()
        assert s.total_input_tokens == 300
        assert s.total_output_tokens == 100

    def test_model_id_recorded(self):
        tracker = CostTracker()
        with cost_context("s", tracker=tracker) as ctx:
            ctx.add_tokens(input_tokens=10, model_id="model-x")
        entry = tracker.summary().entries[0]
        assert entry.model_id == "model-x"

    def test_duration_positive(self):
        import time as _time
        tracker = CostTracker()
        with cost_context("s", tracker=tracker) as ctx:
            ctx.add_tokens(input_tokens=10)
            _time.sleep(0.050)  # ensure measurable elapsed time on low-res timers
        entry = tracker.summary().entries[0]
        assert isinstance(entry.duration_seconds, float)
        assert entry.duration_seconds > 0.0

    def test_add_tokens_accumulates(self):
        tracker = CostTracker()
        with cost_context("s", tracker=tracker) as ctx:
            ctx.add_tokens(input_tokens=100)
            ctx.add_tokens(input_tokens=50, output_tokens=25)
        entry = tracker.summary().entries[0]
        assert entry.input_tokens == 150
        assert entry.output_tokens == 25

    def test_add_from_crew_result_none(self):
        tracker = CostTracker()
        with cost_context("s", tracker=tracker) as ctx:
            ctx.add_from_crew_result(None)  # should not raise
        assert tracker.summary().total_tokens == 0

    def test_add_from_crew_result_dict(self):
        tracker = CostTracker()
        fake_result = {"usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        with cost_context("s", tracker=tracker) as ctx:
            ctx.add_from_crew_result(fake_result)
        s = tracker.summary()
        assert s.total_input_tokens == 100
        assert s.total_output_tokens == 50

    def test_uses_global_tracker_by_default(self):
        reset_tracker()
        with cost_context("s") as ctx:
            ctx.add_tokens(input_tokens=77)
        assert get_tracker().summary().total_input_tokens == 77

    def test_exception_in_block_still_records(self):
        tracker = CostTracker()
        with pytest.raises(ValueError):
            with cost_context("s", tracker=tracker) as ctx:
                ctx.add_tokens(input_tokens=999)
                raise ValueError("test error")
        # record should still have been attempted; tokens were added before raise
        s = tracker.summary()
        assert s.total_input_tokens == 999


# ── Singleton ──────────────────────────────────────────────────────────────────

class TestGetTracker:
    def test_returns_same_instance(self):
        t1 = get_tracker()
        t2 = get_tracker()
        assert t1 is t2

    def test_reset_creates_new_instance(self):
        t1 = get_tracker()
        reset_tracker()
        t2 = get_tracker()
        assert t1 is not t2
