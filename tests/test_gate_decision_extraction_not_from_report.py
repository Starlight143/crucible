"""Regression: ``extract_gate_decision`` must not rebuild a GateDecision from
the final AnalysisReport payload.

Bug (observed in the wild): the analysis crew's last task emits an
AnalysisReport that shares ``consensus`` / ``disagreement`` / ``experiments``
with GateDecision but renames ``overall_score`` -> ``score`` and drops the
gate-control fields.  ``_extract_pydantic_from_result`` prefers the *final*
crew payload, so a GateDecision was rebuilt from that report: pydantic ignored
the unknown ``score``, ``overall_score`` defaulted to 0 and ``codegen_scope``
to ``"production"``.  The pre-codegen floor then saw ``overall_score=0 < 60``
and skipped CodeGen — even though the run genuinely scored 65 in validation
scope (the Crew Completion box showed 65 / validation in the nested
``gate_context_snapshot``).

The fix is a ``reject_if`` discriminator that refuses a ``score``-without-
``overall_score`` payload as a GateDecision, letting extraction fall through to
the arbiter task output where the real GateDecision lives.
"""
from __future__ import annotations

import inspect
import unittest

from crucible.module_runtime import get_runtime
from crucible.modules.section_01_extraction_and_reformat import (
    _extract_gate_decision_raw,
    _looks_like_analysis_report_not_gate,
    extract_gate_decision,
)
from crucible.modules.section_03_models_and_context import GateDecision


# A faithful shape of the final t_format AnalysisReport the user pasted:
# overall_score renamed to `score`; gate fields only in the nested snapshot.
_ANALYSIS_REPORT_PAYLOAD = {
    "project_name": "eth_dual_engine_stress_test_framework",
    "summary": "...",
    "consensus": "方向G（壓力測試驗證框架）是當前首選方向。",
    "disagreement": "各角色對方向G達成一致，無明顯衝突。",
    "experiments": [{"goal": "驗證幣安API數據完整性", "criteria": "缺失率≤1%"}],
    "score": 65,  # <-- the real score, renamed from overall_score
    "mode_used": "Quant",
    "risk_level": "High",
    "analyst_findings": {"research": "..."},
    "gate_context_snapshot": {  # real gate lives here, but is NOT top-level
        "overall_score": 65,
        "codegen_scope": "validation",
        "ready_for_codegen": True,
    },
}

# The arbiter's real GateDecision (t_arbiter task output): uses overall_score.
_ARBITER_GATE_PAYLOAD = {
    "consensus": "方向G（壓力測試驗證框架）是當前首選方向。",
    "disagreement": "各角色對方向G達成一致，無明顯衝突。",
    "experiments": [{"goal": "驗證幣安API數據完整性", "criteria": "缺失率≤1%"}],
    "ready_for_codegen": True,
    "overall_score": 65,
    "confidence": "low",
    "codegen_scope": "validation",
    "validation_objectives": ["實現數據完整性檢查模組"],
    "should_kill": False,
}


class _FakeTaskOutput:
    """Mimics a crewai TaskOutput exposing a parsed ``json_dict``."""

    def __init__(self, json_dict):
        self.pydantic = None
        self.json_dict = json_dict
        self.raw = ""


class _FakeCrewOutput:
    """Mimics a crewai CrewOutput: final payload + per-task outputs."""

    def __init__(self, final_dict, tasks):
        self.pydantic = None
        self.json_dict = final_dict
        self.raw = ""
        self.tasks_output = list(tasks)


class TestDiscriminator(unittest.TestCase):
    def test_score_without_overall_score_is_rejected(self) -> None:
        self.assertTrue(_looks_like_analysis_report_not_gate({"score": 65}))

    def test_overall_score_present_is_kept(self) -> None:
        self.assertFalse(_looks_like_analysis_report_not_gate({"overall_score": 65}))

    def test_both_keys_present_is_kept(self) -> None:
        self.assertFalse(
            _looks_like_analysis_report_not_gate({"score": 65, "overall_score": 65})
        )

    def test_neither_key_present_is_kept(self) -> None:
        self.assertFalse(_looks_like_analysis_report_not_gate({"consensus": "x"}))


class TestGateExtractionFallsThroughToArbiter(unittest.TestCase):
    def setUp(self) -> None:
        get_runtime()  # cross-module namespace sync (resolves GateDecision/normalize)

    def test_extracts_arbiter_gate_not_the_report(self) -> None:
        result = _FakeCrewOutput(
            final_dict=_ANALYSIS_REPORT_PAYLOAD,
            tasks=[
                _FakeTaskOutput(_ARBITER_GATE_PAYLOAD),       # t_arbiter
                _FakeTaskOutput(_ANALYSIS_REPORT_PAYLOAD),    # t_format (final)
            ],
        )
        gate = extract_gate_decision(result)
        self.assertIsNotNone(gate)
        # The real score (65) survives — NOT the defaulted 0 that tripped the floor.
        self.assertEqual(gate.overall_score, 65)
        # Validation scope is preserved — NOT defaulted to "production".
        self.assertEqual(gate.codegen_scope, "validation")
        # No spurious pre-codegen-floor failure.
        self.assertTrue(gate.ready_for_codegen)
        self.assertNotIn(
            "Pre-codegen gate floor", str(gate.failure_details or "")
        )

    def test_report_only_does_not_fabricate_zero_score_gate(self) -> None:
        # When the AnalysisReport is the ONLY gate-shaped candidate, extraction
        # must return None (let the reformatter/fail-closed handle it) rather
        # than silently emitting a bogus overall_score=0 gate.
        result = _FakeCrewOutput(
            final_dict=_ANALYSIS_REPORT_PAYLOAD,
            tasks=[_FakeTaskOutput(_ANALYSIS_REPORT_PAYLOAD)],
        )
        self.assertIsNone(extract_gate_decision(result))

    def test_raw_gate_extraction_returns_65(self) -> None:
        # The un-normalized raw extractor also picks the arbiter gate.
        result = _FakeCrewOutput(
            final_dict=_ANALYSIS_REPORT_PAYLOAD,
            tasks=[
                _FakeTaskOutput(_ARBITER_GATE_PAYLOAD),
                _FakeTaskOutput(_ANALYSIS_REPORT_PAYLOAD),
            ],
        )
        raw = _extract_gate_decision_raw(result)
        self.assertIsNotNone(raw)
        self.assertEqual(raw.overall_score, 65)


class TestWiringStructural(unittest.TestCase):
    """§9.6 producer→consumer pin: the discriminator must stay wired in."""

    def test_raw_extractor_passes_reject_if(self) -> None:
        src = inspect.getsource(_extract_gate_decision_raw)
        self.assertIn("reject_if=_looks_like_analysis_report_not_gate", src)


if __name__ == "__main__":
    unittest.main()
