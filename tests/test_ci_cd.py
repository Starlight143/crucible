# ruff: noqa: E402
"""Tests for crucible.features.ci_cd."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.ci_cd import (
    GitHubAnnotation,
    _safe_int,
    _score_level,
    _severity_to_level,
    build_github_annotations,
    build_step_summary_markdown,
    write_github_outputs,
)


class TestGitHubAnnotation(unittest.TestCase):
    def test_basic_notice(self) -> None:
        ann = GitHubAnnotation(level="notice", message="all good")
        cmd = ann.to_workflow_command()
        self.assertEqual(cmd, "::notice::all good")

    def test_with_file_and_line(self) -> None:
        ann = GitHubAnnotation(level="error", message="bad thing", file="src/main.py", line=42)
        cmd = ann.to_workflow_command()
        self.assertIn("file=src/main.py", cmd)
        self.assertIn("line=42", cmd)
        self.assertIn("::error ", cmd)

    def test_title_percent_encoded(self) -> None:
        ann = GitHubAnnotation(level="warning", message="msg", title="score: 85, high")
        cmd = ann.to_workflow_command()
        self.assertIn("title=score%3A 85%2C high", cmd)

    def test_message_colon_not_encoded(self) -> None:
        # Colons and commas in the message body must NOT be encoded — only in params.
        ann = GitHubAnnotation(level="notice", message="score: 85, risk: low")
        cmd = ann.to_workflow_command()
        self.assertIn("::score: 85, risk: low", cmd)

    def test_message_newline_encoded(self) -> None:
        ann = GitHubAnnotation(level="notice", message="line1\nline2")
        cmd = ann.to_workflow_command()
        self.assertIn("%0A", cmd)

    def test_windows_path_normalised(self) -> None:
        ann = GitHubAnnotation(level="error", message="oops", file="src\\main.py")
        cmd = ann.to_workflow_command()
        self.assertIn("file=src/main.py", cmd)

    def test_line_zero_omitted(self) -> None:
        ann = GitHubAnnotation(level="notice", message="x", line=0)
        cmd = ann.to_workflow_command()
        self.assertNotIn("line=", cmd)

    def test_line_string_handled(self) -> None:
        # line arriving as a string from JSON should still produce a valid command
        ann = GitHubAnnotation(level="error", message="oops", line="12")  # type: ignore[arg-type]
        cmd = ann.to_workflow_command()
        self.assertIn("line=12", cmd)

    def test_line_invalid_string_omitted(self) -> None:
        ann = GitHubAnnotation(level="notice", message="x", line="abc")  # type: ignore[arg-type]
        cmd = ann.to_workflow_command()
        self.assertNotIn("line=", cmd)


class TestSafeInt(unittest.TestCase):
    def test_integer(self) -> None:
        self.assertEqual(_safe_int(5), 5)

    def test_string_integer(self) -> None:
        self.assertEqual(_safe_int("12"), 12)

    def test_none(self) -> None:
        self.assertIsNone(_safe_int(None))

    def test_invalid_string(self) -> None:
        self.assertIsNone(_safe_int("abc"))

    def test_float_string(self) -> None:
        # "12.5" is not directly parseable by int(), _safe_int should return None
        self.assertIsNone(_safe_int("12.5"))


class TestScoreLevel(unittest.TestCase):
    def test_high_score(self) -> None:
        self.assertEqual(_score_level(80), "notice")

    def test_mid_score(self) -> None:
        self.assertEqual(_score_level(55), "warning")

    def test_low_score(self) -> None:
        self.assertEqual(_score_level(40), "error")

    def test_invalid(self) -> None:
        self.assertEqual(_score_level("N/A"), "notice")


class TestSeverityToLevel(unittest.TestCase):
    def test_critical(self) -> None:
        self.assertEqual(_severity_to_level("critical"), "error")

    def test_high(self) -> None:
        self.assertEqual(_severity_to_level("HIGH"), "error")

    def test_medium(self) -> None:
        self.assertEqual(_severity_to_level("medium"), "warning")

    def test_low(self) -> None:
        self.assertEqual(_severity_to_level("low"), "notice")


class TestBuildGitHubAnnotations(unittest.TestCase):
    def _write_analysis(self, td: str, score: float = 80.0) -> None:
        with open(os.path.join(td, "analysis_result.json"), "w") as f:
            json.dump({
                "project_name": "test_proj",
                "score": score,
                "risk_level": "Medium",
            }, f)

    def test_score_annotation_produced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_analysis(td, score=85)
            anns = build_github_annotations(td)
            self.assertTrue(any("85" in a.message for a in anns))

    def test_no_analysis_no_score_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            anns = build_github_annotations(td)
            # No analysis_result.json — no score annotation
            self.assertFalse(any("Analysis score" in a.message for a in anns))

    def test_security_high_issues_annotated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_analysis(td)
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump({
                    "passed": False,
                    "issues": [
                        {
                            "severity": "HIGH",
                            "rule_id": "PAT001",
                            "description": "eval() usage",
                            "file": "main.py",
                            "line": 10,
                        }
                    ],
                }, f)
            anns = build_github_annotations(td)
            sec_anns = [a for a in anns if "PAT001" in a.message]
            self.assertEqual(len(sec_anns), 1)
            self.assertEqual(sec_anns[0].level, "error")


class TestBuildStepSummaryMarkdown(unittest.TestCase):
    def test_renders_project_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "MyQuant", "score": 90, "risk_level": "Low"}, f)
            md = build_step_summary_markdown(td)
            self.assertIn("MyQuant", md)
            self.assertIn("90", md)

    def test_float_string_high_severity_count_no_crash(self) -> None:
        """build_step_summary_markdown must not raise when high_severity_count is a float-string.

        Regression test for: int('2.0') → ValueError in the unguarded code path.
        After fix: int(float('2.0')) → 2 without error.
        """
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "P", "score": 50}, f)
            # Simulate a security_report where high_severity_count is a float-string
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump({
                    "passed": False,
                    "scanner_used": "bandit",
                    "high_severity_count": "2.0",  # float-string — was crashing
                    "issues": [],
                }, f)
            # Must not raise ValueError
            md = build_step_summary_markdown(td)
            self.assertIn("Security Scan", md)

    def test_int_high_severity_count_works(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "P", "score": 50}, f)
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump({
                    "passed": False,
                    "scanner_used": "bandit",
                    "high_severity_count": 3,
                    "issues": [],
                }, f)
            md = build_step_summary_markdown(td)
            self.assertIn("3", md)


class TestWriteGithubOutputs(unittest.TestCase):
    def test_writes_annotation_and_summary_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "P", "score": 75, "risk_level": "Low"}, f)
            write_github_outputs(td)
            self.assertTrue(os.path.isfile(os.path.join(td, "github_annotations.txt")))
            self.assertTrue(os.path.isfile(os.path.join(td, "ci_summary.md")))

    def test_empty_run_dir_no_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # No JSON files — should still produce empty artifacts without crashing
            write_github_outputs(td)
            self.assertTrue(os.path.isfile(os.path.join(td, "github_annotations.txt")))


if __name__ == "__main__":
    unittest.main()
