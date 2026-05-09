# ruff: noqa: E402
"""Tests for the v1.0.5 README failure banner (P2-12) and stagnation marker (P2-11)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.modules.section_03_models_and_context import (
    AnalysisReport,
    CodeBundle,
    Experiment,
    FailureType,
    GateDecision,
    GeneratedFile,
    ReviewReport,
)
from crucible.modules.section_07_selfcheck_output_main import save_project_output


def _make_report() -> AnalysisReport:
    return AnalysisReport(
        project_name="banner_test",
        summary="A summary.",
        consensus="Consensus text.",
        disagreement="Disagreement text.",
        experiments=[Experiment(goal="g", criteria="c")],
        score=40,
        mode_used="Quant",
        risk_level="High",
    )


def _make_bundle() -> CodeBundle:
    return CodeBundle(
        project_type="quant",
        files=[GeneratedFile(path="strategy.py", content="x = 1\n")],
    )


def _make_gate(ready: bool = False) -> GateDecision:
    return GateDecision(
        consensus="ok",
        disagreement="",
        experiments=[],
        ready_for_codegen=ready,
        overall_score=40,
        confidence="medium",
        codegen_scope="production",
    )


class TestFailureBanner(unittest.TestCase):
    def test_banner_rendered_when_quality_pass_false(self) -> None:
        review = ReviewReport(
            passes=False,
            summary="Standard issues remained.",
            issues=[],
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "crucible.modules.section_07_selfcheck_output_main._REPO_ROOT", d
            ):
                project_dir = save_project_output(
                    result=_make_report(),
                    code=_make_bundle(),
                    review=review,
                    gate_decision=_make_gate(),
                    language_hint="English",
                )
                with open(os.path.join(project_dir, "README.md"), encoding="utf-8") as f:
                    md = f.read()
                self.assertIn("Quality review did NOT pass", md)
                self.assertIn(">", md)  # blockquote markdown
                # The gave-up extra must NOT appear when stagnation marker absent.
                self.assertNotIn("early-stop stagnation", md)

    def test_banner_includes_giveup_extra_when_marker_present(self) -> None:
        # v1.0.5 round 3: banner detection MUST go through the structured
        # failure_type field; the summary substring fallback was removed.
        review = ReviewReport(
            passes=False,
            summary="runtime issues remained.",  # no marker in summary
            issues=[],
            failure_type=FailureType.QUALITY_LOOP_GAVE_UP.value,
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "crucible.modules.section_07_selfcheck_output_main._REPO_ROOT", d
            ):
                project_dir = save_project_output(
                    result=_make_report(),
                    code=_make_bundle(),
                    review=review,
                    gate_decision=_make_gate(),
                    language_hint="English",
                )
                with open(os.path.join(project_dir, "README.md"), encoding="utf-8") as f:
                    md = f.read()
                self.assertIn("Quality review did NOT pass", md)
                self.assertIn("early-stop stagnation", md)

    def test_banner_skips_giveup_extra_when_only_summary_has_marker(self) -> None:
        """v1.0.5 round 3 (strict): with substring fallback removed, having
        only a textual marker in summary (no structured failure_type) must
        NOT fire the gave-up banner extra. This guards against future
        regressions where someone re-introduces the substring fallback as a
        'convenience' shortcut."""
        review = ReviewReport(
            passes=False,
            summary=f"runtime issues. [{FailureType.QUALITY_LOOP_GAVE_UP.value}: stuck]",
            issues=[],
            # failure_type intentionally omitted
        )
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "crucible.modules.section_07_selfcheck_output_main._REPO_ROOT", d
            ):
                project_dir = save_project_output(
                    result=_make_report(),
                    code=_make_bundle(),
                    review=review,
                    gate_decision=_make_gate(),
                    language_hint="English",
                )
                with open(os.path.join(project_dir, "README.md"), encoding="utf-8") as f:
                    md = f.read()
                # General failure banner still fires (passes=False).
                self.assertIn("Quality review did NOT pass", md)
                # But the gave-up extra must NOT — only structured field can fire it.
                self.assertNotIn("early-stop stagnation", md)

    def test_banner_skipped_when_quality_pass_true(self) -> None:
        review = ReviewReport(passes=True, summary="all good", issues=[])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "crucible.modules.section_07_selfcheck_output_main._REPO_ROOT", d
            ):
                project_dir = save_project_output(
                    result=_make_report(),
                    code=_make_bundle(),
                    review=review,
                    gate_decision=_make_gate(ready=True),
                    language_hint="English",
                )
                with open(os.path.join(project_dir, "README.md"), encoding="utf-8") as f:
                    md = f.read()
                self.assertNotIn("Quality review did NOT pass", md)

    def test_banner_in_zh_locale(self) -> None:
        review = ReviewReport(passes=False, summary="some issues", issues=[])
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                "crucible.modules.section_07_selfcheck_output_main._REPO_ROOT", d
            ):
                project_dir = save_project_output(
                    result=_make_report(),
                    code=_make_bundle(),
                    review=review,
                    gate_decision=_make_gate(),
                    language_hint="Traditional Chinese",
                )
                with open(os.path.join(project_dir, "README.md"), encoding="utf-8") as f:
                    md = f.read()
                # Chinese banner title must render.
                self.assertIn("品質審查未通過", md)


if __name__ == "__main__":
    unittest.main()
