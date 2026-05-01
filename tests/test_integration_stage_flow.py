# ruff: noqa: E402
"""Integration tests: Stage→Stage data flow (Pydantic serialization/deserialization)."""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.modules.section_03_models_and_context import (
    AnalysisReport,
    Experiment,
    GateContextBundle,
    RunSnapshot,
    load_analysis_report_safe,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def minimal_experiment() -> Experiment:
    """Return the smallest valid Experiment."""
    return Experiment(goal="Measure Sharpe ≥ 1.0 on out-of-sample data", criteria="OOS Sharpe ≥ 1.0")


@pytest.fixture()
def minimal_analysis_report(minimal_experiment: Experiment) -> AnalysisReport:
    """Return the smallest valid AnalysisReport that exercises every required field."""
    return AnalysisReport(
        project_name="test_quant_strategy",
        summary="A minimal test analysis report",
        consensus="All analysts agreed on the backtest framework.",
        disagreement="Risk analyst flagged overfitting concern.",
        experiments=[minimal_experiment],
        score=72,
        mode_used="Quant",
        risk_level="Medium",
    )


@pytest.fixture()
def full_analysis_report(minimal_experiment: Experiment) -> AnalysisReport:
    """Return an AnalysisReport with every optional field populated."""
    return AnalysisReport(
        project_name="full_quant_strategy",
        summary="Comprehensive momentum strategy analysis.",
        consensus="Sharpe-weighted ensemble shows edge.",
        disagreement="OPS analyst disputes transaction cost assumption.",
        experiments=[minimal_experiment],
        score=85,
        mode_used="Quant",
        risk_level="High",
        analyst_findings={
            "research": "Momentum effect documented in literature.",
            "risk": "Max drawdown may exceed 25% in bear markets.",
        },
        gate_context_snapshot={"ready_for_codegen": True, "confidence": "high"},
        codegen_handoff_summary="Implement daily rebalance momentum factor.",
        codegen_requirements=["Use vectorised pandas operations", "Emit JSON metrics to stdout"],
        codegen_constraints=["No forward-looking bias", "No live-trading code"],
        codegen_validation_focus=["Sharpe ≥ 1.0", "Max drawdown < 30%"],
        schema_version=1,
    )


# ---------------------------------------------------------------------------
# AnalysisReport serialization round-trip
# ---------------------------------------------------------------------------

class TestAnalysisReportRoundTrip:
    def test_minimal_report_json_round_trip(self, minimal_analysis_report: AnalysisReport) -> None:
        """
        Verify that a minimal AnalysisReport survives model_dump → JSON → re-parse
        without any field mutation or data loss.
        """
        serialised = json.dumps(minimal_analysis_report.model_dump(), ensure_ascii=False)
        raw = json.loads(serialised)
        restored = AnalysisReport(**raw)

        assert restored.project_name == minimal_analysis_report.project_name
        assert restored.summary == minimal_analysis_report.summary
        assert restored.score == minimal_analysis_report.score
        assert restored.mode_used == minimal_analysis_report.mode_used
        assert restored.risk_level == minimal_analysis_report.risk_level
        assert len(restored.experiments) == 1
        assert restored.experiments[0].goal == minimal_analysis_report.experiments[0].goal

    def test_full_report_json_round_trip(self, full_analysis_report: AnalysisReport) -> None:
        """
        Verify that a fully-populated AnalysisReport round-trips without data loss,
        including nested dicts, lists, and optional string fields.
        """
        serialised = json.dumps(full_analysis_report.model_dump(), ensure_ascii=False)
        raw = json.loads(serialised)
        restored = AnalysisReport(**raw)

        assert restored.analyst_findings == full_analysis_report.analyst_findings
        assert restored.codegen_requirements == full_analysis_report.codegen_requirements
        assert restored.codegen_constraints == full_analysis_report.codegen_constraints
        assert restored.codegen_validation_focus == full_analysis_report.codegen_validation_focus
        assert restored.schema_version == 1

    def test_report_json_identity(self, full_analysis_report: AnalysisReport) -> None:
        """
        Verify that two round-trips produce an identical JSON string (no ordering drift
        or type coercion on the second pass).
        """
        dump1 = json.dumps(full_analysis_report.model_dump(), sort_keys=True)
        raw = json.loads(dump1)
        restored = AnalysisReport(**raw)
        dump2 = json.dumps(restored.model_dump(), sort_keys=True)
        assert dump1 == dump2


# ---------------------------------------------------------------------------
# load_analysis_report_safe
# ---------------------------------------------------------------------------

class TestLoadAnalysisReportSafe:
    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """load_analysis_report_safe must return None when the file does not exist."""
        result = load_analysis_report_safe(str(tmp_path / "nonexistent.json"))
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path: Path) -> None:
        """load_analysis_report_safe must return None and warn for invalid JSON content."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json}", encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = load_analysis_report_safe(str(bad_file))
        assert result is None
        assert any(issubclass(w.category, RuntimeWarning) for w in caught)

    def test_returns_none_for_non_dict_json(self, tmp_path: Path) -> None:
        """load_analysis_report_safe must return None when the top-level value is a list."""
        list_file = tmp_path / "list.json"
        list_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        result = load_analysis_report_safe(str(list_file))
        assert result is None

    def test_loads_valid_report_from_file(self, tmp_path: Path, minimal_analysis_report: AnalysisReport) -> None:
        """load_analysis_report_safe must load and parse a well-formed JSON file."""
        report_file = tmp_path / "analysis_result.json"
        report_file.write_text(
            json.dumps(minimal_analysis_report.model_dump(), ensure_ascii=False),
            encoding="utf-8",
        )
        restored = load_analysis_report_safe(str(report_file))
        assert restored is not None
        assert restored.project_name == minimal_analysis_report.project_name
        assert restored.score == minimal_analysis_report.score

    def test_backfills_compat_defaults_for_v1_schema(self, tmp_path: Path) -> None:
        """
        load_analysis_report_safe must fill in optional fields absent from older
        schema_version=1 files, returning a valid AnalysisReport rather than crashing.
        """
        v1_data: Dict[str, Any] = {
            "project_name": "legacy_project",
            "summary": "Old-format report",
            "consensus": "N/A",
            "disagreement": "N/A",
            "experiments": [],
            "score": 60,
            "mode_used": "Quant",
            "risk_level": "Low",
            # schema_version and codegen_* fields intentionally absent
        }
        legacy_file = tmp_path / "legacy.json"
        legacy_file.write_text(json.dumps(v1_data), encoding="utf-8")
        result = load_analysis_report_safe(str(legacy_file))
        assert result is not None
        assert result.schema_version == 1
        assert result.analyst_findings == {}
        assert result.codegen_requirements == []
        assert result.codegen_constraints == []
        assert result.codegen_validation_focus == []
        assert result.codegen_handoff_summary == ""

    def test_ignores_unknown_fields_from_future_schema(self, tmp_path: Path) -> None:
        """
        load_analysis_report_safe must silently drop unknown fields from future
        schema versions rather than raising a ValidationError.
        """
        future_data: Dict[str, Any] = {
            "project_name": "future_project",
            "summary": "Future schema",
            "consensus": "N/A",
            "disagreement": "N/A",
            "experiments": [],
            "score": 55,
            "mode_used": "SaaS",
            "risk_level": "Medium",
            "schema_version": 99,
            "unknown_future_field": "will be dropped",
            "another_new_field": {"nested": True},
        }
        future_file = tmp_path / "future.json"
        future_file.write_text(json.dumps(future_data), encoding="utf-8")
        result = load_analysis_report_safe(str(future_file))
        assert result is not None
        assert result.project_name == "future_project"
        assert not hasattr(result, "unknown_future_field")


# ---------------------------------------------------------------------------
# _extract_run_row None/missing field tolerance
# ---------------------------------------------------------------------------

class TestExtractRunRowNullTolerance:
    """
    Verify that _extract_run_row in webui/app.py does not crash when analysis JSON
    contains None/missing numeric fields such as total_cost, total_tokens, or quality_score.
    """

    @pytest.fixture(autouse=True)
    def _import_extract_run_row(self) -> None:
        """Import webui.app lazily so the test can be skipped if the module is unavailable."""
        try:
            from webui.app import _extract_run_row  # type: ignore[import]
            self._extract_run_row = _extract_run_row
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"webui.app not importable in test environment: {exc}")

    def _make_run_dir(self, tmp_path: Path, analysis_data: Dict[str, Any]) -> Path:
        run_dir = tmp_path / "run_abc123"
        run_dir.mkdir()
        (run_dir / "analysis_result.json").write_text(
            json.dumps(analysis_data), encoding="utf-8"
        )
        return run_dir

    def test_extract_run_row_with_all_none_numeric_fields(self, tmp_path: Path) -> None:
        """_extract_run_row must not crash when total_cost/total_tokens/score are None."""
        run_dir = self._make_run_dir(
            tmp_path,
            {
                "project_name": "null_fields_test",
                "total_cost": None,
                "total_tokens": None,
                "quality_score": None,
                "score": None,
                "schema_version": 1,
            },
        )
        row = self._extract_run_row(run_dir)
        assert row["run_id"] == "run_abc123"
        assert row["cost"] is None
        assert row["tokens"] is None
        assert row["quality"] is None

    def test_extract_run_row_with_missing_numeric_fields(self, tmp_path: Path) -> None:
        """_extract_run_row must not crash when numeric fields are absent entirely."""
        run_dir = self._make_run_dir(tmp_path, {"schema_version": 1})
        row = self._extract_run_row(run_dir)
        assert row["run_id"] == "run_abc123"
        assert row["cost"] is None

    def test_extract_run_row_with_inf_cost(self, tmp_path: Path) -> None:
        """_extract_run_row must reject infinite float values for cost, storing None."""
        # JSON does not support Infinity natively; we simulate via a very large string-encoded float
        run_dir = self._make_run_dir(
            tmp_path,
            {"total_cost": "Infinity", "schema_version": 1},
        )
        row = self._extract_run_row(run_dir)
        # "Infinity" string → float("Infinity") → not isfinite → stored as None
        assert row["cost"] is None

    def test_extract_run_row_with_string_tokens(self, tmp_path: Path) -> None:
        """_extract_run_row must not crash when total_tokens is a numeric string."""
        run_dir = self._make_run_dir(
            tmp_path,
            {"total_tokens": "4096", "schema_version": 1},
        )
        row = self._extract_run_row(run_dir)
        assert row["tokens"] == 4096


# ---------------------------------------------------------------------------
# Schema compat: win_loss_rate → win_rate
# ---------------------------------------------------------------------------

class TestSchemaCompatMigration:
    """
    Verify that _apply_schema_compat in webui/app.py migrates legacy field names
    from schema_version=None/0 files to their canonical names.
    """

    @pytest.fixture(autouse=True)
    def _import_apply_schema_compat(self) -> None:
        try:
            from webui.app import _apply_schema_compat  # type: ignore[import]
            self._apply_schema_compat = _apply_schema_compat
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"webui.app not importable in test environment: {exc}")

    def test_win_loss_rate_migrates_to_win_rate_when_schema_version_none(self) -> None:
        """win_loss_rate must be aliased to win_rate when schema_version is None."""
        data: Dict[str, Any] = {"win_loss_rate": 0.55, "schema_version": None}
        result = self._apply_schema_compat(data)
        assert "win_rate" in result
        assert result["win_rate"] == 0.55

    def test_win_loss_rate_migrates_when_schema_version_is_zero_string(self) -> None:
        """win_loss_rate must be aliased to win_rate when schema_version is the string '0'."""
        data: Dict[str, Any] = {"win_loss_rate": 0.62, "schema_version": "0"}
        result = self._apply_schema_compat(data)
        assert result["win_rate"] == 0.62

    def test_win_loss_rate_migrates_when_schema_version_is_zero_int(self) -> None:
        """win_loss_rate must be aliased to win_rate when schema_version is integer 0."""
        data: Dict[str, Any] = {"win_loss_rate": 0.48, "schema_version": 0}
        result = self._apply_schema_compat(data)
        assert result["win_rate"] == 0.48

    def test_win_loss_rate_not_migrated_when_schema_version_is_one(self) -> None:
        """
        win_loss_rate must NOT be aliased when schema_version >= 1, because
        the field does not exist in v1+ and may indicate a different meaning.
        """
        data: Dict[str, Any] = {"win_loss_rate": 0.55, "schema_version": 1}
        result = self._apply_schema_compat(data)
        # win_rate should not be injected for v1+ files
        assert "win_rate" not in result

    def test_win_rate_not_overwritten_when_already_present(self) -> None:
        """
        If both win_loss_rate and win_rate exist in a legacy file, the existing
        win_rate must not be overwritten by the migration.
        """
        data: Dict[str, Any] = {
            "win_loss_rate": 0.55,
            "win_rate": 0.70,
            "schema_version": None,
        }
        result = self._apply_schema_compat(data)
        assert result["win_rate"] == 0.70  # original value preserved

    def test_return_pct_migrates_to_total_return_pct(self) -> None:
        """return_pct must be aliased to total_return_pct on legacy files."""
        data: Dict[str, Any] = {"return_pct": 12.5, "schema_version": 0}
        result = self._apply_schema_compat(data)
        assert result["total_return_pct"] == 12.5


# ---------------------------------------------------------------------------
# RunSnapshot round-trip
# ---------------------------------------------------------------------------

class TestRunSnapshotRoundTrip:
    def test_run_snapshot_minimal_round_trip(self) -> None:
        """RunSnapshot must survive model_dump → JSON → re-parse without field loss."""
        snap = RunSnapshot(run_id="run_001")
        serialised = json.dumps(snap.model_dump(), ensure_ascii=False, default=str)
        raw = json.loads(serialised)
        restored = RunSnapshot(**raw)
        assert restored.run_id == "run_001"
        assert restored.schema_version == 1
        assert restored.gate_decisions == []
        assert restored.stage_records == []

    def test_run_snapshot_preserves_nested_outputs(self) -> None:
        """RunSnapshot.outputs dict must survive JSON round-trip with nested structures."""
        snap = RunSnapshot(
            run_id="run_002",
            outputs={
                "analysis": {"score": 80, "risk_level": "Medium"},
                "codegen": {"files": ["main.py", "backtest.py"]},
            },
        )
        raw = json.loads(json.dumps(snap.model_dump(), ensure_ascii=False, default=str))
        restored = RunSnapshot(**raw)
        assert restored.outputs["analysis"]["score"] == 80
        assert "main.py" in restored.outputs["codegen"]["files"]


# ---------------------------------------------------------------------------
# GateContextBundle round-trip
# ---------------------------------------------------------------------------

class TestGateContextBundleRoundTrip:
    def test_gate_context_bundle_round_trip(self) -> None:
        """GateContextBundle must survive model_dump → JSON → re-parse without field loss."""
        bundle = GateContextBundle(
            executive_summary="Ready for codegen with validation scope.",
            analyst_findings={"research": "Momentum factor verified.", "risk": "Drawdown within bounds."},
            implementation_requirements=["Daily rebalance", "Emit JSON metrics"],
            implementation_constraints=["No live trading code"],
            validation_focus=["Sharpe ≥ 1.0", "Max drawdown ≤ 25%"],
            blocking_unknowns=["Transaction cost sensitivity unverified"],
            rerun_signals={"risk": ["Transaction cost assumption needs evidence"]},
        )
        raw = json.loads(json.dumps(bundle.model_dump(), ensure_ascii=False))
        restored = GateContextBundle(**raw)

        assert restored.executive_summary == bundle.executive_summary
        assert restored.analyst_findings == bundle.analyst_findings
        assert restored.implementation_requirements == bundle.implementation_requirements
        assert restored.blocking_unknowns == bundle.blocking_unknowns
        assert restored.rerun_signals == bundle.rerun_signals
