# ruff: noqa: E402
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.module_runtime import get_runtime

qsc = get_runtime()


class TestRuntimeValidation(unittest.TestCase):
    def _bundle(
        self, content: str, path: str = "main.py", project_type: str = "saas"
    ) -> qsc.CodeBundle:
        return qsc.CodeBundle(
            project_type=project_type,
            files=[qsc.GeneratedFile(path=path, content=content)],
        )

    def test_runtime_validation_fastapi(self) -> None:
        content = "\n".join(
            [
                "from fastapi import FastAPI",
                "app = FastAPI()",
                "",
                "@app.get('/health')",
                "def health():",
                "    return {'ok': True}",
            ]
        )
        # v1.0.5 round 3 final: realistic SaaS bundle includes a
        # requirements.txt declaring its web framework, otherwise the H001
        # mode-specific lint correctly flags an undeclared import.
        bundle = qsc.CodeBundle(
            project_type="saas",
            files=[
                qsc.GeneratedFile(path="main.py", content=content),
                qsc.GeneratedFile(
                    path="requirements.txt",
                    content="fastapi==0.110\nuvicorn==0.30\n",
                ),
            ],
        )
        ok, issues, log = qsc.run_runtime_validation(bundle, mode="SaaS")
        self.assertTrue(ok, msg=log)
        self.assertEqual(issues, [])
        self.assertIn("py_compile", log)

    def test_entrypoint_override_missing(self) -> None:
        bundle = self._bundle("app = object()\n", project_type="quant")
        ok, issues, log = qsc.run_runtime_validation(
            bundle, mode="Quant", entrypoint_override="missing.py:app"
        )
        self.assertFalse(ok)
        self.assertTrue(any("Entrypoint override not found" in i.description for i in issues))
        self.assertIn("Entrypoint override not found", log)

    def test_runtime_validation_fails_when_web_mode_has_no_python_files(self) -> None:
        bundle = qsc.CodeBundle(
            project_type="saas",
            files=[qsc.GeneratedFile(path="README.md", content="# not runnable\n")],
        )
        ok, issues, log = qsc.run_runtime_validation(bundle, mode="SaaS")
        self.assertFalse(ok)
        self.assertTrue(
            any("contains no Python entrypoints" in i.description for i in issues)
        )
        self.assertIn("requires Python entrypoints", log)

    def test_quant_mode_does_not_infer_snapshot_validation_from_prompt_keywords(self) -> None:
        bundle = self._bundle(
            "def run() -> int:\n    return 1\n",
            path="strategy.py",
            project_type="quant",
        )
        ok, issues, log = qsc.run_runtime_validation(
            bundle,
            user_problem="Build a /v1/snapshot API endpoint for market snapshots.",
            mode="Quant",
        )
        self.assertTrue(ok)
        self.assertEqual(issues, [])
        self.assertNotIn("Snapshot endpoint validation required.", log)

    def test_quant_project_type_without_explicit_mode_still_blocks_snapshot_keyword_inference(
        self,
    ) -> None:
        bundle = self._bundle(
            "def run() -> int:\n    return 1\n",
            path="strategy.py",
            project_type="quant",
        )
        ok, issues, log = qsc.run_runtime_validation(
            bundle,
            user_problem="Build a /v1/snapshot API endpoint for market snapshots.",
        )
        self.assertTrue(ok)
        self.assertEqual(issues, [])
        self.assertNotIn("Snapshot endpoint validation required.", log)

    def test_agent_project_type_without_explicit_mode_is_not_upgraded_into_saas_web_validation(
        self,
    ) -> None:
        bundle = self._bundle(
            "def run() -> int:\n    return 1\n",
            path="main.py",
            project_type="agent",
        )
        ok, issues, log = qsc.run_runtime_validation(
            bundle,
            user_problem="Build a SaaS dashboard for human operators.",
        )
        self.assertTrue(ok)
        self.assertEqual(issues, [])
        self.assertNotIn("Web app validation required.", log)

    def test_saas_project_type_without_explicit_mode_still_requires_web_validation(self) -> None:
        bundle = self._bundle(
            "def run() -> int:\n    return 1\n",
            path="worker.py",
            project_type="saas",
        )
        ok, issues, log = qsc.run_runtime_validation(bundle)
        self.assertFalse(ok)
        self.assertIn("Web app validation required.", log)
        self.assertTrue(issues, msg=log)
        self.assertIn("No entrypoints detected", log)

    def test_snapshot_route_missing_reports_precise_runtime_fix(self) -> None:
        content = "\n".join(
            [
                "from fastapi import FastAPI",
                "app = FastAPI()",
                "",
                "@app.get('/health')",
                "def health():",
                "    return {'ok': True}",
            ]
        )
        bundle = self._bundle(content)
        ok, issues, log = qsc.run_runtime_validation(
            bundle,
            user_problem="Create a /v1/snapshot API endpoint for market snapshots.",
            mode="SaaS",
        )
        self.assertFalse(ok)
        self.assertIn("snapshot_route_missing", log)
        self.assertTrue(
            any("snapshot route missing" in i.description.lower() for i in issues)
        )
        self.assertTrue(
            any("/v1/snapshot" in (i.suggestion or "") for i in issues)
        )
        self.assertTrue(
            any("/health endpoint alone is insufficient" in (i.suggestion or "") for i in issues)
        )

    def test_web_validation_does_not_infer_saas_mode_from_prompt_without_mode_metadata(
        self,
    ) -> None:
        self.assertFalse(
            qsc.requires_web_validation("Build a SaaS dashboard for human operators.")
        )

    def test_web_validation_fails_closed_when_explicit_mode_conflicts_with_bundle_project_type(
        self,
    ) -> None:
        bundle = self._bundle(
            "def run() -> int:\n    return 1\n",
            path="main.py",
            project_type="agent",
        )
        self.assertFalse(
            qsc.requires_web_validation(
                None,
                mode="SaaS",
                code_bundle=bundle,
            )
        )

    def test_web_validation_fails_closed_when_explicit_mode_conflicts_with_analysis_mode(
        self,
    ) -> None:
        analysis_report = qsc.AnalysisReport(
            project_name="mode_conflict",
            summary="summary",
            consensus="consensus",
            disagreement="disagreement",
            experiments=[],
            score=80,
            mode_used="Agent",
            risk_level="Medium",
        )
        self.assertFalse(
            qsc.requires_web_validation(
                None,
                mode="SaaS",
                analysis_report=analysis_report,
            )
        )

    def test_snapshot_validation_fails_closed_when_explicit_mode_conflicts_with_bundle_project_type(
        self,
    ) -> None:
        bundle = self._bundle(
            "def run() -> int:\n    return 1\n",
            path="strategy.py",
            project_type="quant",
        )
        self.assertFalse(
            qsc.requires_snapshot_validation(
                "Build a /v1/snapshot API endpoint for market snapshots.",
                bundle,
                mode="SaaS",
            )
        )

    def test_runtime_validation_rejects_invalid_code_bundle_instead_of_applying_saas_prompt_heuristics(
        self,
    ) -> None:
        bundle = self._bundle(
            "from fastapi import FastAPI\napp = FastAPI()\n",
            path="main.py",
            project_type="unknown-mode",
        )
        ok, issues, log = qsc.run_runtime_validation(
            bundle,
            user_problem="Build a SaaS dashboard for human operators.",
        )
        self.assertFalse(ok)
        self.assertIn("Invalid CodeBundle rejected before validation.", log)
        self.assertTrue(
            any("invalid codebundle" in i.description.lower() for i in issues),
            msg=log,
        )
        self.assertNotIn("Web app validation required.", log)

    def test_runtime_validation_fails_closed_when_explicit_mode_conflicts_with_bundle_project_type(
        self,
    ) -> None:
        bundle = self._bundle(
            "\n".join(
                [
                    "from fastapi import FastAPI",
                    "app = FastAPI()",
                    "",
                    "@app.get('/health')",
                    "def health():",
                    "    return {'ok': True}",
                ]
            ),
            path="main.py",
            project_type="saas",
        )
        ok, issues, log = qsc.run_runtime_validation(bundle, mode="Quant")
        self.assertFalse(ok)
        self.assertIn("Mode/project_type mismatch rejected before validation.", log)
        self.assertTrue(
            any("conflicted with the explicitly requested mode" in i.description for i in issues),
            msg=log,
        )
        self.assertNotIn("Web app validation required.", log)
        self.assertNotIn("py_compile", log)


if __name__ == "__main__":
    unittest.main()
