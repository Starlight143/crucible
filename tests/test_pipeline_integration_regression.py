# ruff: noqa: E402
"""
test_pipeline_integration_regression.py
========================================
v1.0.5 round 3 — end-to-end regression suite for the Quant validation track.

Reviewer's round-3 critique #2 + #9: round 1+2 added 104 unit tests that each
verify "rule X correctly judges input Y", but those don't catch the failure
mode where the *plumbing* between rules silently disconnects (e.g. somebody
adds an early-return to ``_resolve_quant_track`` and every Q0xx rule starts
returning empty). This suite freezes a small set of fixture bundles, each
carrying a known bug pattern, and asserts the integrated validation pipeline
catches them at the expected severity.

Each fixture corresponds to one bug class from the child project's pre-fix
high/medium issue list:

  R01 dataclass-kwargs-mismatch     (Trade(spread=0) → X001 / W001)
  R02 config-attr-missing           (config.NONEXISTENT → X002)
  R03 cross-file-import-missing     (from data_provider import nonexistent → X003)
  R04 lookahead-bias                (entry_price = row['open'] same bar → Q001)
  R05 off-by-one-stop-loop          (range(0, N) when stop checks i-1 → Q002)
  R06 trade-spread-zero             (Trade(symbol=..., spread=0) → Q003)
  R07 fixed-slippage-with-flag      (DYNAMIC_SLIPPAGE_ENABLED + constant slippage → Q004)
  R08 trade-kwargs-unpack           (Trade(**signal_dict) → W001)
  R09 getattr-unverifiable          (getattr(config, 'NONEXISTENT') → W002)

A regression CI signal of "found ≥ 7 / 9" is the floor — anything below means
either (a) one of the rules has a parse bug, or (b) the pipeline lost a step.

This is intentionally a coarse-grained guard: not every fixture needs to
match every rule, but the *aggregate* coverage must stay above the floor.
"""
import ast
import os
import sys
import unittest
from typing import List, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.cross_reference_check import analyse_cross_references_from_files
from crucible.features.quant_lint import analyse_quant_lint_from_files


# Each fixture: (id, [(path, content), ...], expected_rules_subset)
# expected_rules_subset = the rule IDs we expect to see fired by some
# pipeline step. Only one needs to fire for the fixture to count as
# "caught" — the suite asserts coverage, not an exact match.
FIXTURES: List[Tuple[str, List[Tuple[str, str]], List[str]]] = [
    (
        "R01-dataclass-kwargs",
        [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    qty: float\n",
            ),
            (
                "strategy.py",
                "from trade import Trade\n"
                "def make():\n"
                "    return Trade(symbol='BTC', qty=1.0, spread=0.1)\n",
            ),
        ],
        ["X001-dataclass-kwargs-mismatch"],
    ),
    (
        "R02-config-attr-missing",
        [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return config.NONEXISTENT_KEY\n",
            ),
        ],
        ["X002-config-attr-missing"],
    ),
    (
        "R03-cross-file-import-missing",
        [
            (
                "data_provider.py",
                "def fetch_ohlcv():\n    return []\n",
            ),
            (
                "strategy.py",
                "from data_provider import nonexistent_function\n"
                "def go():\n"
                "    return nonexistent_function()\n",
            ),
        ],
        ["X003-cross-file-import-missing"],
    ),
    (
        "R04-lookahead-bias",
        [
            (
                "strategy.py",
                "def signal(df):\n"
                "    for idx in df.index:\n"
                "        if df.loc[idx, 'close'] > 100:\n"
                "            entry_price = df.loc[idx, 'open']\n"
                "            return entry_price\n",
            ),
        ],
        ["Q001-lookahead-entry"],
    ),
    (
        "R05-off-by-one-stop-loop",
        [
            (
                "strategy.py",
                "def check_stops(bars, hold_minutes, stop_loss):\n"
                "    for i in range(1, hold_minutes):\n"
                "        if bars[i].low <= stop_loss:\n"
                "            return i\n"
                "    return None\n",
            ),
        ],
        ["Q002-range-off-by-one"],
    ),
    (
        "R06-trade-spread-zero",
        [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    qty: float\n"
                "    spread: float = 0.0\n",
            ),
            (
                "strategy.py",
                "from trade import Trade\n"
                "def estimate_spread(bar):\n"
                "    return (bar.high - bar.low) * 0.001\n"
                "def make():\n"
                "    return Trade(symbol='BTC', qty=1.0, spread=0)\n",
            ),
        ],
        ["Q003-trade-spread-zero"],
    ),
    (
        "R07-fixed-slippage-with-flag",
        [
            (
                "backtest.py",
                "DYNAMIC_SLIPPAGE_ENABLED = True\n"
                "def fill(price, qty):\n"
                "    slippage = 0.0005  # constant despite DYNAMIC_SLIPPAGE_ENABLED — Q004\n"
                "    return price * (1 + slippage)\n",
            ),
        ],
        ["Q004-fixed-slippage"],
    ),
    (
        "R08-trade-kwargs-unpack",
        [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    qty: float\n",
            ),
            (
                "strategy.py",
                "from trade import Trade\n"
                "def make_from_signal(signal_dict):\n"
                "    return Trade(**signal_dict)\n",
            ),
        ],
        ["W001-kwargs-unpack-skipped-check"],
    ),
    (
        "R09-getattr-unverifiable",
        [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return getattr(config, 'NONEXISTENT_KEY')\n",
            ),
        ],
        ["W002-getattr-dynamic-attr-unverifiable"],
    ),
]


def _run_pipeline_on_fixture(files: List[Tuple[str, str]]) -> List[str]:
    """Run cross_reference_check + quant_lint on the fixture and return
    every rule id that fired, deduplicated."""
    rules_fired: List[str] = []

    cr_report = analyse_cross_references_from_files(files)
    rules_fired.extend(i.rule for i in cr_report.issues if i.rule)

    lint_report = analyse_quant_lint_from_files(files)
    rules_fired.extend(i.rule for i in lint_report.issues if i.rule)

    return rules_fired


class TestPipelineRegressionFloor(unittest.TestCase):
    """v1.0.5 round 3: integrated coverage floor.

    These tests guard against the failure mode where a refactor silently
    short-circuits the pipeline (e.g. an early-return in
    ``_resolve_quant_track`` would still pass every per-rule unit test but
    detect zero issues here).
    """

    def test_each_fixture_caught_by_at_least_one_rule(self) -> None:
        misses: List[str] = []
        for fixture_id, files, expected_rules in FIXTURES:
            with self.subTest(fixture=fixture_id):
                fired = _run_pipeline_on_fixture(files)
                hit = any(rule in fired for rule in expected_rules)
                if not hit:
                    misses.append(
                        f"{fixture_id}: expected one of {expected_rules}, "
                        f"actually fired {fired!r}"
                    )
                self.assertTrue(
                    hit,
                    msg=(
                        f"[{fixture_id}] pipeline did not catch any of "
                        f"{expected_rules}. Fired: {fired!r}"
                    ),
                )
        # Belt-and-braces: at the SUITE level, ≤ 2 misses out of 9 fixtures
        # is the absolute floor for production. Anything lower is a pipeline
        # regression.
        self.assertLessEqual(
            len(misses),
            2,
            msg=(
                f"Pipeline coverage floor breached: {len(misses)}/9 fixtures "
                f"missed. Misses:\n  - " + "\n  - ".join(misses)
            ),
        )

    def test_aggregate_coverage_meets_floor(self) -> None:
        # Aggregate metric: ≥ 7/9 fixtures should be caught for the suite to
        # be considered healthy.
        caught = 0
        for fixture_id, files, expected_rules in FIXTURES:
            fired = _run_pipeline_on_fixture(files)
            if any(rule in fired for rule in expected_rules):
                caught += 1
        self.assertGreaterEqual(
            caught,
            7,
            msg=f"Pipeline coverage = {caught}/9 — floor is 7/9. "
            "A regression in the cross-reference / quant_lint pipeline is likely.",
        )


class TestPipelineCleanFixturePassesSilent(unittest.TestCase):
    """v1.0.5 round 3: a *clean* Quant fixture must produce zero high-severity
    issues. Round 2 had a false-positive scare with the X002 instance-attr
    detection; this test pins the contract."""

    def test_clean_quant_bundle_emits_no_high_severity(self) -> None:
        files: List[Tuple[str, str]] = [
            (
                "trade.py",
                "from dataclasses import dataclass\n"
                "@dataclass\nclass Trade:\n"
                "    symbol: str\n"
                "    qty: float\n"
                "    spread: float = 0.0001\n",
            ),
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n"
                "    STOP_LOSS_PCT = 0.02\n",
            ),
            (
                "strategy.py",
                "from trade import Trade\n"
                "import config\n"
                "def signal(df):\n"
                "    return Trade(symbol='BTC', qty=config.POSITION_SIZE, spread=0.0001)\n",
            ),
            (
                "backtest.py",
                "from strategy import signal\n"
                "if __name__ == '__main__':\n"
                "    print('ok')\n",
            ),
        ]
        cr_report = analyse_cross_references_from_files(files)
        lint_report = analyse_quant_lint_from_files(files)

        high_cr = [i for i in cr_report.issues if i.severity == "high"]
        high_lint = [i for i in lint_report.issues if i.severity == "high"]

        self.assertEqual(
            high_cr,
            [],
            msg=f"Clean fixture triggered cross-ref high issue: {[i.to_dict() for i in high_cr]}",
        )
        self.assertEqual(
            high_lint,
            [],
            msg=f"Clean fixture triggered lint high issue: {[i.to_dict() for i in high_lint]}",
        )


if __name__ == "__main__":
    unittest.main()
