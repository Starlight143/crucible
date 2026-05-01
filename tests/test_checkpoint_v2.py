"""Tests for v2 additions in crucible.features.checkpoint"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.checkpoint import (
    CheckpointManager,
    StageState,
    StageCheckpoint,
    ResumeInfo,
    STAGE_ORDER,
)


@pytest.fixture
def run_dir(tmp_path):
    return str(tmp_path / "run_dir")


# ── StageState ────────────────────────────────────────────────────────────────

class TestStageState:
    def test_values(self):
        assert StageState.PENDING.value == "pending"
        assert StageState.RUNNING.value == "running"
        assert StageState.COMPLETED.value == "completed"
        assert StageState.FAILED.value == "failed"
        assert StageState.SKIPPED.value == "skipped"

    def test_is_terminal_completed(self):
        assert StageState.COMPLETED.is_terminal
        assert StageState.SKIPPED.is_terminal

    def test_is_not_terminal_pending(self):
        assert not StageState.PENDING.is_terminal
        assert not StageState.RUNNING.is_terminal
        assert not StageState.FAILED.is_terminal

    def test_is_successful(self):
        assert StageState.COMPLETED.is_successful
        assert not StageState.FAILED.is_successful

    def test_from_str_valid(self):
        assert StageState.from_str("completed") == StageState.COMPLETED
        assert StageState.from_str("FAILED") == StageState.FAILED

    def test_from_str_unknown_defaults_pending(self):
        assert StageState.from_str("garbage_value") == StageState.PENDING
        assert StageState.from_str("") == StageState.PENDING


# ── set_state / get_state ─────────────────────────────────────────────────────

class TestSetGetState:
    def test_default_is_pending(self, run_dir):
        mgr = CheckpointManager(run_dir)
        assert mgr.get_state("librarian_research") == StageState.PENDING

    def test_set_and_get_running(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.set_state("research_swarm", StageState.RUNNING)
        assert mgr.get_state("research_swarm") == StageState.RUNNING

    def test_set_failed_with_error(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.set_state("codegen", StageState.FAILED, error="ValueError: bad input")
        assert mgr.get_state("codegen") == StageState.FAILED

    def test_set_skipped(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.set_state("direction_debate", StageState.SKIPPED)
        assert mgr.get_state("direction_debate") == StageState.SKIPPED

    def test_error_stored_in_state_file(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.set_state("codegen", StageState.FAILED, error="RuntimeError: crash")
        state_path = os.path.join(run_dir, "checkpoints", "codegen.state.json")
        with open(state_path) as f:
            data = json.load(f)
        assert data["error"] == "RuntimeError: crash"

    def test_error_truncated_at_500(self, run_dir):
        mgr = CheckpointManager(run_dir)
        long_error = "x" * 1000
        mgr.set_state("codegen", StageState.FAILED, error=long_error)
        state_path = os.path.join(run_dir, "checkpoints", "codegen.state.json")
        with open(state_path) as f:
            data = json.load(f)
        assert len(data["error"]) <= 500


# ── save_stage → COMPLETED state ──────────────────────────────────────────────

class TestSaveStageUpdatesState:
    def test_save_stage_sets_completed(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("research_swarm", {"data": "x"})
        assert mgr.get_state("research_swarm") == StageState.COMPLETED

    def test_state_in_meta_json(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("analysis_crew", {"key": "val"})
        meta_path = os.path.join(run_dir, "checkpoints", "analysis_crew.meta.json")
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["state"] == StageState.COMPLETED.value


# ── clear_stage removes state file ────────────────────────────────────────────

class TestClearStage:
    def test_clear_removes_state_file(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("codegen", {"x": 1})
        mgr.clear_stage("codegen")
        assert mgr.get_state("codegen") == StageState.PENDING

    def test_clear_all_removes_state_files(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("research_swarm", {})
        mgr.save_stage("codegen", {})
        mgr.clear_all()
        assert mgr.get_state("research_swarm") == StageState.PENDING
        assert mgr.get_state("codegen") == StageState.PENDING


# ── get_resume_info v2 ────────────────────────────────────────────────────────

class TestResumeInfoV2:
    def test_stage_states_populated(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("librarian_research", {"data": 1})
        mgr.set_state("research_swarm", StageState.FAILED)
        info = mgr.get_resume_info()
        assert info.stage_states.get("librarian_research") == StageState.COMPLETED
        assert info.stage_states.get("research_swarm") == StageState.FAILED

    def test_checkpoint_state_field(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("librarian_research", {"x": 1})
        info = mgr.get_resume_info()
        cp = info.checkpoints[0]
        assert cp.state == StageState.COMPLETED

    def test_to_dict_includes_stage_states(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("librarian_research", {})
        d = mgr.get_resume_info().to_dict()
        assert "stage_states" in d
        assert d["stage_states"]["librarian_research"] == "completed"


# ── get_failed_stages ─────────────────────────────────────────────────────────

class TestGetFailedStages:
    def test_empty_when_no_failures(self, run_dir):
        mgr = CheckpointManager(run_dir)
        assert mgr.get_failed_stages() == []

    def test_detects_failed_stage(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.set_state("codegen", StageState.FAILED)
        assert "codegen" in mgr.get_failed_stages()

    def test_does_not_include_completed(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("research_swarm", {})
        assert "research_swarm" not in mgr.get_failed_stages()


# ── state_context ─────────────────────────────────────────────────────────────

class TestStateContext:
    def test_sets_running_on_enter(self, run_dir):
        mgr = CheckpointManager(run_dir)
        state_during = []

        # Inspect state from inside context by reading state file directly
        checkpoint_dir = os.path.join(run_dir, "checkpoints")
        with mgr.state_context("codegen") as ctx:
            state_during.append(mgr.get_state("codegen"))
            ctx.save({"code": "print()"})

        assert state_during[0] == StageState.RUNNING

    def test_sets_completed_on_exit_with_save(self, run_dir):
        mgr = CheckpointManager(run_dir)
        with mgr.state_context("codegen") as ctx:
            ctx.save({"generated": True})
        assert mgr.get_state("codegen") == StageState.COMPLETED

    def test_sets_completed_on_exit_without_save(self, run_dir):
        mgr = CheckpointManager(run_dir)
        with mgr.state_context("research_swarm"):
            pass  # no ctx.save
        assert mgr.get_state("research_swarm") == StageState.COMPLETED

    def test_sets_failed_on_exception(self, run_dir):
        mgr = CheckpointManager(run_dir)
        with pytest.raises(ValueError):
            with mgr.state_context("analysis_crew"):
                raise ValueError("crew exploded")
        assert mgr.get_state("analysis_crew") == StageState.FAILED

    def test_save_persists_data(self, run_dir):
        mgr = CheckpointManager(run_dir)
        with mgr.state_context("codegen") as ctx:
            ctx.save({"result": 42})
        data = mgr.load_stage("codegen")
        assert data == {"result": 42}

    def test_multiple_saves_last_wins(self, run_dir):
        mgr = CheckpointManager(run_dir)
        with mgr.state_context("codegen") as ctx:
            ctx.save({"v": 1})
            ctx.save({"v": 2})
        assert mgr.load_stage("codegen") == {"v": 2}


# ── state_context + cooperative cancellation ──────────────────────────────────

class TestStateContextCancellationPropagation:
    """
    Regression tests: state_context must NOT mark a stage FAILED on cooperative
    cancellation (OperationCancelledError).

    Previously ``except Exception`` in ``state_context`` caught
    ``OperationCancelledError`` and wrote ``StageState.FAILED`` before re-raising,
    silently misclassifying an intentional cancellation as a pipeline stage failure.
    This would cause the resume logic and monitoring to report failures and
    potentially trigger spurious auto-remediation for cancelled operations.
    """

    def test_cancellation_propagates_out_of_state_context(self, run_dir):
        """OperationCancelledError must escape state_context without being swallowed."""
        from crucible.cancellation import OperationCancelledError

        mgr = CheckpointManager(run_dir)
        with pytest.raises(OperationCancelledError):
            with mgr.state_context("codegen"):
                raise OperationCancelledError("user cancelled")

    def test_cancelled_stage_is_not_marked_failed(self, run_dir):
        """
        When OperationCancelledError is raised inside state_context, the stage
        must NOT be marked FAILED.  The stage is left as RUNNING (set at context
        entry) — resume logic can re-run it from scratch on the next attempt.
        """
        from crucible.cancellation import OperationCancelledError

        mgr = CheckpointManager(run_dir)
        with pytest.raises(OperationCancelledError):
            with mgr.state_context("analysis_crew"):
                raise OperationCancelledError("user cancelled")

        state = mgr.get_state("analysis_crew")
        assert state != StageState.FAILED, (
            f"state_context incorrectly marked stage as FAILED on cooperative "
            f"cancellation; got {state!r}. OperationCancelledError must not be "
            f"treated as a stage failure."
        )

    def test_ordinary_exception_still_marks_stage_failed(self, run_dir):
        """Ordinary exceptions must still mark the stage FAILED (regression guard)."""
        mgr = CheckpointManager(run_dir)
        with pytest.raises(ValueError):
            with mgr.state_context("codegen"):
                raise ValueError("real failure")

        assert mgr.get_state("codegen") == StageState.FAILED


# ── Backward compatibility ────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_legacy_checkpoint_without_state_file_reads_as_completed(self, run_dir):
        """Checkpoints created before v2 (no .state.json) should read as COMPLETED."""
        mgr = CheckpointManager(run_dir)
        # Manually create only the data file (no state file)
        os.makedirs(os.path.join(run_dir, "checkpoints"), exist_ok=True)
        stage_path = os.path.join(run_dir, "checkpoints", "research_swarm.json")
        with open(stage_path, "w") as f:
            json.dump({"legacy": True}, f)
        assert mgr.get_state("research_swarm") == StageState.COMPLETED

    def test_is_stage_complete_unchanged(self, run_dir):
        mgr = CheckpointManager(run_dir)
        assert not mgr.is_stage_complete("codegen")
        mgr.save_stage("codegen", {"x": 1})
        assert mgr.is_stage_complete("codegen")

    def test_load_stage_unchanged(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("analysis_crew", {"findings": "positive"})
        data = mgr.load_stage("analysis_crew")
        assert data == {"findings": "positive"}

    def test_summary_text_includes_state(self, run_dir):
        mgr = CheckpointManager(run_dir)
        mgr.save_stage("librarian_research", {})
        text = mgr.summary_text()
        assert "completed" in text
