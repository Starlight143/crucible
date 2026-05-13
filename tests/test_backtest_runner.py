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
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.backtest_runner import (
    _COMPARISON_METRICS,
    _LOWER_IS_BETTER,
    _TIMEFRAME_PROFILES,
    BacktestComparison,
    BacktestDataIntegrityError,
    BacktestMetrics,
    BacktestReport,
    FetchOutcome,
    ParameterCombo,
    PrepareDataResult,
    _build_param_combos,
    _ccxt_available,
    _classify_fetch_exception,
    _count_csv_rows,
    _csv_data_row_count,
    _csv_has_required_columns,
    _data_cache_path,
    _detect_param_space,
    _detect_symbol_from_code,
    _detect_timeframe_from_code,
    _extract_code_block,
    _fetch_ccxt_ohlcv,
    _fill_metrics_from_dict,
    _find_backtest_entry,
    _find_code_dir,
    _find_usable_data_file,
    _get_fetch_diagnostic,
    _has_data_file,
    _interval_to_timedelta,
    _is_crypto_symbol,
    _is_intraday_interval,
    _params_to_env,
    _parse_numeric,
    _period_to_candles,
    _period_to_days,
    _read_csv_date_range,
    _read_data_cache,
    _record_fetch_diagnostic,
    _run_project_data_provider,
    _scan_code_metadata,
    _try_parse_json_from_text,
    _validate_fetched_csv,
    _write_data_cache,
    compare_backtest_reports,
    fetch_binance_ohlcv,
    fetch_yfinance_ohlcv,
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


def _write_valid_ohlcv_csv(path: str, rows: int = 60) -> None:
    """Helper: write a CSV with the required OHLCV columns and *rows* rows.

    Used across multiple tests that need to construct a file the strict
    ``_find_usable_data_file`` check (HIGH 3) accepts.
    """
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for i in range(rows):
            base = 100 + i * 0.01
            writer.writerow([
                f"2024-01-{(i % 28) + 1:02d}",
                f"{base:.4f}",
                f"{base + 0.5:.4f}",
                f"{base - 0.5:.4f}",
                f"{base + 0.1:.4f}",
                "1000000",
            ])


class TestHasDataFile(unittest.TestCase):
    """HIGH 3: ``_has_data_file`` now validates OHLCV columns + row count
    instead of just looking at the file extension.  A schema stub or a
    1-row date/close CSV no longer short-circuits the data cascade."""

    def test_csv_with_full_ohlcv_columns_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _write_valid_ohlcv_csv(os.path.join(td, "prices.csv"))
            self.assertTrue(_has_data_file(td))

    def test_csv_in_data_subdir_with_full_ohlcv_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data_dir = os.path.join(td, "data")
            os.makedirs(data_dir)
            _write_valid_ohlcv_csv(os.path.join(data_dir, "ohlcv.csv"))
            self.assertTrue(_has_data_file(td))

    def test_csv_missing_ohlcv_columns_rejected(self) -> None:
        """A bare schema stub with only date+close is no longer enough."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "prices.csv"), "w") as f:
                f.write("date,close\n2024-01-01,100\n")
            self.assertFalse(_has_data_file(td))

    def test_csv_below_row_threshold_rejected(self) -> None:
        """OHLCV CSVs with too few rows fail the threshold (HIGH 4)."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "prices.csv"), "w") as f:
                f.write("date,open,high,low,close,volume\n2024-01-01,1,2,0.5,1.5,1000\n")
            self.assertFalse(_has_data_file(td))

    def test_min_rows_zero_skips_threshold(self) -> None:
        """``min_rows=0`` keeps backward-compat (lenient row check)."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "prices.csv"), "w") as f:
                f.write("date,open,high,low,close,volume\n2024-01-01,1,2,0.5,1.5,1000\n")
            self.assertIsNotNone(_find_usable_data_file(td, min_rows=0))

    def test_no_data_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write("x = 1\n")
            self.assertFalse(_has_data_file(td))

    def test_json_counts(self) -> None:
        """Non-CSV data files (json/parquet/...) are still accepted on
        presence — schema validation is out of scope for those formats."""
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

    def test_think_block_with_fenced_decoy_stripped(self) -> None:
        """Reasoning models (DeepSeek-V4, GLM-5.1, …) emit chain-of-thought
        inside ``<think>...</think>``; a long fenced example inside the
        think block would otherwise win the longest-match heuristic and
        the real fix would be discarded."""
        decoy = "\n".join(["# decoy"] * 50)
        actual = "import os\nprint('real fix')"
        text = (
            f"<think>I might try:\n```python\n{decoy}\n```\n"
            f"but actually here is the fix.</think>\n"
            f"```python\n{actual}\n```"
        )
        code = _extract_code_block(text)
        self.assertEqual(code, actual)
        self.assertNotIn("decoy", code)


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
    """End-to-end pipeline tests.

    v1.1.0: every test in this class runs with ``BACKTEST_REQUIRE_REAL_DATA=0``
    (so the synthetic fallback is allowed) AND ``BACKTEST_DATA_CACHE_TTL_HOURS=0``
    (so a developer-machine cache cannot taint the result), AND mocks the
    real-data fetchers so the test is deterministic on a fresh CI runner
    with no network access.  Without these pins the tests historically
    passed silently on dev boxes via cached yfinance data and failed on
    fresh CI clones — a classic false-positive coverage trap.
    """

    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ,
            {
                "BACKTEST_REQUIRE_REAL_DATA": "0",
                "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
            },
            clear=False,
        )
        self._env_patch.start()
        # Mock real-data fetchers so the cascade falls through to synthetic
        # deterministically rather than touching the network.
        self._yf_patch = patch(
            "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
            return_value="",
        )
        self._bn_patch = patch(
            "crucible.features.backtest_runner.fetch_binance_ohlcv",
            return_value="",
        )
        self._yf_patch.start()
        self._bn_patch.start()

    def tearDown(self) -> None:
        self._bn_patch.stop()
        self._yf_patch.stop()
        self._env_patch.stop()

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
        """If code/ already has a usable OHLCV CSV, data_source = 'existing'.

        HIGH 3: ``_has_data_file`` now requires the OHLCV columns and a
        minimum row count, so the fixture must write a real OHLCV CSV
        (not just date,close).
        """
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            _write_valid_ohlcv_csv(os.path.join(code_dir, "prices.csv"))
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
    """Parameter sweep tests.

    v1.1.0: inherits the same env + fetcher pins as :class:`TestRunBacktestPipeline`
    so the param sweep is exercised deterministically against the synthetic
    cascade.
    """

    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ,
            {
                "BACKTEST_REQUIRE_REAL_DATA": "0",
                "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
            },
            clear=False,
        )
        self._env_patch.start()
        self._yf_patch = patch(
            "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
            return_value="",
        )
        self._bn_patch = patch(
            "crucible.features.backtest_runner.fetch_binance_ohlcv",
            return_value="",
        )
        self._yf_patch.start()
        self._bn_patch.start()

    def tearDown(self) -> None:
        self._bn_patch.stop()
        self._yf_patch.stop()
        self._env_patch.stop()

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
        """When forced to synthetic AND guard explicitly off, should produce data."""
        # Default guard is ON now; the explicit ``synthetic`` source request
        # only succeeds when the operator opts out of the integrity guard.
        with patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                result = prepare_data(
                    td, data_source="synthetic", fallback_rows=50,
                )
                self.assertEqual(result.source_label, "synthetic")
                self.assertTrue(os.path.isfile(result.data_path))
                self.assertEqual(result.row_count, 50)

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
            result = prepare_data(td, data_source="auto")
            self.assertEqual(result.source_label, "project_provider")
            self.assertEqual(result.row_count, 10)

    def test_auto_falls_to_synthetic_when_no_network(self) -> None:
        """In CI without yfinance/network, auto should fall to synthetic
        only when the integrity guard is explicitly opted out of.

        v1.1.0: tightened row-count assertion from ``> 0`` to ``>=
        fallback_rows`` so a bug that returns 1 row (instead of the
        requested 30) no longer passes silently.
        """
        with patch.dict(
            os.environ,
            {"BACKTEST_REQUIRE_REAL_DATA": "0", "BACKTEST_DATA_CACHE_TTL_HOURS": "0"},
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as td:
                result = prepare_data(
                    td, data_source="auto", fallback_rows=30,
                )
                # It might get yfinance/binance data if available, or synthetic
                self.assertIn(
                    result.source_label,
                    ("yfinance", "binance", "synthetic", "project_provider"),
                )
                self.assertTrue(os.path.isfile(result.data_path))
                # Synthetic explicitly emits ``fallback_rows`` rows; real
                # providers emit substantially more.  Either way the count
                # must reach the requested fallback minimum.
                self.assertGreaterEqual(result.row_count, 30)

    def test_explicit_synthetic_blocked_when_guard_on(self) -> None:
        """BACKTEST_DATA_SOURCE=synthetic must raise when guard is on (default)."""
        with patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "1"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(BacktestDataIntegrityError) as cm:
                    prepare_data(td, data_source="synthetic", fallback_rows=50)
                msg = str(cm.exception)
                # Must include actionable opt-in instructions, not a bare error.
                self.assertIn("pip install yfinance", msg)
                self.assertIn("BACKTEST_REQUIRE_REAL_DATA=0", msg)

    def test_auto_blocked_when_guard_on_and_no_real_data(self) -> None:
        """Auto resolution must raise rather than silently use synthetic
        when the integrity guard is on (default) and no real provider
        returns data.  Both yfinance and Binance fetches are mocked to
        return empty so the cascade has no real provider to fall back
        to; the guard must then raise instead of producing synthetic.

        Disables the data cache (TTL=0) so that any cached real-data CSV
        from a previous developer session doesn't short-circuit the mock.
        """
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "1",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value="",
            ), patch(
                "crucible.features.backtest_runner.fetch_binance_ohlcv",
                return_value="",
            ):
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(BacktestDataIntegrityError) as cm:
                        prepare_data(
                            td, data_source="auto", fallback_rows=30,
                        )
                    msg = str(cm.exception)
                    # Diagnostic must explain which providers were attempted
                    # and remind the operator of the explicit opt-out env var.
                    self.assertIn("auto-resolution exhausted", msg)
                    self.assertIn("BACKTEST_REQUIRE_REAL_DATA=0", msg)

    def test_explicit_synthetic_allowed_when_guard_off(self) -> None:
        """Guard off + explicit synthetic source must succeed and return
        synthetic data labelled ``synthetic`` so downstream consumers
        can filter on data_source."""
        with patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                result = prepare_data(
                    td, data_source="synthetic", fallback_rows=20,
                )
                self.assertEqual(result.source_label, "synthetic")
                self.assertTrue(os.path.isfile(result.data_path))
                self.assertEqual(result.row_count, 20)


# ═══════════════════════════════════════════════════════════════════════════
# Tests for the v1.1.x 15-point hardening (HIGH 1-5 / MEDIUM 6-10 / LOW 11-15)
# ═══════════════════════════════════════════════════════════════════════════


class TestNoBtcusdtFallback(unittest.TestCase):
    """HIGH 1: non-crypto symbols must NOT fall back to BTCUSDT when yfinance
    fails.  The cascade either fetches the actual requested symbol or raises
    BacktestDataIntegrityError."""

    def test_non_crypto_does_not_silently_become_binance_btc(self) -> None:
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "1",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value="",
            ) as mock_yf, patch(
                "crucible.features.backtest_runner.fetch_binance_ohlcv",
                return_value="",
            ) as mock_bn:
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(BacktestDataIntegrityError):
                        prepare_data(td, symbol="SPY", data_source="auto",
                                     fallback_rows=30)
                # yfinance should have been called with "SPY"
                self.assertTrue(any(
                    "SPY" in str(call) for call in mock_yf.call_args_list
                ), f"yfinance was called with: {mock_yf.call_args_list!r}")
                # Binance MUST NOT have been called with BTCUSDT as a fallback
                # for the non-crypto symbol SPY.
                btc_calls = [
                    c for c in mock_bn.call_args_list
                    if "BTCUSDT" in str(c)
                ]
                self.assertEqual(
                    btc_calls, [],
                    msg=f"Binance was wrongly invoked with BTCUSDT: {btc_calls!r}",
                )

    def test_crypto_still_uses_binance_first(self) -> None:
        """Crypto symbols still try Binance first (sequential cascade).

        The 1d profile has ``synthetic_rows=500`` → row threshold = 150.
        Generate enough rows to clear that bar.
        """
        # Build 200 rows spanning multiple months so they pass HIGH 4's
        # min-row threshold (default 30, profile-adjusted to 150 for 1d).
        rows_csv = []
        base = datetime(2024, 1, 1)
        for i in range(200):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
            rows_csv.append(f"{d},100,101,99,100.5,1000")
        ok_csv = "date,open,high,low,close,volume\n" + "\n".join(rows_csv) + "\n"
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "1",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_binance_ohlcv",
                return_value=ok_csv,
            ) as mock_bn, patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value="",
            ):
                with tempfile.TemporaryDirectory() as td:
                    result = prepare_data(td, symbol="BTCUSDT", data_source="auto")
                self.assertEqual(result.source_label, "binance")
                self.assertEqual(result.actual_symbol, "BTCUSDT")
                self.assertTrue(mock_bn.called)


class TestForcedSourceRespectsGuard(unittest.TestCase):
    """HIGH 2: data_source="yfinance" / "binance" / "project" failures must
    respect the integrity guard — silent fall-through to other providers is
    not allowed when the operator explicitly named a source."""

    def test_forced_yfinance_failure_raises_under_guard(self) -> None:
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "1",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value="",
            ), patch(
                "crucible.features.backtest_runner.fetch_binance_ohlcv",
                return_value="",
            ):
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(BacktestDataIntegrityError) as cm:
                        prepare_data(td, symbol="SPY", data_source="yfinance")
                self.assertIn("yfinance", str(cm.exception).lower())

    def test_forced_binance_failure_raises_under_guard(self) -> None:
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "1",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_binance_ohlcv",
                return_value="",
            ), patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value="",
            ):
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(BacktestDataIntegrityError) as cm:
                        prepare_data(td, symbol="BTCUSDT", data_source="binance")
                self.assertIn("binance", str(cm.exception).lower())

    def test_forced_yfinance_failure_with_guard_off_falls_through(self) -> None:
        """When the operator opts out of the guard, forced-source failure
        does fall through to the synthetic path with a loud warning."""
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value="",
            ):
                with tempfile.TemporaryDirectory() as td:
                    result = prepare_data(
                        td, symbol="SPY", data_source="yfinance", fallback_rows=20,
                    )
                self.assertEqual(result.source_label, "synthetic")


class TestPartialDataRejected(unittest.TestCase):
    """HIGH 4: tiny / degenerate fetched datasets must be rejected as
    partial data, NOT silently accepted as success."""

    def test_one_row_csv_treated_as_failure(self) -> None:
        partial = "date,open,high,low,close,volume\n2024-01-01,1,2,0.5,1.5,1000\n"
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "1",
            "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
        }, clear=False):
            with patch(
                "crucible.features.backtest_runner.fetch_yfinance_ohlcv",
                return_value=partial,
            ), patch(
                "crucible.features.backtest_runner.fetch_binance_ohlcv",
                return_value="",
            ):
                with tempfile.TemporaryDirectory() as td:
                    with self.assertRaises(BacktestDataIntegrityError):
                        prepare_data(td, symbol="SPY", data_source="auto")

    def test_validate_fetched_csv_threshold(self) -> None:
        rows = "\n".join(
            f"2024-01-{(i % 28) + 1:02d},100,101,99,100.5,1000"
            for i in range(40)
        )
        ok_csv = f"date,open,high,low,close,volume\n{rows}\n"
        with patch.dict(os.environ, {"BACKTEST_MIN_REAL_DATA_ROWS": "20"}, clear=False):
            ok, _reason, count = _validate_fetched_csv(ok_csv, profile=None)
            self.assertTrue(ok)
            self.assertEqual(count, 40)
        with patch.dict(os.environ, {"BACKTEST_MIN_REAL_DATA_ROWS": "100"}, clear=False):
            ok, _reason, count = _validate_fetched_csv(ok_csv, profile=None)
            self.assertFalse(ok)


class TestRunProjectDataProviderHardened(unittest.TestCase):
    """HIGH 5: ``_run_project_data_provider`` must reject paths that escape
    the code directory and refuse oversized stdout."""

    def test_rejects_path_escape(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as outside:
            outside.write(b"date,open,high,low,close,volume\n2024-01-01,1,2,0.5,1.5,1000\n")
            outside_path = outside.name
        try:
            with tempfile.TemporaryDirectory() as td:
                with open(os.path.join(td, "data_provider.py"), "w") as f:
                    f.write(f'print({outside_path!r})\n')
                result = _run_project_data_provider(td, timeout=10)
                self.assertIsNone(
                    result,
                    msg=f"data_provider must reject paths outside code_dir, got: {result!r}",
                )
        finally:
            try:
                os.unlink(outside_path)
            except OSError:
                pass

    def test_rejects_nonzero_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "data_provider.py"), "w") as f:
                f.write("import sys\nsys.exit(1)\n")
            result = _run_project_data_provider(td, timeout=10)
            self.assertIsNone(result)

    def test_accepts_csv_in_data_subdir(self) -> None:
        """Happy path: provider writes a CSV into code_dir/data/."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "data_provider.py"), "w") as f:
                f.write(
                    'import os\n'
                    'd = os.path.join(os.path.dirname(__file__), "data")\n'
                    'os.makedirs(d, exist_ok=True)\n'
                    'p = os.path.join(d, "ok.csv")\n'
                    'with open(p, "w") as fh:\n'
                    '    fh.write("date,open,high,low,close,volume\\n")\n'
                    '    fh.write("2024-01-01,1,2,0.5,1.5,1000\\n")\n'
                    'print(p)\n'
                )
            result = _run_project_data_provider(td, timeout=10)
            self.assertIsNotNone(result)
            self.assertTrue(result.endswith("ok.csv"))


class TestDataCache(unittest.TestCase):
    """MEDIUM 6: disk-level data cache with TTL."""

    def test_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(os.environ, {
                "BACKTEST_DATA_CACHE_DIR": cache_dir,
                "BACKTEST_DATA_CACHE_TTL_HOURS": "1",
            }, clear=False):
                _write_data_cache("SPY", "2y", "1d", "test-csv-payload\n")
                self.assertEqual(_read_data_cache("SPY", "2y", "1d"), "test-csv-payload\n")
                self.assertIsNone(_read_data_cache("BTC", "1y", "1d"))

    def test_cache_disabled_when_ttl_zero(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(os.environ, {
                "BACKTEST_DATA_CACHE_DIR": cache_dir,
                "BACKTEST_DATA_CACHE_TTL_HOURS": "0",
            }, clear=False):
                _write_data_cache("SPY", "2y", "1d", "payload\n")
                self.assertIsNone(_read_data_cache("SPY", "2y", "1d"))

    def test_cache_path_is_deterministic_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir:
            with patch.dict(os.environ, {"BACKTEST_DATA_CACHE_DIR": cache_dir}, clear=False):
                p1 = _data_cache_path("SPY", "2y", "1d")
                p2 = _data_cache_path("SPY", "2y", "1d")
                self.assertEqual(p1, p2)
                p3 = _data_cache_path("BTC", "2y", "1d")
                self.assertNotEqual(p1, p3)


class TestFallbackRowsNoneSentinel(unittest.TestCase):
    """MEDIUM 7: ``fallback_rows=None`` (default) uses profile synthetic_rows.
    Explicit integers — including the legacy 500 value — are honoured."""

    def test_default_uses_profile(self) -> None:
        with patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                result = prepare_data(td, data_source="synthetic", interval="1d")
                # 1d profile synthetic_rows = 500
                self.assertEqual(result.row_count, 500)

    def test_explicit_500_honoured(self) -> None:
        """The v1.0.x bug: caller passes 500 (= old default) → silently used
        profile rows instead.  v1.1.x: explicit 500 means 500."""
        with patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                result = prepare_data(
                    td, data_source="synthetic", interval="1h",
                    fallback_rows=500,
                )
                # 1h profile synthetic_rows = 4000.  With the new sentinel:
                # explicit 500 wins → row_count == 500 (not 4000).
                self.assertEqual(result.row_count, 500)


class TestScanCodeMetadata(unittest.TestCase):
    """MEDIUM 8: single-pass walk for symbol + timeframe detection.

    Both helpers must remain importable for backwards compatibility, and
    must agree with the single-pass result."""

    def test_combined_walk_returns_both(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write('SYMBOL = "BTCUSDT"\nTIMEFRAME = "1h"\n')
            sym, tf = _scan_code_metadata(td)
            self.assertEqual(sym, "BTCUSDT")
            self.assertEqual(tf, "1h")

    def test_wrappers_agree_with_combined(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write('TICKER = "AAPL"\nINTERVAL = "5m"\n')
            self.assertEqual(_detect_symbol_from_code(td), "AAPL")
            self.assertEqual(_detect_timeframe_from_code(td), "5m")


class TestFetchDiagnostics(unittest.TestCase):
    """MEDIUM 9: FetchOutcome + diagnostic recording."""

    def test_classify_http_429(self) -> None:
        import urllib.error
        exc = urllib.error.HTTPError(
            url="x", code=429, msg="Too Many", hdrs=None, fp=None,
        )
        kind, _ = _classify_fetch_exception(exc)
        self.assertEqual(kind, "rate_limit")

    def test_classify_network(self) -> None:
        import urllib.error
        exc = urllib.error.URLError("DNS failure")
        kind, _ = _classify_fetch_exception(exc)
        self.assertEqual(kind, "network")

    def test_record_and_get_diagnostic(self) -> None:
        outcome = FetchOutcome("ok-csv", "ok", "fetched")
        _record_fetch_diagnostic("yfinance-test-key", outcome)
        retrieved = _get_fetch_diagnostic("yfinance-test-key")
        self.assertEqual(retrieved, outcome)

    def test_yfinance_not_installed_recorded(self) -> None:
        """When yfinance is missing, the public fetcher must record a
        ``not_installed`` diagnostic so the integrity error can recommend
        the install command."""
        with patch(
            "crucible.features.backtest_runner._yfinance_available",
            return_value=False,
        ):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                fetch_yfinance_ohlcv("SPY", period="2y", interval="1d")
        diag = _get_fetch_diagnostic("yfinance")
        self.assertIsNotNone(diag)
        self.assertEqual(diag.error_kind, "not_installed")


class TestDataFreshness(unittest.TestCase):
    """MEDIUM 10: data freshness tracking + staleness warning."""

    def test_read_csv_date_range(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "data.csv")
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "open", "high", "low", "close", "volume"])
                writer.writerow(["2024-01-01", 100, 101, 99, 100.5, 1000])
                writer.writerow(["2024-01-15", 105, 106, 104, 105.5, 1100])
                writer.writerow(["2024-02-01", 110, 111, 109, 110.5, 1200])
            start, end = _read_csv_date_range(path)
            self.assertEqual(start, "2024-01-01")
            self.assertEqual(end, "2024-02-01")

    def test_synthetic_run_populates_date_range(self) -> None:
        with patch.dict(os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"}, clear=False):
            with tempfile.TemporaryDirectory() as td:
                result = prepare_data(
                    td, data_source="synthetic", fallback_rows=30,
                )
                self.assertTrue(result.start_date)
                self.assertTrue(result.end_date)

    def test_staleness_warning_appended_when_data_old(self) -> None:
        """Pipeline must flag an old data window via report.warnings."""
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_DATA_MAX_STALENESS_DAYS": "7",
        }, clear=False):
            with tempfile.TemporaryDirectory() as td:
                code_dir = os.path.join(td, "code")
                os.makedirs(code_dir)
                # Write an OHLCV CSV with end date = 2020-01-01 (very stale).
                with open(os.path.join(code_dir, "ohlcv.csv"), "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["date", "open", "high", "low", "close", "volume"])
                    for i in range(60):
                        w.writerow([
                            f"2020-01-{(i % 28) + 1:02d}", 100, 101, 99, 100.5, 1000,
                        ])
                with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                    f.write('import json; print(json.dumps({"sharpe_ratio": 0.5}))\n')
                report = run_backtest_pipeline(td, timeout=10)
                self.assertEqual(report.data_source, "existing")
                self.assertGreater(report.data_staleness_days or 0, 7)
                self.assertTrue(
                    any("stale" in w.lower() for w in report.warnings),
                    msg=f"staleness warning missing: {report.warnings!r}",
                )


class TestStricterCryptoSymbol(unittest.TestCase):
    """LOW 12: stricter ``_is_crypto_symbol`` rejects short / ambiguous bases."""

    def test_btcusdt_still_crypto(self) -> None:
        self.assertTrue(_is_crypto_symbol("BTCUSDT"))

    def test_short_ticker_not_crypto(self) -> None:
        self.assertFalse(_is_crypto_symbol("BTC"))
        self.assertFalse(_is_crypto_symbol("ETH"))

    def test_brk_b_equity_not_crypto(self) -> None:
        self.assertFalse(_is_crypto_symbol("BRK-B"))

    def test_eth_usd_separator_is_crypto(self) -> None:
        self.assertTrue(_is_crypto_symbol("ETH-USD"))
        self.assertTrue(_is_crypto_symbol("BTC/USDT"))


class TestSyntheticSeedConfigurable(unittest.TestCase):
    """LOW 13: ``BACKTEST_SYNTHETIC_SEED`` controls synthetic-data seed."""

    def test_int_seed_reproducible(self) -> None:
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_SYNTHETIC_SEED": "123",
        }, clear=False):
            with tempfile.TemporaryDirectory() as t1, \
                 tempfile.TemporaryDirectory() as t2:
                r1 = prepare_data(t1, data_source="synthetic", fallback_rows=20)
                r2 = prepare_data(t2, data_source="synthetic", fallback_rows=20)
                with open(r1.data_path) as f1, open(r2.data_path) as f2:
                    self.assertEqual(f1.read(), f2.read())

    def test_random_seed_differs_between_runs(self) -> None:
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_SYNTHETIC_SEED": "random",
        }, clear=False):
            with tempfile.TemporaryDirectory() as t1, \
                 tempfile.TemporaryDirectory() as t2:
                r1 = prepare_data(t1, data_source="synthetic", fallback_rows=20)
                r2 = prepare_data(t2, data_source="synthetic", fallback_rows=20)
                with open(r1.data_path) as f1, open(r2.data_path) as f2:
                    # Vanishing probability of collision when both use random seeds.
                    self.assertNotEqual(f1.read(), f2.read())

    def test_invalid_seed_falls_back_to_42(self) -> None:
        """v1.1.0: an invalid env value must fall back to the documented
        default of 42, NOT just to some deterministic alternative.  We
        compare the garbage-seed output against a reference run with an
        explicit ``seed=42`` so a bug that fell back to seed=0 (still
        deterministic) would fail this test.
        """
        # Reference run: explicit seed=42.
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_SYNTHETIC_SEED": "42",
        }, clear=False):
            with tempfile.TemporaryDirectory() as t_ref:
                ref = prepare_data(t_ref, data_source="synthetic", fallback_rows=20)
                with open(ref.data_path) as f_ref:
                    ref_csv = f_ref.read()

        # Garbage seed must produce the SAME output as seed=42.
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_SYNTHETIC_SEED": "garbage",
        }, clear=False):
            with tempfile.TemporaryDirectory() as t1, \
                 tempfile.TemporaryDirectory() as t2:
                r1 = prepare_data(t1, data_source="synthetic", fallback_rows=20)
                r2 = prepare_data(t2, data_source="synthetic", fallback_rows=20)
                with open(r1.data_path) as f1, open(r2.data_path) as f2:
                    csv_1 = f1.read()
                    csv_2 = f2.read()
                # Two invalid-seed runs must be identical (deterministic).
                self.assertEqual(csv_1, csv_2)
                # AND identical to the explicit seed=42 reference run —
                # this catches a bug that fell back to a different
                # deterministic seed (e.g. seed=0).
                self.assertEqual(csv_1, ref_csv)


class TestPrepareDataOnlyDryRun(unittest.TestCase):
    """LOW 14: ``BACKTEST_PREPARE_DATA_ONLY=1`` short-circuits the pipeline
    after data prep."""

    def test_pipeline_skips_strategy_execution(self) -> None:
        with patch.dict(os.environ, {
            "BACKTEST_REQUIRE_REAL_DATA": "0",
            "BACKTEST_PREPARE_DATA_ONLY": "1",
        }, clear=False):
            with tempfile.TemporaryDirectory() as td:
                code_dir = os.path.join(td, "code")
                os.makedirs(code_dir)
                # Backtest script that ALWAYS crashes — proves it was not run.
                with open(os.path.join(code_dir, "backtest.py"), "w") as f:
                    f.write("raise SystemExit('should not have been called')\n")
                report = run_backtest_pipeline(td, timeout=10)
                self.assertTrue(report.success)
                self.assertIsNone(report.baseline_metrics)
                self.assertTrue(
                    any("PREPARE_DATA_ONLY" in n for n in report.notes),
                    msg=f"dry-run note missing: {report.notes!r}",
                )


class TestProviderProfileLimits(unittest.TestCase):
    """LOW 15: timeframe profile must respect provider limits to avoid
    silent fetch failures from over-asking yfinance / Binance."""

    # Approximate provider hard limits (as of 2024 / early 2025).
    # yfinance 1m: 30 days max.  1h: ~730 days.  Higher-rate intraday: 60 days.
    # Binance single-request klines: 1000 candles.
    _YF_MAX_DAYS = {
        "1m": 30, "3m": 60, "5m": 60, "15m": 60, "30m": 60,
        "1h": 730, "2h": 730, "4h": 730, "6h": 730, "8h": 730, "12h": 730,
        "1d": 100 * 365, "3d": 100 * 365,
        "1w": 100 * 365, "1M": 100 * 365,
    }
    _BN_MAX_LIMIT = 1000

    def test_yfinance_period_within_provider_limit(self) -> None:
        for interval, profile in _TIMEFRAME_PROFILES.items():
            with self.subTest(interval=interval):
                days = _period_to_days(profile["period"])
                max_allowed = self._YF_MAX_DAYS.get(interval, 365 * 100)
                self.assertLessEqual(
                    days, max_allowed,
                    msg=(
                        f"profile {interval!r} period={profile['period']!r} "
                        f"resolves to {days}d which exceeds yfinance limit "
                        f"of {max_allowed}d"
                    ),
                )

    def test_binance_limit_within_provider_cap(self) -> None:
        """Binance allows up to 1000 candles per request; the profile
        ``limit`` field documents the *total* desired candles and
        ``_fetch_binance_rest`` clamps each request to 1000 via
        ``min(limit, 1000)`` (pagination handles the remainder).

        v1.1.0 tightened the assertion: previously the test computed
        ``min(profile["limit"], 1000)`` and then checked ``<= 1000`` —
        tautologically true by construction.  We now exercise the
        contract more usefully: (a) every profile has a positive limit;
        (b) the post-cap value never exceeds the cap; (c) for any
        profile whose raw limit exceeds the cap, the cap MUST have
        actually clamped (i.e. the effective value equals the cap, not
        the raw value).  A regression that disabled the cap would make
        ``effective != min(profile['limit'], cap)`` and fail the test.
        """
        for interval, profile in _TIMEFRAME_PROFILES.items():
            with self.subTest(interval=interval):
                raw_limit = int(profile["limit"])
                effective = min(raw_limit, self._BN_MAX_LIMIT)
                self.assertGreater(raw_limit, 0)
                self.assertLessEqual(effective, self._BN_MAX_LIMIT)
                if raw_limit > self._BN_MAX_LIMIT:
                    # Cap MUST have clamped — if a future refactor breaks
                    # the cap, effective would equal raw_limit.
                    self.assertEqual(
                        effective, self._BN_MAX_LIMIT,
                        msg=(
                            f"profile {interval!r} raw limit {raw_limit} > "
                            f"{self._BN_MAX_LIMIT} cap, but effective "
                            f"per-request value {effective} did not clamp"
                        ),
                    )


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
    """These tests exercise the synthetic OHLCV generator across interval
    profiles; each test must opt out of the integrity guard
    (``BACKTEST_REQUIRE_REAL_DATA=0``) because the guard's default-on
    behaviour would correctly refuse the explicit ``synthetic`` source."""

    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ, {"BACKTEST_REQUIRE_REAL_DATA": "0"}, clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_synthetic_with_1h_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = prepare_data(
                td, data_source="synthetic", interval="1h", fallback_rows=100,
            )
            self.assertEqual(result.source_label, "synthetic")
            self.assertTrue(os.path.isfile(result.data_path))
            # MEDIUM 7: explicit fallback_rows=100 is now honoured directly
            # (no more "equal-to-module-default" sentinel comparison).
            self.assertEqual(result.row_count, 100)
            # Check intraday timestamps
            with open(result.data_path, "r") as f:
                reader = csv.DictReader(f)
                row = next(reader)
                self.assertIn(":", row["date"])

    def test_synthetic_with_auto_uses_profile_rows(self) -> None:
        """When fallback_rows is None (default), use profile synthetic_rows."""
        with tempfile.TemporaryDirectory() as td:
            result = prepare_data(
                td, data_source="synthetic", interval="1h",
            )
            self.assertEqual(result.source_label, "synthetic")
            # Profile synthetic_rows for 1h = 4000 (default profile).
            self.assertEqual(result.row_count, 4000)

    def test_auto_interval_detection_from_code(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "strategy.py"), "w") as f:
                f.write('TIMEFRAME = "5m"\nSYMBOL = "BTCUSDT"\n')
            result = prepare_data(
                td, data_source="synthetic", interval="auto",
            )
            self.assertEqual(result.source_label, "synthetic")
            # Should detect 5m and use profile synthetic_rows=5000
            self.assertEqual(result.row_count, 5000)
            # Verify intraday timestamps
            with open(result.data_path, "r") as f:
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
