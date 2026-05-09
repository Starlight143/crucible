# ruff: noqa: E402
"""Tests for crucible.features.quant_lint (v1.0.5 P1-5)."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.quant_lint import (
    QuantLintIssue,
    QuantLintReport,
    analyse_quant_lint_from_files,
)


class TestLookaheadEntry(unittest.TestCase):
    """Q001-lookahead-entry"""

    def test_entry_open_with_close_signal_in_same_function(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row['close'] > 100:\n"
                "            entry_price = row['open']\n"
                "            return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].severity, "high")

    def test_attribute_form_is_detected(self) -> None:
        files = [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for row in df:\n"
                "        if row.close > 100:\n"
                "            entry_price = row.open\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(len(bad), 1)

    def test_no_close_signal_no_alert(self) -> None:
        """If the function does not consult `close`, the open-entry is fine."""
        files = [
            (
                "strategy.py",
                "def initialise(df):\n"
                "    row = df[0]\n"
                "    entry_price = row['open']\n"
                "    return entry_price\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(bad, [])

    def test_module_scope_open_assign_skipped(self) -> None:
        """``entry_price = row['open']`` outside a function is too noisy to flag."""
        files = [
            ("strategy.py", "row = {'open': 1, 'close': 2}\nentry_price = row['open']\n"),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q001-lookahead-entry"]
        self.assertEqual(bad, [])


class TestRangeOffByOne(unittest.TestCase):
    """Q002-range-off-by-one"""

    def test_range_excludes_last_bar(self) -> None:
        files = [
            (
                "backtest.py",
                "def check(prices, hold_minutes, stop_price):\n"
                "    for i in range(1, hold_minutes):\n"
                "        if prices[i].low <= stop_price:\n"
                "            return i\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q002-range-off-by-one"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].severity, "medium")

    def test_correct_inclusive_range_does_not_fire(self) -> None:
        files = [
            (
                "backtest.py",
                "def check(prices, hold_minutes, stop_price):\n"
                "    for i in range(1, hold_minutes + 1):\n"
                "        if prices[i].low <= stop_price:\n"
                "            return i\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q002-range-off-by-one"]
        self.assertEqual(bad, [])

    def test_unrelated_range_skipped(self) -> None:
        """Loops that don't look like stop-checks should not fire."""
        files = [
            (
                "util.py",
                "def numbered(hold_minutes):\n"
                "    out = []\n"
                "    for i in range(1, hold_minutes):\n"
                "        out.append(i)\n"
                "    return out\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q002-range-off-by-one"]
        self.assertEqual(bad, [])


class TestTradeSpreadZero(unittest.TestCase):
    """Q003-trade-spread-zero"""

    def test_spread_zero_when_estimator_exists(self) -> None:
        files = [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    spread: float\n"
                "def estimate_spread(price):\n"
                "    return price * 0.0001\n"
                "def make():\n"
                "    return Trade(symbol='X', spread=0)\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q003-trade-spread-zero"]
        self.assertEqual(len(bad), 1)

    def test_spread_zero_without_estimator_does_not_fire(self) -> None:
        files = [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    spread: float\n"
                "def make():\n"
                "    return Trade(symbol='X', spread=0)\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q003-trade-spread-zero"]
        self.assertEqual(bad, [])


class TestFixedSlippage(unittest.TestCase):
    """Q004-fixed-slippage"""

    def test_dynamic_advertised_but_constant_assignment(self) -> None:
        files = [
            (
                "exec.py",
                "DYNAMIC_SLIPPAGE_ENABLED = True\n"
                "ORDERBOOK_SLIPPAGE_PCT = 0.0001\n"
                "def fill(order):\n"
                "    slippage = ORDERBOOK_SLIPPAGE_PCT\n"
                "    return slippage\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q004-fixed-slippage"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].severity, "medium")

    def test_dynamic_with_call_does_not_fire(self) -> None:
        """If slippage is computed via a function call, it counts as dynamic."""
        files = [
            (
                "exec.py",
                "DYNAMIC_SLIPPAGE_ENABLED = True\n"
                "def fill(order):\n"
                "    slippage = compute_slippage(order)\n"
                "    return slippage\n"
                "def compute_slippage(o):\n"
                "    return 0.001 * (o.qty ** 0.5)\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q004-fixed-slippage"]
        self.assertEqual(bad, [])

    def test_no_flag_no_check(self) -> None:
        files = [
            (
                "exec.py",
                "def fill(o):\n"
                "    slippage = 0.001\n"
                "    return slippage\n",
            ),
        ]
        report = analyse_quant_lint_from_files(files)
        bad = [i for i in report.issues if i.rule == "Q004-fixed-slippage"]
        self.assertEqual(bad, [])


class TestReport(unittest.TestCase):
    def test_passes_when_no_issues(self) -> None:
        report = analyse_quant_lint_from_files([("clean.py", "x = 1\n")])
        self.assertTrue(report.passes)
        self.assertEqual(report.issues, [])

    def test_to_dict_round_trip(self) -> None:
        issue = QuantLintIssue(
            severity="high",
            category="bug",
            description="…",
            file="x.py",
            line=1,
            suggestion="…",
            rule="Q001-lookahead-entry",
        )
        report = QuantLintReport(passes=False, issues=[issue], files_scanned=1)
        d = report.to_dict()
        self.assertFalse(d["passes"])
        self.assertEqual(len(d["issues"]), 1)
        self.assertEqual(d["issues"][0]["rule"], "Q001-lookahead-entry")


if __name__ == "__main__":
    unittest.main()
