"""Tests for crucible/context_pressure.py"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crucible.context_pressure import (
    ContextWindowCriticalError,
    ContextWindowMonitor,
    _CRITICAL_THRESHOLD,
    _WARNING_THRESHOLDS,
)


# ── Basic state ───────────────────────────────────────────────────────────────

class TestContextWindowMonitorBasic:
    def test_utilization_starts_at_zero(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=1000)
        assert monitor.utilization() == 0.0

    def test_used_tokens_starts_at_zero(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=1000)
        assert monitor.used_tokens == 0

    def test_max_tokens_reflects_config(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=50_000)
        assert monitor.max_tokens == 50_000

    def test_remaining_tokens_equals_max_at_start(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        assert monitor.remaining_tokens() == 10_000

    def test_record_text_increases_used_tokens(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=100_000)
        # record_text now delegates to count_tokens (tiktoken or CJK-aware heuristic).
        # The exact count depends on the estimator, but it must be >= 1.
        monitor.record_text("a" * 40)
        assert monitor.used_tokens >= 1

    def test_record_tokens_increases_used_tokens(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=100_000)
        monitor.record_tokens(500)
        assert monitor.used_tokens == 500

    def test_record_tokens_ignores_negative(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=100_000)
        monitor.record_tokens(-100)
        assert monitor.used_tokens == 0

    def test_utilization_after_record(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=1_000)
        monitor.record_tokens(500)
        assert abs(monitor.utilization() - 0.5) < 1e-6

    def test_remaining_tokens_decreases(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=1_000)
        monitor.record_tokens(300)
        assert monitor.remaining_tokens() == 700


# ── get_stats ─────────────────────────────────────────────────────────────────

class TestGetStats:
    def test_stats_structure(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000, stage="test_stage")
        monitor.record_tokens(1_000)
        stats = monitor.get_stats()
        assert stats["max_tokens"] == 10_000
        assert stats["used_tokens"] == 1_000
        assert stats["remaining_tokens"] == 9_000
        assert abs(stats["utilization"] - 0.1) < 1e-4
        assert stats["utilization_pct"] == "10.0%"
        assert stats["is_critical"] is False
        assert stats["stage"] == "test_stage"

    def test_is_critical_true_at_critical_threshold(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(9_500)
        stats = monitor.get_stats()
        assert stats["is_critical"] is True


# ── raise_if_critical ─────────────────────────────────────────────────────────

class TestRaiseIfCritical:
    def test_no_raise_below_critical(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(9_000)  # 90%
        monitor.raise_if_critical()  # should not raise

    def test_raises_at_critical_threshold(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(9_500)  # 95%
        with pytest.raises(ContextWindowCriticalError) as exc_info:
            monitor.raise_if_critical()
        exc = exc_info.value
        assert exc.used_tokens == 9_500
        assert exc.max_tokens == 10_000
        assert exc.utilization >= _CRITICAL_THRESHOLD

    def test_raises_above_critical_threshold(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(10_000)  # 100%
        with pytest.raises(ContextWindowCriticalError):
            monitor.raise_if_critical()

    def test_critical_error_is_runtime_error(self) -> None:
        exc = ContextWindowCriticalError(0.96, 10000, 9600)
        assert isinstance(exc, RuntimeError)

    def test_critical_error_message_contains_pct(self) -> None:
        exc = ContextWindowCriticalError(0.96, 10000, 9600)
        assert "96.0%" in str(exc)


# ── Warning thresholds ────────────────────────────────────────────────────────

class TestWarningThresholds:
    def test_thresholds_fire_once_each(self) -> None:
        """Each threshold must fire exactly once, even if we exceed it by a lot."""
        monitor = ContextWindowMonitor(max_tokens=10_000)
        warned_levels: list[float] = []

        # Monkeypatch _emit_threshold_warning to record threshold hits without
        # side-effects (avoids telemetry noise in tests).
        original_emit = monitor._emit_threshold_warning

        def recording_emit(threshold: float, util: float, used: int) -> None:
            warned_levels.append(threshold)
            original_emit(threshold, util, used)

        monitor._emit_threshold_warning = recording_emit

        # Jump straight to 100% — all thresholds should fire once
        monitor.record_tokens(10_000)
        assert len(set(warned_levels)) == len(warned_levels), "Each threshold must fire once"
        assert set(warned_levels) == set(_WARNING_THRESHOLDS)

        # More tokens — no additional warnings
        before = len(warned_levels)
        monitor.record_tokens(1_000)
        assert len(warned_levels) == before

    def test_thresholds_are_idempotent(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(9_500)
        warned_before = set(monitor._warned)
        monitor.record_tokens(100)
        warned_after = set(monitor._warned)
        assert warned_before == warned_after


# ── reset ─────────────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_clears_used_tokens(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(5_000)
        monitor.reset()
        assert monitor.used_tokens == 0

    def test_reset_clears_warned_set(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(9_000)
        assert len(monitor._warned) > 0
        monitor.reset()
        assert len(monitor._warned) == 0

    def test_warnings_re_fire_after_reset(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=10_000)
        monitor.record_tokens(7_500)  # crosses 70%
        assert 0.70 in monitor._warned
        monitor.reset()
        monitor.record_tokens(7_500)  # should cross 70% again
        assert 0.70 in monitor._warned


# ── Thread-safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_record_tokens_are_all_counted(self) -> None:
        monitor = ContextWindowMonitor(max_tokens=1_000_000)
        n_threads = 20
        tokens_per_thread = 1_000

        def worker() -> None:
            for _ in range(tokens_per_thread):
                monitor.record_tokens(1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = n_threads * tokens_per_thread
        assert monitor.used_tokens == expected
