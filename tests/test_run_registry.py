# ruff: noqa: E402
"""Tests for crucible.features.run_registry."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.run_registry import (
    RunRecord,
    RunRegistry,
)


class TestRunRecord(unittest.TestCase):
    def test_default_values(self) -> None:
        r = RunRecord(
            run_id="run_001", run_dir="/tmp/run_001",
            project_name="test", score=80.0,
            risk_level="Medium", mode="quant",
            provider="openrouter", timestamp="2024-01-01",
        )
        self.assertFalse(r.has_security_report)
        self.assertTrue(r.security_passed)
        self.assertFalse(r.has_validation_report)
        self.assertIsNone(r.validation_verdict)


class TestRunRegistry(unittest.TestCase):
    def _make_run(self, saved_dir: str, run_id: str, score: float) -> str:
        run_dir = os.path.join(saved_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)
        analysis = {
            "project_name": "test_project",
            "score": score,
            "risk_level": "Medium",
        }
        with open(os.path.join(run_dir, "analysis_result.json"), "w") as f:
            json.dump(analysis, f)
        return run_dir

    def test_sync_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 80)
            self._make_run(saved, "run_002", 90)

            registry = RunRegistry(td)
            count = registry.sync()
            self.assertEqual(count, 2)
            self.assertEqual(registry.count_runs(), 2)
            registry.close()

    def test_query_top_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 60)
            self._make_run(saved, "run_002", 90)
            self._make_run(saved, "run_003", 75)

            registry = RunRegistry(td)
            registry.sync()
            top = registry.query_top_runs(limit=2)
            self.assertEqual(len(top), 2)
            self.assertEqual(top[0].score, 90)
            self.assertEqual(top[1].score, 75)
            registry.close()

    def test_query_top_runs_by_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 80)

            registry = RunRegistry(td)
            registry.sync()
            results = registry.query_top_runs(project_name="test_project")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].project_name, "test_project")

            no_results = registry.query_top_runs(project_name="nonexistent")
            self.assertEqual(len(no_results), 0)
            registry.close()

    def test_query_recent_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 80)

            registry = RunRegistry(td)
            registry.sync()
            recent = registry.query_recent_runs(limit=5)
            self.assertGreater(len(recent), 0)
            registry.close()

    def test_query_project_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 70)
            self._make_run(saved, "run_002", 85)

            registry = RunRegistry(td)
            registry.sync()
            history = registry.query_project_history("test_project")
            self.assertEqual(len(history), 2)
            registry.close()

    def test_query_failed_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            run_dir = self._make_run(saved, "run_sec", 50)
            # Add a failing security report
            with open(os.path.join(run_dir, "security_report.json"), "w") as f:
                json.dump({"passed": False}, f)

            registry = RunRegistry(td)
            registry.sync()
            failed = registry.query_failed_security()
            self.assertEqual(len(failed), 1)
            self.assertFalse(failed[0].security_passed)
            registry.close()

    def test_sync_no_saved_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = RunRegistry(td)
            count = registry.sync()
            self.assertEqual(count, 0)
            registry.close()

    def test_summary_text_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = RunRegistry(td)
            text = registry.summary_text()
            self.assertIn("empty", text)
            registry.close()

    def test_summary_text_with_data(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 80)

            registry = RunRegistry(td)
            registry.sync()
            text = registry.summary_text()
            self.assertIn("1 run", text)
            self.assertIn("Average score", text)
            registry.close()

    def test_upsert_updates_existing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            saved = os.path.join(td, "saved_projects")
            os.makedirs(saved)
            self._make_run(saved, "run_001", 60)

            registry = RunRegistry(td)
            registry.sync()
            # Update the analysis
            run_dir = os.path.join(saved, "run_001")
            with open(os.path.join(run_dir, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "test_project", "score": 95}, f)
            registry.sync()
            self.assertEqual(registry.count_runs(), 1)
            top = registry.query_top_runs()
            self.assertEqual(top[0].score, 95)
            registry.close()

    def test_close_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = RunRegistry(td)
            registry.close()
            registry.close()  # Should not raise

    def test_count_runs_empty_db(self) -> None:
        """count_runs() must return 0 on a fresh registry with no sync."""
        with tempfile.TemporaryDirectory() as td:
            registry = RunRegistry(td)
            self.assertEqual(registry.count_runs(), 0)
            registry.close()


if __name__ == "__main__":
    unittest.main()
