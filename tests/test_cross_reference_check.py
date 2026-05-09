# ruff: noqa: E402
"""Tests for crucible.features.cross_reference_check (v1.0.5 P0-2)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.cross_reference_check import (
    CrossReferenceIssue,
    CrossReferenceReport,
    analyse_cross_references,
    analyse_cross_references_from_files,
)


class TestDataclassKwargsMismatch(unittest.TestCase):
    """X001 — ``Trade(side=...)`` while Trade has no `side` field."""

    def test_dataclass_kwargs_mismatch_high_severity(self) -> None:
        files = [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    price: float\n"
                "    size: float\n",
            ),
            (
                "backtest.py",
                "from trade import Trade\n"
                "def make():\n"
                "    return Trade(side='long', quantity=10)\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        self.assertFalse(report.passes)
        rules = sorted({i.rule for i in report.issues})
        self.assertIn("X001-dataclass-kwargs-mismatch", rules)
        side_issues = [i for i in report.issues if "side" in i.description]
        self.assertEqual(len(side_issues), 1)
        self.assertEqual(side_issues[0].severity, "high")
        self.assertEqual(side_issues[0].file, "backtest.py")

    def test_known_kwargs_dont_fire(self) -> None:
        files = [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    price: float\n",
            ),
            (
                "backtest.py",
                "from trade import Trade\n"
                "Trade(symbol='X', price=1.0)\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X001-dataclass-kwargs-mismatch"]
        self.assertEqual(bad, [])

    def test_kwargs_accepted_when_init_has_double_star(self) -> None:
        """If the class defines `__init__(self, **kw)` we cannot statically check."""
        files = [
            (
                "trade.py",
                "class Trade:\n"
                "    def __init__(self, **kw):\n"
                "        self.kw = kw\n",
            ),
            (
                "backtest.py",
                "from trade import Trade\n"
                "Trade(any_name=1)\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X001-dataclass-kwargs-mismatch"]
        self.assertEqual(bad, [])


class TestConfigAttrMissing(unittest.TestCase):
    """X002 — ``config.X`` where X is undefined."""

    def test_config_attr_missing_high_severity(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n"
                "    LOOKBACK_DAYS = 30\n"
                "config = Config()\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def x():\n"
                "    return config.POSITION_SIZE_QUOTE\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X002-config-attr-missing"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].severity, "high")
        self.assertIn("POSITION_SIZE_QUOTE", bad[0].description)

    def test_existing_attr_does_not_fire(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n"
                "config = Config()\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def x():\n"
                "    return config.POSITION_SIZE\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        self.assertTrue(report.passes)


class TestImportMissing(unittest.TestCase):
    """X003 — ``from foo import bar`` where bar is undefined in foo."""

    def test_import_missing_symbol_high_severity(self) -> None:
        files = [
            ("foo.py", "def existing():\n    pass\n"),
            ("caller.py", "from foo import non_existent\n"),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X003-cross-file-import-missing"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].severity, "high")

    def test_import_existing_symbol_does_not_fire(self) -> None:
        files = [
            ("foo.py", "def existing():\n    pass\n"),
            ("caller.py", "from foo import existing\n"),
        ]
        report = analyse_cross_references_from_files(files)
        self.assertTrue(report.passes)

    def test_star_import_does_not_emit_false_positives(self) -> None:
        """`from foo import *` makes us conservative — no missing-name claims."""
        files = [
            ("foo.py", "from bar import *\n"),
            ("caller.py", "from foo import anything_at_all\n"),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X003-cross-file-import-missing"]
        self.assertEqual(bad, [])

    def test_external_import_skipped(self) -> None:
        """Stdlib/PyPI imports are not in our bundle and must not be flagged."""
        files = [
            ("caller.py", "from numpy import array\n"),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X003-cross-file-import-missing"]
        self.assertEqual(bad, [])


class TestPositionalTypeMismatch(unittest.TestCase):
    """X004 — string literal passed to a parameter typed as int."""

    def test_string_for_int_param(self) -> None:
        files = [
            (
                "data_provider.py",
                "def fetch_historical_data(symbol: str, days: int = 90, source: str = 'yfinance'):\n"
                "    pass\n",
            ),
            (
                "caller.py",
                "from data_provider import fetch_historical_data\n"
                "fetch_historical_data('BTC', '3mo', 'binance')\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X004-positional-arg-type-mismatch"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].severity, "high")
        self.assertIn("days", bad[0].description)

    def test_default_value_inferred_type(self) -> None:
        """If no annotation, fall back to the default's literal kind."""
        files = [
            (
                "fn.py",
                "def f(x, days=90):\n"
                "    pass\n",
            ),
            (
                "caller.py",
                "from fn import f\n"
                "f(0, '3mo')\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X004-positional-arg-type-mismatch"]
        self.assertEqual(len(bad), 1)

    def test_optional_int_unwrapped(self) -> None:
        """``Optional[int]`` resolves to int for the type sniff."""
        files = [
            (
                "fn.py",
                "from typing import Optional\n"
                "def f(x: Optional[int] = None):\n"
                "    pass\n",
            ),
            (
                "caller.py",
                "from fn import f\n"
                "f('not-an-int')\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X004-positional-arg-type-mismatch"]
        self.assertEqual(len(bad), 1)

    def test_no_annotation_no_default_no_check(self) -> None:
        """Without any type signal, we must not guess."""
        files = [
            ("fn.py", "def f(x, y):\n    pass\n"),
            (
                "caller.py",
                "from fn import f\n"
                "f(0, 'whatever')\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        bad = [i for i in report.issues if i.rule == "X004-positional-arg-type-mismatch"]
        self.assertEqual(bad, [])


class TestDirectoryWalker(unittest.TestCase):
    """analyse_cross_references writing to a tmp dir matches the in-memory API."""

    def test_walks_real_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "trade.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n    symbol: str\n"
                )
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from trade import Trade\n"
                    "Trade(unknown_kw=1)\n"
                )
            report = analyse_cross_references(d)
            self.assertFalse(report.passes)
            self.assertGreaterEqual(report.files_scanned, 2)
            rules = {i.rule for i in report.issues}
            self.assertIn("X001-dataclass-kwargs-mismatch", rules)

    def test_tests_directory_skipped(self) -> None:
        """Files under tests/ are not scanned (intentional bad kwargs are common)."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "trade.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n    symbol: str\n"
                )
            os.makedirs(os.path.join(d, "tests"))
            with open(os.path.join(d, "tests", "test_trade.py"), "w", encoding="utf-8") as f:
                f.write(
                    "from trade import Trade\n"
                    "Trade(unknown_kw=1)\n"  # would fire if scanned
                )
            report = analyse_cross_references(d)
            self.assertTrue(report.passes)


class TestReport(unittest.TestCase):
    def test_to_dict_round_trip(self) -> None:
        issue = CrossReferenceIssue(
            severity="high",
            category="bug",
            description="x",
            file="a.py",
            line=1,
            suggestion="fix",
            rule="X001-dataclass-kwargs-mismatch",
        )
        report = CrossReferenceReport(passes=False, issues=[issue], files_scanned=1)
        d = report.to_dict()
        self.assertEqual(d["passes"], False)
        self.assertEqual(d["files_scanned"], 1)
        self.assertEqual(len(d["issues"]), 1)
        self.assertEqual(d["issues"][0]["rule"], "X001-dataclass-kwargs-mismatch")


if __name__ == "__main__":
    unittest.main()
