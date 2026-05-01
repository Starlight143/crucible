"""Tests for crucible.features.batch_runner"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.batch_runner import (
    BatchProjectResult,
    BatchSummaryReport,
    _run_single_project,
    discover_projects,
)


# ── _run_single_project ───────────────────────────────────────────────────────

class TestRunSingleProject:
    def test_success_returns_successful_result(self, tmp_path):
        """Successful run_fn produces BatchProjectResult(success=True)."""
        run_dir = str(tmp_path / "run_out")
        os.makedirs(run_dir, exist_ok=True)

        def run_fn(proj_dir: str) -> str:
            return run_dir

        project_dir = str(tmp_path / "project")
        os.makedirs(project_dir, exist_ok=True)
        result = _run_single_project(project_dir, run_fn)

        assert result.success is True
        assert result.run_dir == run_dir
        assert result.error == ""

    def test_ordinary_exception_returns_failed_result(self, tmp_path):
        """RuntimeError from run_fn is captured as BatchProjectResult(success=False)."""
        def run_fn(proj_dir: str) -> None:
            raise RuntimeError("analysis failed")

        project_dir = str(tmp_path / "project")
        os.makedirs(project_dir, exist_ok=True)
        result = _run_single_project(project_dir, run_fn)

        assert result.success is False
        assert "analysis failed" in result.error

    def test_cancellation_propagates_out_of_run_single_project(self, tmp_path):
        """
        OperationCancelledError from run_fn must NOT be swallowed as a project
        failure.  It must propagate to abort the entire batch.

        Previously `except Exception` caught OperationCancelledError and returned
        BatchProjectResult(success=False, error=...), silently treating intentional
        cancellation as an ordinary per-project failure while continuing the batch.
        """
        from crucible.cancellation import OperationCancelledError

        def cancelling_run_fn(proj_dir: str) -> None:
            raise OperationCancelledError("user cancelled")

        project_dir = str(tmp_path / "project")
        os.makedirs(project_dir, exist_ok=True)

        with pytest.raises(OperationCancelledError):
            _run_single_project(project_dir, cancelling_run_fn)

    def test_project_name_set_from_basename(self, tmp_path):
        """project_name must be the last component of project_dir."""
        project_dir = str(tmp_path / "my_strategy")
        os.makedirs(project_dir, exist_ok=True)

        result = _run_single_project(project_dir, lambda _: None)

        assert result.project_name == "my_strategy"

    def test_duration_positive(self, tmp_path):
        """duration_seconds must be a positive float recorded by wall-clock."""
        import time as _time

        project_dir = str(tmp_path / "project")
        os.makedirs(project_dir, exist_ok=True)

        # Use a callback with a measurable delay so wall-clock > 0 is guaranteed.
        # Windows time.monotonic() has ~15ms resolution; use 50ms for a safe margin.
        result = _run_single_project(project_dir, lambda _: _time.sleep(0.050))

        assert isinstance(result.duration_seconds, float)
        assert result.duration_seconds > 0.0


# ── discover_projects ────────────────────────────────────────────────────────

class TestDiscoverProjects:
    def test_finds_python_project_dir(self, tmp_path):
        """A sub-directory containing .py files is discovered."""
        proj = tmp_path / "my_proj"
        proj.mkdir()
        (proj / "main.py").write_text("x = 1")

        found = discover_projects(str(tmp_path))
        assert str(proj) in found

    def test_ignores_empty_dir(self, tmp_path):
        """A sub-directory with no Python files is not discovered."""
        empty = tmp_path / "empty_dir"
        empty.mkdir()

        found = discover_projects(str(tmp_path))
        assert str(empty) not in found

    def test_finds_pyproject_toml_project(self, tmp_path):
        """A sub-directory with pyproject.toml counts as a Python project."""
        proj = tmp_path / "pkg_proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "pkg"')

        found = discover_projects(str(tmp_path))
        assert str(proj) in found

    def test_nonexistent_batch_dir_returns_empty(self, tmp_path):
        """A non-existent batch_dir returns an empty list without raising."""
        found = discover_projects(str(tmp_path / "nonexistent"))
        assert found == []
