# ruff: noqa: E402
"""Tests for the v1.0.5 pre-codegen gate score floor (P0-4)."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.modules.section_03_models_and_context import (
    FailureType,
    GateDecision,
    _normalize_gate_decision,
)


class TestPreCodegenGateFloor(unittest.TestCase):
    """Low overall_score must be downgraded to ready_for_codegen=False."""

    def setUp(self) -> None:
        # Ensure no stale env override from another test.
        os.environ.pop("CRUCIBLE_PRE_CODEGEN_MIN_SCORE", None)

    def test_low_score_is_downgraded(self) -> None:
        gate = GateDecision(
            consensus="ok",
            disagreement="",
            experiments=[],
            ready_for_codegen=True,
            overall_score=40,
            confidence="medium",
            codegen_scope="production",
        )
        normalized = _normalize_gate_decision(gate)
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertFalse(normalized.ready_for_codegen)
        self.assertEqual(normalized.failure_type, FailureType.LOW_CONFIDENCE.value)
        self.assertIn("60", normalized.failure_details or "")

    def test_high_score_passes_through(self) -> None:
        gate = GateDecision(
            consensus="ok",
            disagreement="",
            experiments=[],
            ready_for_codegen=True,
            overall_score=80,
            confidence="high",
            codegen_scope="production",
        )
        normalized = _normalize_gate_decision(gate)
        assert normalized is not None
        self.assertTrue(normalized.ready_for_codegen)
        self.assertEqual(normalized.failure_type, FailureType.NONE.value)

    def test_score_at_threshold_passes(self) -> None:
        gate = GateDecision(
            consensus="ok",
            disagreement="",
            experiments=[],
            ready_for_codegen=True,
            overall_score=60,
            confidence="medium",
            codegen_scope="production",
        )
        normalized = _normalize_gate_decision(gate)
        assert normalized is not None
        self.assertTrue(normalized.ready_for_codegen)

    def test_validation_scope_ignores_floor(self) -> None:
        """Validation-scope codegen is allowed below the production floor."""
        gate = GateDecision(
            consensus="ok",
            disagreement="",
            experiments=[],
            ready_for_codegen=True,
            overall_score=20,
            confidence="low",
            codegen_scope="validation",
            validation_scope_reason="Phase 0 calibration harness",
            validation_objectives=["calibrate the metric"],
        )
        normalized = _normalize_gate_decision(gate)
        assert normalized is not None
        self.assertTrue(normalized.ready_for_codegen)

    def test_env_override_can_disable_floor(self) -> None:
        os.environ["CRUCIBLE_PRE_CODEGEN_MIN_SCORE"] = "0"
        try:
            gate = GateDecision(
                consensus="ok",
                disagreement="",
                experiments=[],
                ready_for_codegen=True,
                overall_score=10,
                confidence="low",
                codegen_scope="production",
            )
            normalized = _normalize_gate_decision(gate)
            assert normalized is not None
            self.assertTrue(normalized.ready_for_codegen)
        finally:
            os.environ.pop("CRUCIBLE_PRE_CODEGEN_MIN_SCORE", None)

    def test_env_override_higher_threshold(self) -> None:
        os.environ["CRUCIBLE_PRE_CODEGEN_MIN_SCORE"] = "75"
        try:
            gate = GateDecision(
                consensus="ok",
                disagreement="",
                experiments=[],
                ready_for_codegen=True,
                overall_score=70,
                confidence="medium",
                codegen_scope="production",
            )
            normalized = _normalize_gate_decision(gate)
            assert normalized is not None
            self.assertFalse(normalized.ready_for_codegen)
        finally:
            os.environ.pop("CRUCIBLE_PRE_CODEGEN_MIN_SCORE", None)

    def test_failure_type_quality_loop_gave_up_exists(self) -> None:
        # Sanity check: the new enum value is well-formed.
        self.assertEqual(
            FailureType.QUALITY_LOOP_GAVE_UP.value, "QUALITY_LOOP_GAVE_UP"
        )


if __name__ == "__main__":
    unittest.main()
