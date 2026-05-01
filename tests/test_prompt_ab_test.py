"""Tests for crucible.features.prompt_ab_test"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.prompt_ab_test import (
    ABTestConfig,
    ABTestReport,
    VariantResult,
    _extract_variant_result,
    _find_latest_run_dir,
    _load_analysis_result,
    _run_variant,
    _write_variant_context_file,
    _remove_file_safe,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_analysis_result(
    tmp_path,
    *,
    dir_name: str = "run_001",
    score: float = 72.0,
    risk_level: str = "medium",
    gate_decision: str = "proceed",
    consensus: str = "Strong market fit.",
    disagreement: str = "Risk concerns persist.",
    blocking_risks: list = None,
    experiments: list = None,
    codegen_scope: str = "production",
) -> str:
    """Create a fake run directory with analysis_result.json."""
    run_dir = tmp_path / dir_name
    run_dir.mkdir()
    data = {
        "score": score,
        "risk_level": risk_level,
        "gate_decision": gate_decision,
        "consensus": consensus,
        "disagreement": disagreement,
        "blocking_risks": blocking_risks or [],
        "experiments": experiments or [],
        "codegen_scope": codegen_scope,
    }
    (run_dir / "analysis_result.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    return str(run_dir)


# ── _load_analysis_result ────────────────────────────────────────────────────

class TestLoadAnalysisResult:
    def test_loads_valid_json(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path, score=80.0)
        data = _load_analysis_result(run_dir)
        assert data["score"] == 80.0

    def test_returns_empty_dict_when_file_missing(self, tmp_path):
        result = _load_analysis_result(str(tmp_path / "nonexistent"))
        assert result == {}

    def test_returns_empty_dict_on_invalid_json(self, tmp_path):
        run_dir = tmp_path / "bad_run"
        run_dir.mkdir()
        (run_dir / "analysis_result.json").write_text("not-json", encoding="utf-8")
        result = _load_analysis_result(str(run_dir))
        assert result == {}

    def test_returns_empty_dict_when_json_is_not_dict(self, tmp_path):
        run_dir = tmp_path / "list_run"
        run_dir.mkdir()
        (run_dir / "analysis_result.json").write_text("[1, 2, 3]", encoding="utf-8")
        result = _load_analysis_result(str(run_dir))
        assert result == {}


# ── _find_latest_run_dir ──────────────────────────────────────────────────────

class TestFindLatestRunDir:
    def test_returns_none_when_no_saved_projects(self, tmp_path):
        result = _find_latest_run_dir(str(tmp_path), created_after=0.0)
        assert result is None

    def test_finds_dir_created_after_timestamp(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        run_dir = saved / "20260101_run_a"
        run_dir.mkdir()
        result = _find_latest_run_dir(str(tmp_path), created_after=0.0)
        assert result == str(run_dir)

    def test_returns_none_when_all_dirs_too_old(self, tmp_path):
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        run_dir = saved / "20260101_run_a"
        run_dir.mkdir()
        future_ts = time.time() + 3600
        result = _find_latest_run_dir(str(tmp_path), created_after=future_ts)
        assert result is None

    def test_created_after_uses_wall_clock_not_monotonic(self, tmp_path):
        """
        Regression: _run_variant previously used time.monotonic() for created_after,
        which is incomparable with os.path.getmtime() (Unix epoch time).
        time.monotonic() returns a small number (seconds since boot, e.g. 5000),
        while epoch time is ~1.7e9.  Using monotonic as created_after would make
        _find_latest_run_dir always return a result regardless of actual creation time.
        This test verifies that epoch-time filtering works correctly.
        """
        saved = tmp_path / "saved_projects"
        saved.mkdir()
        run_dir = saved / "20260101_run_a"
        run_dir.mkdir()

        # An epoch timestamp clearly in the future: all existing dirs are "too old"
        epoch_future = time.time() + 86400  # 1 day ahead
        result_with_epoch = _find_latest_run_dir(str(tmp_path), created_after=epoch_future)
        assert result_with_epoch is None, (
            "With a future epoch timestamp, no dirs should match"
        )

        # A very small "monotonic-like" value: all existing dirs should be "newer"
        # (this was the old broken behaviour — monotonic ~5000 vs epoch ~1.7e9)
        monotonic_like_ts = 9999.0  # looks like seconds-since-boot, not epoch
        result_with_monotonic = _find_latest_run_dir(str(tmp_path), created_after=monotonic_like_ts)
        assert result_with_monotonic == str(run_dir), (
            "With a tiny (monotonic-like) value as created_after, "
            "all dirs should pass the mtime > created_after filter"
        )


# ── _extract_variant_result ───────────────────────────────────────────────────

class TestExtractVariantResult:
    def test_extracts_score_and_risk(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path, score=65.0, risk_level="high")
        vr = _extract_variant_result("test", run_dir, 10.0, None)
        assert vr.score == 65.0
        assert vr.risk_level == "high"

    def test_extracts_blocking_risks(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path, blocking_risks=["r1", "r2"])
        vr = _extract_variant_result("test", run_dir, 5.0, None)
        assert vr.blocking_risks == ["r1", "r2"]

    def test_extracts_experiments_count(self, tmp_path):
        run_dir = _make_analysis_result(
            tmp_path,
            experiments=[{"goal": "g1"}, {"goal": "g2"}],
        )
        vr = _extract_variant_result("test", run_dir, 5.0, None)
        assert vr.experiments_count == 2

    def test_handles_none_run_dir(self):
        vr = _extract_variant_result("noop", None, 0.0, "error msg")
        assert vr.run_dir is None
        assert vr.score is None
        assert vr.error == "error msg"

    def test_label_preserved(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path)
        vr = _extract_variant_result("variant_x", run_dir, 1.0, None)
        assert vr.label == "variant_x"

    def test_elapsed_preserved(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path)
        vr = _extract_variant_result("v", run_dir, 42.5, None)
        assert vr.elapsed_seconds == 42.5

    def test_to_dict_keys(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path)
        vr = _extract_variant_result("v", run_dir, 1.0, None)
        d = vr.to_dict()
        for key in ("label", "score", "risk_level", "gate_decision",
                    "consensus_length", "disagreement_length",
                    "blocking_risks_count", "experiments_count",
                    "codegen_scope", "elapsed_seconds", "error", "log_file"):
            assert key in d

    def test_log_file_propagated(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path)
        vr = _extract_variant_result("v", run_dir, 1.0, None, log_file="/tmp/v.log")
        assert vr.log_file == "/tmp/v.log"
        assert vr.to_dict()["log_file"] == "/tmp/v.log"

    def test_log_file_defaults_none(self, tmp_path):
        run_dir = _make_analysis_result(tmp_path)
        vr = _extract_variant_result("v", run_dir, 1.0, None)
        assert vr.log_file is None


# ── ABTestReport ──────────────────────────────────────────────────────────────

class TestABTestReport:
    def _make_report(
        self,
        score_a: float = 70.0,
        score_b: float = 80.0,
        risk_a: str = "medium",
        risk_b: str = "medium",
        gate_a: str = "proceed",
        gate_b: str = "proceed",
    ) -> ABTestReport:
        va = VariantResult(
            label="a", run_dir=None, elapsed_seconds=1.0,
            score=score_a, risk_level=risk_a, gate_decision=gate_a,
            consensus="a_consensus", disagreement="a_dis",
            blocking_risks=[], experiments_count=0, codegen_scope="production",
        )
        vb = VariantResult(
            label="b", run_dir=None, elapsed_seconds=2.0,
            score=score_b, risk_level=risk_b, gate_decision=gate_b,
            consensus="b_consensus", disagreement="b_dis",
            blocking_risks=[], experiments_count=0, codegen_scope="production",
        )
        return ABTestReport(
            config_variant_a_label="variant_a",
            config_variant_b_label="variant_b",
            variant_a=va,
            variant_b=vb,
        )

    def test_score_delta_positive(self):
        report = self._make_report(score_a=60.0, score_b=80.0)
        assert report.score_delta() == pytest.approx(20.0)

    def test_score_delta_negative(self):
        report = self._make_report(score_a=80.0, score_b=60.0)
        assert report.score_delta() == pytest.approx(-20.0)

    def test_score_delta_zero(self):
        report = self._make_report(score_a=70.0, score_b=70.0)
        assert report.score_delta() == pytest.approx(0.0)

    def test_score_delta_none_when_missing(self):
        va = VariantResult(
            label="a", run_dir=None, elapsed_seconds=1.0,
            score=None, risk_level=None, gate_decision=None,
            consensus="", disagreement="",
            blocking_risks=[], experiments_count=0, codegen_scope="",
        )
        vb = VariantResult(
            label="b", run_dir=None, elapsed_seconds=2.0,
            score=80.0, risk_level=None, gate_decision=None,
            consensus="", disagreement="",
            blocking_risks=[], experiments_count=0, codegen_scope="",
        )
        report = ABTestReport("a", "b", va, vb)
        assert report.score_delta() is None

    def test_summary_text_contains_labels(self):
        report = self._make_report()
        text = report.summary_text()
        assert "variant_a" in text
        assert "variant_b" in text

    def test_summary_text_reports_score_delta(self):
        report = self._make_report(score_a=60.0, score_b=80.0)
        text = report.summary_text()
        assert "+20" in text or "B > A" in text

    def test_summary_text_reports_risk_change(self):
        report = self._make_report(risk_a="low", risk_b="high")
        text = report.summary_text()
        assert "low" in text and "high" in text

    def test_to_dict_structure(self):
        report = self._make_report()
        d = report.to_dict()
        assert "variant_a" in d
        assert "variant_b" in d
        assert "score_delta" in d
        assert "created_at" in d

    def test_to_dict_score_delta(self):
        report = self._make_report(score_a=50.0, score_b=75.0)
        d = report.to_dict()
        assert d["score_delta"] == pytest.approx(25.0)


# ── _write_variant_context_file ───────────────────────────────────────────────

class TestWriteVariantContextFile:
    def test_returns_none_for_empty_context(self, tmp_path):
        result = _write_variant_context_file("v", "", str(tmp_path))
        assert result is None

    def test_returns_none_for_whitespace_context(self, tmp_path):
        result = _write_variant_context_file("v", "   ", str(tmp_path))
        assert result is None

    def test_writes_file_for_non_empty_context(self, tmp_path):
        path = _write_variant_context_file("v", "some context", str(tmp_path))
        assert path is not None
        assert os.path.isfile(path)
        os.remove(path)

    def test_file_contains_label_and_context(self, tmp_path):
        path = _write_variant_context_file("risk_v", "downside risks", str(tmp_path))
        assert path is not None
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "risk_v" in content
        assert "downside risks" in content
        os.remove(path)


# ── _remove_file_safe ─────────────────────────────────────────────────────────

class TestRemoveFileSafe:
    def test_removes_existing_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("x")
        _remove_file_safe(str(f))
        assert not f.exists()

    def test_safe_when_file_missing(self, tmp_path):
        _remove_file_safe(str(tmp_path / "nonexistent.txt"))  # should not raise

    def test_safe_with_none(self):
        _remove_file_safe(None)  # should not raise


# ── ABTestConfig ──────────────────────────────────────────────────────────────

class TestABTestConfig:
    def test_default_labels(self):
        cfg = ABTestConfig(output_dir="/tmp/out")
        assert cfg.variant_a_label == "variant_a"
        assert cfg.variant_b_label == "variant_b"

    def test_default_contexts_empty(self):
        cfg = ABTestConfig(output_dir="/tmp/out")
        assert cfg.variant_a_extra_context == ""
        assert cfg.variant_b_extra_context == ""

    def test_default_args_empty(self):
        cfg = ABTestConfig(output_dir="/tmp/out")
        assert cfg.shared_extra_args == []
        assert cfg.variant_a_extra_args == []
        assert cfg.variant_b_extra_args == []


# ── _run_variant log file ──────────────────────────────────────────────────────

class TestRunVariantLogFile:
    """
    Verify that _run_variant writes subprocess output to a per-variant log file
    and returns its path as the 4th element of the return tuple.
    """

    def _make_fake_proc_result(self, returncode: int = 0):
        result = mock.MagicMock()
        result.returncode = returncode
        return result

    def test_log_file_created_on_success(self, tmp_path):
        output_dir = str(tmp_path / "out")
        workspace_dir = str(tmp_path)

        # Stub subprocess.run and time.time so _find_latest_run_dir returns None
        with mock.patch(
            "crucible.features.prompt_ab_test.subprocess.run",
            return_value=self._make_fake_proc_result(0),
        ), mock.patch(
            "crucible.features.prompt_ab_test.time.time",
            return_value=1_700_000_000.0,
        ):
            run_dir, elapsed, error, log_file = _run_variant(
                "variant_x", "", [], [], workspace_dir, output_dir
            )

        assert log_file is not None
        assert os.path.isfile(log_file), "Log file must exist after _run_variant"
        assert "variant_x" in os.path.basename(log_file)

    def test_log_file_path_matches_label(self, tmp_path):
        output_dir = str(tmp_path / "out")

        with mock.patch(
            "crucible.features.prompt_ab_test.subprocess.run",
            return_value=self._make_fake_proc_result(0),
        ), mock.patch(
            "crucible.features.prompt_ab_test.time.time",
            return_value=1_700_000_000.0,
        ):
            _, _, _, log_file = _run_variant(
                "my_label", "", [], [], str(tmp_path), output_dir
            )

        assert log_file is not None
        assert os.path.basename(log_file) == "my_label.log"

    def test_error_set_on_nonzero_returncode(self, tmp_path):
        output_dir = str(tmp_path / "out")

        with mock.patch(
            "crucible.features.prompt_ab_test.subprocess.run",
            return_value=self._make_fake_proc_result(1),
        ), mock.patch(
            "crucible.features.prompt_ab_test.time.time",
            return_value=1_700_000_000.0,
        ):
            _, _, error, _ = _run_variant(
                "v", "", [], [], str(tmp_path), output_dir
            )

        assert error is not None
        assert "exit code 1" in error
