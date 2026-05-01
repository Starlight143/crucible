"""Tests for crucible.convergence_guard"""
from __future__ import annotations

import sys
import os
import time
import warnings

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.convergence_guard import (
    ConvergenceError,
    ConvergenceStats,
    LoopConvergenceGuard,
    StaleLoopWarning,
    _hash_signature,
)


# ── LoopConvergenceGuard basic lifecycle ──────────────────────────────────────

class TestBasicLifecycle:
    def test_enter_returns_guard(self):
        guard = LoopConvergenceGuard("test", max_iterations=10)
        with guard as g:
            assert g is guard

    def test_no_error_on_normal_exit(self):
        with LoopConvergenceGuard("normal", max_iterations=5) as g:
            for _ in range(3):
                g.tick()

    def test_iterations_increments(self):
        with LoopConvergenceGuard("count", max_iterations=10) as g:
            assert g.iterations == 0
            g.tick()
            assert g.iterations == 1
            g.tick()
            assert g.iterations == 2


# ── max_iterations cap ────────────────────────────────────────────────────────

class TestMaxIterations:
    def test_raises_on_exceeding_cap(self):
        with pytest.raises(ConvergenceError) as exc_info:
            with LoopConvergenceGuard("cap", max_iterations=3) as g:
                for _ in range(10):
                    g.tick()
        assert exc_info.value.reason == "max_iterations"
        assert exc_info.value.name == "cap"

    def test_raises_exactly_at_cap_plus_one(self):
        with pytest.raises(ConvergenceError):
            with LoopConvergenceGuard("exact", max_iterations=2) as g:
                g.tick()   # 1 — OK
                g.tick()   # 2 — OK
                g.tick()   # 3 — raises

    def test_no_raise_at_exact_cap(self):
        with LoopConvergenceGuard("atcap", max_iterations=3) as g:
            g.tick()
            g.tick()
            g.tick()  # exactly 3 — should not raise

    def test_zero_disables_cap(self):
        with LoopConvergenceGuard("nocap", max_iterations=0, timeout_seconds=0.0) as g:
            for _ in range(200):
                g.tick()  # must not raise


# ── timeout cap ───────────────────────────────────────────────────────────────

class TestTimeout:
    def test_raises_on_timeout(self):
        with pytest.raises(ConvergenceError) as exc_info:
            with LoopConvergenceGuard(
                "tout", max_iterations=0, timeout_seconds=0.05
            ) as g:
                for _ in range(1000):
                    g.tick()
                    time.sleep(0.01)
        assert exc_info.value.reason == "timeout"

    def test_zero_disables_timeout(self):
        # With a tiny max_iterations and no timeout cap, we still hit the iter cap
        with pytest.raises(ConvergenceError) as exc_info:
            with LoopConvergenceGuard(
                "notout", max_iterations=2, timeout_seconds=0.0
            ) as g:
                for _ in range(10):
                    g.tick()
        assert exc_info.value.reason == "max_iterations"


# ── stale signature detection ─────────────────────────────────────────────────

class TestStaleDetection:
    def test_warns_on_repeated_signature(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LoopConvergenceGuard(
                "stale", max_iterations=100, stale_threshold=3
            ) as g:
                for _ in range(5):
                    g.tick(signature="same_sig")
        stale_warns = [w for w in caught if issubclass(w.category, StaleLoopWarning)]
        assert len(stale_warns) >= 1

    def test_no_warn_on_varying_signatures(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LoopConvergenceGuard(
                "vary", max_iterations=20, stale_threshold=3
            ) as g:
                for i in range(10):
                    g.tick(signature=f"sig_{i}")
        stale_warns = [w for w in caught if issubclass(w.category, StaleLoopWarning)]
        assert len(stale_warns) == 0

    def test_stale_raises_when_configured(self):
        with pytest.raises(ConvergenceError) as exc_info:
            with LoopConvergenceGuard(
                "raise_stale",
                max_iterations=100,
                stale_threshold=3,
                stale_raises=True,
            ) as g:
                for _ in range(10):
                    g.tick(signature="stuck")
        assert exc_info.value.reason == "stale_signature"

    def test_zero_stale_threshold_disables(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LoopConvergenceGuard(
                "nostale", max_iterations=20, stale_threshold=0
            ) as g:
                for _ in range(10):
                    g.tick(signature="same")
        stale_warns = [w for w in caught if issubclass(w.category, StaleLoopWarning)]
        assert len(stale_warns) == 0

    def test_consecutive_resets_on_new_sig(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LoopConvergenceGuard(
                "reset_consec", max_iterations=100, stale_threshold=3
            ) as g:
                # Two consecutive same, then different — resets consecutive counter
                g.tick(signature="a")
                g.tick(signature="a")
                g.tick(signature="b")  # consecutive resets to 1
                g.tick(signature="b")
                g.tick(signature="b")  # now consecutive=3 — warn
        stale_warns = [w for w in caught if issubclass(w.category, StaleLoopWarning)]
        assert len(stale_warns) >= 1


# ── stats ─────────────────────────────────────────────────────────────────────

class TestStats:
    def test_stats_after_ticks(self):
        import time as _time
        with LoopConvergenceGuard("stats", max_iterations=20) as g:
            _time.sleep(0.050)  # ensure measurable elapsed time on low-res timers
            for i in range(5):
                g.tick(signature=f"sig_{i % 2}")
            s = g.stats()
        assert s.iterations == 5
        assert s.unique_signatures == 2
        assert s.elapsed_seconds > 0.0

    def test_stats_is_snapshot(self):
        with LoopConvergenceGuard("snap", max_iterations=20) as g:
            g.tick(signature="x")
            s1 = g.stats()
            g.tick(signature="y")
            s2 = g.stats()
        assert s2.iterations == s1.iterations + 1

    def test_stopped_by_populated_on_error(self):
        with pytest.raises(ConvergenceError):
            with LoopConvergenceGuard("sb", max_iterations=2) as g:
                for _ in range(10):
                    g.tick()
        # After exit, stopped_by should be set (guard retains state)
        assert g._stopped_by == "max_iterations"


# ── elapsed_seconds property ──────────────────────────────────────────────────

class TestElapsedSeconds:
    def test_elapsed_increases(self):
        with LoopConvergenceGuard("ela", max_iterations=100) as g:
            t1 = g.elapsed_seconds
            time.sleep(0.05)
            t2 = g.elapsed_seconds
        assert t2 > t1


# ── _hash_signature helper ────────────────────────────────────────────────────

class TestHashSignature:
    def test_same_input_same_hash(self):
        assert _hash_signature("hello") == _hash_signature("hello")

    def test_different_input_different_hash(self):
        assert _hash_signature("a") != _hash_signature("b")

    def test_returns_12_chars(self):
        assert len(_hash_signature("test")) == 12
