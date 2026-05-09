# ruff: noqa: E402
"""v1.0.5 round 2 extras — Q001 broader patterns, noqa, dirty-data, live_trader smoke,
ReviewReport.failure_type, run_meta plumbing."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.quant_lint import analyse_quant_lint_from_files
from crucible.features.quant_smoke import (
    quant_smoke_dryrun,
    synthesise_ohlcv_csv,
    _build_live_trader_smoke_script,
)
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


class TestQ001BroaderPatterns(unittest.TestCase):
    def test_df_loc_tuple_form_detected(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx in df.index:\n"
                "        if df.loc[idx, 'close'] > 100:\n"
                "            entry_price = df.loc[idx, 'open']\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(len(bad), 1)

    def test_float_wrapper_detected(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row.close > 0:\n"
                "            entry_price = float(row.open)\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(len(bad), 1)

    def test_alternate_variable_names_detected(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row['close'] > 0:\n"
                "            buy_price = row['open']\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(len(bad), 1)


class TestQ001Noqa(unittest.TestCase):
    def test_noqa_q001_suppresses(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row['close'] > 0:\n"
                "            entry_price = row['open']  # noqa: Q001\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(bad, [])

    def test_bare_noqa_suppresses(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row['close'] > 0:\n"
                "            entry_price = row['open']  # noqa\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(bad, [])

    def test_noqa_unrelated_rule_does_not_suppress(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row['close'] > 0:\n"
                "            entry_price = row['open']  # noqa: Q002\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(len(bad), 1)


class TestDirtyDataFixture(unittest.TestCase):
    def test_inject_anomalies_adds_nan_volume(self) -> None:
        csv = synthesise_ohlcv_csv(n_rows=20, seed=1, inject_anomalies=True)
        self.assertIn("NaN", csv)
        self.assertIn(",0\n", csv)  # zero-volume row exists

    def test_clean_default_unchanged(self) -> None:
        csv = synthesise_ohlcv_csv(n_rows=20, seed=1)
        self.assertNotIn("NaN", csv)

    def test_anomalies_row_count_preserved(self) -> None:
        n = 30
        csv = synthesise_ohlcv_csv(n_rows=n, inject_anomalies=True)
        body = csv.strip().splitlines()[1:]  # skip header
        self.assertEqual(len(body), n)


class TestLiveTraderSmoke(unittest.TestCase):
    def test_no_live_trader_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write("print('ok')\n")
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("\n")
            self.assertIsNone(_build_live_trader_smoke_script(d))

    def test_live_trader_present_returns_script(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "live_trader.py"), "w", encoding="utf-8") as f:
                f.write("# live trader\n")
            script = _build_live_trader_smoke_script(d)
            self.assertIsInstance(script, str)
            assert script is not None
            self.assertIn("ccxt", script)

    def test_live_trader_clean_passes_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for name, body in (
                ("strategy.py", "def run(): return 0\n"),
                ("backtest.py", "if __name__ == '__main__':\n    print('ok')\n"),
                (
                    "live_trader.py",
                    "import ccxt\n"
                    "class LiveTrader:\n"
                    "    def __init__(self, *a, **kw):\n"
                    "        self.ex = ccxt.binance()\n"
                    "    def run_loop(self):\n"
                    "        for _ in range(5):\n"
                    "            t = self.ex.fetch_ticker('BTC/USDT')\n"
                    "            assert t['last'] > 0\n",
                ),
            ):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(body)
            result = quant_smoke_dryrun(d, timeout_seconds=30)
            self.assertTrue(result.passes, msg=result.log)
            self.assertTrue(result.live_trader_passes is True, msg=result.live_trader_log)

    def test_live_trader_with_runtime_error_emits_high_issue(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for name, body in (
                ("strategy.py", "def run(): return 0\n"),
                ("backtest.py", "if __name__ == '__main__':\n    print('ok')\n"),
                (
                    "live_trader.py",
                    "class LiveTrader:\n"
                    "    def __init__(self):\n"
                    "        pass\n"
                    "    def run_loop(self):\n"
                    "        raise TypeError('Trade kwargs mismatch')\n",
                ),
            ):
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(body)
            result = quant_smoke_dryrun(d, timeout_seconds=30)
            self.assertFalse(result.passes)
            self.assertEqual(result.live_trader_passes, False)
            rules = {i["rule"] for i in result.issues}
            # Either typeerror-tagged or generic live-trader-failed.
            self.assertTrue(
                any(r.startswith("Q02") for r in rules),
                msg=f"got rules={rules}",
            )


class TestReviewReportFailureType(unittest.TestCase):
    def test_field_default_none(self) -> None:
        report = ReviewReport(passes=True, summary="ok", issues=[])
        self.assertIsNone(report.failure_type)

    def test_field_set_explicitly(self) -> None:
        report = ReviewReport(
            passes=False,
            summary="x",
            issues=[],
            failure_type=FailureType.QUALITY_LOOP_GAVE_UP.value,
        )
        self.assertEqual(report.failure_type, "QUALITY_LOOP_GAVE_UP")


def _make_report() -> AnalysisReport:
    return AnalysisReport(
        project_name="test",
        summary="A summary.",
        consensus="Consensus.",
        disagreement="Disagreement.",
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


def _make_gate() -> GateDecision:
    return GateDecision(
        consensus="ok",
        disagreement="",
        experiments=[],
        ready_for_codegen=False,
        overall_score=40,
        confidence="medium",
        codegen_scope="production",
    )


class TestRunMetaPlumbing(unittest.TestCase):
    """P2-11 round 2: review.failure_type and quality_passed must reach run_meta.json."""

    def test_run_meta_includes_quality_loop_failure_type(self) -> None:
        review = ReviewReport(
            passes=False,
            summary="…",
            issues=[],
            failure_type="QUALITY_LOOP_GAVE_UP",
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
                import json as _json

                with open(
                    os.path.join(project_dir, "run_meta.json"), encoding="utf-8"
                ) as f:
                    meta = _json.load(f)
                self.assertEqual(meta.get("quality_loop_failure_type"), "QUALITY_LOOP_GAVE_UP")
                self.assertEqual(meta.get("quality_passed"), False)

    def test_run_meta_quality_passed_only_when_no_failure_type(self) -> None:
        review = ReviewReport(passes=True, summary="ok", issues=[])
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
                import json as _json

                with open(
                    os.path.join(project_dir, "run_meta.json"), encoding="utf-8"
                ) as f:
                    meta = _json.load(f)
                self.assertEqual(meta.get("quality_passed"), True)
                self.assertIsNone(meta.get("quality_loop_failure_type"))

    def test_review_failure_type_powers_banner(self) -> None:
        """Banner uses structured failure_type, not just summary substring."""
        review = ReviewReport(
            passes=False,
            summary="generic message without marker",  # no QUALITY_LOOP_GAVE_UP substring
            issues=[],
            failure_type="QUALITY_LOOP_GAVE_UP",
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
                with open(
                    os.path.join(project_dir, "README.md"), encoding="utf-8"
                ) as f:
                    md = f.read()
                self.assertIn("Quality review did NOT pass", md)
                self.assertIn("early-stop stagnation", md)


if __name__ == "__main__":
    unittest.main()
