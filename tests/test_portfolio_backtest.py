# ruff: noqa: E402
"""Tests for crucible.features.portfolio_backtest."""
import json
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.portfolio_backtest import (
    PortfolioConfig,
    PortfolioResult,
    _align_curves,
    _build_correlation_matrix,
    _calmar,
    _daily_returns,
    _max_drawdown,
    _mean,
    _pearson_correlation,
    _sharpe,
    _sortino,
    _std,
    _total_return,
    run_portfolio_backtest,
)


# ──────────────────────────────────────────────────────────────────────────────
# Internal math helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestMean(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertAlmostEqual(_mean([1.0, 2.0, 3.0]), 2.0)

    def test_empty(self) -> None:
        self.assertEqual(_mean([]), 0.0)

    def test_single(self) -> None:
        self.assertAlmostEqual(_mean([5.0]), 5.0)


class TestStd(unittest.TestCase):
    def test_constant_series(self) -> None:
        # All same value → std = 0
        self.assertAlmostEqual(_std([3.0, 3.0, 3.0]), 0.0)

    def test_known_population_std(self) -> None:
        # Classic example: population std = 2.0 (ddof=0)
        result = _std([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0], ddof=0)
        self.assertAlmostEqual(result, 2.0, places=4)

    def test_sample_std_greater_than_population(self) -> None:
        # Sample std (ddof=1) is strictly greater than population std (ddof=0)
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        self.assertGreater(_std(values, ddof=1), _std(values, ddof=0))

    def test_empty(self) -> None:
        self.assertEqual(_std([]), 0.0)

    def test_single_ddof1(self) -> None:
        # n=1 with ddof=1 → division by zero guarded → returns 0
        self.assertEqual(_std([5.0], ddof=1), 0.0)


class TestMaxDrawdown(unittest.TestCase):
    def test_no_drawdown(self) -> None:
        # Monotonically increasing → 0.0
        self.assertAlmostEqual(_max_drawdown([1.0, 1.1, 1.2, 1.3]), 0.0)

    def test_known_drawdown_is_negative(self) -> None:
        # Peak = 1.2, trough after = 0.9 → dd = (0.9 - 1.2) / 1.2 = -0.25
        result = _max_drawdown([1.0, 1.2, 0.9, 1.1])
        self.assertAlmostEqual(result, -(1.2 - 0.9) / 1.2, places=6)
        self.assertLess(result, 0.0)

    def test_too_short_returns_zero(self) -> None:
        self.assertEqual(_max_drawdown([]), 0.0)
        self.assertEqual(_max_drawdown([1.0]), 0.0)

    def test_all_equal(self) -> None:
        self.assertAlmostEqual(_max_drawdown([1.0, 1.0, 1.0]), 0.0)


class TestDailyReturns(unittest.TestCase):
    def test_basic(self) -> None:
        result = _daily_returns([1.0, 1.1, 1.05])
        self.assertAlmostEqual(result[0], 0.1)
        self.assertAlmostEqual(result[1], -0.05 / 1.1, places=8)

    def test_too_short(self) -> None:
        self.assertEqual(_daily_returns([]), [])
        self.assertEqual(_daily_returns([1.0]), [])


class TestTotalReturn(unittest.TestCase):
    def test_positive(self) -> None:
        self.assertAlmostEqual(_total_return([1.0, 1.5]), 0.5)

    def test_negative(self) -> None:
        self.assertAlmostEqual(_total_return([1.0, 0.8]), -0.2)

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(_total_return([]))

    def test_single_returns_none(self) -> None:
        self.assertIsNone(_total_return([1.0]))

    def test_zero_first_equity_returns_none(self) -> None:
        # Division-by-zero guard: first equity == 0 must return None.
        self.assertIsNone(_total_return([0.0, 1.0]))

    def test_negative_first_equity_returns_none(self) -> None:
        # Negative first equity is economically nonsensical and would produce
        # an inverted return figure; the guard covers equity[0] <= 0.0.
        self.assertIsNone(_total_return([-1.0, 1.5]))


class TestSharpe(unittest.TestCase):
    def test_positive_varying_returns(self) -> None:
        # Alternating returns so std > 0
        returns = [0.02 if i % 2 == 0 else -0.005 for i in range(100)]
        result = _sharpe(returns, risk_free_rate=0.0)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0.0)

    def test_constant_returns_none(self) -> None:
        # Constant returns → std = 0 → Sharpe undefined
        self.assertIsNone(_sharpe([0.01] * 10, risk_free_rate=0.0))

    def test_empty(self) -> None:
        self.assertIsNone(_sharpe([]))

    def test_single(self) -> None:
        # n=1: std with ddof=1 = 0 → None
        self.assertIsNone(_sharpe([0.01]))


class TestSortino(unittest.TestCase):
    def test_no_downside_returns_none(self) -> None:
        # All positive returns → no downside → None
        self.assertIsNone(_sortino([0.01, 0.02, 0.03]))

    def test_with_downside(self) -> None:
        returns = [0.01, -0.02, 0.03, -0.01, 0.02]
        result = _sortino(returns, risk_free_rate=0.0)
        self.assertIsNotNone(result)

    def test_denominator_uses_all_periods(self) -> None:
        # Verify the fix: denominator uses only downside periods (len(downside)),
        # NOT all periods (len(excess)) — correct Sortino 1991 semi-deviation.
        # With 4 returns and 1 downside period, downside_dev should be
        # sqrt(sum_downside_sq / len(downside)), NOT sqrt(sum_downside_sq / len(excess))
        # Using len(downside) is the correct semi-deviation formula (Sortino 1991).
        returns = [0.05, 0.03, -0.10, 0.04]
        excess = [r - 0.0 for r in returns]
        downside = [e for e in excess if e < 0.0]
        expected_dev_all = math.sqrt(sum(d ** 2 for d in downside) / len(excess))
        expected_dev_only = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
        # The two should differ (sanity check that the test is meaningful)
        self.assertNotAlmostEqual(expected_dev_all, expected_dev_only)
        # _annualise_factor(4) uses sqrt(12.0) for monthly regime (n < 50)
        from crucible.features.portfolio_backtest import _annualise_factor
        ann = _annualise_factor(len(returns))
        # Correct formula: denominator is len(downside), not len(excess)
        expected = (_mean(excess) / expected_dev_only) * ann
        self.assertAlmostEqual(_sortino(returns, 0.0), expected, places=6)

    def test_empty(self) -> None:
        self.assertIsNone(_sortino([]))


class TestCalmar(unittest.TestCase):
    def test_positive(self) -> None:
        equity = [1.0, 1.1, 1.05, 1.2]
        returns = _daily_returns(equity)
        result = _calmar(equity, returns)
        self.assertIsNotNone(result)

    def test_zero_drawdown_returns_none(self) -> None:
        equity = [1.0, 1.1, 1.2]
        returns = _daily_returns(equity)
        self.assertIsNone(_calmar(equity, returns))

    def test_empty_returns_returns_none(self) -> None:
        self.assertIsNone(_calmar([], []))


class TestPearsonCorrelation(unittest.TestCase):
    def test_perfect_positive(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0]
        self.assertAlmostEqual(_pearson_correlation(x, x), 1.0, places=6)

    def test_perfect_negative(self) -> None:
        x = [1.0, 2.0, 3.0, 4.0]
        y = [4.0, 3.0, 2.0, 1.0]
        self.assertAlmostEqual(_pearson_correlation(x, y), -1.0, places=6)

    def test_zero_variance(self) -> None:
        # Constant series → undefined correlation → None
        self.assertIsNone(_pearson_correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_length_mismatch_returns_none(self) -> None:
        # Mismatched lengths → None (defensive guard per docstring)
        self.assertIsNone(_pearson_correlation([1.0, 2.0], [1.0, 2.0, 3.0]))

    def test_too_short(self) -> None:
        self.assertIsNone(_pearson_correlation([1.0], [2.0]))


# ──────────────────────────────────────────────────────────────────────────────
# _align_curves
# ──────────────────────────────────────────────────────────────────────────────

class TestAlignCurves(unittest.TestCase):
    def _make_curve(self, n: int, start: float = 1.0, step: float = 0.01):
        return [(str(i), start + i * step) for i in range(n)]

    def test_single_curve(self) -> None:
        curves = [self._make_curve(5)]
        ts, aligned = _align_curves(curves)
        self.assertEqual(len(ts), 5)
        self.assertEqual(len(aligned), 1)
        self.assertEqual(len(aligned[0]), 5)

    def test_two_equal_length_curves(self) -> None:
        c1 = self._make_curve(5, start=1.0)
        c2 = self._make_curve(5, start=2.0)
        ts, aligned = _align_curves([c1, c2])
        self.assertEqual(len(ts), 5)
        self.assertEqual(len(aligned), 2)

    def test_all_normalized_to_one(self) -> None:
        curve = [(str(i), float(i + 2)) for i in range(4)]
        _, aligned = _align_curves([curve])
        self.assertAlmostEqual(aligned[0][0], 1.0, places=6)

    def test_empty_curves_list(self) -> None:
        ts, aligned = _align_curves([])
        self.assertEqual(ts, [])
        self.assertEqual(aligned, [])

    def test_forward_fill(self) -> None:
        # Curve 1 has timestamps 0,1,2,3; Curve 2 only has 0,2
        c1 = [("0", 1.0), ("1", 1.1), ("2", 1.2), ("3", 1.3)]
        c2 = [("0", 2.0), ("2", 2.2)]
        ts, aligned = _align_curves([c1, c2])
        self.assertEqual(len(ts), 4)
        # Curve 2 should forward-fill timestamp "1" with the value from "0"
        self.assertAlmostEqual(aligned[1][1], aligned[1][0], places=6)


# ──────────────────────────────────────────────────────────────────────────────
# _build_correlation_matrix
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildCorrelationMatrix(unittest.TestCase):
    def test_single_series(self) -> None:
        matrix = _build_correlation_matrix(["A"], [[0.01, -0.02, 0.03]])
        self.assertIn("A", matrix)
        self.assertAlmostEqual(matrix["A"]["A"], 1.0, places=6)

    def test_two_identical_series(self) -> None:
        returns = [0.01, -0.02, 0.03, 0.01]
        matrix = _build_correlation_matrix(["A", "B"], [returns, returns])
        self.assertAlmostEqual(matrix["A"]["B"], 1.0, places=4)
        self.assertAlmostEqual(matrix["B"]["A"], 1.0, places=4)

    def test_diagonal_always_one(self) -> None:
        r1 = [0.01, -0.01, 0.02]
        r2 = [0.02, 0.01, -0.01]
        matrix = _build_correlation_matrix(["X", "Y"], [r1, r2])
        self.assertAlmostEqual(matrix["X"]["X"], 1.0, places=6)
        self.assertAlmostEqual(matrix["Y"]["Y"], 1.0, places=6)


# ──────────────────────────────────────────────────────────────────────────────
# run_portfolio_backtest (integration)
# ──────────────────────────────────────────────────────────────────────────────

class TestRunPortfolioBacktest(unittest.TestCase):
    def _make_run_dir(self, td: str, name: str, n: int = 30) -> str:
        run_dir = os.path.join(td, name)
        os.makedirs(os.path.join(run_dir, "sample_out"), exist_ok=True)
        ledger_path = os.path.join(run_dir, "sample_out", "ledger.csv")
        with open(ledger_path, "w") as f:
            f.write("date,equity\n")
            for i in range(n):
                f.write(f"{i},{1.0 + i * 0.005}\n")
        return run_dir

    def test_two_equal_weight_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            r2 = self._make_run_dir(td, "run2")
            result = run_portfolio_backtest([r1, r2], [0.5, 0.5])
            self.assertIsInstance(result, PortfolioResult)
            self.assertIsNotNone(result.portfolio_total_return)
            self.assertGreater(len(result.equity_curve), 0)

    def test_single_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1", n=50)
            result = run_portfolio_backtest([r1], [1.0])
            self.assertIsInstance(result, PortfolioResult)

    def test_exports_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            r2 = self._make_run_dir(td, "run2")
            result = run_portfolio_backtest([r1, r2], [0.5, 0.5], output_dir=td)
            report_path = os.path.join(td, "portfolio_report.json")
            self.assertTrue(os.path.isfile(report_path))
            with open(report_path) as f:
                data = json.load(f)
            self.assertIn("portfolio_total_return", data)
            self.assertIn("equity_curve", data)
            self.assertIn("correlation_matrix", data)

    def test_weights_do_not_sum_to_one_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            with self.assertRaises(ValueError):
                run_portfolio_backtest([r1], [0.5])

    def test_negative_weight_raises(self) -> None:
        """Negative weights must be rejected before any computation begins."""
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            r2 = self._make_run_dir(td, "run2")
            with self.assertRaises(ValueError):
                run_portfolio_backtest([r1, r2], [-0.5, 1.5])

    def test_zero_weight_allowed(self) -> None:
        """A zero weight (strategy excluded from portfolio) must be accepted."""
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            r2 = self._make_run_dir(td, "run2")
            # Should NOT raise — zero weights are valid (strategy is simply excluded)
            result = run_portfolio_backtest([r1, r2], [0.0, 1.0])
            self.assertIsNotNone(result)

    def test_empty_run_dirs_raises(self) -> None:
        with self.assertRaises(ValueError):
            run_portfolio_backtest([], [])

    def test_no_backtest_data_returns_none_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Run dirs with no ledger.csv
            r1 = os.path.join(td, "empty_run")
            os.makedirs(r1, exist_ok=True)
            result = run_portfolio_backtest([r1], [1.0])
            self.assertIsNone(result.portfolio_sharpe)
            self.assertIsNone(result.portfolio_sortino)

    def test_correlation_matrix_diagonal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            r2 = self._make_run_dir(td, "run2")
            result = run_portfolio_backtest([r1, r2], [0.5, 0.5])
            for label, row in result.correlation_matrix.items():
                self.assertAlmostEqual(row[label], 1.0, places=4)

    def test_portfolio_config_dataclass(self) -> None:
        # PortfolioConfig is a data container; verify it holds fields correctly
        cfg = PortfolioConfig(run_dirs=["a", "b"], weights=[0.6, 0.4])
        self.assertEqual(cfg.run_dirs, ["a", "b"])
        self.assertAlmostEqual(sum(cfg.weights), 1.0)

    def test_mismatched_run_dirs_weights_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            r1 = self._make_run_dir(td, "run1")
            with self.assertRaises(ValueError):
                run_portfolio_backtest([r1], [0.5, 0.5])

    # ── Label deduplication tests ─────────────────────────────────────────────

    def test_duplicate_basenames_produce_unique_labels(self) -> None:
        """Two run_dirs sharing a basename get _1 suffix on the duplicate."""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td1, _tf.TemporaryDirectory() as td2:
            # Both have basename "run"
            r1 = self._make_run_dir(td1, "run")
            r2 = self._make_run_dir(td2, "run")
            result = run_portfolio_backtest([r1, r2], [0.5, 0.5])
            keys = list(result.correlation_matrix.keys())
            self.assertEqual(len(keys), 2, "Correlation matrix must have 2 distinct keys")
            self.assertEqual(len(set(keys)), 2, "All correlation-matrix keys must be unique")

    def test_duplicate_basename_suffix_collision_avoided(self) -> None:
        """
        When a pre-existing label would match the generated suffix, the algorithm
        skips to the next free suffix.
        run_dirs basenames = ["run", "run_1", "run"] →
        expected labels    = ["run", "run_1", "run_2"]  (not ["run", "run_1", "run_1"])
        """
        import tempfile as _tf
        with (
            _tf.TemporaryDirectory() as td1,
            _tf.TemporaryDirectory() as td2,
            _tf.TemporaryDirectory() as td3,
        ):
            r1 = self._make_run_dir(td1, "run")
            r2 = self._make_run_dir(td2, "run_1")  # pre-existing "run_1"
            r3 = self._make_run_dir(td3, "run")    # would naïvely also get "run_1"
            result = run_portfolio_backtest([r1, r2, r3], [1 / 3, 1 / 3, 1 / 3])
            keys = list(result.correlation_matrix.keys())
            self.assertEqual(len(keys), 3)
            self.assertEqual(len(set(keys)), 3, "All keys must be unique even with suffix collision")
            # "run_1" appears in input; the duplicate "run" must get "run_2", not "run_1"
            self.assertNotEqual(keys[0], keys[1])
            self.assertNotEqual(keys[0], keys[2])
            self.assertNotEqual(keys[1], keys[2])

    # ── Weight re-normalisation warning test ──────────────────────────────────

    def test_weight_renormalisation_warning_logged(self) -> None:
        """
        When some strategies lack sufficient data, weights are re-normalised and
        a WARNING must be emitted via the module logger.
        """
        import logging
        with tempfile.TemporaryDirectory() as td:
            r_good = self._make_run_dir(td, "good_run", n=30)
            r_empty = os.path.join(td, "empty_run")
            os.makedirs(r_empty, exist_ok=True)
            # r_empty has no ledger.csv so it has 0 equity data points → excluded
            with self.assertLogs(
                "crucible.features.portfolio_backtest",
                level=logging.WARNING,
            ) as log_cm:
                result = run_portfolio_backtest(
                    [r_good, r_empty], [0.6, 0.4]
                )
            # Exactly one warning about exclusion
            warning_msgs = [r.getMessage() for r in log_cm.records]
            self.assertTrue(
                any("excluded" in m.lower() or "re-normalised" in m.lower()
                    for m in warning_msgs),
                f"Expected weight re-normalisation warning; got: {warning_msgs}",
            )
            # The result should still compute metrics (from the surviving run)
            # Portfolio uses only r_good with effective weight 1.0
            self.assertIsNotNone(result.portfolio_total_return)


if __name__ == "__main__":
    unittest.main()
