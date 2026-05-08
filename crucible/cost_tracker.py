"""
crucible/cost_tracker.py
================================
Per-stage LLM cost tracking for the analysis pipeline.

Inspired by Claude Code's ``cost-tracker.ts``, this module provides:

* ``StageUsage``      — immutable record of token usage for one pipeline stage.
* ``CostTracker``     — accumulates per-stage usage; thread-safe; supports
                        optional per-stage budget guards.
* ``cost_context()``  — context manager that automatically records start/end
                        and integrates with ``runtime_logging``.
* Module-level singleton ``get_tracker()`` so the whole process shares one
                        cost ledger.

Design notes
------------
* No external dependencies — token counts are injected by callers (from
  CrewAI usage callbacks or OpenRouter response headers); this module only
  accumulates and reports.
* Pricing is configured via env vars so it survives model changes without
  code edits.
* All monetary arithmetic uses integer micro-dollar cents to avoid float
  drift in long sessions.
* ``CostTracker`` is an *append-only* ledger; recorded entries are never
  mutated, matching the immutable-ledger principle used for credits/billing.

Usage::

    from crucible.cost_tracker import get_tracker, cost_context

    # Manual recording
    tracker = get_tracker()
    tracker.record("research_swarm", input_tokens=4200, output_tokens=800)

    # Context-manager (auto-records at exit)
    with cost_context("direction_debate") as ctx:
        result = crew.kickoff()
        ctx.add_tokens(input_tokens=result.usage.input, output_tokens=result.usage.output)

    # Session summary
    summary = tracker.summary()
    print(summary.total_cost_usd)
    print(summary.by_stage)
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

if __package__ == "crucible":
    from .cancellation import OperationCancelledError
    from .runtime_logging import get_logger, log_event
else:  # pragma: no cover
    from cancellation import OperationCancelledError  # type: ignore[no-redef]
    from runtime_logging import get_logger, log_event  # type: ignore[no-redef]

LOGGER = get_logger(__name__)

# ── Pricing defaults (per-million tokens, USD) ───────────────────────────────
# All configurable via env vars; defaults match OpenRouter mid-tier models.

_DEFAULT_INPUT_PRICE_PER_M = 0.50   # USD per 1 M input tokens
_DEFAULT_OUTPUT_PRICE_PER_M = 1.50  # USD per 1 M output tokens


try:
    from . import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default)


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


def _usd_per_token(price_per_million: float) -> float:
    """Convert price-per-million-tokens to price-per-token (float)."""
    return price_per_million / 1_000_000.0


# ── Core data model ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StageUsage:
    """
    Immutable record of LLM token usage for a single pipeline stage invocation.
    """
    stage: str
    input_tokens: int
    output_tokens: int
    duration_seconds: float
    timestamp: float = field(default_factory=time.time)
    model_id: str = ""
    cost_usd: float = 0.0   # computed by CostTracker at record time

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "duration_seconds": round(self.duration_seconds, 2),
            "cost_usd": round(self.cost_usd, 6),
            "model_id": self.model_id,
        }


@dataclass
class CostSummary:
    """Aggregated cost summary across all recorded stages."""
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    by_stage: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    entries: List[StageUsage] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "by_stage": self.by_stage,
        }

    def format_summary(self) -> str:
        """Human-readable cost summary."""
        lines = [
            "── Cost Summary ─────────────────────────────",
            f"  Total tokens : {self.total_tokens:,}  "
            f"(in={self.total_input_tokens:,} / out={self.total_output_tokens:,})",
            f"  Estimated USD: ${self.total_cost_usd:.4f}",
            "",
            "  By stage:",
        ]
        for stage, info in self.by_stage.items():
            lines.append(
                f"    {stage:<30}  "
                f"tokens={info.get('total_tokens', 0):>8,}  "
                f"cost=${info.get('cost_usd', 0.0):.4f}  "
                f"({info.get('duration_seconds', 0):.1f}s)"
            )
        lines.append("─────────────────────────────────────────────")
        return "\n".join(lines)


# ── Budget guard ─────────────────────────────────────────────────────────────

class StageBudgetExceededError(RuntimeError):
    """Raised when a stage's cost exceeds its configured budget."""

    def __init__(self, stage: str, cost_usd: float, budget_usd: float) -> None:
        self.stage = stage
        self.cost_usd = cost_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"Stage '{stage}' cost ${cost_usd:.4f} exceeds budget ${budget_usd:.4f}."
        )


# ── CostTracker ───────────────────────────────────────────────────────────────

class CostTracker:
    """
    Append-only per-stage cost ledger.

    Thread-safe via an internal lock.  Multiple threads may record usage
    concurrently (e.g., parallel research lanes); reads snapshot consistently.

    Parameters
    ----------
    input_price_per_million:
        USD cost per 1 M input tokens.
        Defaults to ``COST_TRACKER_INPUT_PRICE_PER_M`` env var → 0.50.
    output_price_per_million:
        USD cost per 1 M output tokens.
        Defaults to ``COST_TRACKER_OUTPUT_PRICE_PER_M`` env var → 1.50.
    stage_budget_usd:
        Optional dict mapping stage name → max USD spend.  If a recorded
        entry exceeds the budget for its stage, ``StageBudgetExceededError``
        is raised.  ``None`` disables budget enforcement.
    """

    def __init__(
        self,
        *,
        input_price_per_million: Optional[float] = None,
        output_price_per_million: Optional[float] = None,
        stage_budget_usd: Optional[Dict[str, float]] = None,
    ) -> None:
        self._input_price: float = _usd_per_token(
            input_price_per_million
            if input_price_per_million is not None
            else _env_float("COST_TRACKER_INPUT_PRICE_PER_M", _DEFAULT_INPUT_PRICE_PER_M)
        )
        self._output_price: float = _usd_per_token(
            output_price_per_million
            if output_price_per_million is not None
            else _env_float("COST_TRACKER_OUTPUT_PRICE_PER_M", _DEFAULT_OUTPUT_PRICE_PER_M)
        )
        self._stage_budget: Dict[str, float] = dict(stage_budget_usd or {})
        self._entries: List[StageUsage] = []
        # Running cumulative cost per stage, kept in sync with ``_entries`` under
        # ``_lock`` so the budget-guard fast path is O(1) per record() instead of
        # re-scanning the whole ledger (which becomes O(n²) over a long session).
        self._stage_cost_total: Dict[str, float] = {}
        self._lock = threading.Lock()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _compute_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            max(0, input_tokens) * self._input_price
            + max(0, output_tokens) * self._output_price
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def record(
        self,
        stage: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        duration_seconds: float = 0.0,
        model_id: str = "",
    ) -> StageUsage:
        """
        Record token usage for *stage* and return the created ``StageUsage``.

        Raises ``StageBudgetExceededError`` if the stage has a configured
        budget and this record pushes accumulated spending over the limit.
        """
        cost = self._compute_cost(input_tokens, output_tokens)
        entry = StageUsage(
            stage=stage,
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            duration_seconds=max(0.0, duration_seconds),
            model_id=model_id,
            cost_usd=cost,
        )
        # Budget guard: compute cumulative cost inside the same lock that protects
        # _entries so concurrent record() calls can't both slip under the budget.
        _exceeded: Optional[Tuple[float, float]] = None
        with self._lock:
            if stage in self._stage_budget:
                # Compute prospective cumulative *before* appending so that a
                # rejected entry never pollutes the ledger or future cumulative sums.
                # Use the per-stage running total so this is O(1) instead of
                # rescanning the entire ledger on every record().
                current_total = self._stage_cost_total.get(stage, 0.0)
                prospective = current_total + entry.cost_usd
                budget_lim = self._stage_budget[stage]
                if prospective > budget_lim:
                    _exceeded = (prospective, budget_lim)
                else:
                    self._entries.append(entry)
                    self._stage_cost_total[stage] = prospective
            else:
                self._entries.append(entry)
                self._stage_cost_total[stage] = (
                    self._stage_cost_total.get(stage, 0.0) + entry.cost_usd
                )

        if _exceeded is not None:
            log_event(
                LOGGER,
                30,
                "stage_cost_budget_exceeded",
                f"Stage '{stage}': budget ${_exceeded[1]:.4f} exceeded by ${_exceeded[0]:.4f} — entry rejected",
                stage=stage,
                cost_usd=round(cost, 6),
                model_id=model_id or "(unknown)",
            )
            raise StageBudgetExceededError(stage, _exceeded[0], _exceeded[1])

        log_event(
            LOGGER,
            20,
            "stage_cost_recorded",
            f"Stage '{stage}': {entry.total_tokens:,} tokens  ${cost:.4f}",
            stage=stage,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cost_usd=round(cost, 6),
            duration_seconds=round(entry.duration_seconds, 2),
            model_id=model_id or "(unknown)",
        )

        return entry

    def summary(self) -> CostSummary:
        """Return a consistent snapshot of accumulated cost across all stages."""
        with self._lock:
            entries_snapshot = list(self._entries)

        total_in = sum(e.input_tokens for e in entries_snapshot)
        total_out = sum(e.output_tokens for e in entries_snapshot)
        total_cost = round(sum(e.cost_usd for e in entries_snapshot), 6)

        by_stage: Dict[str, Dict[str, Any]] = {}
        for entry in entries_snapshot:
            s = entry.stage
            if s not in by_stage:
                by_stage[s] = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "duration_seconds": 0.0,
                    "calls": 0,
                }
            agg = by_stage[s]
            agg["input_tokens"] += entry.input_tokens
            agg["output_tokens"] += entry.output_tokens
            agg["total_tokens"] += entry.total_tokens
            agg["cost_usd"] = round(agg["cost_usd"] + entry.cost_usd, 6)
            agg["duration_seconds"] = round(agg["duration_seconds"] + entry.duration_seconds, 2)
            agg["calls"] += 1

        return CostSummary(
            total_input_tokens=total_in,
            total_output_tokens=total_out,
            total_cost_usd=total_cost,
            by_stage=by_stage,
            entries=entries_snapshot,
        )

    def reset(self) -> None:
        """Clear all recorded entries (mainly for tests)."""
        with self._lock:
            self._entries.clear()
            self._stage_cost_total.clear()


# ── Cost context manager ──────────────────────────────────────────────────────

class _CostContext:
    """
    Internal helper returned by ``cost_context()``.

    Call ``ctx.add_tokens(input_tokens=..., output_tokens=...)`` inside the
    ``with`` block to declare token usage.  The record is committed to the
    tracker on ``__exit__``.
    """

    def __init__(self, tracker: CostTracker, stage: str) -> None:
        self._tracker = tracker
        self._stage = stage
        self._input_tokens = 0
        self._output_tokens = 0
        self._model_id = ""
        self._start = 0.0

    def add_tokens(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model_id: str = "",
    ) -> None:
        """Accumulate token counts within the context block."""
        self._input_tokens += max(0, input_tokens)
        self._output_tokens += max(0, output_tokens)
        if model_id:
            self._model_id = model_id

    def add_from_crew_result(self, crew_result: Any) -> None:
        """
        Attempt to extract token usage from a CrewAI kickoff result.

        Handles both ``CrewOutput`` with a ``token_usage`` attribute and plain
        dicts with ``usage`` / ``usage_metadata`` keys (OpenRouter format).
        """
        if crew_result is None:
            return
        # CrewAI CrewOutput
        usage = getattr(crew_result, "token_usage", None)
        if usage is not None:
            self._input_tokens += max(0, int(getattr(usage, "prompt_tokens", 0) or 0))
            self._output_tokens += max(0, int(getattr(usage, "completion_tokens", 0) or 0))
            return
        # OpenRouter / raw dict
        if isinstance(crew_result, dict):
            raw = crew_result.get("usage")
            if raw is None:
                raw = crew_result.get("usage_metadata")
            if raw is None:
                raw = {}
            self._input_tokens += max(0, int(raw.get("prompt_tokens", 0) or 0))
            self._output_tokens += max(0, int(raw.get("completion_tokens", 0) or 0))


@contextlib.contextmanager
def cost_context(
    stage: str,
    *,
    tracker: Optional[CostTracker] = None,
) -> Iterator[_CostContext]:
    """
    Context manager that records LLM cost for *stage* on exit.

    Example::

        with cost_context("analysis_crew") as ctx:
            result = crew.kickoff()
            ctx.add_from_crew_result(result)
    """
    _tracker = tracker if tracker is not None else get_tracker()
    ctx = _CostContext(_tracker, stage)
    ctx._start = time.monotonic()
    try:
        yield ctx
    finally:
        duration = time.monotonic() - ctx._start
        try:
            _tracker.record(
                stage,
                input_tokens=ctx._input_tokens,
                output_tokens=ctx._output_tokens,
                duration_seconds=duration,
                model_id=ctx._model_id,
            )
        except StageBudgetExceededError:
            raise
        except OperationCancelledError:
            # ``OperationCancelledError`` extends ``RuntimeError`` (an
            # ``Exception`` subclass), so the broad except below would
            # otherwise silently swallow user-initiated cancellation.
            raise
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("cost_context: failed to record usage for '%s': %s", stage, exc)


# ── Module-level singleton ────────────────────────────────────────────────────

_DEFAULT_TRACKER: Optional[CostTracker] = None
_TRACKER_LOCK = threading.Lock()


def get_tracker() -> CostTracker:
    """Return the process-wide default ``CostTracker`` (lazy-init, thread-safe)."""
    global _DEFAULT_TRACKER
    with _TRACKER_LOCK:
        if _DEFAULT_TRACKER is None:
            _DEFAULT_TRACKER = CostTracker()
    return _DEFAULT_TRACKER


def reset_tracker() -> None:
    """Reset the process-wide tracker (mainly for tests)."""
    global _DEFAULT_TRACKER
    with _TRACKER_LOCK:
        _DEFAULT_TRACKER = None
