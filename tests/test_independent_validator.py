# ruff: noqa: E402
"""Tests for crucible.features.independent_validator."""
import json
import os
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.independent_validator import (
    AdversarialFinding,
    ExecutionPhaseResult,
    FileCheckResult,
    IndependentValidationReport,
    _adversarial_review,
    _collect_source_for_review,
    _extract_json_from_response,
    _safe_int_or_none,
    _smoke_check,
    _syntax_check,
    validate_run,
)


class TestFileCheckResult(unittest.TestCase):
    def test_passed_result(self) -> None:
        r = FileCheckResult(file="main.py", passed=True)
        self.assertTrue(r.passed)
        self.assertEqual(r.error, "")

    def test_failed_result(self) -> None:
        r = FileCheckResult(file="bad.py", passed=False, error="SyntaxError")
        self.assertFalse(r.passed)
        self.assertIn("SyntaxError", r.error)


class TestExecutionPhaseResult(unittest.TestCase):
    def test_phase_attributes(self) -> None:
        r = ExecutionPhaseResult(phase="syntax_check", passed=True)
        self.assertEqual(r.phase, "syntax_check")
        self.assertTrue(r.passed)
        self.assertEqual(r.return_code, 0)
        self.assertFalse(r.timed_out)

    def test_timeout_result(self) -> None:
        r = ExecutionPhaseResult(
            phase="pytest", passed=False, timed_out=True,
            stderr="timed out after 60s",
        )
        self.assertFalse(r.passed)
        self.assertTrue(r.timed_out)


class TestIndependentValidationReport(unittest.TestCase):
    def test_to_dict_empty_report(self) -> None:
        r = IndependentValidationReport(success=True, overall_verdict="pass")
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["overall_verdict"], "pass")
        self.assertEqual(d["execution_phases"], [])
        self.assertEqual(d["adversarial_findings"], [])

    def test_to_dict_with_phases_and_findings(self) -> None:
        r = IndependentValidationReport(
            success=True,
            overall_verdict="warning",
            execution_phases=[
                ExecutionPhaseResult(
                    phase="syntax_check", passed=True,
                    file_results=[FileCheckResult(file="x.py", passed=True)],
                ),
            ],
            adversarial_findings=[
                AdversarialFinding(
                    severity="medium", category="design",
                    file="x.py", description="unused import", line=3,
                ),
            ],
            adversarial_summary="Minor issues found.",
        )
        d = r.to_dict()
        self.assertEqual(len(d["execution_phases"]), 1)
        self.assertEqual(d["execution_phases"][0]["phase"], "syntax_check")
        self.assertEqual(len(d["adversarial_findings"]), 1)
        self.assertEqual(d["adversarial_findings"][0]["severity"], "medium")
        self.assertEqual(d["adversarial_findings"][0]["line"], 3)

    def test_to_dict_truncates_long_stdout(self) -> None:
        r = IndependentValidationReport(
            success=True,
            execution_phases=[
                ExecutionPhaseResult(
                    phase="pytest", passed=True,
                    stdout="x" * 5000,
                ),
            ],
        )
        d = r.to_dict()
        self.assertLessEqual(len(d["execution_phases"][0]["stdout"]), 2000)

    def test_summary_text_contains_verdict(self) -> None:
        r = IndependentValidationReport(
            success=True, overall_verdict="fail",
            execution_phases=[
                ExecutionPhaseResult(phase="syntax_check", passed=False),
            ],
        )
        text = r.summary_text()
        self.assertIn("FAIL", text)
        self.assertIn("syntax_check", text)

    def test_summary_text_shows_findings(self) -> None:
        r = IndependentValidationReport(
            success=True, overall_verdict="warning",
            adversarial_findings=[
                AdversarialFinding(
                    severity="high", category="security",
                    file="main.py", description="SQL injection risk",
                    line=42,
                ),
            ],
            adversarial_summary="One security issue found.",
        )
        text = r.summary_text()
        self.assertIn("HIGH", text)
        self.assertIn("SQL injection risk", text)
        self.assertIn("main.py:42", text)
        self.assertIn("One security issue found.", text)


class TestSafeIntOrNone(unittest.TestCase):
    def test_int_value(self) -> None:
        self.assertEqual(_safe_int_or_none(42), 42)

    def test_string_int(self) -> None:
        self.assertEqual(_safe_int_or_none("12"), 12)

    def test_none(self) -> None:
        self.assertIsNone(_safe_int_or_none(None))

    def test_invalid_string(self) -> None:
        self.assertIsNone(_safe_int_or_none("abc"))

    def test_float_value(self) -> None:
        self.assertEqual(_safe_int_or_none(3.7), 3)


class TestExtractJsonFromResponse(unittest.TestCase):
    def test_direct_json(self) -> None:
        raw = '{"verdict": "pass", "findings": [], "summary": "ok"}'
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "pass")

    def test_json_in_code_fence(self) -> None:
        raw = '```json\n{"verdict": "fail", "findings": []}\n```'
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "fail")

    def test_json_in_plain_fence(self) -> None:
        raw = '```\n{"verdict": "warning"}\n```'
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "warning")

    def test_json_with_prose(self) -> None:
        raw = 'Here is my review:\n{"verdict": "pass", "findings": [], "summary": "all good"}\nDone.'
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "pass")

    def test_non_json_returns_none(self) -> None:
        result = _extract_json_from_response("This is not JSON at all")
        self.assertIsNone(result)

    def test_array_not_dict_returns_none(self) -> None:
        result = _extract_json_from_response('[1, 2, 3]')
        self.assertIsNone(result)

    def test_empty_string_returns_none(self) -> None:
        result = _extract_json_from_response("")
        self.assertIsNone(result)

    def test_brace_in_string_value_parsed_correctly(self) -> None:
        """
        Regression: the forward brace-scan fallback did not track
        string context.  A '}' or '{' inside a JSON string value corrupted the
        depth counter, causing mis-detection of the JSON boundary and a
        spurious json.JSONDecodeError → returning None instead of the dict.
        """
        raw = (
            'My review:\n'
            '{"verdict": "pass", "summary": "score={95}", "findings": []}\n'
            'Done.'
        )
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["summary"], "score={95}")

    def test_opening_brace_in_string_value_parsed_correctly(self) -> None:
        """String values containing '{' must not inflate the depth counter."""
        raw = '{"verdict": "fail", "summary": "fmt={bad}", "findings": []}'
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["summary"], "fmt={bad}")

    def test_think_tag_decoy_json_stripped(self) -> None:
        """Reasoning models (DeepSeek-V3/V4, GLM-5.1, Qwen-3.5, o1-class)
        emit chain-of-thought inside ``<think>...</think>`` ahead of the
        answer.  Any decoy JSON token inside the reasoning block must not
        be returned as the adversarial-review result."""
        raw = (
            '<think>Maybe the verdict could be {"verdict": "fail", '
            '"summary": "draft idea"}, but I need to check first.</think>\n'
            '{"verdict": "pass", "findings": [], "summary": "all good"}'
        )
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["summary"], "all good")

    def test_thinking_tag_alias_stripped(self) -> None:
        raw = (
            '<thinking>{"draft": true}</thinking>'
            '{"verdict": "pass", "findings": []}'
        )
        result = _extract_json_from_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["verdict"], "pass")


class TestSyntaxCheck(unittest.TestCase):
    def test_valid_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("def hello():\n    return 'world'\n")
            with open(os.path.join(code_dir, "utils.py"), "w") as f:
                f.write("x = 1 + 2\n")

            result = _syntax_check(code_dir)
            self.assertTrue(result.passed)
            self.assertEqual(result.phase, "syntax_check")
            self.assertEqual(len(result.file_results), 2)
            self.assertTrue(all(f.passed for f in result.file_results))

    def test_invalid_python_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "bad.py"), "w") as f:
                f.write("def broken(\n")  # syntax error

            result = _syntax_check(code_dir)
            self.assertFalse(result.passed)
            self.assertEqual(len(result.file_results), 1)
            self.assertFalse(result.file_results[0].passed)
            self.assertIn("bad.py", result.file_results[0].file)

    def test_mixed_valid_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "good.py"), "w") as f:
                f.write("x = 1\n")
            with open(os.path.join(code_dir, "bad.py"), "w") as f:
                f.write("def (\n")

            result = _syntax_check(code_dir)
            self.assertFalse(result.passed)
            passed = [f for f in result.file_results if f.passed]
            failed = [f for f in result.file_results if not f.passed]
            self.assertEqual(len(passed), 1)
            self.assertEqual(len(failed), 1)

    def test_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            result = _syntax_check(code_dir)
            self.assertTrue(result.passed)
            self.assertEqual(len(result.file_results), 0)

    def test_skips_non_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "readme.md"), "w") as f:
                f.write("# Hello\n")
            with open(os.path.join(code_dir, "data.json"), "w") as f:
                f.write("{}\n")
            result = _syntax_check(code_dir)
            self.assertTrue(result.passed)
            self.assertEqual(len(result.file_results), 0)

    def test_prunes_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            cache_dir = os.path.join(code_dir, "__pycache__")
            os.makedirs(cache_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")
            # Create a .py file in __pycache__ that shouldn't be checked
            with open(os.path.join(cache_dir, "fake.py"), "w") as f:
                f.write("def broken(\n")

            result = _syntax_check(code_dir)
            self.assertTrue(result.passed)
            self.assertEqual(len(result.file_results), 1)
            self.assertEqual(result.file_results[0].file, "main.py")


class TestSmokeCheck(unittest.TestCase):
    def test_no_main_py(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            result = _smoke_check(code_dir, timeout=10)
            self.assertTrue(result.passed)
            self.assertIn("No main.py found", result.stdout)

    def test_valid_main_with_argparse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write(textwrap.dedent("""\
                    import argparse
                    parser = argparse.ArgumentParser()
                    parser.add_argument("--name", default="world")
                    args = parser.parse_args()
                    print(f"hello {args.name}")
                """))

            result = _smoke_check(code_dir, timeout=15)
            self.assertTrue(result.passed)
            self.assertEqual(result.return_code, 0)

    def test_main_with_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("raise RuntimeError('boom')\n")

            result = _smoke_check(code_dir, timeout=15)
            self.assertFalse(result.passed)
            self.assertIn("Traceback", result.stderr)


class TestCollectSourceForReview(unittest.TestCase):
    def test_collects_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("print('hello')\n")
            with open(os.path.join(code_dir, "utils.py"), "w") as f:
                f.write("x = 1\n")

            source = _collect_source_for_review(code_dir)
            self.assertIn("--- main.py ---", source)
            self.assertIn("--- utils.py ---", source)
            self.assertIn("print('hello')", source)

    def test_respects_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "big.py"), "w") as f:
                f.write("x = 1\n" * 10000)  # large file

            source = _collect_source_for_review(code_dir, max_total_chars=500)
            # Should be truncated close to budget (allow overhead for headers/omit msg)
            self.assertLessEqual(len(source), 1500)

    def test_respects_budget_across_directories(self) -> None:
        """budget_exceeded flag must stop os.walk from continuing to subdirs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            sub1 = os.path.join(code_dir, "sub1")
            sub2 = os.path.join(code_dir, "sub2")
            os.makedirs(sub1)
            os.makedirs(sub2)
            # sub1 will exceed the budget
            with open(os.path.join(sub1, "a.py"), "w") as f:
                f.write("x = 1\n" * 5000)
            # sub2 comes after — should produce at most ONE "omitted" line
            with open(os.path.join(sub2, "b.py"), "w") as f:
                f.write("y = 2\n" * 5000)

            source = _collect_source_for_review(code_dir, max_total_chars=500)
            # Only one "omitted" message should appear, not two
            self.assertEqual(source.count("omitted: token budget exceeded"), 1)

    def test_skips_non_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "data.json"), "w") as f:
                f.write('{"key": "value"}')

            source = _collect_source_for_review(code_dir)
            self.assertNotIn("data.json", source)

    def test_prunes_ignored_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            cache_dir = os.path.join(code_dir, "__pycache__")
            os.makedirs(cache_dir)
            with open(os.path.join(cache_dir, "cached.py"), "w") as f:
                f.write("# should be ignored\n")

            source = _collect_source_for_review(code_dir)
            self.assertNotIn("cached.py", source)


class TestAdversarialReview(unittest.TestCase):
    def test_clean_code_returns_pass(self) -> None:
        """LLM that returns clean verdict."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "pass",
                        "findings": [],
                        "summary": "Code looks clean.",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("def hello():\n    return 'world'\n")

            findings, summary, verdict = _adversarial_review(
                code_dir, {}, MockLLM(),
            )
            self.assertEqual(verdict, "pass")
            self.assertEqual(findings, [])
            self.assertIn("clean", summary.lower())

    def test_findings_are_parsed(self) -> None:
        """LLM that returns findings."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "fail",
                        "findings": [
                            {
                                "severity": "high",
                                "category": "security",
                                "file": "main.py",
                                "line": 10,
                                "description": "SQL injection via string format",
                            },
                            {
                                "severity": "low",
                                "category": "design",
                                "file": "utils.py",
                                "line": 5,
                                "description": "Magic number",
                            },
                        ],
                        "summary": "Security issue found.",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")

            findings, summary, verdict = _adversarial_review(
                code_dir, {}, MockLLM(),
            )
            self.assertEqual(verdict, "fail")
            self.assertEqual(len(findings), 2)
            self.assertEqual(findings[0].severity, "high")
            self.assertEqual(findings[0].category, "security")
            self.assertEqual(findings[0].line, 10)
            self.assertEqual(findings[1].severity, "low")

    def test_handles_markdown_fenced_json(self) -> None:
        """LLM wraps JSON in markdown fences."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = (
                        '```json\n'
                        '{"verdict": "warning", "findings": [], "summary": "ok"}\n'
                        '```'
                    )
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            findings, summary, verdict = _adversarial_review(
                code_dir, {}, MockLLM(),
            )
            self.assertEqual(verdict, "warning")

    def test_handles_empty_llm_response(self) -> None:
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = ""
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            findings, summary, verdict = _adversarial_review(
                code_dir, {}, MockLLM(),
            )
            self.assertEqual(verdict, "unknown")
            self.assertIn("empty", summary.lower())

    def test_handles_unparseable_llm_response(self) -> None:
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = "I refuse to output JSON."
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            findings, summary, verdict = _adversarial_review(
                code_dir, {}, MockLLM(),
            )
            self.assertEqual(verdict, "unknown")
            self.assertIn("Could not parse", summary)

    def test_no_source_files_returns_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)

            class MockLLM:
                def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                    raise AssertionError("LLM should not be called")

            findings, summary, verdict = _adversarial_review(
                code_dir, {}, MockLLM(),
            )
            self.assertEqual(verdict, "pass")
            self.assertIn("No source files", summary)

    def test_invalid_severity_normalized_to_medium(self) -> None:
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "fail",
                        "findings": [
                            {
                                "severity": "EXTREME",
                                "category": "logic_error",
                                "file": "x.py",
                                "description": "test",
                            },
                        ],
                        "summary": "test",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            findings, _, _ = _adversarial_review(code_dir, {}, MockLLM())
            self.assertEqual(findings[0].severity, "medium")

    def test_invalid_verdict_normalized_to_warning(self) -> None:
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "UNKNOWN_VERDICT",
                        "findings": [],
                        "summary": "ok",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            _, _, verdict = _adversarial_review(code_dir, {}, MockLLM())
            self.assertEqual(verdict, "warning")

    def test_line_as_string_handled(self) -> None:
        """JSON line value might be a string from some LLMs."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "fail",
                        "findings": [
                            {
                                "severity": "low",
                                "category": "correctness",
                                "file": "x.py",
                                "line": "15",
                                "description": "test",
                            },
                        ],
                        "summary": "test",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            findings, _, _ = _adversarial_review(code_dir, {}, MockLLM())
            self.assertEqual(findings[0].line, 15)


class TestAdversarialVerdictPreserved(unittest.TestCase):
    """
    Regression: the verdict returned by _adversarial_review() was
    discarded with '_'.  IndependentValidationReport had no adversarial_verdict
    field, so an LLM "fail" judgment was silently dropped even when all individual
    findings were medium/low severity, causing the overall_verdict to stay at
    "warning" instead of "fail".
    """

    def _make_llm(self, verdict: str, findings: list, summary: str = "ok"):
        class _MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class _R:
                    content = json.dumps({
                        "verdict": verdict,
                        "findings": findings,
                        "summary": summary,
                    })
                return _R()
        return _MockLLM()

    def test_adversarial_verdict_stored_on_report(self) -> None:
        """adversarial_verdict field must be populated from LLM response."""
        llm = self._make_llm("fail", [], "nothing good")
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")
            report = validate_run(td, llm=llm, timeout=15)
        self.assertEqual(report.adversarial_verdict, "fail")

    def test_adversarial_verdict_in_to_dict(self) -> None:
        """to_dict() must include adversarial_verdict key."""
        r = IndependentValidationReport(
            success=True, overall_verdict="pass",
            adversarial_verdict="warning",
        )
        d = r.to_dict()
        self.assertIn("adversarial_verdict", d)
        self.assertEqual(d["adversarial_verdict"], "warning")

    def test_llm_fail_verdict_with_low_findings_causes_overall_fail(self) -> None:
        """
        When LLM returns verdict="fail" with only low-severity findings,
        overall_verdict must be "fail", not "warning".  The LLM's explicit
        failure judgment must not be down-graded by the absence of high/critical
        individual findings.
        """
        llm = self._make_llm(
            verdict="fail",
            findings=[{
                "severity": "low",
                "category": "design",
                "file": "x.py",
                "description": "Minor style issue",
            }],
            summary="Multiple low-level issues combine to produce overall failure.",
        )
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")
            report = validate_run(td, llm=llm, timeout=15)
        self.assertEqual(report.adversarial_verdict, "fail")
        self.assertEqual(report.overall_verdict, "fail",
                         "LLM 'fail' verdict must escalate overall_verdict to 'fail'")
        self.assertFalse(report.success)

    def test_llm_warning_verdict_preserved_in_to_dict(self) -> None:
        """adversarial_verdict is serialised to the JSON report."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")
            llm = self._make_llm("warning", [], "Looks mostly fine.")
            validate_run(td, llm=llm, timeout=15)
            report_path = os.path.join(td, "independent_validation_report.json")
            self.assertTrue(os.path.isfile(report_path))
            with open(report_path) as fh:
                data = json.load(fh)
            self.assertIn("adversarial_verdict", data)
            self.assertEqual(data["adversarial_verdict"], "warning")


class TestValidateRun(unittest.TestCase):
    def test_no_code_dir_returns_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = validate_run(tmpdir)
            self.assertFalse(report.success)
            self.assertEqual(report.overall_verdict, "fail")
            self.assertTrue(any("No code/" in e for e in report.errors))

    def test_valid_code_without_llm(self) -> None:
        """Phase B only — syntax check passes, no LLM review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write(textwrap.dedent("""\
                    import argparse
                    parser = argparse.ArgumentParser()
                    args = parser.parse_args()
                    print("ok")
                """))

            report = validate_run(tmpdir, timeout=15)
            self.assertTrue(report.success)
            self.assertEqual(report.overall_verdict, "pass")
            self.assertEqual(len(report.execution_phases), 3)
            # Syntax check should pass
            self.assertTrue(report.execution_phases[0].passed)
            # pytest skipped (no tests)
            self.assertTrue(report.execution_phases[1].passed)
            # No adversarial review
            self.assertEqual(report.adversarial_findings, [])

    def test_syntax_error_causes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "broken.py"), "w") as f:
                f.write("def (\n")

            report = validate_run(tmpdir, timeout=15)
            self.assertEqual(report.overall_verdict, "fail")
            self.assertFalse(report.success)
            self.assertFalse(report.execution_phases[0].passed)

    def test_with_mock_llm_clean(self) -> None:
        """Phase B + Phase A — LLM says clean."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "pass",
                        "findings": [],
                        "summary": "All good.",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")

            report = validate_run(tmpdir, llm=MockLLM(), timeout=15)
            self.assertTrue(report.success)
            self.assertEqual(report.overall_verdict, "pass")
            self.assertEqual(report.adversarial_findings, [])

    def test_with_mock_llm_findings(self) -> None:
        """Phase B passes but Phase A finds high-severity issues → verdict fail."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "fail",
                        "findings": [
                            {
                                "severity": "high",
                                "category": "security",
                                "file": "main.py",
                                "line": 1,
                                "description": "Hardcoded secret",
                            },
                        ],
                        "summary": "Security issue.",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("SECRET = 'abc123'\n")

            report = validate_run(tmpdir, llm=MockLLM(), timeout=15)
            self.assertEqual(report.overall_verdict, "fail")
            self.assertEqual(len(report.adversarial_findings), 1)
            self.assertEqual(report.adversarial_findings[0].severity, "high")

    def test_low_severity_findings_cause_warning(self) -> None:
        """Low-severity findings → warning, not fail."""
        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                class R:
                    content = json.dumps({
                        "verdict": "warning",
                        "findings": [
                            {
                                "severity": "low",
                                "category": "design",
                                "file": "x.py",
                                "description": "Minor issue",
                            },
                        ],
                        "summary": "Minor issues only.",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("a = 1\n")

            report = validate_run(tmpdir, llm=MockLLM(), timeout=15)
            self.assertEqual(report.overall_verdict, "warning")

    def test_persists_report_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")

            validate_run(tmpdir, timeout=15)
            report_path = os.path.join(tmpdir, "independent_validation_report.json")
            self.assertTrue(os.path.isfile(report_path))

            with open(report_path, "r") as f:
                data = json.load(f)
            self.assertIn("overall_verdict", data)
            self.assertIn("execution_phases", data)

    def test_reads_analysis_result(self) -> None:
        """Verify the prompt includes analysis claims when available."""
        prompts_received: list = []

        class MockLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                prompts_received.append(prompt)
                class R:
                    content = json.dumps({
                        "verdict": "pass", "findings": [], "summary": "ok",
                    })
                return R()

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")
            with open(os.path.join(tmpdir, "analysis_result.json"), "w") as f:
                json.dump({"score": 85, "risk_level": "low"}, f)

            validate_run(tmpdir, llm=MockLLM(), timeout=15)
            self.assertEqual(len(prompts_received), 1)
            self.assertIn("score: 85", prompts_received[0])
            self.assertIn("risk_level: low", prompts_received[0])

    def test_llm_exception_does_not_crash(self) -> None:
        """LLM that throws should not crash validate_run.

        ``_call_llm`` catches exceptions internally and returns None,
        so ``_adversarial_review`` treats it as an empty response — the
        verdict becomes "unknown" and the summary mentions empty response.
        Phase B should still complete normally.
        """
        class BrokenLLM:
            def invoke(self, prompt: str):  # noqa: ANN001, ANN201
                raise RuntimeError("LLM is down")

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("x = 1\n")

            report = validate_run(tmpdir, llm=BrokenLLM(), timeout=15)
            # Phase B should still pass
            self.assertTrue(report.execution_phases[0].passed)
            # Adversarial review got empty response (exception caught by _call_llm)
            self.assertIn("empty", report.adversarial_summary.lower())


class TestPytestPythonPath(unittest.TestCase):
    """
    Regression: _run_pytest_suite passed run_dir to _make_safe_env()
    instead of run_dir/code/, so PYTHONPATH did not include the generated code
    root.  Tests that import from sibling modules failed with ModuleNotFoundError
    when no conftest.py was present to add the path manually.
    """

    def test_pythonpath_includes_code_dir(self) -> None:
        """_make_safe_env must receive run_dir/code, not run_dir."""
        from unittest.mock import patch, call as mock_call
        import subprocess as _subprocess

        with tempfile.TemporaryDirectory() as tmpdir:
            code_dir = os.path.join(tmpdir, "code")
            tests_dir = os.path.join(code_dir, "tests")
            os.makedirs(tests_dir)
            # Create a minimal test file so the early "no test files" guard is passed
            with open(os.path.join(tests_dir, "test_dummy.py"), "w") as f:
                f.write("def test_pass(): pass\n")

            captured_env_dirs = []

            original_make_safe_env = __import__(
                "crucible.features.independent_validator",
                fromlist=["_make_safe_env"],
            )._make_safe_env

            def _spy_make_safe_env(code_dir_arg: str):
                captured_env_dirs.append(code_dir_arg)
                return original_make_safe_env(code_dir_arg)

            with patch(
                "crucible.features.independent_validator._make_safe_env",
                side_effect=_spy_make_safe_env,
            ), patch.object(
                _subprocess, "run",
                return_value=_subprocess.CompletedProcess([], returncode=0,
                                                          stdout="1 passed", stderr=""),
            ):
                from crucible.features.independent_validator import _run_pytest_suite
                _run_pytest_suite(tmpdir, timeout=10)

        # After the fix, _make_safe_env must be called with run_dir/code, not run_dir
        self.assertTrue(
            any(d == code_dir for d in captured_env_dirs),
            f"Expected _make_safe_env called with '{code_dir}', got {captured_env_dirs}. "
            "PYTHONPATH must include the code/ directory so imports work without conftest."
        )


if __name__ == "__main__":
    unittest.main()
