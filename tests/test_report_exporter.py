# ruff: noqa: E402
"""Tests for crucible.features.report_exporter."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.report_exporter import (
    _build_dependency_section,
    _build_overview_section,
    _build_remediation_section,
    _build_security_section,
    _build_validation_section,
    _esc,
    _score_color,
    _severity_color,
    _SimplePDFWriter,
    _wrap_text,
    export_html_report,
    export_pdf_report,
)


class TestEsc(unittest.TestCase):
    def test_escapes_html(self) -> None:
        self.assertEqual(_esc("<script>"), "&lt;script&gt;")

    def test_none_returns_empty(self) -> None:
        self.assertEqual(_esc(None), "")


class TestScoreColor(unittest.TestCase):
    def test_high_score(self) -> None:
        self.assertEqual(_score_color(85), "#22c55e")

    def test_mid_score(self) -> None:
        self.assertEqual(_score_color(60), "#eab308")

    def test_low_score(self) -> None:
        self.assertEqual(_score_color(30), "#ef4444")

    def test_invalid_score(self) -> None:
        self.assertEqual(_score_color("N/A"), "#9ca3af")


class TestSeverityColor(unittest.TestCase):
    def test_critical(self) -> None:
        self.assertEqual(_severity_color("CRITICAL"), "#ef4444")

    def test_medium(self) -> None:
        self.assertEqual(_severity_color("MEDIUM"), "#eab308")

    def test_low(self) -> None:
        self.assertEqual(_severity_color("LOW"), "#9ca3af")


class TestBuildOverviewSection(unittest.TestCase):
    def test_renders_project_name(self) -> None:
        analysis = {"project_name": "TestProject", "score": 74, "risk_level": "Medium"}
        meta = {"mode": "quant", "llm_provider": "openrouter", "timestamp": "2024-01-01"}
        html = _build_overview_section(analysis, meta)
        self.assertIn("TestProject", html)
        self.assertIn("74/100", html)
        self.assertIn("Medium", html)

    def test_missing_score(self) -> None:
        analysis = {}
        meta = {}
        html = _build_overview_section(analysis, meta)
        self.assertIn("N/A", html)

    def test_consensus_and_disagreement(self) -> None:
        analysis = {
            "consensus": "All agree",
            "disagreement": "Risk disagrees",
        }
        html = _build_overview_section(analysis, {})
        self.assertIn("All agree", html)
        self.assertIn("Risk disagrees", html)


class TestBuildSecuritySection(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(_build_security_section({}), "")

    def test_passed_scan(self) -> None:
        sec = {"passed": True, "scanner_used": "bandit", "high_severity_count": 0}
        html = _build_security_section(sec)
        self.assertIn("PASS", html)
        self.assertIn("bandit", html)

    def test_high_count_as_float_does_not_raise(self) -> None:
        """int(float(...)) must handle JSON floats (e.g. 2.5 → 2) without ValueError."""
        sec = {"passed": False, "scanner_used": "bandit", "high_severity_count": 2.5}
        html = _build_security_section(sec)
        self.assertIn("FAIL", html)
        self.assertIn("2", html)  # 2.5 truncated to 2

    def test_failed_scan_with_issues(self) -> None:
        sec = {
            "passed": False,
            "scanner_used": "bandit",
            "high_severity_count": 2,
            "issues": [
                {"severity": "HIGH", "rule_id": "B101", "file": "main.py", "description": "eval"},
            ],
        }
        html = _build_security_section(sec)
        self.assertIn("FAIL", html)
        self.assertIn("B101", html)

    def test_severity_color_uses_unescaped_string(self) -> None:
        # The color lookup must use the raw severity string, not the HTML-escaped one.
        # If it used the escaped string, _severity_color would always return the default
        # gray (#9ca3af) for any severity containing HTML special chars.
        # This test verifies HIGH gets the red colour (#ef4444), not gray.
        sec = {
            "passed": False,
            "scanner_used": "bandit",
            "high_severity_count": 1,
            "issues": [
                {"severity": "HIGH", "rule_id": "B999", "file": "x.py", "description": "test"},
            ],
        }
        html = _build_security_section(sec)
        self.assertIn("#ef4444", html)  # red for HIGH — must not be #9ca3af (gray)

    def test_description_truncated_before_escaping(self) -> None:
        # Truncation must happen on the raw string before html.escape().
        # Previously _esc(desc)[:120] could split in the middle of an HTML entity
        # (e.g. "&amp;" → "&amp") producing malformed HTML.
        long_desc = "A" * 110 + " & B"  # raw: 115 chars with an ampersand
        sec = {
            "passed": False,
            "scanner_used": "bandit",
            "high_severity_count": 1,
            "issues": [
                {"severity": "HIGH", "rule_id": "B001", "file": "x.py", "description": long_desc},
            ],
        }
        html = _build_security_section(sec)
        # The HTML must not contain broken entity fragments — all & must be &amp;
        self.assertNotIn("&B", html)   # orphaned '&' without entity encoding


class TestBuildValidationSection(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(_build_validation_section({}), "")

    def test_pass_verdict(self) -> None:
        val = {"overall_verdict": "pass"}
        html = _build_validation_section(val)
        self.assertIn("PASS", html)

    def test_with_phases(self) -> None:
        val = {
            "overall_verdict": "fail",
            "execution_phases": [
                {"phase": "syntax_check", "passed": True},
                {"phase": "pytest", "passed": False},
            ],
        }
        html = _build_validation_section(val)
        self.assertIn("syntax_check", html)
        self.assertIn("pytest", html)

    def test_adversarial_severity_color_uses_unescaped_string(self) -> None:
        # Same as the security section fix: color must use raw severity.
        val = {
            "overall_verdict": "fail",
            "adversarial_findings": [
                {"severity": "critical", "category": "logic", "file": "y.py",
                 "description": "division by zero"},
            ],
        }
        html = _build_validation_section(val)
        self.assertIn("#ef4444", html)  # red for CRITICAL


class TestBuildDependencySection(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(_build_dependency_section({}), "")

    def test_with_vulnerabilities(self) -> None:
        dep = {
            "passed": False,
            "scanner_used": "pip-audit",
            "vulnerability_count": 1,
            "vulnerabilities": [
                {"package": "urllib3", "installed_version": "1.25.0",
                 "vuln_id": "CVE-2023-1234", "fix_version": "1.26.18"},
            ],
        }
        html = _build_dependency_section(dep)
        self.assertIn("FAIL", html)
        self.assertIn("urllib3", html)
        self.assertIn("CVE-2023-1234", html)


class TestBuildRemediationSection(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(_build_remediation_section({}), "")

    def test_renders_stats(self) -> None:
        remed = {
            "rounds_executed": 2,
            "total_patches_applied": 3,
            "total_patches_attempted": 4,
            "initial_issue_count": 5,
            "final_issue_count": 0,
        }
        html = _build_remediation_section(remed)
        self.assertIn("Rounds: 2", html)
        self.assertIn("3/4", html)


class TestExportHtmlReport(unittest.TestCase):
    def test_generates_html(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Create minimal analysis
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "MyProject", "score": 80}, f)
            path = export_html_report(td)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("<!DOCTYPE html>", html)
            self.assertIn("MyProject", html)
            self.assertIn("80/100", html)

    def test_custom_filename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = export_html_report(td, output_filename="custom.html")
            self.assertTrue(path.endswith("custom.html"))
            self.assertTrue(os.path.isfile(path))

    def test_empty_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = export_html_report(td)
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("Unknown", html)


# ──────────────────────────────────────────────────────────────────────────────
# _wrap_text
# ──────────────────────────────────────────────────────────────────────────────

class TestWrapText(unittest.TestCase):
    def test_short_text_unchanged(self) -> None:
        self.assertEqual(_wrap_text("hello", max_chars=80), ["hello"])

    def test_empty_string(self) -> None:
        result = _wrap_text("", max_chars=80)
        self.assertEqual(result, [""])

    def test_long_line_wrapped(self) -> None:
        words = ["word"] * 30
        lines = _wrap_text(" ".join(words), max_chars=20)
        for line in lines:
            self.assertLessEqual(len(line), 20)

    def test_very_long_word_force_broken(self) -> None:
        long_word = "a" * 200
        lines = _wrap_text(long_word, max_chars=10)
        for line in lines:
            self.assertLessEqual(len(line), 10)
        self.assertEqual("".join(lines), long_word)

    def test_multiword_wraps_correctly(self) -> None:
        lines = _wrap_text("hello world foo bar", max_chars=11)
        self.assertIn("hello world", lines)


# ──────────────────────────────────────────────────────────────────────────────
# _SimplePDFWriter
# ──────────────────────────────────────────────────────────────────────────────

class TestSimplePDFWriter(unittest.TestCase):
    def _make_pdf(self, **kwargs: object) -> bytes:
        w = _SimplePDFWriter()
        w.add_page()
        w.draw_text(50, 750, "Hello PDF", size=14, bold=True)
        w.draw_text(50, 720, "Normal line", size=11)
        w.draw_line(50, 710, 545, 710)
        return w.render()

    def test_render_returns_bytes(self) -> None:
        self.assertIsInstance(self._make_pdf(), bytes)

    def test_pdf_header(self) -> None:
        pdf = self._make_pdf()
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))

    def test_pdf_footer(self) -> None:
        pdf = self._make_pdf()
        self.assertIn(b"%%EOF", pdf)

    def test_xref_table_present(self) -> None:
        pdf = self._make_pdf()
        self.assertIn(b"xref", pdf)
        self.assertIn(b"startxref", pdf)

    def test_multiple_pages(self) -> None:
        w = _SimplePDFWriter()
        for i in range(3):
            w.add_page()
            w.draw_text(50, 750, f"Page {i + 1}")
        pdf = w.render()
        self.assertIn(b"/Count 3", pdf)

    def test_empty_writer_renders_one_page(self) -> None:
        w = _SimplePDFWriter()
        pdf = w.render()
        self.assertIn(b"%PDF-1.4", pdf)

    def test_non_ascii_replaced(self) -> None:
        w = _SimplePDFWriter()
        w.add_page()
        w.draw_text(50, 750, "中文字符")
        pdf = w.render()
        # Non-latin chars replaced with '?'; PDF must still be valid
        self.assertIn(b"%%EOF", pdf)

    def test_pdf_string_escaping(self) -> None:
        w = _SimplePDFWriter()
        w.add_page()
        w.draw_text(50, 750, "line(one)back\\slash")
        pdf = w.render()
        self.assertIn(rb"\(one\)", pdf)
        self.assertIn(rb"\\slash", pdf)

    def test_draw_text_without_page_raises(self) -> None:
        w = _SimplePDFWriter()
        with self.assertRaises(RuntimeError):
            w.draw_text(50, 750, "oops")


# ──────────────────────────────────────────────────────────────────────────────
# export_pdf_report
# ──────────────────────────────────────────────────────────────────────────────

class TestExportPdfReport(unittest.TestCase):
    def test_generates_pdf_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = export_pdf_report(td)
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.endswith(".pdf"))

    def test_pdf_content_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "TestProj", "score": 90}, f)
            path = export_pdf_report(td)
            with open(path, "rb") as f:
                data = f.read()
            self.assertTrue(data.startswith(b"%PDF"))
            self.assertIn(b"%%EOF", data)

    def test_custom_filename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = export_pdf_report(td, output_filename="my_report.pdf")
            self.assertTrue(path.endswith("my_report.pdf"))
            self.assertTrue(os.path.isfile(path))

    def test_empty_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = export_pdf_report(td)
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as f:
                data = f.read()
            self.assertTrue(data.startswith(b"%PDF"))

    def test_with_backtest_data(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "backtest_report.json"), "w") as f:
                json.dump({
                    "sharpe_ratio": 1.5,
                    "max_drawdown": 0.12,
                    "total_return": 0.35,
                }, f)
            path = export_pdf_report(td)
            self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()
