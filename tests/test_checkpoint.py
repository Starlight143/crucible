# ruff: noqa: E402
"""Tests for crucible.features.checkpoint."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.checkpoint import (
    STAGE_ORDER,
    CheckpointManager,
    ResumeInfo,
    StageCheckpoint,
)


class TestStageCheckpoint(unittest.TestCase):
    def test_default_values(self) -> None:
        cp = StageCheckpoint(stage_name="research_swarm", timestamp="2024-01-01T00:00:00Z")
        self.assertEqual(cp.duration_seconds, 0.0)
        self.assertEqual(cp.data_keys, [])


class TestResumeInfo(unittest.TestCase):
    def test_to_dict(self) -> None:
        info = ResumeInfo(
            run_dir="/tmp/test",
            has_checkpoints=True,
            completed_stages=["librarian_research"],
            last_completed_stage="librarian_research",
            next_stage="research_swarm",
        )
        d = info.to_dict()
        self.assertTrue(d["has_checkpoints"])
        self.assertEqual(d["last_completed_stage"], "librarian_research")
        self.assertEqual(d["next_stage"], "research_swarm")


class TestCheckpointManager(unittest.TestCase):
    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            data = {"key": "value", "count": 42}
            mgr.save_stage("research_swarm", data)

            self.assertTrue(mgr.is_stage_complete("research_swarm"))
            loaded = mgr.load_stage("research_swarm")
            self.assertEqual(loaded, data)

    def test_not_complete_without_save(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            self.assertFalse(mgr.is_stage_complete("research_swarm"))
            self.assertIsNone(mgr.load_stage("research_swarm"))

    def test_clear_stage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.save_stage("codegen", {"files": []})
            self.assertTrue(mgr.is_stage_complete("codegen"))
            mgr.clear_stage("codegen")
            self.assertFalse(mgr.is_stage_complete("codegen"))

    def test_clear_all(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.save_stage("librarian_research", {"ctx": "test"})
            mgr.save_stage("research_swarm", {"data": "test"})
            mgr.clear_all()
            self.assertFalse(mgr.is_stage_complete("librarian_research"))
            self.assertFalse(mgr.is_stage_complete("research_swarm"))

    def test_start_stage_records_timing(self) -> None:
        import time as _time
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.start_stage("codegen")
            _time.sleep(0.050)  # ensure measurable elapsed time on low-res timers
            mgr.save_stage("codegen", {"x": 1})
            # Verify meta file has duration
            meta_path = os.path.join(td, "checkpoints", "codegen.meta.json")
            self.assertTrue(os.path.isfile(meta_path))
            with open(meta_path) as f:
                meta = json.load(f)
            self.assertGreater(meta["duration_seconds"], 0)

    def test_get_resume_info_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            info = mgr.get_resume_info()
            self.assertFalse(info.has_checkpoints)
            self.assertIsNone(info.last_completed_stage)
            self.assertIsNone(info.next_stage)

    def test_get_resume_info_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.save_stage("librarian_research", {"ctx": "test"})
            mgr.save_stage("research_swarm", {"data": "test"})
            info = mgr.get_resume_info()
            self.assertTrue(info.has_checkpoints)
            self.assertEqual(info.last_completed_stage, "research_swarm")
            self.assertEqual(info.next_stage, "direction_debate")
            self.assertEqual(len(info.completed_stages), 2)

    def test_get_resume_info_all_complete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            for stage in STAGE_ORDER:
                mgr.save_stage(stage, {"done": True})
            info = mgr.get_resume_info()
            self.assertEqual(info.last_completed_stage, "postprocessing")
            self.assertIsNone(info.next_stage)

    def test_summary_text_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            text = mgr.summary_text()
            self.assertIn("No checkpoints", text)

    def test_summary_text_with_stages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.save_stage("librarian_research", {"ctx": "ok"})
            text = mgr.summary_text()
            self.assertIn("librarian_research", text)
            self.assertIn("Completed stages", text)

    def test_data_keys_stored(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.save_stage("analysis_crew", {"findings": [], "score": 80})
            meta_path = os.path.join(td, "checkpoints", "analysis_crew.meta.json")
            with open(meta_path) as f:
                meta = json.load(f)
            self.assertIn("findings", meta["data_keys"])
            self.assertIn("score", meta["data_keys"])


    def test_clear_all_partial_failure(self) -> None:
        """clear_all() must remove remaining files even if one removal fails.

        We simulate a partially-removed checkpoint directory by deleting one
        checkpoint file before calling clear_all() — the remaining files must
        still be cleaned up and is_stage_complete() must return False for all.
        """
        with tempfile.TemporaryDirectory() as td:
            mgr = CheckpointManager(td)
            mgr.save_stage("librarian_research", {"ctx": "test"})
            mgr.save_stage("research_swarm", {"data": "test"})
            mgr.save_stage("codegen", {"files": []})

            # Manually remove one file to simulate a mid-clear failure state
            checkpoint_dir = os.path.join(td, "checkpoints")
            os.remove(os.path.join(checkpoint_dir, "research_swarm.json"))

            # clear_all() must not raise and must remove what remains
            mgr.clear_all()

            self.assertFalse(mgr.is_stage_complete("librarian_research"))
            self.assertFalse(mgr.is_stage_complete("research_swarm"))
            self.assertFalse(mgr.is_stage_complete("codegen"))


class TestStageOrder(unittest.TestCase):
    def test_has_expected_stages(self) -> None:
        self.assertIn("librarian_research", STAGE_ORDER)
        self.assertIn("research_swarm", STAGE_ORDER)
        self.assertIn("codegen", STAGE_ORDER)
        self.assertIn("postprocessing", STAGE_ORDER)
        self.assertEqual(STAGE_ORDER[0], "librarian_research")
        self.assertEqual(STAGE_ORDER[-1], "postprocessing")


if __name__ == "__main__":
    unittest.main()
