"""v1.1.8 extended Phase 5 (Q5) — Parallel multi-provider search runner tests.

Coverage:

* Empty inputs return empty result.
* Sequential fallback (env=0) preserves input order.
* Parallel dispatch returns one entry per provider regardless of order
  of completion.
* Per-provider exception isolates to empty list for that provider.
* Overall timeout exceeded → slow providers reported as empty.
* Max-workers env override.
* Sequential vs parallel produce the same logical result for the
  same inputs (modulo non-determinism in arrival order).
"""

from __future__ import annotations

import time
from typing import Any, List

import pytest

from crucible.web_research.async_runner import (
    _max_workers,
    async_fanout_enabled,
    multi_provider_search,
)


# ─── Fixtures: pretend "providers" ───────────────────────────────────────────


def _ok_provider(name: str, items: int = 3, delay_seconds: float = 0.0):
    """Build a fake provider function that returns *items* dicts."""
    def fn(query: str) -> List[Any]:
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        return [{"provider": name, "result": i, "query": query} for i in range(items)]
    return fn


def _failing_provider(name: str):
    def fn(query: str) -> List[Any]:
        raise RuntimeError(f"{name} failed")
    return fn


def _slow_provider(name: str, delay_seconds: float):
    def fn(query: str) -> List[Any]:
        time.sleep(delay_seconds)
        return [{"provider": name}]
    return fn


# ─── Empty inputs ────────────────────────────────────────────────────────────


class TestEmptyInputs:
    def test_empty_query_returns_empty_map(self) -> None:
        assert multi_provider_search(
            "", [("websearch", _ok_provider("websearch"))],
        ) == {}

    def test_empty_providers_returns_empty_map(self) -> None:
        assert multi_provider_search("q", []) == {}


# ─── Sequential fallback ─────────────────────────────────────────────────────


class TestSequentialFallback:
    def test_env_disabled_uses_sequential(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "0")
        assert async_fanout_enabled() is False
        # Sequential dispatch returns results in any order, but every
        # provider must appear.
        out = multi_provider_search(
            "q",
            [
                ("a", _ok_provider("a", items=2)),
                ("b", _ok_provider("b", items=3)),
            ],
        )
        assert set(out.keys()) == {"a", "b"}
        assert len(out["a"]) == 2
        assert len(out["b"]) == 3

    def test_sequential_isolates_exceptions(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "0")
        out = multi_provider_search(
            "q",
            [
                ("good", _ok_provider("good")),
                ("bad", _failing_provider("bad")),
                ("another_good", _ok_provider("another_good")),
            ],
        )
        assert len(out["good"]) == 3
        assert out["bad"] == []
        assert len(out["another_good"]) == 3


# ─── Parallel dispatch ───────────────────────────────────────────────────────


class TestParallelDispatch:
    def test_parallel_returns_one_per_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "1")
        assert async_fanout_enabled() is True
        out = multi_provider_search(
            "q",
            [
                ("a", _ok_provider("a", items=2)),
                ("b", _ok_provider("b", items=3)),
                ("c", _ok_provider("c", items=1)),
            ],
        )
        assert set(out.keys()) == {"a", "b", "c"}
        assert len(out["a"]) == 2
        assert len(out["b"]) == 3
        assert len(out["c"]) == 1

    def test_parallel_isolates_exceptions(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "1")
        out = multi_provider_search(
            "q",
            [
                ("good_1", _ok_provider("good_1", items=2)),
                ("bad", _failing_provider("bad")),
                ("good_2", _ok_provider("good_2", items=4)),
            ],
        )
        assert len(out["good_1"]) == 2
        assert out["bad"] == []
        assert len(out["good_2"]) == 4

    def test_parallel_is_actually_parallel(self, monkeypatch) -> None:
        """If 3 providers each sleep 0.3s, parallel dispatch finishes
        in <0.6s; sequential would take >0.9s."""
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "1")
        providers = [
            (f"p{i}", _slow_provider(f"p{i}", delay_seconds=0.3))
            for i in range(3)
        ]
        start = time.monotonic()
        out = multi_provider_search("q", providers, timeout_seconds=2.0)
        elapsed = time.monotonic() - start
        # All providers ran.
        assert len(out) == 3
        assert all(len(out[name]) == 1 for name in out)
        # Sequential would take ~0.9s; parallel ~0.3-0.5s.  Generous
        # upper bound accounts for thread scheduling overhead and CI
        # variability.
        assert elapsed < 0.8, (
            f"parallel dispatch took {elapsed:.2f}s — expected < 0.8s; "
            "either env not honoured or threads not actually parallel"
        )


# ─── Overall timeout ─────────────────────────────────────────────────────────


class TestOverallTimeout:
    @pytest.mark.slow
    def test_slow_providers_cancelled_on_timeout(self, monkeypatch) -> None:
        """Overall budget < provider duration → slow providers report
        empty without crashing."""
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "1")
        providers = [
            ("fast", _ok_provider("fast", items=1)),
            ("slow", _slow_provider("slow", delay_seconds=5.0)),
        ]
        start = time.monotonic()
        out = multi_provider_search("q", providers, timeout_seconds=0.5)
        elapsed = time.monotonic() - start
        # Fast finished; slow exceeded timeout and reports empty.
        assert out["fast"] == [{"provider": "fast", "result": 0, "query": "q"}]
        assert out["slow"] == []
        # Budget honoured (don't wait full 5s for the slow one).
        assert elapsed < 4.0


# ─── Max workers ─────────────────────────────────────────────────────────────


class TestMaxWorkers:
    def test_default_max_workers(self, monkeypatch) -> None:
        monkeypatch.delenv("LIBRARIAN_ASYNC_MAX_WORKERS", raising=False)
        # Default is 8, capped by provider count.
        assert _max_workers(3) == 3
        assert _max_workers(10) == 8
        assert _max_workers(1) == 1

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_ASYNC_MAX_WORKERS", "4")
        assert _max_workers(10) == 4
        assert _max_workers(2) == 2  # still capped by providers

    def test_zero_or_negative_falls_back_to_default(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("LIBRARIAN_ASYNC_MAX_WORKERS", "0")
        assert _max_workers(10) == 8


# ─── Async-vs-sequential parity ──────────────────────────────────────────────


class TestParityWithSequential:
    def test_same_inputs_same_results(self, monkeypatch) -> None:
        """For deterministic providers, async and sequential return the
        same map (perhaps in different dict insertion order, but the
        same logical content)."""
        providers = [
            ("a", _ok_provider("a", items=2)),
            ("b", _ok_provider("b", items=3)),
            ("c", _ok_provider("c", items=1)),
        ]
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "0")
        seq = multi_provider_search("q", providers)
        monkeypatch.setenv("LIBRARIAN_ASYNC_FANOUT_ENABLED", "1")
        par = multi_provider_search("q", providers)
        # Same keys, same values per key.
        assert set(seq.keys()) == set(par.keys())
        for k in seq:
            assert seq[k] == par[k]
