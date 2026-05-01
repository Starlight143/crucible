# ruff: noqa: E402
"""Integration tests: Gate Controller decision logic."""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.modules.section_03_models_and_context import (
    Experiment,
    FailureType,
    GateDecision,
    ScoreVector,
    _apply_gate_failure,
    _build_gate_context_snapshot,
    _classify_gate_failure,
    _gate_is_validation_scope,
    _normalize_gate_decision,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_experiment(goal: str = "Validate Sharpe", criteria: str = "Sharpe ≥ 1.0") -> Experiment:
    return Experiment(goal=goal, criteria=criteria)


def _make_gate(
    *,
    ready_for_codegen: bool = True,
    should_kill: bool = False,
    kill_reason: Optional[str] = None,
    blocking_risks: Optional[List[str]] = None,
    agents_needing_rerun: Optional[List[str]] = None,
    rerun_reasons: Optional[Dict[str, str]] = None,
    required_experiments_before_codegen: Optional[List[str]] = None,
    confidence: str = "high",
    overall_score: int = 75,
    codegen_scope: str = "production",
    direction_feedback_needed: bool = False,
    failure_type: str = FailureType.NONE.value,
    failure_details: Optional[str] = None,
    score_breakdown: Optional[ScoreVector] = None,
) -> GateDecision:
    """Create a GateDecision with sensible defaults for use in tests."""
    return GateDecision(
        consensus="All analysts converged on the backtest approach.",
        disagreement="Risk analyst flags parameter sensitivity.",
        experiments=[_make_experiment()],
        ready_for_codegen=ready_for_codegen,
        should_kill=should_kill,
        kill_reason=kill_reason,
        blocking_risks=blocking_risks or [],
        agents_needing_rerun=agents_needing_rerun or [],
        rerun_reasons=rerun_reasons or {},
        required_experiments_before_codegen=required_experiments_before_codegen or [],
        confidence=confidence,
        overall_score=overall_score,
        codegen_scope=codegen_scope,
        direction_feedback_needed=direction_feedback_needed,
        failure_type=failure_type,
        failure_details=failure_details,
        score_breakdown=score_breakdown or ScoreVector(
            feasibility=70, risk=60, roi=75, uncertainty=40
        ),
    )


# ---------------------------------------------------------------------------
# Proceed (ready_for_codegen=True, no kill)
# ---------------------------------------------------------------------------

class TestGateProceed:
    def test_proceed_when_no_blocking_risks_and_high_score(self) -> None:
        """
        Gate must remain ready_for_codegen=True when no blocking risks are present
        and the score and confidence are within acceptable bounds.
        """
        gate = _make_gate(ready_for_codegen=True, blocking_risks=[], confidence="high", overall_score=80)
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.ready_for_codegen is True
        assert normalised.should_kill is False

    def test_proceed_classify_returns_failure_type_none(self) -> None:
        """
        _classify_gate_failure must return FailureType.NONE for a clean proceed gate,
        meaning no failure classification was raised.
        """
        gate = _make_gate(ready_for_codegen=True, confidence="high", overall_score=82)
        ft, details = _classify_gate_failure(gate)
        assert ft == FailureType.NONE
        assert details == ""

    def test_proceed_context_snapshot_contains_ready_flag(self) -> None:
        """
        _build_gate_context_snapshot must preserve ready_for_codegen=True in the
        snapshot dict so downstream stages can read it without accessing the model.
        """
        gate = _make_gate(ready_for_codegen=True)
        snapshot = _build_gate_context_snapshot(gate)
        assert snapshot["ready_for_codegen"] is True
        assert snapshot["should_kill"] is False

    @pytest.mark.parametrize("score", [60, 70, 85, 100])
    def test_proceed_across_valid_score_range(self, score: int) -> None:
        """
        Gate must not flip to killed/not-ready solely because the overall score
        varies within the valid 0–100 range.
        """
        gate = _make_gate(ready_for_codegen=True, overall_score=score, blocking_risks=[])
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.ready_for_codegen is True


# ---------------------------------------------------------------------------
# Kill (should_kill=True)
# ---------------------------------------------------------------------------

class TestGateKill:
    def test_kill_when_should_kill_is_true(self) -> None:
        """
        Gate with should_kill=True must classify as POLICY_VIOLATION and expose
        the kill_reason through _classify_gate_failure.
        """
        gate = _make_gate(
            should_kill=True,
            ready_for_codegen=False,
            kill_reason="Critical risk: strategy violates leverage constraints.",
        )
        ft, details = _classify_gate_failure(gate)
        assert ft == FailureType.POLICY_VIOLATION
        assert "leverage" in details

    def test_kill_disables_direction_feedback(self) -> None:
        """
        _normalize_gate_decision must set direction_feedback_needed=False when
        should_kill=True, preventing a pointless direction-debate retry.
        """
        gate = _make_gate(
            should_kill=True,
            ready_for_codegen=False,
            kill_reason="Insufficient evidence.",
            direction_feedback_needed=True,
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.direction_feedback_needed is False

    def test_kill_context_snapshot_preserves_kill_reason(self) -> None:
        """
        _build_gate_context_snapshot must surface should_kill=True and the
        kill_reason so the pipeline caller can log and halt cleanly.
        """
        reason = "Risk model rejected all positions."
        gate = _make_gate(should_kill=True, ready_for_codegen=False, kill_reason=reason)
        snapshot = _build_gate_context_snapshot(gate)
        assert snapshot["should_kill"] is True
        assert snapshot["kill_reason"] == reason

    def test_kill_when_low_confidence_and_no_allowance(self) -> None:
        """
        A gate with confidence='low' and no validation-scope allowance must be
        classified as LOW_CONFIDENCE failure.
        """
        gate = _make_gate(
            ready_for_codegen=False,
            confidence="low",
            overall_score=40,
        )
        ft, _details = _classify_gate_failure(gate)
        assert ft == FailureType.LOW_CONFIDENCE

    def test_kill_reason_is_none_when_none_provided(self) -> None:
        """
        When should_kill=True but no kill_reason is supplied, _classify_gate_failure
        must still return POLICY_VIOLATION with a non-empty fallback message.
        """
        gate = _make_gate(should_kill=True, ready_for_codegen=False, kill_reason=None)
        ft, details = _classify_gate_failure(gate)
        assert ft == FailureType.POLICY_VIOLATION
        assert len(details) > 0


# ---------------------------------------------------------------------------
# Refine (agents_needing_rerun populated)
# ---------------------------------------------------------------------------

class TestGateRefine:
    def test_refine_with_non_empty_agents_needing_rerun(self) -> None:
        """
        A gate with agents_needing_rerun populated must preserve those agent names
        after normalisation, indicating which analysts should be re-run.
        """
        gate = _make_gate(
            ready_for_codegen=False,
            agents_needing_rerun=["risk", "research"],
            rerun_reasons={
                "risk": "Transaction cost assumption unverified.",
                "research": "Missing regime analysis for 2020 crash.",
            },
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert len(normalised.agents_needing_rerun) >= 1
        # Both originally specified agents must still be present
        assert "risk" in normalised.agents_needing_rerun
        assert "research" in normalised.agents_needing_rerun

    def test_refine_selective_rerun_list_is_non_empty(self) -> None:
        """
        After normalisation, agents_needing_rerun must be non-empty when
        originally set — downstream code relies on this to select which analysts
        to re-execute rather than running the full crew.
        """
        gate = _make_gate(
            ready_for_codegen=False,
            agents_needing_rerun=["biz"],
            rerun_reasons={"biz": "Market sizing data is stale."},
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert len(normalised.agents_needing_rerun) > 0

    def test_refine_context_snapshot_exposes_rerun_list(self) -> None:
        """
        _build_gate_context_snapshot must expose agents_needing_rerun so that
        the pipeline runner can read the refine list without holding the model.
        """
        gate = _make_gate(
            ready_for_codegen=False,
            agents_needing_rerun=["ops"],
            rerun_reasons={"ops": "Infrastructure cost estimate missing."},
        )
        snapshot = _build_gate_context_snapshot(gate)
        assert "ops" in snapshot["agents_needing_rerun"]
        assert "ops" in snapshot["rerun_reasons"]

    def test_refine_deduplicated_by_normalize(self) -> None:
        """
        _normalize_gate_decision must deduplicate agents_needing_rerun so that
        the same role cannot appear twice in the selective-rerun list.
        """
        gate = _make_gate(
            ready_for_codegen=False,
            agents_needing_rerun=["risk", "risk", "research"],
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.agents_needing_rerun.count("risk") == 1

    def test_refine_rerun_reasons_preserved_in_snapshot(self) -> None:
        """
        Each per-agent rerun reason must appear verbatim in the gate context
        snapshot dict so the selective-rerun note injected into prompts is accurate.
        """
        reason_text = "Volatility surface data was unavailable during initial run."
        gate = _make_gate(
            ready_for_codegen=False,
            agents_needing_rerun=["risk"],
            rerun_reasons={"risk": reason_text},
        )
        snapshot = _build_gate_context_snapshot(gate)
        assert snapshot["rerun_reasons"]["risk"] == reason_text


# ---------------------------------------------------------------------------
# Blocking risks force ready_for_codegen to False
# ---------------------------------------------------------------------------

class TestGateBlockingRisks:
    def test_blocking_risks_override_ready_for_codegen_true(self) -> None:
        """
        _normalize_gate_decision must set ready_for_codegen=False whenever
        blocking_risks is non-empty, regardless of what the LLM originally emitted.
        This prevents a contradictory state (ready=True + blocking risks) from
        reaching the codegen stage.
        """
        gate = _make_gate(
            ready_for_codegen=True,  # intentionally contradictory
            blocking_risks=["Unvalidated leverage calculation", "Missing OOS period"],
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.ready_for_codegen is False

    def test_conflicting_output_classified_for_ready_true_with_blocking_risks(self) -> None:
        """
        _classify_gate_failure must return CONFLICTING_OUTPUT when ready_for_codegen=True
        but blocking_risks is non-empty, flagging the contradiction for audit.
        """
        gate = _make_gate(
            ready_for_codegen=True,
            blocking_risks=["Unstable leverage"],
        )
        ft, _details = _classify_gate_failure(gate)
        assert ft == FailureType.CONFLICTING_OUTPUT

    def test_blocking_risks_list_deduplicated(self) -> None:
        """
        _normalize_gate_decision must deduplicate blocking_risks so that the same
        risk text cannot appear twice in the gate context snapshot.
        """
        gate = _make_gate(
            ready_for_codegen=False,
            blocking_risks=["Leverage limit", "Leverage limit", "Missing liquidity data"],
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.blocking_risks.count("Leverage limit") == 1


# ---------------------------------------------------------------------------
# Iteration / maximum-rerun limit (model-level field validation)
# ---------------------------------------------------------------------------

class TestGateIterationLimit:
    def test_gate_overall_score_clamped_to_100(self) -> None:
        """
        GateDecision.overall_score must be rejected at construction time if > 100,
        because the field is defined with ge=0, le=100.
        """
        with pytest.raises(Exception):
            GateDecision(
                consensus="x",
                disagreement="y",
                experiments=[_make_experiment()],
                overall_score=101,  # violates le=100 constraint
            )

    def test_gate_overall_score_clamped_to_0(self) -> None:
        """
        GateDecision.overall_score must be rejected at construction time if < 0.
        """
        with pytest.raises(Exception):
            GateDecision(
                consensus="x",
                disagreement="y",
                experiments=[_make_experiment()],
                overall_score=-1,  # violates ge=0 constraint
            )

    def test_gate_confidence_normalised_to_medium_for_invalid_value(self) -> None:
        """
        _normalize_gate_decision must coerce an invalid confidence string (e.g. 'extreme')
        to 'medium' so downstream code that branches on low/medium/high always gets
        a valid value.
        """
        gate = _make_gate(confidence="extreme", overall_score=70)
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.confidence == "medium"

    def test_gate_direction_feedback_suppressed_when_ready_for_codegen(self) -> None:
        """
        _normalize_gate_decision must clear direction_feedback_needed when
        ready_for_codegen=True: if the gate approved codegen, a direction-debate
        bounce would be wasteful and contradictory.
        """
        gate = _make_gate(
            ready_for_codegen=True,
            direction_feedback_needed=True,  # contradictory — should be suppressed
        )
        normalised = _normalize_gate_decision(gate)
        assert normalised is not None
        assert normalised.direction_feedback_needed is False


# ---------------------------------------------------------------------------
# _apply_gate_failure
# ---------------------------------------------------------------------------

class TestApplyGateFailure:
    def test_apply_gate_failure_sets_type_when_none(self) -> None:
        """
        _apply_gate_failure must set failure_type when it is currently NONE
        (i.e. no previous failure has been recorded).
        """
        gate = _make_gate(failure_type=FailureType.NONE.value)
        updated = _apply_gate_failure(gate, FailureType.LOW_CONFIDENCE, "Confidence too low.")
        assert updated is not None
        assert updated.failure_type == FailureType.LOW_CONFIDENCE.value
        assert updated.failure_details == "Confidence too low."

    def test_apply_gate_failure_does_not_overwrite_existing_type_by_default(self) -> None:
        """
        _apply_gate_failure must not overwrite an already-set failure_type unless
        overwrite=True is explicitly passed.
        """
        gate = _make_gate(failure_type=FailureType.EXECUTION_ERROR.value, failure_details="Original error.")
        updated = _apply_gate_failure(gate, FailureType.LOW_CONFIDENCE, "Confidence too low.")
        assert updated is not None
        assert updated.failure_type == FailureType.EXECUTION_ERROR.value

    def test_apply_gate_failure_overwrites_when_requested(self) -> None:
        """
        _apply_gate_failure must replace an existing failure_type when overwrite=True.
        """
        gate = _make_gate(failure_type=FailureType.EXECUTION_ERROR.value)
        updated = _apply_gate_failure(
            gate, FailureType.POLICY_VIOLATION, "Hard policy block.", overwrite=True
        )
        assert updated is not None
        assert updated.failure_type == FailureType.POLICY_VIOLATION.value
        assert updated.failure_details == "Hard policy block."

    def test_apply_gate_failure_returns_none_for_none_input(self) -> None:
        """_apply_gate_failure must return None when given None — null-safe contract."""
        result = _apply_gate_failure(None, FailureType.LOW_CONFIDENCE)
        assert result is None


# ---------------------------------------------------------------------------
# Validation scope
# ---------------------------------------------------------------------------

class TestGateValidationScope:
    def test_validation_scope_detected_when_codegen_scope_is_validation(self) -> None:
        """
        _gate_is_validation_scope must return True when codegen_scope='validation',
        triggering the validation-first codegen path.
        """
        gate = _make_gate(codegen_scope="validation")
        assert _gate_is_validation_scope(gate) is True

    def test_production_scope_is_not_validation(self) -> None:
        """
        _gate_is_validation_scope must return False for the default 'production' scope.
        """
        gate = _make_gate(codegen_scope="production")
        assert _gate_is_validation_scope(gate) is False

    def test_none_gate_is_not_validation_scope(self) -> None:
        """_gate_is_validation_scope must return False for a None gate (null-safe)."""
        assert _gate_is_validation_scope(None) is False
