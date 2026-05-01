# ruff: noqa: E402
"""Integration tests: Checkpoint save/load/resume integrity."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.checkpoint import (
    STAGE_ORDER,
    CheckpointManager,
    ResumeInfo,
    StageCheckpoint,
    StageState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(tmp_path: Path, run_id: str = "run_test") -> CheckpointManager:
    """Return a CheckpointManager rooted at a fresh temporary directory."""
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return CheckpointManager(str(run_dir))


def _checkpoint_dir(tmp_path: Path, run_id: str = "run_test") -> Path:
    return tmp_path / run_id / "checkpoints"


# ---------------------------------------------------------------------------
# Stage 1 – checkpoint file created after stage completion
# ---------------------------------------------------------------------------

class TestCheckpointFileCreation:
    def test_checkpoint_file_created_on_save(self, tmp_path: Path) -> None:
        """
        Calling save_stage() must create a JSON checkpoint file under
        {run_dir}/checkpoints/{stage_name}.json.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("research_swarm", {"context": "initial research"})

        ckpt_file = _checkpoint_dir(tmp_path) / "research_swarm.json"
        assert ckpt_file.is_file(), "Checkpoint data file must be created by save_stage()"

    def test_meta_file_created_alongside_checkpoint(self, tmp_path: Path) -> None:
        """
        save_stage() must also create a .meta.json file alongside the data file
        so that resume logic can read timing and state metadata without parsing data.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("direction_debate", {"decision": "proceed"})

        meta_file = _checkpoint_dir(tmp_path) / "direction_debate.meta.json"
        assert meta_file.is_file(), "Meta file must be created alongside checkpoint data file"

    def test_state_file_created_alongside_checkpoint(self, tmp_path: Path) -> None:
        """
        save_stage() must persist a .state.json file so that get_state() can return
        StageState.COMPLETED without requiring the full data file to be parsed.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("analysis_crew", {"score": 78})

        state_file = _checkpoint_dir(tmp_path) / "analysis_crew.state.json"
        assert state_file.is_file(), "State file must be created by save_stage()"

    def test_is_stage_complete_returns_true_after_save(self, tmp_path: Path) -> None:
        """
        is_stage_complete() must return True immediately after save_stage() is called,
        confirming the checkpoint is readable for resume purposes.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("codegen", {"files": []})
        assert mgr.is_stage_complete("codegen") is True

    def test_is_stage_complete_returns_false_before_save(self, tmp_path: Path) -> None:
        """
        is_stage_complete() must return False for a stage that has never been saved,
        so the pipeline knows it must run that stage rather than resuming past it.
        """
        mgr = _make_manager(tmp_path)
        assert mgr.is_stage_complete("research_swarm") is False


# ---------------------------------------------------------------------------
# Stage 2 – required fields in checkpoint meta/state
# ---------------------------------------------------------------------------

class TestCheckpointRequiredFields:
    def test_meta_json_contains_stage_name(self, tmp_path: Path) -> None:
        """
        The .meta.json file must contain a 'stage_name' key matching the stage
        so that audit tooling can identify the file without depending on filename.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("librarian_research", {"query": "momentum"})

        meta_path = _checkpoint_dir(tmp_path) / "librarian_research.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["stage_name"] == "librarian_research"

    def test_meta_json_contains_timestamp(self, tmp_path: Path) -> None:
        """
        The .meta.json file must contain an ISO-8601 'timestamp' field so that
        dashboards and audit logs can show when each stage completed.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("research_swarm", {"findings": []})

        meta_path = _checkpoint_dir(tmp_path) / "research_swarm.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "timestamp" in meta
        assert len(str(meta["timestamp"])) > 0

    def test_meta_json_contains_duration_seconds(self, tmp_path: Path) -> None:
        """
        The .meta.json file must contain a numeric 'duration_seconds' field so that
        performance monitoring tools can track stage execution time.
        """
        mgr = _make_manager(tmp_path)
        mgr.start_stage("codegen")
        mgr.save_stage("codegen", {"files": ["main.py"]})

        meta_path = _checkpoint_dir(tmp_path) / "codegen.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "duration_seconds" in meta
        assert isinstance(meta["duration_seconds"], (int, float))
        assert meta["duration_seconds"] >= 0

    def test_meta_json_contains_state_field(self, tmp_path: Path) -> None:
        """
        The .meta.json file must contain a 'state' field equal to 'completed' so that
        the v2 state machine and legacy code can both read stage completion status.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("postprocessing", {"readme": "generated"})

        meta_path = _checkpoint_dir(tmp_path) / "postprocessing.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["state"] == StageState.COMPLETED.value

    def test_meta_json_contains_data_keys(self, tmp_path: Path) -> None:
        """
        The .meta.json file must enumerate the top-level keys of the saved data dict,
        so that tools can determine what was checkpointed without reading the full data.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("analysis_crew", {"score": 80, "report": {}, "gate": {}})

        meta_path = _checkpoint_dir(tmp_path) / "analysis_crew.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "data_keys" in meta
        assert "score" in meta["data_keys"]
        assert "report" in meta["data_keys"]


# ---------------------------------------------------------------------------
# Stage 3 – load restores exactly saved state
# ---------------------------------------------------------------------------

class TestCheckpointLoadRestoresState:
    def test_load_stage_returns_exact_dict(self, tmp_path: Path) -> None:
        """
        load_stage() must return a dict identical to what was passed to save_stage(),
        with no field mutation, type coercion, or data loss during JSON serialisation.
        """
        payload: Dict[str, Any] = {
            "run_id": "run_abc",
            "score": 85,
            "findings": ["momentum", "mean reversion"],
            "nested": {"key": "value", "count": 42},
        }
        mgr = _make_manager(tmp_path)
        mgr.save_stage("analysis_crew", payload)

        restored = mgr.load_stage("analysis_crew")
        assert restored == payload

    def test_load_stage_returns_none_for_missing_stage(self, tmp_path: Path) -> None:
        """
        load_stage() must return None for a stage that has never been saved,
        so that the pipeline can treat None as a clean-start signal.
        """
        mgr = _make_manager(tmp_path)
        result = mgr.load_stage("nonexistent_stage")
        assert result is None

    def test_get_state_returns_completed_after_save(self, tmp_path: Path) -> None:
        """
        get_state() must return StageState.COMPLETED after save_stage() is called,
        confirming that the v2 state machine reflects the data-file write.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("research_swarm", {"data": "ok"})
        assert mgr.get_state("research_swarm") == StageState.COMPLETED

    def test_get_state_returns_pending_for_unsaved_stage(self, tmp_path: Path) -> None:
        """
        get_state() must return StageState.PENDING for a stage that was never started,
        distinguishing it from FAILED or SKIPPED states.
        """
        mgr = _make_manager(tmp_path)
        assert mgr.get_state("codegen") == StageState.PENDING


# ---------------------------------------------------------------------------
# Stage 4 – corrupted checkpoint triggers clean restart (not a crash)
# ---------------------------------------------------------------------------

class TestCorruptedCheckpointHandling:
    def test_load_stage_returns_none_for_corrupted_json(self, tmp_path: Path) -> None:
        """
        load_stage() must return None when the checkpoint file contains invalid JSON
        rather than raising an exception, so the pipeline can restart cleanly.
        """
        mgr = _make_manager(tmp_path)
        ckpt_dir = _checkpoint_dir(tmp_path)
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        # Write malformed JSON to simulate a partially written / corrupted file
        (ckpt_dir / "research_swarm.json").write_text(
            "{corrupted: json content <<<", encoding="utf-8"
        )
        result = mgr.load_stage("research_swarm")
        assert result is None, "Corrupted JSON must produce None, not an exception"

    def test_is_stage_complete_returns_true_for_corrupted_file(self, tmp_path: Path) -> None:
        """
        is_stage_complete() checks only file existence, not content validity.
        A corrupted file still counts as 'present', so the caller must use
        load_stage() and check for None to detect corruption.
        """
        mgr = _make_manager(tmp_path)
        ckpt_dir = _checkpoint_dir(tmp_path)
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "analysis_crew.json").write_text("{bad}", encoding="utf-8")
        # File exists → is_stage_complete is True; load will return None
        assert mgr.is_stage_complete("analysis_crew") is True
        assert mgr.load_stage("analysis_crew") is None

    def test_get_resume_info_handles_corrupted_meta_gracefully(self, tmp_path: Path) -> None:
        """
        get_resume_info() must not raise when a .meta.json file is corrupted; it must
        silently use empty/default metadata for that stage and continue loading others.
        """
        mgr = _make_manager(tmp_path)
        # Write a valid data file but a corrupted meta file
        ckpt_dir = _checkpoint_dir(tmp_path)
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "research_swarm.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
        (ckpt_dir / "research_swarm.meta.json").write_text("{BAD JSON!!!", encoding="utf-8")

        # Must not raise
        info = mgr.get_resume_info()
        assert info.has_checkpoints is True
        assert "research_swarm" in info.completed_stages

    def test_save_and_load_after_corrupted_stage(self, tmp_path: Path) -> None:
        """
        After detecting a corrupted checkpoint, saving a fresh checkpoint for the same
        stage must overwrite the corruption and produce a loadable result.
        """
        mgr = _make_manager(tmp_path)
        ckpt_dir = _checkpoint_dir(tmp_path)
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        (ckpt_dir / "codegen.json").write_text("{BROKEN", encoding="utf-8")

        # Overwrite with valid data
        mgr.save_stage("codegen", {"files": ["main.py"]})
        result = mgr.load_stage("codegen")
        assert result == {"files": ["main.py"]}


# ---------------------------------------------------------------------------
# Stage 5 – run_id isolation (different run_id must not share checkpoint data)
# ---------------------------------------------------------------------------

class TestRunIdIsolation:
    def test_different_run_dirs_do_not_share_checkpoints(self, tmp_path: Path) -> None:
        """
        Checkpoints saved under run_A must not be visible to a CheckpointManager
        rooted at run_B, enforcing run-level isolation.
        """
        mgr_a = _make_manager(tmp_path, "run_A")
        mgr_b = _make_manager(tmp_path, "run_B")

        mgr_a.save_stage("research_swarm", {"run": "A"})

        # run_B must not see run_A's checkpoint
        assert mgr_b.is_stage_complete("research_swarm") is False
        assert mgr_b.load_stage("research_swarm") is None

    def test_same_stage_in_two_run_dirs_stores_independently(self, tmp_path: Path) -> None:
        """
        Two CheckpointManagers for different run directories must store the same
        stage name independently, with no cross-contamination of data.
        """
        mgr_a = _make_manager(tmp_path, "run_X")
        mgr_b = _make_manager(tmp_path, "run_Y")

        mgr_a.save_stage("codegen", {"source": "run_X"})
        mgr_b.save_stage("codegen", {"source": "run_Y"})

        assert mgr_a.load_stage("codegen") == {"source": "run_X"}
        assert mgr_b.load_stage("codegen") == {"source": "run_Y"}

    def test_resume_info_does_not_leak_between_run_dirs(self, tmp_path: Path) -> None:
        """
        get_resume_info() for run_B must not report stages that were only saved
        under run_A, even when both share the same parent tmp directory.
        """
        mgr_a = _make_manager(tmp_path, "run_P")
        mgr_b = _make_manager(tmp_path, "run_Q")

        for stage in ["librarian_research", "research_swarm", "direction_debate"]:
            mgr_a.save_stage(stage, {"done": True})

        info_b = mgr_b.get_resume_info()
        assert info_b.has_checkpoints is False
        assert info_b.completed_stages == []


# ---------------------------------------------------------------------------
# Stage 6 – deleting the checkpoint file triggers full restart from Stage 0
# ---------------------------------------------------------------------------

class TestCheckpointDeletion:
    def test_delete_single_stage_removes_file(self, tmp_path: Path) -> None:
        """
        clear_stage() must delete the checkpoint data file so that is_stage_complete()
        returns False and the pipeline re-runs that stage from scratch.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("research_swarm", {"data": "ok"})
        assert mgr.is_stage_complete("research_swarm") is True

        mgr.clear_stage("research_swarm")
        assert mgr.is_stage_complete("research_swarm") is False

    def test_clear_all_removes_all_stages(self, tmp_path: Path) -> None:
        """
        clear_all() must remove every checkpoint so that all stages return False
        from is_stage_complete(), causing a full restart from Stage 0.
        """
        mgr = _make_manager(tmp_path)
        stages_to_save = ["librarian_research", "research_swarm", "analysis_crew", "codegen"]
        for stage in stages_to_save:
            mgr.save_stage(stage, {"done": True})

        mgr.clear_all()

        for stage in stages_to_save:
            assert mgr.is_stage_complete(stage) is False, f"{stage} must be cleared by clear_all()"

    def test_clear_all_causes_resume_info_to_show_no_checkpoints(self, tmp_path: Path) -> None:
        """
        After clear_all(), get_resume_info() must report has_checkpoints=False
        so the pipeline entry-point triggers a full restart from Stage 0.
        """
        mgr = _make_manager(tmp_path)
        for stage in STAGE_ORDER[:3]:
            mgr.save_stage(stage, {"progress": stage})

        mgr.clear_all()
        info = mgr.get_resume_info()
        assert info.has_checkpoints is False
        assert info.last_completed_stage is None
        assert info.next_stage is None

    def test_missing_checkpoint_dir_causes_no_checkpoints_resume_info(self, tmp_path: Path) -> None:
        """
        When no checkpoint directory exists at all (fresh run dir), get_resume_info()
        must return has_checkpoints=False rather than raising an OSError.
        """
        run_dir = tmp_path / "fresh_run"
        run_dir.mkdir()
        mgr = CheckpointManager(str(run_dir))
        info = mgr.get_resume_info()
        assert info.has_checkpoints is False
        assert info.next_stage is None

    def test_load_after_delete_returns_none(self, tmp_path: Path) -> None:
        """
        After clear_stage() is called, load_stage() must return None so callers
        treating None as a re-run signal behave correctly.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("direction_debate", {"approved": True})
        mgr.clear_stage("direction_debate")
        assert mgr.load_stage("direction_debate") is None


# ---------------------------------------------------------------------------
# Resume position: get_resume_info returns correct next_stage
# ---------------------------------------------------------------------------

class TestResumePosition:
    def test_resume_info_next_stage_is_first_stage_when_empty(self, tmp_path: Path) -> None:
        """
        With no completed stages, get_resume_info() must report next_stage=None
        (no last_completed_stage → no resume point), meaning the pipeline starts
        from the first stage in STAGE_ORDER.
        """
        mgr = _make_manager(tmp_path)
        info = mgr.get_resume_info()
        assert info.has_checkpoints is False
        assert info.last_completed_stage is None
        assert info.next_stage is None

    def test_resume_info_correct_next_stage_after_partial_completion(self, tmp_path: Path) -> None:
        """
        After completing the first two stages, get_resume_info().next_stage must
        be the third stage in STAGE_ORDER so the pipeline skips already-done stages.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage(STAGE_ORDER[0], {"done": True})
        mgr.save_stage(STAGE_ORDER[1], {"done": True})

        info = mgr.get_resume_info()
        assert info.last_completed_stage == STAGE_ORDER[1]
        assert info.next_stage == STAGE_ORDER[2]

    def test_resume_info_next_stage_is_none_when_all_complete(self, tmp_path: Path) -> None:
        """
        When every stage in STAGE_ORDER is complete, next_stage must be None
        indicating that the run finished and there is nothing left to resume.
        """
        mgr = _make_manager(tmp_path)
        for stage in STAGE_ORDER:
            mgr.save_stage(stage, {"done": True})

        info = mgr.get_resume_info()
        assert info.last_completed_stage == STAGE_ORDER[-1]
        assert info.next_stage is None

    def test_resume_info_completed_stages_list_preserves_order(self, tmp_path: Path) -> None:
        """
        get_resume_info().completed_stages must list stages in STAGE_ORDER order,
        not in filesystem traversal order, so resume logic always gets a predictable list.
        """
        mgr = _make_manager(tmp_path)
        # Save in reverse order to expose any ordering assumption bugs
        for stage in reversed(STAGE_ORDER[:4]):
            mgr.save_stage(stage, {"done": True})

        info = mgr.get_resume_info()
        assert info.completed_stages == list(STAGE_ORDER[:4])

    def test_resume_info_to_dict_is_json_serialisable(self, tmp_path: Path) -> None:
        """
        ResumeInfo.to_dict() must produce a dict that is fully JSON-serialisable
        so that the pipeline can embed it in run metadata without custom encoders.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("librarian_research", {"ctx": "test"})
        info = mgr.get_resume_info()
        # Must not raise
        serialised = json.dumps(info.to_dict())
        assert "librarian_research" in serialised


# ---------------------------------------------------------------------------
# v2 state machine: set_state / get_state / state_context
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_set_state_persists_failed(self, tmp_path: Path) -> None:
        """
        set_state(FAILED) must write a state file readable by get_state(),
        allowing the pipeline to distinguish a crashed stage from a pending one.
        """
        mgr = _make_manager(tmp_path)
        mgr.set_state("research_swarm", StageState.FAILED, error="timeout after 300s")
        assert mgr.get_state("research_swarm") == StageState.FAILED

    def test_set_state_persists_running(self, tmp_path: Path) -> None:
        """
        set_state(RUNNING) must be readable by get_state() so that crash-recovery
        tooling can detect in-progress stages that were killed mid-execution.
        """
        mgr = _make_manager(tmp_path)
        mgr.set_state("codegen", StageState.RUNNING)
        assert mgr.get_state("codegen") == StageState.RUNNING

    def test_get_failed_stages_returns_failed_only(self, tmp_path: Path) -> None:
        """
        get_failed_stages() must return exactly the stages whose state is FAILED,
        excluding COMPLETED, RUNNING, and PENDING stages.
        """
        mgr = _make_manager(tmp_path)
        mgr.save_stage("librarian_research", {"done": True})         # COMPLETED
        mgr.set_state("research_swarm", StageState.FAILED, error="x")  # FAILED
        mgr.set_state("direction_debate", StageState.RUNNING)         # RUNNING

        failed = mgr.get_failed_stages()
        assert "research_swarm" in failed
        assert "librarian_research" not in failed
        assert "direction_debate" not in failed

    def test_state_context_marks_completed_on_success(self, tmp_path: Path) -> None:
        """
        state_context() must transition the stage from RUNNING → COMPLETED when
        the with-block exits normally, even if ctx.save() is not called.
        """
        mgr = _make_manager(tmp_path)
        with mgr.state_context("postprocessing"):
            pass  # no ctx.save() intentionally

        assert mgr.get_state("postprocessing") == StageState.COMPLETED

    def test_state_context_marks_failed_on_exception(self, tmp_path: Path) -> None:
        """
        state_context() must transition the stage to FAILED and re-raise the exception
        when the with-block raises, so the caller can handle or log the failure.
        """
        mgr = _make_manager(tmp_path)
        with pytest.raises(ValueError, match="deliberate test failure"):
            with mgr.state_context("analysis_crew"):
                raise ValueError("deliberate test failure")

        assert mgr.get_state("analysis_crew") == StageState.FAILED

    def test_state_context_save_persists_data_and_marks_completed(self, tmp_path: Path) -> None:
        """
        ctx.save(data) inside state_context() must both persist the checkpoint data
        and mark the stage COMPLETED in the state file.
        """
        mgr = _make_manager(tmp_path)
        with mgr.state_context("codegen") as ctx:
            ctx.save({"files": ["strategy.py"]})

        assert mgr.get_state("codegen") == StageState.COMPLETED
        assert mgr.load_stage("codegen") == {"files": ["strategy.py"]}
