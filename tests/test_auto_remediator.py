# ruff: noqa: E402
"""Tests for crucible.features.auto_remediator."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.auto_remediator import (
    RemediationPatch,
    RemediationReport,
    _build_security_fix_prompt,
    _build_validation_fix_prompt,
    _call_llm,
    _collect_security_issues,
    _collect_validation_issues,
    _is_valid_python,
    _strip_code_fences,
    remediate_run,
)
from crucible.features.test_generator import _syntax_error_detail


class TestRemediationPatch(unittest.TestCase):
    def test_default_values(self) -> None:
        p = RemediationPatch(
            file="main.py", round_number=1, source="security", issues_targeted=3
        )
        self.assertFalse(p.applied)
        self.assertFalse(p.syntax_valid)
        self.assertEqual(p.issues_remaining, 0)
        self.assertEqual(p.error, "")

    def test_applied_patch(self) -> None:
        p = RemediationPatch(
            file="x.py", round_number=2, source="validation",
            issues_targeted=1, applied=True, syntax_valid=True,
        )
        self.assertTrue(p.applied)
        self.assertTrue(p.syntax_valid)


class TestRemediationReport(unittest.TestCase):
    def test_to_dict(self) -> None:
        r = RemediationReport(
            success=True, rounds_executed=1,
            total_patches_attempted=2, total_patches_applied=1,
            initial_issue_count=3, final_issue_count=1,
        )
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["rounds_executed"], 1)
        self.assertEqual(d["total_patches_applied"], 1)
        self.assertEqual(d["initial_issue_count"], 3)
        self.assertEqual(d["final_issue_count"], 1)

    def test_summary_text(self) -> None:
        r = RemediationReport(
            success=True, rounds_executed=2,
            total_patches_attempted=3, total_patches_applied=2,
            initial_issue_count=5, final_issue_count=0,
        )
        text = r.summary_text()
        self.assertIn("Auto-Remediation Report", text)
        self.assertIn("2/3 applied", text)
        self.assertIn("5 →", text)


class TestStripCodeFences(unittest.TestCase):
    def test_strips_python_fences(self) -> None:
        raw = '```python\nprint("hi")\n```'
        self.assertEqual(_strip_code_fences(raw), 'print("hi")')

    def test_strips_plain_fences(self) -> None:
        raw = '```\ncode\n```'
        self.assertEqual(_strip_code_fences(raw), "code")

    def test_no_fences(self) -> None:
        raw = 'print("hi")'
        self.assertEqual(_strip_code_fences(raw), 'print("hi")')


class TestIsValidPython(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertTrue(_is_valid_python("x = 1\nprint(x)\n"))

    def test_invalid(self) -> None:
        self.assertFalse(_is_valid_python("def foo(:\n"))


class TestSyntaxErrorDetail(unittest.TestCase):
    """
    Regression: test_generator._syntax_error_detail captures and
    returns the SyntaxError message so error reports include the actual error
    instead of a generic "has persistent syntax errors" string.
    """

    def test_returns_empty_string_for_valid_code(self) -> None:
        self.assertEqual(_syntax_error_detail("x = 1\n"), "")

    def test_returns_error_message_for_invalid_code(self) -> None:
        detail = _syntax_error_detail("def foo(:\n")
        self.assertIsInstance(detail, str)
        self.assertGreater(len(detail), 0, "Expected non-empty SyntaxError message")

    def test_error_message_contains_useful_info(self) -> None:
        """The returned string must convey what went wrong (not be a generic placeholder)."""
        detail = _syntax_error_detail("x = (\n")  # unclosed parenthesis
        # Python SyntaxError for this should mention the problematic token / EOF
        self.assertNotEqual(detail, "unknown")
        self.assertGreater(len(detail), 3)


class TestCallLlm(unittest.TestCase):
    def test_invoke_style(self) -> None:
        class FakeLLM:
            def invoke(self, prompt):
                class Resp:
                    content = "fixed code"
                return Resp()
        self.assertEqual(_call_llm(FakeLLM(), "fix"), "fixed code")

    def test_callable_style(self) -> None:
        result = _call_llm(lambda p: "result", "prompt")
        self.assertEqual(result, "result")

    def test_none_on_exception(self) -> None:
        class BadLLM:
            def invoke(self, prompt):
                raise RuntimeError("boom")
        self.assertIsNone(_call_llm(BadLLM(), "fix"))

    def test_none_content(self) -> None:
        class NullLLM:
            def invoke(self, prompt):
                class Resp:
                    content = None
                return Resp()
        self.assertIsNone(_call_llm(NullLLM(), "fix"))

    def test_empty_string_content_returns_none(self) -> None:
        """Empty string content should return None (not a valid patch)."""
        class EmptyLLM:
            def invoke(self, prompt):
                class Resp:
                    content = ""
                return Resp()
        self.assertIsNone(_call_llm(EmptyLLM(), "fix"))

    def test_crlf_code_fence_stripped(self) -> None:
        """Windows CRLF line endings in LLM responses must be normalised."""
        raw = "```python\r\nprint('hi')\r\n```"
        self.assertEqual(_strip_code_fences(raw), "print('hi')")

    def test_crlf_without_language_tag(self) -> None:
        raw = "```\r\nx = 1\r\n```"
        self.assertEqual(_strip_code_fences(raw), "x = 1")


class TestCollectSecurityIssues(unittest.TestCase):
    def test_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = _collect_security_issues(td)
            self.assertEqual(result, {})

    def test_collects_high_issues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = {
                "issues": [
                    {"severity": "HIGH", "file": "main.py", "rule_id": "B101"},
                    {"severity": "LOW", "file": "main.py", "rule_id": "B102"},
                    {"severity": "CRITICAL", "file": "util.py", "rule_id": "B103"},
                ]
            }
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump(report, f)
            result = _collect_security_issues(td)
            self.assertIn("main.py", result)
            self.assertIn("util.py", result)
            self.assertEqual(len(result["main.py"]), 1)  # only HIGH
            self.assertEqual(len(result["util.py"]), 1)  # CRITICAL


class TestCollectValidationIssues(unittest.TestCase):
    def test_no_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_collect_validation_issues(td), {})

    def test_collects_high_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = {
                "adversarial_findings": [
                    {"severity": "high", "file": "a.py", "category": "logic"},
                    {"severity": "low", "file": "b.py", "category": "style"},
                ]
            }
            with open(os.path.join(td, "independent_validation_report.json"), "w") as f:
                json.dump(report, f)
            result = _collect_validation_issues(td)
            self.assertIn("a.py", result)
            self.assertNotIn("b.py", result)


class TestBuildFixPrompts(unittest.TestCase):
    def test_security_fix_prompt(self) -> None:
        issues = [{"severity": "HIGH", "rule_id": "B101", "description": "eval used", "line": 5}]
        prompt = _build_security_fix_prompt(issues, "x = eval(input())", "main.py")
        self.assertIn("security issues", prompt)
        self.assertIn("B101", prompt)
        self.assertIn("main.py", prompt)

    def test_validation_fix_prompt(self) -> None:
        findings = [{"severity": "high", "category": "logic", "description": "off by one", "line": 10}]
        prompt = _build_validation_fix_prompt(findings, "for i in range(10):", "util.py")
        self.assertIn("code review findings", prompt)
        self.assertIn("logic", prompt)


class TestRemediateRun(unittest.TestCase):
    def test_no_code_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = remediate_run(td, None)
            self.assertFalse(report.success)
            self.assertTrue(report.errors, "expected at least one error message")
            self.assertIn("No code/", report.errors[0])

    def test_no_issues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "code"))
            report = remediate_run(td, None)
            self.assertTrue(report.success)
            self.assertEqual(report.initial_issue_count, 0)
            self.assertEqual(report.final_issue_count, 0)

    def test_applies_valid_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Create source file
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = eval(input())\n")
            # Create security report
            sec = {
                "issues": [
                    {"severity": "HIGH", "file": "main.py", "rule_id": "B101",
                     "description": "eval used", "line": 1}
                ]
            }
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump(sec, f)

            class FixLLM:
                def invoke(self, prompt):
                    class Resp:
                        content = "x = int(input())\n"
                    return Resp()

            report = remediate_run(td, FixLLM(), max_rounds=1)
            self.assertEqual(report.total_patches_applied, 1)
            # Verify the file was patched
            with open(os.path.join(code_dir, "main.py")) as f:
                self.assertIn("int(input())", f.read())

    def test_skips_syntax_invalid_patch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = eval(input())\n")
            sec = {
                "issues": [
                    {"severity": "HIGH", "file": "main.py", "rule_id": "B101",
                     "description": "eval used", "line": 1}
                ]
            }
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump(sec, f)

            class BadLLM:
                def invoke(self, prompt):
                    class Resp:
                        content = "def foo(:\n"  # syntax error
                    return Resp()

            report = remediate_run(td, BadLLM(), max_rounds=1)
            self.assertEqual(report.total_patches_applied, 0)
            self.assertEqual(report.total_patches_attempted, 1)


class TestValidationIssueNoRetry(unittest.TestCase):
    """
    Regression: validation issues were re-collected from the same
    stale JSON every round, causing the same files to be re-patched up to
    max_rounds times even after the issues were already addressed.

    Fix: track patched validation files in _patched_val_files and exclude them
    from subsequent rounds.
    """

    def test_validation_file_not_repatched_after_success(self) -> None:
        """A validation-patched file must NOT be re-attempted in round 2."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")

            # Validation report that points at main.py — will not change between rounds
            val_report = {
                "adversarial_findings": [
                    {"severity": "HIGH", "file": "main.py",
                     "category": "logic", "description": "potential issue", "line": 1}
                ]
            }
            with open(os.path.join(td, "independent_validation_report.json"), "w") as f:
                json.dump(val_report, f)

            call_count = {"n": 0}

            class CountingLLM:
                def invoke(self, prompt):
                    call_count["n"] += 1
                    class Resp:
                        content = "x = 2\n"  # valid patch
                    return Resp()

            # With max_rounds=3, the old code would call LLM 3 times (once per round).
            # With the fix it must call LLM exactly once (only round 1).
            report = remediate_run(td, CountingLLM(), max_rounds=3)

        self.assertEqual(call_count["n"], 1, (
            f"LLM called {call_count['n']} times; expected 1. "
            "Validation file must not be re-patched after successful round."
        ))
        self.assertEqual(report.total_patches_applied, 1)

    def test_severity_normalization_consistent(self) -> None:
        """
        Regression: _collect_validation_issues used .lower() while
        _collect_security_issues used .upper() for severity comparison.
        Now both use .upper(). Both 'HIGH' and 'high' inputs must be collected.
        """
        with tempfile.TemporaryDirectory() as td:
            # Validation report with uppercase severity (was previously missed
            # if the check was .lower() vs ("HIGH","CRITICAL"))
            val_report = {
                "adversarial_findings": [
                    {"severity": "HIGH", "file": "a.py",
                     "category": "logic", "description": "bug", "line": 1},
                    {"severity": "high", "file": "b.py",
                     "category": "logic", "description": "bug", "line": 2},
                    {"severity": "Critical", "file": "c.py",
                     "category": "logic", "description": "bug", "line": 3},
                ]
            }
            with open(os.path.join(td, "independent_validation_report.json"), "w") as f:
                json.dump(val_report, f)

            result = _collect_validation_issues(td)
            # After fix: all three severity variants must be collected
            self.assertIn("a.py", result, "uppercase HIGH must be collected")
            self.assertIn("b.py", result, "lowercase high must be collected")
            self.assertIn("c.py", result, "mixed-case Critical must be collected")


if __name__ == "__main__":
    unittest.main()
