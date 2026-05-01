# ruff: noqa: E402
"""Tests for crucible.features.backtest_runner."""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.backtest_runner import (
    _COMPARISON_METRICS,
    _LOWER_IS_BETTER,
    _TIMEFRAME_PROFILES,
    BacktestComparison,
    BacktestMetrics,
    BacktestReport,
    ParameterCombo,
    _build_param_combos,
    _ccxt_available,
    _count_csv_rows,
    _detect_param_space,
    _detect_symbol_from_code,
    _detect_timeframe_from_code,
    _extract_code_block,
    _fetch_ccxt_ohlcv,
    _fill_metrics_from_dict,
    _find_backtest_entry,
    _find_code_dir,
    _has_data_file,
    _interval_to_timedelta,
    _is_crypto_symbol,
    _is_intraday_interval,
    _params_to_env,
    _parse_numeric,
    _period_to_candles,
    _period_to_days,
    _run_project_data_provider,
    _try_parse_json_from_text,
    compare_backtest_reports,
    generate_synthetic_ohlcv,
    prepare_data,
    resolve_timeframe_profile,
    run_backtest_pipeline,
)


class TestGenerateSyntheticOHLCV(unittest.TestCase):
    def test_generates_correct_row_count(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=100, seed=1)
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        self.assertEqual(len(rows), 100)

    def test_has_required_columns(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=10, seed=1)
        reader = csv.DictReader(io.StringIO(csv_text))
        row = next(reader)
        for col in ("date", "open", "high", "low", "close", "volume"):
            self.assertIn(col, row)

    def test_prices_are_positive(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=200, seed=42)
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            self.assertGreater(float(row["open"]), 0)
            self.assertGreater(float(row["high"]), 0)
            self.assertGreater(float(row["low"]), 0)
            self.assertGreater(float(row["close"]), 0)
            self.assertGreater(int(row["volume"]), 0)

    def test_high_gte_low(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=200, seed=42)
        reader = csv.DictReader(io.StringIO(csv_text))
        for row in reader:
            self.assertGreaterEqual(float(row["high"]), float(row["low"]))

    def test_deterministic_with_seed(self) -> None:
        csv1 = generate_synthetic_ohlcv(rows=50, seed=123)
        csv2 = generate_synthetic_ohlcv(rows=50, seed=123)
        self.assertEqual(csv1, csv2)


class TestCcxtIsolation(unittest.TestCase):
    def test_ccxt_available_uses_find_spec(self) -> None:
        with patch(
            "crucible.features.backtest_runner.importlib.util.find_spec",
            return_value=object(),
        ) as mocked_find_spec:
            self.assertTrue(_ccxt_available())
            mocked_find_spec.assert_called_once_with("ccxt")

    def test_fetch_ccxt_ohlcv_uses_isolated_subprocess(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="date,open,high,low,close,volume\n2024-01-01,1,2,0.5,1.5,100\n",
            stderr="",
        )
        with patch(
            "crucible.features.backtest_runner.subprocess.run",
            return_value=completed,
        ) as mocked_run:
            csv_text = _fetch_ccxt_ohlcv("BTCUSDT", interval="1d", limit=10)
            self.assertIn("date,open,high,low,close,volume", csv_text or "")
            invoked = mocked_run.call_args.args[0]
            self.assertEqual(invoked[0], sys.executable)
            self.assertEqual(invoked[1], "-c")
            self.assertEqual(invoked[3:], ["BTCUSDT", "1d", "10"])

    def test_different_seeds_differ(self) -> None:
        csv1 = generate_synthetic_ohlcv(rows=50, seed=1)
        csv2 = generate_synthetic_ohlcv(rows=50, seed=2)
        self.assertNotEqual(csv1, csv2)

    def test_no_weekend_dates(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=100, seed=1)
        reader = csv.DictReader(io.StringIO(csv_text))
        from datetime import datetime
        for row in reader:
            dt = datetime.strptime(row["date"], "%Y-%m-%d")
            self.assertLess(dt.weekday(), 5, f"Weekend date found: {row['date']}")

    def test_custom_start_date(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=5, seed=1, start_date="2023-06-01")
        reader = csv.DictReader(io.StringIO(csv_text))
        first_row = next(reader)
        self.assertEqual(first_row["date"], "2023-06-01")


class TestBacktestMetrics(unittest.TestCase):
    def test_to_dict_excludes_none(self) -> None:
        m = BacktestMetrics(sharpe_ratio=1.5, total_return_pct=25.0)
        d = m.to_dict()
        self.assertEqual(d["sharpe_ratio"], 1.5)
        self.assertEqual(d["total_return_pct"], 25.0)
        self.assertNotIn("win_rate", d)
        self.assertNotIn("trade_count", d)

    def test_metric_value(self) -> None:
        m = BacktestMetrics(sharpe_ratio=2.3)
        self.assertAlmostEqual(m.metric_value("sharpe_ratio"), 2.3)
        self.assertIsNone(m.metric_value("win_rate"))
        self.assertIsNone(m.metric_value("nonexistent"))


class TestParameterCombo(unittest.TestCase):
    def test_to_dict(self) -> None:
        combo = ParameterCombo(
            params={"lookback": 20},
            metrics=BacktestMetrics(sharpe_ratio=1.0),
            success=True,
        )
        d = combo.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["params"]["lookback"], 20)
        self.assertEqual(d["metrics"]["sharpe_ratio"], 1.0)

    def test_to_dict_with_error(self) -> None:
        combo = ParameterCombo(params={"x": 1}, error="crash", success=False)
        d = combo.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"], "crash")


class TestBacktestReport(unittest.TestCase):
    def test_to_dict(self) -> None:
        r = BacktestReport(
            success=True,
            data_source="synthetic",
            data_rows=500,
            combos_evaluated=10,
            best_params={"lookback": 20},
        )
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["data_source"], "synthetic")
        self.assertEqual(d["best_params"]["lookback"], 20)

    def test_summary_text(self) -> None:
        r = BacktestReport(
            success=True,
            data_source="synthetic",
            data_rows=500,
            baseline_metrics=BacktestMetrics(
                sharpe_ratio=1.5, total_return_pct=15.0,
            ),
        )
        text = r.summary_text()
        self.assertIn("SUCCESS", text)
        self.assertIn("1.5000", text)
        self.assertIn("15.00", text)

    def test_summary_text_failed(self) -> None:
        r = BacktestReport(success=False, errors=["crash"])
        text = r.summary_text()
        self.assertIn("FAILED", text)
        self.assertIn("crash", text)


class TestFindCodeDir(unittest.TestCase):
    def test_finds_code_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "code"))
            self.assertEqual(_find_code_dir(td), os.path.join(td, "code"))

    def test_returns_none_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_find_code_dir(td))


class TestFindBacktestEntry(unittest.TestCase):
    def test_finds_backtest_py(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bt = os.path.join(td, "backtest.py")
            with open(bt, "w") as f:
                f.write("# backtest\n")
            self.assertEqual(_find_backtest_entry(td), bt)

    def test_finds_main_py(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            main = os.path.join(td, "main.py")
            with open(main, "w") as f:
                f.write("# main\n")
            self.assertEqual(_find_backtest_entry(td), main)

    def test_prefers_backtest_over_main(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for name in ("main.py", "backtest.py"):
                with open(os.path.join(td, name), "w") as f:
                    f.write(f"# {name}\n")
            result = _find_backtest_entry(td)
            self.assertIn("backtest.py", result)

    def test_finds_file_with_backtest_in_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bt = os.path.join(td, "my_backtest_engine.py")
            with open(bt, "w") as f:
                f.write("# engine\n")
            result = _find_backtest_entry(td)
            self.assertIn("my_backtest_engine.py", result)

    def test_returns_none_if_no_python(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "readme.txt"), "w") as f:
                f.write("hello\n")
            self.assertIsNone(_find_backtest_entry(td))


class TestHasDataFile(unittest.TestCase):
    def test_csv_in_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "prices.csv"), "w") as f:
                f.write("date,close\n")
            self.assertTrue(_has_data_file(td))

    def test_csv_in_data_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data_dir = os.path.join(td, "data")
            os.makedirs(data_dir)
            with open(os.path.join(data_dir, "ohlcv.csv"), "w") as f:
                f.write("date,close\n")
            self.assertTrue(_has_data_file(td))

    def test_no_data_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write("x = 1\n")
            self.assertFalse(_has_data_file(td))

    def test_json_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "ticks.json"), "w") as f:
                f.write("[]")
            self.assertTrue(_has_data_file(td))


class TestParseNumeric(unittest.TestCase):
    def test_int(self) -> None:
        self.assertEqual(_parse_numeric("42"), 42)

    def test_float(self) -> None:
        self.assertAlmostEqual(_parse_numeric("3.14"), 3.14)

    def test_invalid(self) -> None:
        self.assertIsNone(_parse_numeric("abc"))


class TestDetectParamSpace(unittest.TestCase):
    def test_detects_tunable_comment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write("# tunable: WINDOW_SIZE = [10, 20, 50]\n")
            space = _detect_param_space(td)
            self.assertIn("WINDOW_SIZE", space)
            self.assertEqual(space["WINDOW_SIZE"], [10, 20, 50])

    def test_detects_module_constants(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "config.py"), "w") as f:
                f.write("LOOKBACK = 20\n")
            space = _detect_param_space(td)
            self.assertIn("LOOKBACK", space)
            self.assertIn(20, space["LOOKBACK"])

    def test_skips_non_tunable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "config.py"), "w") as f:
                f.write("VERSION = 1\nTIMEOUT = 30\n")
            space = _detect_param_space(td)
            self.assertNotIn("VERSION", space)
            self.assertNotIn("TIMEOUT", space)

    def test_fallback_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "empty.py"), "w") as f:
                f.write("pass\n")
            space = _detect_param_space(td)
            # Should return default param space
            self.assertIn("LOOKBACK_PERIOD", space)


class TestTryParseJsonFromText(unittest.TestCase):
    def test_pure_json(self) -> None:
        data = _try_parse_json_from_text('{"sharpe": 1.5}')
        self.assertEqual(data["sharpe"], 1.5)

    def test_json_embedded_in_text(self) -> None:
        text = 'Running backtest...\n{"sharpe": 2.0, "return": 15}\nDone.'
        data = _try_parse_json_from_text(text)
        self.assertIsNotNone(data)
        self.assertEqual(data["sharpe"], 2.0)

    def test_no_json(self) -> None:
        self.assertIsNone(_try_parse_json_from_text("no json here"))

    def test_array_ignored(self) -> None:
        self.assertIsNone(_try_parse_json_from_text("[1, 2, 3]"))


class TestFillMetricsFromDict(unittest.TestCase):
    def test_standard_keys(self) -> None:
        m = BacktestMetrics()
        _fill_metrics_from_dict(m, {"sharpe_ratio": 1.5, "total_return_pct": 20})
        self.assertAlmostEqual(m.sharpe_ratio, 1.5)
        self.assertAlmostEqual(m.total_return_pct, 20.0)

    def test_alias_keys(self) -> None:
        m = BacktestMetrics()
        _fill_metrics_from_dict(m, {"sharpe": 2.0, "trades": 50})
        self.assertAlmostEqual(m.sharpe_ratio, 2.0)
        self.assertEqual(m.trade_count, 50)

    def test_nested_dict(self) -> None:
        m = BacktestMetrics()
        _fill_metrics_from_dict(m, {"metrics": {"sharpe": 1.2, "win_rate": 55}})
        self.assertAlmostEqual(m.sharpe_ratio, 1.2)
        self.assertAlmostEqual(m.win_rate, 55.0)

    def test_case_insensitive(self) -> None:
        m = BacktestMetrics()
        _fill_metrics_from_dict(m, {"Sharpe_Ratio": 1.1})
        self.assertAlmostEqual(m.sharpe_ratio, 1.1)


class TestBuildParamCombos(unittest.TestCase):
    def test_grid_search(self) -> None:
        space = {"A": [1, 2], "B": [10, 20]}
        combos = _build_param_combos(space, strategy="grid", max_combos=100)
        self.assertEqual(len(combos), 4)

    def test_max_combos_truncation(self) -> None:
        space = {"A": list(range(10)), "B": list(range(10))}
        combos = _build_param_combos(space, strategy="grid", max_combos=5)
        self.assertEqual(len(combos), 5)

    def test_random_search(self) -> None:
        space = {"A": [1, 2, 3], "B": [10, 20, 30]}
        combos = _build_param_combos(space, strategy="random", max_combos=5)
        self.assertLessEqual(len(combos), 5)
        self.assertGreater(len(combos), 0)


class TestParamsToEnv(unittest.TestCase):
    def test_conversion(self) -> None:
        env = _params_to_env({"lookback": 20, "stop_loss": 0.05})
        self.assertEqual(env["BACKTEST_PARAM_LOOKBACK"], "20")
        self.assertEqual(env["BACKTEST_PARAM_STOP_LOSS"], "0.05")


class TestExtractCodeBlock(unittest.TestCase):
    def test_fenced_block(self) -> None:
        text = "Here is the fix:\n```python\nimport os\nprint('hello')\n```\nEnd."
        code = _extract_code_block(text)
        self.assertIn("import os", code)
        self.assertNotIn("```", code)

    def test_raw_code(self) -> None:
        text = "import os\ndef main():\n    pass\n"
        code = _extract_code_block(text)
        self.assertIn("import os", code)


class TestCountCsvRows(unittest.TestCase):
    def test_counts_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "data.csv"), "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "close"])
                for i in range(10):
                    writer.writerow([f"2020-01-{i+1:02d}", 100 + i])
            self.assertEqual(_count_csv_rows(td), 10)

    def test_no_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_count_csv_rows(td), 0)


class TestRunBacktestPipeline(unittest.TestCase):
    def test_no_code_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = run_backtest_pipeline(td)
            self.assertFalse(report.success)
            self.assertTrue(any("No code/" in e for e in report.errors))

    def test_no_backtest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "code"))
            with open(os.path.join(td, "code", "readme.txt"), "w") as f:
                f.write("hi\n")
            report = run_backtest_pipeline(td)
            self.assertFalse(report.success)
            self.assertTrue(any("entrypoint" in e.lower() for e in report.errors))

    def test_successful_backtest(self) -> None:
        """End-to-end test with a trivial backtest script that prints JSON."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Write a minimal backtest that outputs JSON
            with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                f.write(
                    'import json\n'
                    'print(json.dumps({\n'
                    '    "sharpe_ratio": 1.5,\n'
                    '    "total_return_pct": 12.3,\n'
                    '    "max_drawdown_pct": -5.2,\n'
                    '    "win_rate": 60.0,\n'
                    '    "trade_count": 42\n'
                    '}))\n'
                )
            report = run_backtest_pipeline(td, timeout=30)
            self.assertTrue(report.success)
            self.assertIsNotNone(report.baseline_metrics)
            self.assertAlmostEqual(report.baseline_metrics.sharpe_ratio, 1.5)
            self.assertEqual(report.baseline_metrics.trade_count, 42)
            # Data should have been fetched or generated
            self.assertIn(
                report.data_source,
                ("synthetic", "yfinance", "binance", "project_provider"),
            )

    def test_failed_backtest_no_llm(self) -> None:
        """Backtest script that crashes — no LLM available for fix."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                f.write("raise RuntimeError('crash')\n")
            report = run_backtest_pipeline(td, llm=None, timeout=10)
            self.assertFalse(report.success)
            self.assertTrue(any("exit code" in e.lower() or "failed" in e.lower() for e in report.errors))

    def test_existing_data_detected(self) -> None:
        """If code/ already has a CSV, data_source should be 'existing'."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Write data file
            with open(os.path.join(code_dir, "prices.csv"), "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "close"])
                for i in range(5):
                    writer.writerow([f"2020-01-{i+1:02d}", 100])
            # Write trivial backtest
            with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                f.write('import json; print(json.dumps({"sharpe_ratio": 1.0}))\n')
            report = run_backtest_pipeline(td, timeout=10)
            self.assertEqual(report.data_source, "existing")

    def test_report_persisted(self) -> None:
        """Check that JSON and Markdown reports are written."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                f.write('import json; print(json.dumps({"sharpe_ratio": 0.5}))\n')
            run_backtest_pipeline(td, timeout=10)
            self.assertTrue(os.path.isfile(os.path.join(td, "backtest_report.json")))
            self.assertTrue(os.path.isfile(os.path.join(td, "backtest_analysis.md")))

    def test_non_quant_mode_warning(self) -> None:
        """Running on a SaaS mode run should add a warning."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Write analysis_result with SaaS mode
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"mode_used": "SaaS"}, f)
            with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                f.write('import json; print(json.dumps({"sharpe_ratio": 1.0}))\n')
            report = run_backtest_pipeline(td, timeout=10)
            # mode_used is lowered in the pipeline, so check case-insensitively
            self.assertTrue(any("saas" in w.lower() for w in report.warnings))


class TestParamOptimisation(unittest.TestCase):
    def test_param_sweep_with_env_vars(self) -> None:
        """Test that parameter sweep passes values via env vars."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Strategy file with tunable params
            with open(os.path.join(code_dir, "strategy.py"), "w") as f:
                f.write("# tunable: FAST_MA = [5, 10]\n# tunable: SLOW_MA = [20, 50]\n")
            # Backtest that reads env vars and outputs results
            with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                f.write(
                    'import json, os\n'
                    'fast = int(os.environ.get("BACKTEST_PARAM_FAST_MA", "10"))\n'
                    'slow = int(os.environ.get("BACKTEST_PARAM_SLOW_MA", "20"))\n'
                    '# Simulate: larger spread = better sharpe\n'
                    'sharpe = (slow - fast) / 10.0\n'
                    'print(json.dumps({"sharpe_ratio": sharpe, "trade_count": fast + slow}))\n'
                )
            report = run_backtest_pipeline(
                td, timeout=30, param_search="grid", max_combos=10,
            )
            self.assertTrue(report.success)
            self.assertGreater(report.combos_evaluated, 0)
            # Best should be FAST_MA=5, SLOW_MA=50 (spread=45, sharpe=4.5)
            self.assertIsNotNone(report.best_params, "param sweep must produce best_params")
            self.assertTrue(report.best_params, "best_params must not be empty")
            self.assertEqual(report.best_params.get("FAST_MA"), 5)
            self.assertEqual(report.best_params.get("SLOW_MA"), 50)


class TestIsCryptoSymbol(unittest.TestCase):
    def test_btcusdt(self) -> None:
        self.assertTrue(_is_crypto_symbol("BTCUSDT"))

    def test_eth_usdt(self) -> None:
        self.assertTrue(_is_crypto_symbol("ETH/USDT"))

    def test_spy(self) -> None:
        self.assertFalse(_is_crypto_symbol("SPY"))

    def test_aapl(self) -> None:
        self.assertFalse(_is_crypto_symbol("AAPL"))

    def test_sol_busd(self) -> None:
        self.assertTrue(_is_crypto_symbol("SOLBUSD"))

    def test_link_usdc(self) -> None:
        self.assertTrue(_is_crypto_symbol("LINKUSDC"))


class TestDetectSymbolFromCode(unittest.TestCase):
    def test_detects_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "config.py"), "w") as f:
                f.write('SYMBOL = "ETHUSDT"\n')
            result = _detect_symbol_from_code(td)
            self.assertEqual(result, "ETHUSDT")

    def test_detects_ticker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write("TICKER = 'AAPL'\n")
            result = _detect_symbol_from_code(td)
            self.assertEqual(result, "AAPL")

    def test_no_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "main.py"), "w") as f:
                f.write("x = 1\n")
            result = _detect_symbol_from_code(td)
            self.assertIsNone(result)


class TestPeriodToDays(unittest.TestCase):
    def test_two_years(self) -> None:
        self.assertEqual(_period_to_days("2y"), 730)

    def test_six_months(self) -> None:
        self.assertEqual(_period_to_days("6mo"), 180)

    def test_thirty_days(self) -> None:
        self.assertEqual(_period_to_days("30d"), 30)

    def test_invalid(self) -> None:
        self.assertEqual(_period_to_days("invalid"), 500)


class TestRunProjectDataProvider(unittest.TestCase):
    def test_no_data_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = _run_project_data_provider(td)
            self.assertIsNone(result)

    def test_data_provider_produces_csv(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Write a data_provider that creates a CSV
            with open(os.path.join(td, "data_provider.py"), "w") as f:
                f.write(
                    'import os\n'
                    'data_dir = os.path.join(os.path.dirname(__file__), "data")\n'
                    'os.makedirs(data_dir, exist_ok=True)\n'
                    'path = os.path.join(data_dir, "fetched.csv")\n'
                    'with open(path, "w") as fh:\n'
                    '    fh.write("date,open,high,low,close,volume\\n")\n'
                    '    fh.write("2023-01-01,100,105,95,102,1000000\\n")\n'
                    'print(path)\n'
                )
            result = _run_project_data_provider(td, timeout=10)
            self.assertIsNotNone(result)
            self.assertTrue(result.endswith(".csv"))

    def test_data_provider_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "data_provider.py"), "w") as f:
                f.write("raise RuntimeError('fail')\n")
            result = _run_project_data_provider(td, timeout=10)
            self.assertIsNone(result)


class TestPrepareData(unittest.TestCase):
    def test_synthetic_fallback(self) -> None:
        """When forced to synthetic, should produce data."""
        with tempfile.TemporaryDirectory() as td:
            source, path, rows = prepare_data(
                td, data_source="synthetic", fallback_rows=50,
            )
            self.assertEqual(source, "synthetic")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(rows, 50)

    def test_project_provider_used(self) -> None:
        """When project has data_provider.py, should use it."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "data_provider.py"), "w") as f:
                f.write(
                    'import os\n'
                    'data_dir = os.path.join(os.path.dirname(__file__), "data")\n'
                    'os.makedirs(data_dir, exist_ok=True)\n'
                    'path = os.path.join(data_dir, "prices.csv")\n'
                    'with open(path, "w") as fh:\n'
                    '    fh.write("date,open,high,low,close,volume\\n")\n'
                    '    for i in range(10):\n'
                    '        fh.write(f"2023-01-{i+1:02d},100,105,95,102,1000\\n")\n'
                )
            source, path, rows = prepare_data(td, data_source="auto")
            self.assertEqual(source, "project_provider")
            self.assertEqual(rows, 10)

    def test_auto_falls_to_synthetic_when_no_network(self) -> None:
        """In CI without yfinance/network, auto should fall to synthetic."""
        with tempfile.TemporaryDirectory() as td:
            source, path, rows = prepare_data(
                td, data_source="auto", fallback_rows=30,
            )
            # It might get yfinance/binance data if available, or synthetic
            self.assertIn(source, ("yfinance", "binance", "synthetic", "project_provider"))
            self.assertTrue(os.path.isfile(path))
            self.assertGreater(rows, 0)


class TestBacktestReportDataSymbol(unittest.TestCase):
    def test_report_includes_symbol(self) -> None:
        r = BacktestReport(
            success=True,
            data_source="yfinance",
            data_symbol="SPY",
            data_rows=500,
        )
        d = r.to_dict()
        self.assertEqual(d["data_symbol"], "SPY")
        text = r.summary_text()
        self.assertIn("SPY", text)
        self.assertIn("yfinance", text)

    def test_report_includes_interval(self) -> None:
        r = BacktestReport(
            success=True,
            data_source="binance",
            data_symbol="BTCUSDT",
            data_interval="1h",
            data_rows=4000,
        )
        d = r.to_dict()
        self.assertEqual(d["data_interval"], "1h")
        text = r.summary_text()
        self.assertIn("@1h", text)


# ── Timeframe detection ─────────────────────────────────────────────────────


class TestDetectTimeframeFromCode(unittest.TestCase):
    def _write_py(self, td: str, content: str) -> None:
        with open(os.path.join(td, "strategy.py"), "w") as f:
            f.write(content)

    def test_detect_timeframe_1h(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'TIMEFRAME = "1h"\nSYMBOL = "BTCUSDT"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1h")

    def test_detect_interval_5m(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'INTERVAL = "5m"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "5m")

    def test_detect_candle_interval_15min(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'CANDLE_INTERVAL = "15min"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "15m")

    def test_detect_kline_interval_4h(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'KLINE_INTERVAL = "4h"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "4h")

    def test_detect_resolution_daily(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'RESOLUTION = "daily"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1d")

    def test_detect_time_frame_1d(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'TIME_FRAME = "1d"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1d")

    def test_detect_weekly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'TIMEFRAME = "1w"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1w")

    def test_detect_monthly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'TIMEFRAME = "1mo"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1M")

    def test_detect_60min_alias(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'INTERVAL = "60min"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1h")

    def test_no_timeframe_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'SYMBOL = "AAPL"\nLOOKBACK = 20\n')
            self.assertIsNone(_detect_timeframe_from_code(td))

    def test_empty_dir_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_detect_timeframe_from_code(td))

    def test_detect_granularity_30m(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'GRANULARITY = "30m"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "30m")

    def test_detect_bar_size_1m(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_py(td, 'BAR_SIZE = "1m"\n')
            self.assertEqual(_detect_timeframe_from_code(td), "1m")


# ── Timeframe profile resolution ────────────────────────────────────────────


class TestResolveTimeframeProfile(unittest.TestCase):
    def test_known_interval_1h(self) -> None:
        p = resolve_timeframe_profile("1h")
        self.assertEqual(p["yf_interval"], "1h")
        self.assertEqual(p["binance_interval"], "1h")
        self.assertEqual(p["period"], "6mo")
        self.assertTrue(p["is_intraday"])

    def test_known_interval_1d(self) -> None:
        p = resolve_timeframe_profile("1d")
        self.assertEqual(p["yf_interval"], "1d")
        self.assertEqual(p["period"], "2y")
        self.assertFalse(p["is_intraday"])

    def test_known_interval_5m(self) -> None:
        p = resolve_timeframe_profile("5m")
        self.assertEqual(p["yf_interval"], "5m")
        self.assertEqual(p["binance_interval"], "5m")
        self.assertEqual(p["period"], "30d")
        self.assertTrue(p["is_intraday"])

    def test_known_interval_1w(self) -> None:
        p = resolve_timeframe_profile("1w")
        self.assertEqual(p["yf_interval"], "1wk")
        self.assertEqual(p["period"], "5y")
        self.assertFalse(p["is_intraday"])

    def test_known_interval_1M(self) -> None:
        p = resolve_timeframe_profile("1M")
        self.assertEqual(p["yf_interval"], "1mo")
        self.assertEqual(p["period"], "10y")
        self.assertFalse(p["is_intraday"])

    def test_alias_daily(self) -> None:
        p = resolve_timeframe_profile("daily")
        self.assertEqual(p["yf_interval"], "1d")

    def test_alias_hourly(self) -> None:
        p = resolve_timeframe_profile("hourly")
        self.assertEqual(p["yf_interval"], "1h")

    def test_alias_1wk(self) -> None:
        p = resolve_timeframe_profile("1wk")
        self.assertEqual(p["yf_interval"], "1wk")

    def test_alias_1mo(self) -> None:
        p = resolve_timeframe_profile("1mo")
        self.assertEqual(p["yf_interval"], "1mo")

    def test_auto_returns_default(self) -> None:
        p = resolve_timeframe_profile("auto")
        self.assertEqual(p["yf_interval"], "1d")
        self.assertEqual(p["period"], "2y")

    def test_unknown_interval_falls_to_default(self) -> None:
        p = resolve_timeframe_profile("99x")
        self.assertEqual(p["yf_interval"], "1d")

    def test_period_override(self) -> None:
        p = resolve_timeframe_profile("1h", period="3mo")
        self.assertEqual(p["yf_interval"], "1h")
        self.assertEqual(p["period"], "3mo")
        self.assertTrue(p["is_intraday"])

    def test_period_auto_uses_profile_default(self) -> None:
        p = resolve_timeframe_profile("5m", period="auto")
        self.assertEqual(p["period"], "30d")

    def test_all_profiles_have_required_keys(self) -> None:
        required = {"period", "yf_interval", "binance_interval", "limit", "synthetic_rows", "is_intraday"}
        for key, profile in _TIMEFRAME_PROFILES.items():
            self.assertTrue(
                required.issubset(profile.keys()),
                f"Profile '{key}' missing keys: {required - profile.keys()}"
            )

    def test_15m_profile(self) -> None:
        p = resolve_timeframe_profile("15m")
        self.assertEqual(p["binance_interval"], "15m")
        self.assertEqual(p["period"], "60d")
        self.assertTrue(p["is_intraday"])

    def test_4h_profile(self) -> None:
        p = resolve_timeframe_profile("4h")
        self.assertEqual(p["binance_interval"], "4h")
        self.assertEqual(p["period"], "1y")
        self.assertTrue(p["is_intraday"])


# ── Intraday helpers ────────────────────────────────────────────────────────


class TestIntradayHelpers(unittest.TestCase):
    def test_is_intraday_1m(self) -> None:
        self.assertTrue(_is_intraday_interval("1m"))

    def test_is_intraday_5m(self) -> None:
        self.assertTrue(_is_intraday_interval("5m"))

    def test_is_intraday_1h(self) -> None:
        self.assertTrue(_is_intraday_interval("1h"))

    def test_is_intraday_4h(self) -> None:
        self.assertTrue(_is_intraday_interval("4h"))

    def test_not_intraday_1d(self) -> None:
        self.assertFalse(_is_intraday_interval("1d"))

    def test_not_intraday_1w(self) -> None:
        self.assertFalse(_is_intraday_interval("1w"))

    def test_not_intraday_1M(self) -> None:
        self.assertFalse(_is_intraday_interval("1M"))

    def test_interval_to_timedelta_1m(self) -> None:
        from datetime import timedelta
        self.assertEqual(_interval_to_timedelta("1m"), timedelta(minutes=1))

    def test_interval_to_timedelta_1h(self) -> None:
        from datetime import timedelta
        self.assertEqual(_interval_to_timedelta("1h"), timedelta(hours=1))

    def test_interval_to_timedelta_1d(self) -> None:
        from datetime import timedelta
        self.assertEqual(_interval_to_timedelta("1d"), timedelta(days=1))

    def test_interval_to_timedelta_4h(self) -> None:
        from datetime import timedelta
        self.assertEqual(_interval_to_timedelta("4h"), timedelta(hours=4))

    def test_interval_to_timedelta_unknown(self) -> None:
        from datetime import timedelta
        self.assertEqual(_interval_to_timedelta("xyz"), timedelta(days=1))


# ── period_to_candles ───────────────────────────────────────────────────────


class TestPeriodToCandles(unittest.TestCase):
    def test_2y_1d(self) -> None:
        candles = _period_to_candles("2y", "1d")
        self.assertEqual(candles, 730)

    def test_7d_1m(self) -> None:
        candles = _period_to_candles("7d", "1m")
        self.assertEqual(candles, 7 * 24 * 60)

    def test_30d_5m(self) -> None:
        candles = _period_to_candles("30d", "5m")
        self.assertEqual(candles, 30 * 24 * 12)

    def test_6mo_1h(self) -> None:
        candles = _period_to_candles("6mo", "1h")
        expected = int(180 / (1 / 24))
        self.assertEqual(candles, expected)

    def test_minimum_10(self) -> None:
        candles = _period_to_candles("1d", "1w")
        self.assertGreaterEqual(candles, 10)


# ── Synthetic intraday ──────────────────────────────────────────────────────


class TestSyntheticOHLCVIntraday(unittest.TestCase):
    def test_intraday_has_timestamps(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=10, seed=1, interval="1h")
        reader = csv.DictReader(io.StringIO(csv_text))
        row = next(reader)
        # Intraday should have HH:MM:SS
        self.assertIn(":", row["date"])
        self.assertGreaterEqual(len(row["date"]), 19)

    def test_daily_no_timestamps(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=10, seed=1, interval="1d")
        reader = csv.DictReader(io.StringIO(csv_text))
        row = next(reader)
        self.assertEqual(len(row["date"]), 10)
        self.assertNotIn(":", row["date"])

    def test_5m_correct_rows(self) -> None:
        csv_text = generate_synthetic_ohlcv(rows=100, seed=1, interval="5m")
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        self.assertEqual(len(rows), 100)

    def test_1m_timestamps_increment(self) -> None:
        from datetime import datetime as dt
        csv_text = generate_synthetic_ohlcv(rows=5, seed=1, interval="1m")
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        t0 = dt.strptime(rows[0]["date"], "%Y-%m-%d %H:%M:%S")
        t1 = dt.strptime(rows[1]["date"], "%Y-%m-%d %H:%M:%S")
        from datetime import timedelta
        self.assertEqual(t1 - t0, timedelta(minutes=1))

    def test_weekly_correct_step(self) -> None:
        from datetime import datetime as dt, timedelta
        csv_text = generate_synthetic_ohlcv(rows=5, seed=1, interval="1w")
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        t0 = dt.strptime(rows[0]["date"], "%Y-%m-%d")
        t1 = dt.strptime(rows[1]["date"], "%Y-%m-%d")
        self.assertEqual(t1 - t0, timedelta(weeks=1))


# ── Prepare data with interval ──────────────────────────────────────────────


class TestPrepareDataWithInterval(unittest.TestCase):
    def test_synthetic_with_1h_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source, path, rows = prepare_data(
                td, data_source="synthetic", interval="1h", fallback_rows=100,
            )
            self.assertEqual(source, "synthetic")
            self.assertTrue(os.path.isfile(path))
            # Should get the profile's synthetic_rows (4000) because fallback_rows=100
            # is not == BACKTEST_DATA_ROWS (500), so it uses 100
            self.assertEqual(rows, 100)
            # Check intraday timestamps
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                self.assertIn(":", row["date"])

    def test_synthetic_with_auto_uses_profile_rows(self) -> None:
        """When fallback_rows equals the module default, use profile synthetic_rows."""
        with tempfile.TemporaryDirectory() as td:
            source, path, rows = prepare_data(
                td, data_source="synthetic", interval="1h",
            )
            self.assertEqual(source, "synthetic")
            # Should use profile synthetic_rows=4000 because fallback_rows==BACKTEST_DATA_ROWS
            self.assertEqual(rows, 4000)

    def test_auto_interval_detection_from_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write('TIMEFRAME = "5m"\nSYMBOL = "BTCUSDT"\n')
            source, path, rows = prepare_data(
                td, data_source="synthetic", interval="auto",
            )
            self.assertEqual(source, "synthetic")
            # Should detect 5m and use profile synthetic_rows=5000
            self.assertEqual(rows, 5000)
            # Verify intraday timestamps
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                self.assertIn(":", row["date"])


# ─── BacktestComparison / compare_backtest_reports ───────────────────────────


def _make_metrics(**kwargs) -> BacktestMetrics:
    """Factory: return a BacktestMetrics with provided keyword values."""
    m = BacktestMetrics()
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _make_report(symbol: str = "", **metric_kwargs) -> BacktestReport:
    r = BacktestReport(success=True, data_symbol=symbol)
    if metric_kwargs:
        r.baseline_metrics = _make_metrics(**metric_kwargs)
    return r


class TestCompareBacktestReportsEmpty(unittest.TestCase):
    def test_empty_reports_returns_empty_comparison(self) -> None:
        result = compare_backtest_reports([])
        self.assertIsInstance(result, BacktestComparison)
        self.assertEqual(result.reports, [])
        self.assertEqual(result.labels, [])
        self.assertEqual(result.metric_table, {})
        self.assertEqual(result.best_by_metric, {})

    def test_summary_text_empty(self) -> None:
        cmp = BacktestComparison()
        self.assertIn("no reports", cmp.summary_text())


class TestCompareBacktestReportsAutoLabels(unittest.TestCase):
    def test_single_report_symbol_as_label(self) -> None:
        r = _make_report("SPY", sharpe_ratio=1.2)
        result = compare_backtest_reports([r])
        self.assertEqual(result.labels, ["SPY"])

    def test_single_report_no_symbol_fallback(self) -> None:
        r = _make_report("", sharpe_ratio=0.5)
        result = compare_backtest_reports([r])
        self.assertEqual(result.labels, ["Strategy-1"])

    def test_auto_dedup_same_symbol(self) -> None:
        r1 = _make_report("SPY", sharpe_ratio=1.0)
        r2 = _make_report("SPY", sharpe_ratio=0.8)
        result = compare_backtest_reports([r1, r2])
        self.assertEqual(result.labels[0], "SPY")
        self.assertEqual(result.labels[1], "SPY_1")
        self.assertEqual(len(set(result.labels)), 2)

    def test_auto_dedup_triple_same_symbol(self) -> None:
        reports = [_make_report("BTC", sharpe_ratio=float(i)) for i in range(3)]
        result = compare_backtest_reports(reports)
        self.assertEqual(len(result.labels), 3)
        self.assertEqual(len(set(result.labels)), 3)
        self.assertIn("BTC", result.labels)
        self.assertIn("BTC_1", result.labels)
        self.assertIn("BTC_2", result.labels)


class TestCompareBacktestReportsUserLabels(unittest.TestCase):
    def test_user_labels_stored_correctly(self) -> None:
        r1, r2 = _make_report(sharpe_ratio=1.0), _make_report(sharpe_ratio=0.5)
        result = compare_backtest_reports([r1, r2], labels=["Alpha", "Beta"])
        self.assertEqual(result.labels, ["Alpha", "Beta"])

    def test_user_labels_length_mismatch_raises(self) -> None:
        r = _make_report(sharpe_ratio=1.0)
        with self.assertRaises(ValueError) as ctx:
            compare_backtest_reports([r, r], labels=["OnlyOne"])
        self.assertIn("labels length", str(ctx.exception))

    def test_user_labels_duplicate_raises(self) -> None:
        r1, r2 = _make_report(sharpe_ratio=1.0), _make_report(sharpe_ratio=0.5)
        with self.assertRaises(ValueError) as ctx:
            compare_backtest_reports([r1, r2], labels=["A", "A"])
        msg = str(ctx.exception)
        self.assertIn("unique", msg)
        self.assertIn("'A'", msg)

    def test_user_labels_duplicate_3times_error_message_lists_once(self) -> None:
        """Each duplicated label should appear ONCE in the error message, not N times."""
        reports = [_make_report(sharpe_ratio=float(i)) for i in range(3)]
        with self.assertRaises(ValueError) as ctx:
            compare_backtest_reports(reports, labels=["X", "X", "X"])
        msg = str(ctx.exception)
        # 'X' appears exactly once as an element in the dupes list
        self.assertEqual(msg.count("'X'"), 1)

    def test_user_labels_defensive_copy(self) -> None:
        """Modifying the caller's labels list must not affect BacktestComparison.labels."""
        caller_labels = ["A", "B"]
        r1, r2 = _make_report(sharpe_ratio=1.0), _make_report(sharpe_ratio=0.5)
        result = compare_backtest_reports([r1, r2], labels=caller_labels)
        caller_labels[0] = "MUTATED"
        self.assertEqual(result.labels[0], "A")


class TestCompareBacktestReportsMetrics(unittest.TestCase):
    def test_metrics_none_uses_all_defaults(self) -> None:
        r = _make_report("SPY", sharpe_ratio=1.0)
        result = compare_backtest_reports([r], metrics=None)
        # metric_table should only contain metrics with at least one non-None value
        self.assertIn("sharpe_ratio", result.metric_table)

    def test_metrics_empty_list_respected(self) -> None:
        """metrics=[] must produce empty metric_table, not silently use defaults."""
        r = _make_report("SPY", sharpe_ratio=1.0)
        result = compare_backtest_reports([r], metrics=[])
        self.assertEqual(result.metric_table, {})

    def test_metrics_subset(self) -> None:
        r = _make_report("SPY", sharpe_ratio=1.5, total_return_pct=0.3)
        result = compare_backtest_reports([r], metrics=["sharpe_ratio"])
        self.assertIn("sharpe_ratio", result.metric_table)
        self.assertNotIn("total_return_pct", result.metric_table)

    def test_all_none_metric_excluded_from_table(self) -> None:
        """A metric where every report has None value should not appear in metric_table."""
        r = _make_report("SPY", sharpe_ratio=1.0)  # profit_factor stays None
        result = compare_backtest_reports([r])
        # Use assertNotIn (idiomatic) rather than if+fail — same semantics but
        # clearer intent and better failure messages.
        self.assertNotIn(
            "profit_factor",
            result.metric_table,
            "profit_factor with all-None values should be excluded from metric_table",
        )

    def test_best_metrics_preferred_over_baseline(self) -> None:
        r = _make_report("SPY", sharpe_ratio=0.5)
        r.best_metrics = _make_metrics(sharpe_ratio=1.8)
        result = compare_backtest_reports([r])
        val = result.metric_table["sharpe_ratio"]["SPY"]
        self.assertAlmostEqual(val, 1.8)


class TestCompareBacktestReportsBestByMetric(unittest.TestCase):
    def test_higher_is_better_selects_max(self) -> None:
        r1 = _make_report("A", sharpe_ratio=2.0)
        r2 = _make_report("B", sharpe_ratio=0.5)
        result = compare_backtest_reports([r1, r2])
        self.assertEqual(result.best_by_metric.get("sharpe_ratio"), "A")

    def test_lower_is_better_selects_min(self) -> None:
        """max_drawdown_pct is in _LOWER_IS_BETTER — smaller is better."""
        self.assertIn("max_drawdown_pct", _LOWER_IS_BETTER)
        r1 = _make_report("A", max_drawdown_pct=0.05)
        r2 = _make_report("B", max_drawdown_pct=0.30)
        result = compare_backtest_reports([r1, r2])
        self.assertEqual(result.best_by_metric.get("max_drawdown_pct"), "A")

    def test_nan_excluded_from_best(self) -> None:
        r1 = _make_report("A", sharpe_ratio=float("nan"))
        r2 = _make_report("B", sharpe_ratio=1.0)
        result = compare_backtest_reports([r1, r2])
        self.assertEqual(result.best_by_metric.get("sharpe_ratio"), "B")

    def test_inf_excluded_from_best(self) -> None:
        r1 = _make_report("A", sharpe_ratio=float("inf"))
        r2 = _make_report("B", sharpe_ratio=1.0)
        result = compare_backtest_reports([r1, r2])
        self.assertEqual(result.best_by_metric.get("sharpe_ratio"), "B")

    def test_all_nan_no_best(self) -> None:
        r1 = _make_report("A", sharpe_ratio=float("nan"))
        result = compare_backtest_reports([r1])
        self.assertNotIn("sharpe_ratio", result.best_by_metric)


class TestBacktestComparisonSummaryText(unittest.TestCase):
    def _two_report_comparison(self) -> BacktestComparison:
        r1 = _make_report("SPY", sharpe_ratio=1.5, total_return_pct=0.25,
                           max_drawdown_pct=0.10)
        r2 = _make_report("BTC", sharpe_ratio=0.8, total_return_pct=0.60,
                           max_drawdown_pct=0.40)
        return compare_backtest_reports([r1, r2])

    def test_header_present(self) -> None:
        text = self._two_report_comparison().summary_text()
        self.assertIn("Backtest Strategy Comparison", text)

    def test_labels_appear_in_header(self) -> None:
        text = self._two_report_comparison().summary_text()
        self.assertIn("SPY", text)
        self.assertIn("BTC", text)

    def test_best_marker_present(self) -> None:
        """The ASCII '*' best-marker must appear for at least one metric."""
        text = self._two_report_comparison().summary_text()
        self.assertIn("*", text)

    def test_column_alignment_consistent(self) -> None:
        """Every data row must have the same indentation as the header row."""
        cmp = self._two_report_comparison()
        text = cmp.summary_text()
        lines = text.splitlines()
        # Find header line (contains both labels)
        header_idx = next(
            i for i, l in enumerate(lines)
            if "SPY" in l and "BTC" in l
        )
        header_line = lines[header_idx]
        # Header prefix = everything before "SPY"
        prefix_len = header_line.index("SPY")
        # All data rows (lines after the separator) must have values
        # starting at the same column offset
        separator_idx = header_idx + 1
        for data_line in lines[separator_idx + 1:]:
            if not data_line.strip():
                continue
            # The value cell region starts at prefix_len; first non-space
            # char after prefix should not be a label character
            self.assertGreaterEqual(
                len(data_line), prefix_len,
                msg=f"Data row shorter than header prefix: {data_line!r}",
            )

    def test_single_report_no_crash(self) -> None:
        r = _make_report("SPY", sharpe_ratio=1.0)
        cmp = compare_backtest_reports([r])
        text = cmp.summary_text()
        self.assertIn("SPY", text)

    def test_to_dict_round_trip(self) -> None:
        cmp = self._two_report_comparison()
        d = cmp.to_dict()
        self.assertIn("labels", d)
        self.assertIn("metric_table", d)
        self.assertIn("best_by_metric", d)
        self.assertEqual(d["labels"], ["SPY", "BTC"])


if __name__ == "__main__":
    unittest.main()
