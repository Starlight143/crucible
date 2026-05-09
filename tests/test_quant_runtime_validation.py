# ruff: noqa: E402
"""Tests for the v1.0.5 Quant runtime-validation track in section_06."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.modules.section_03_models_and_context import (
    CodeBundle,
    GeneratedFile,
    QUANT_ENTRYPOINT_FILES,
)
from crucible.modules.section_06_runtime_quality_api import (
    _check_production_quant_tests,
    _detect_quant_entrypoints,
    _is_quant_validation,
    _run_quant_import_smoke,
    run_runtime_validation,
)


def _make_bundle(files, project_type="quant"):
    return CodeBundle(
        project_type=project_type,
        files=[GeneratedFile(path=p, content=c) for p, c in files],
    )


class TestQuantEntrypointDetection(unittest.TestCase):
    def test_quant_entrypoint_set_includes_canonical_names(self) -> None:
        for name in ("backtest.py", "strategy.py", "trade.py", "data_provider.py"):
            self.assertIn(name, QUANT_ENTRYPOINT_FILES)

    def test_detect_quant_entrypoints(self) -> None:
        py_files = [
            "/tmp/x/backtest.py",
            "/tmp/x/strategy.py",
            "/tmp/x/utils.py",
        ]
        entries = _detect_quant_entrypoints(py_files)
        labels = {os.path.basename(e.path) for e in entries}
        self.assertEqual(labels, {"backtest.py", "strategy.py"})

    def test_is_quant_validation_via_project_type(self) -> None:
        bundle = _make_bundle([("backtest.py", "x = 1\n")])
        self.assertTrue(_is_quant_validation(bundle, mode=None))
        saas = _make_bundle([("app.py", "x = 1\n")], project_type="saas")
        self.assertFalse(_is_quant_validation(saas, mode=None))


class TestProductionTestsCheck(unittest.TestCase):
    """P2-10: Quant production scope must ship a tests/ directory."""

    def test_missing_tests_emits_high_severity_issue(self) -> None:
        bundle = _make_bundle(
            [
                ("backtest.py", "print('ok')\n"),
                ("strategy.py", "x=1\n"),
            ],
        )
        issues = _check_production_quant_tests(
            bundle, mode=None, codegen_scope="production"
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, "high")
        self.assertIn("tests/", issues[0].description)

    def test_present_tests_does_not_fire(self) -> None:
        bundle = _make_bundle(
            [
                ("backtest.py", "x=1\n"),
                ("tests/test_strategy.py", "def test_x(): pass\n"),
            ],
        )
        self.assertEqual(
            _check_production_quant_tests(
                bundle, mode=None, codegen_scope="production"
            ),
            [],
        )

    def test_mvp_scope_skipped(self) -> None:
        bundle = _make_bundle([("backtest.py", "x=1\n")])
        self.assertEqual(
            _check_production_quant_tests(bundle, mode=None, codegen_scope="mvp"),
            [],
        )

    def test_saas_skipped(self) -> None:
        bundle = _make_bundle(
            [("app.py", "x=1\n")],
            project_type="saas",
        )
        self.assertEqual(
            _check_production_quant_tests(
                bundle, mode=None, codegen_scope="production"
            ),
            [],
        )

    def test_default_treated_as_production(self) -> None:
        """When no scope is supplied, treat the bundle as production (strictest)."""
        bundle = _make_bundle([("backtest.py", "x=1\n"), ("strategy.py", "x=1\n")])
        # No codegen_scope arg → default "production" → fires.
        issues = _check_production_quant_tests(bundle, mode=None)
        self.assertEqual(len(issues), 1)


class TestRuntimeValidationQuantTrack(unittest.TestCase):
    """End-to-end check: a Quant bundle with kwargs mismatch fails validation."""

    def test_clean_quant_bundle_passes(self) -> None:
        bundle = _make_bundle(
            [
                (
                    "trade.py",
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n"
                    "    symbol: str\n"
                    "    price: float\n",
                ),
                (
                    "strategy.py",
                    "def signal(): return 0\n",
                ),
                (
                    "backtest.py",
                    "from trade import Trade\n"
                    "if __name__ == '__main__':\n"
                    "    t = Trade(symbol='X', price=1.0)\n"
                    "    print('{\"sharpe_ratio\": 0.0}')\n",
                ),
                (
                    "tests/test_strategy.py",
                    "def test_run(): assert True\n",
                ),
            ],
        )
        ok, issues, log = run_runtime_validation(bundle, mode="quant")
        self.assertTrue(ok, msg=f"issues={issues}\nlog=\n{log}")
        self.assertEqual(issues, [])
        # The new log line must show the Quant track ran.
        self.assertIn("Quant mode detected", log)

    def test_kwargs_mismatch_in_quant_bundle_fails(self) -> None:
        bundle = _make_bundle(
            [
                (
                    "trade.py",
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n"
                    "    symbol: str\n",
                ),
                ("strategy.py", "def s(): pass\n"),
                (
                    "backtest.py",
                    "from trade import Trade\n"
                    "Trade(side='long', quantity=10)\n",
                ),
                (
                    "tests/test_x.py",
                    "def test_x(): assert True\n",
                ),
            ],
        )
        ok, issues, log = run_runtime_validation(bundle, mode="quant")
        self.assertFalse(ok, msg=f"log=\n{log}")
        # X001 cross-ref issues must be in the issue list.
        descriptions = " ".join(i.description for i in issues)
        self.assertIn("Trade", descriptions)

    def test_lookahead_lint_in_quant_bundle_fails(self) -> None:
        bundle = _make_bundle(
            [
                (
                    "strategy.py",
                    "def signal(df):\n"
                    "    for row in df:\n"
                    "        if row['close'] > 0:\n"
                    "            entry_price = row['open']\n"
                    "            return entry_price\n",
                ),
                ("backtest.py", "if __name__ == '__main__':\n    print('ok')\n"),
                ("tests/test_x.py", "def test_x(): assert True\n"),
            ],
        )
        ok, issues, log = run_runtime_validation(bundle, mode="quant")
        self.assertFalse(ok)
        rules_found = " ".join(i.description for i in issues)
        self.assertIn("Look-ahead bias", rules_found)

    def test_missing_tests_in_production_quant_fails(self) -> None:
        bundle = _make_bundle(
            [
                ("backtest.py", "if __name__ == '__main__':\n    print('ok')\n"),
                ("strategy.py", "def s(): pass\n"),
            ],
        )
        # Production-scope tests/ enforcement is opt-in via env var to keep
        # backwards compatibility with existing fixtures that don't ship tests.
        os.environ["CRUCIBLE_QUANT_REQUIRE_TESTS"] = "1"
        try:
            ok, issues, log = run_runtime_validation(bundle, mode="quant")
        finally:
            os.environ.pop("CRUCIBLE_QUANT_REQUIRE_TESTS", None)
        self.assertFalse(ok)
        descriptions = " ".join(i.description for i in issues)
        self.assertIn("tests/", descriptions)

    def test_missing_tests_silent_when_opt_in_disabled(self) -> None:
        """Without CRUCIBLE_QUANT_REQUIRE_TESTS, missing tests/ does not block."""
        bundle = _make_bundle(
            [
                ("backtest.py", "if __name__ == '__main__':\n    print('ok')\n"),
                ("strategy.py", "def s(): pass\n"),
            ],
        )
        os.environ.pop("CRUCIBLE_QUANT_REQUIRE_TESTS", None)
        ok, issues, log = run_runtime_validation(bundle, mode="quant")
        descriptions = " ".join(i.description for i in issues)
        self.assertNotIn("missing tests/", descriptions)
        # Nothing else should fail either — clean bundle.
        self.assertTrue(ok, msg=f"issues={issues}\nlog={log}")


class TestQuantImportSmoke(unittest.TestCase):
    """Direct test of _run_quant_import_smoke against a real tmp dir."""

    def test_clean_modules_pass(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for name, body in (
                ("backtest.py", "x = 1\n"),
                ("strategy.py", "y = 2\n"),
            ):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(body)
            py_files = [os.path.join(d, "backtest.py"), os.path.join(d, "strategy.py")]
            issues, log = _run_quant_import_smoke(py_files, d, dict(os.environ))
            self.assertEqual(issues, [], msg=log)

    def test_import_error_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write("import not_a_real_module_anywhere_zzz\n")
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("y = 2\n")
            py_files = [os.path.join(d, "backtest.py"), os.path.join(d, "strategy.py")]
            issues, log = _run_quant_import_smoke(py_files, d, dict(os.environ))
            self.assertGreaterEqual(len(issues), 1)
            self.assertEqual(issues[0].severity, "high")


if __name__ == "__main__":
    unittest.main()
