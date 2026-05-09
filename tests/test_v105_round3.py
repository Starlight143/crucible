# ruff: noqa: E402
"""v1.0.5 round 3 tests:

- Q024 live_trader behavioral SL assertion
- Schema-first hoist generalisation (AST-based, not filename-locked)
- failure_type strict enum + substring-fallback removal
- W001 (Trade(**kwargs)) and W002 (getattr unverifiable) escape-path warnings
- ReviewFailureType drift guard
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.cross_reference_check import analyse_cross_references_from_files
from crucible.features.quant_smoke import (
    _build_live_trader_smoke_script,
    quant_smoke_dryrun,
)
from crucible.modules.section_03_models_and_context import (
    CodeBundle,
    CodegenBatchPlan,
    CodegenFilePlan,
    FailureType,
    GeneratedFile,
    ReviewReport,
    _coerce_review_failure_type,
    _REVIEW_REPORT_ALLOWED_FAILURE_TYPES,
)
from crucible.modules.section_05_analysis_and_codegen import (
    _extract_quant_schema_signatures,
    _file_plan_looks_schema_shaped,
    _quant_hoist_schema_first,
)


# ─── Q024 live_trader behavioural assertion ──────────────────────────────────


class TestQ024LiveTraderBehavioral(unittest.TestCase):
    """The smoke script must emit exit code 3 when stop_loss is mentioned in
    the live_trader source but no close/exit ever fires after a 40% drop."""

    def _write_bundle(self, d: str, live_trader_body: str) -> None:
        files = (
            ("strategy.py", "def run(): return 0\n"),
            ("backtest.py", "if __name__ == '__main__':\n    print('ok')\n"),
            ("live_trader.py", live_trader_body),
        )
        for name, body in files:
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(body)

    def test_smoke_script_includes_ramp_down_price_series(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self._write_bundle(d, "class LiveTrader:\n    pass\n")
            script = _build_live_trader_smoke_script(d)
            self.assertIsNotNone(script)
            assert script is not None
            # Ramp-down constants must be present so Q024 can fire.
            self.assertIn("100.0 - 4.0 * i", script)
            self.assertIn("LIVE_TRADER_BEHAVIORAL", script)
            self.assertIn("Q024", "Q024-live-trader-sl-unreachable")

    def test_q024_fires_when_sl_present_but_never_closes(self) -> None:
        # LiveTrader has stop_loss config and opens a position on every tick
        # but the close path is gated behind a time check that's always False.
        body = (
            "import ccxt\n"
            "class LiveTrader:\n"
            "    def __init__(self, *a, **kw):\n"
            "        self.ex = ccxt.binance()\n"
            "        self.stop_loss = 95.0  # never used :)\n"
            "        self.has_position = False\n"
            "    def run_loop(self):\n"
            "        for _ in range(5):\n"
            "            t = self.ex.fetch_ticker('BTC/USDT')\n"
            "            if not self.has_position:\n"
            "                self.ex.create_market_order('BTC/USDT', 'buy', 1.0)\n"
            "                self.has_position = True\n"
            "            # Bug: close branch is unreachable.\n"
            "            if False:  # pretend this is `now > entry_time + hold_minutes` always-False\n"
            "                self.ex.create_market_order('BTC/USDT', 'sell', 1.0)\n"
        )
        with tempfile.TemporaryDirectory() as d:
            self._write_bundle(d, body)
            result = quant_smoke_dryrun(d, timeout_seconds=30)
            # Backtest passes; live_trader behaviour fails.
            self.assertFalse(result.passes)
            self.assertEqual(result.live_trader_passes, False)
            rules = {i["rule"] for i in result.issues}
            self.assertIn("Q024-live-trader-sl-unreachable", rules)

    def test_q024_silent_when_no_sl_marker(self) -> None:
        # No "stop_loss" in source — the silent-SL bug class doesn't apply,
        # so Q024 must NOT fire even though no closes happen.
        body = (
            "import ccxt\n"
            "class LiveTrader:\n"
            "    def __init__(self, *a, **kw):\n"
            "        self.ex = ccxt.binance()\n"
            "    def run_loop(self):\n"
            "        for _ in range(3):\n"
            "            t = self.ex.fetch_ticker('BTC/USDT')\n"
            "            assert t['last'] > 0\n"
        )
        with tempfile.TemporaryDirectory() as d:
            self._write_bundle(d, body)
            result = quant_smoke_dryrun(d, timeout_seconds=30)
            self.assertTrue(result.passes, msg=result.log)
            self.assertTrue(result.live_trader_passes is True, msg=result.live_trader_log)
            rules = {i["rule"] for i in result.issues}
            self.assertNotIn("Q024-live-trader-sl-unreachable", rules)

    def test_q024_silent_when_sl_present_and_closes_fire(self) -> None:
        # LiveTrader has SL and properly closes when price < SL. Should NOT trigger Q024.
        body = (
            "import ccxt\n"
            "class LiveTrader:\n"
            "    def __init__(self, *a, **kw):\n"
            "        self.ex = ccxt.binance()\n"
            "        self.stop_loss = 95.0\n"
            "        self.has_position = False\n"
            "        self.entry_price = None\n"
            "    def run_loop(self):\n"
            "        for _ in range(8):\n"
            "            t = self.ex.fetch_ticker('BTC/USDT')\n"
            "            price = t['last']\n"
            "            if not self.has_position:\n"
            "                self.ex.create_market_order('BTC/USDT', 'buy', 1.0)\n"
            "                self.entry_price = price\n"
            "                self.has_position = True\n"
            "            elif price <= self.stop_loss:\n"
            "                self.ex.create_market_order('BTC/USDT', 'sell', 1.0)\n"
            "                self.has_position = False\n"
        )
        with tempfile.TemporaryDirectory() as d:
            self._write_bundle(d, body)
            result = quant_smoke_dryrun(d, timeout_seconds=30)
            self.assertTrue(result.passes, msg=result.log)
            rules = {i["rule"] for i in result.issues}
            self.assertNotIn("Q024-live-trader-sl-unreachable", rules)


# ─── Schema-first hoist generalisation ──────────────────────────────────────


class TestSchemaHoistGeneralisation(unittest.TestCase):
    """v1.0.5 round 3: the hoist must catch schema-shaped files even when the
    LLM merges Trade + Config into ``models.py`` or renames them."""

    def _file_plan(
        self,
        path: str,
        purpose: str = "",
        depends_on=None,
        must_contain=None,
    ) -> CodegenFilePlan:
        return CodegenFilePlan(
            path=path,
            purpose=purpose,
            depends_on=list(depends_on or []),
            must_contain=list(must_contain or []),
        )

    def test_file_plan_schema_detection_via_purpose(self) -> None:
        plan = self._file_plan("models.py", purpose="Trade record and Order dataclass definitions.")
        self.assertTrue(_file_plan_looks_schema_shaped(plan))

    def test_file_plan_schema_detection_via_must_contain(self) -> None:
        plan = self._file_plan(
            "domain.py",
            purpose="Domain layer.",
            must_contain=["@dataclass", "class Trade"],
        )
        self.assertTrue(_file_plan_looks_schema_shaped(plan))

    def test_file_plan_non_schema_returns_false(self) -> None:
        plan = self._file_plan("backtest.py", purpose="Backtest engine implementing event loop.")
        self.assertFalse(_file_plan_looks_schema_shaped(plan))

    def test_file_plan_basemodel_hint_detected(self) -> None:
        plan = self._file_plan(
            "schemas.py",
            purpose="Pydantic schema layer.",
            must_contain=["class Order(BaseModel)"],
        )
        self.assertTrue(_file_plan_looks_schema_shaped(plan))

    def test_hoist_lifts_renamed_schema_module(self) -> None:
        # LLM put the schema in ``models.py`` instead of ``trade.py``.
        batches = [
            CodegenBatchPlan(name="batch_1", objective="exec", files=["backtest.py"]),
            CodegenBatchPlan(
                name="batch_2", objective="schema", files=["models.py"]
            ),
        ]
        file_map = {
            "backtest.py": self._file_plan(
                "backtest.py",
                purpose="Backtest runner.",
                depends_on=["models.py"],
            ),
            "models.py": self._file_plan(
                "models.py",
                purpose="Trade and Order dataclasses (schema layer).",
                must_contain=["@dataclass", "class Trade"],
            ),
        }
        out = _quant_hoist_schema_first(
            batches,
            known_paths={"backtest.py", "models.py"},
            file_map=file_map,
            batch_size=4,
        )
        self.assertIn("models.py", out[0].files)
        for b in out[1:]:
            self.assertNotIn("models.py", b.files)

    def test_hoist_still_works_when_neither_canonical_nor_purpose(self) -> None:
        # A file with no canonical filename AND no schema-purpose marker is
        # NOT lifted — the hoist must remain conservative.
        batches = [
            CodegenBatchPlan(name="batch_1", objective="x", files=["lib.py"]),
            CodegenBatchPlan(name="batch_2", objective="y", files=["main.py"]),
        ]
        file_map = {
            "lib.py": self._file_plan("lib.py", purpose="utility helpers."),
            "main.py": self._file_plan("main.py", purpose="entrypoint."),
        }
        out = _quant_hoist_schema_first(
            batches,
            known_paths={"lib.py", "main.py"},
            file_map=file_map,
            batch_size=4,
        )
        self.assertEqual(out, batches)


class TestSchemaSignatureExtractionGeneralised(unittest.TestCase):
    """v1.0.5 round 3: extraction reads schema-shaped classes from any .py
    file, not only ``trade.py`` / ``config.py``."""

    def _bundle(self, files):
        return CodeBundle(
            project_type="quant",
            files=[GeneratedFile(path=p, content=c) for p, c in files],
        )

    def test_extracts_dataclass_in_models_py(self) -> None:
        bundle = self._bundle(
            [
                (
                    "models.py",
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n"
                    "    symbol: str\n"
                    "    qty: float\n",
                )
            ]
        )
        text = _extract_quant_schema_signatures(bundle, project_type="quant")
        self.assertIn("Trade", text)
        self.assertIn("symbol: str", text)

    def test_extracts_basemodel_subclass(self) -> None:
        bundle = self._bundle(
            [
                (
                    "schemas.py",
                    "from pydantic import BaseModel\n"
                    "class Order(BaseModel):\n"
                    "    symbol: str\n"
                    "    qty: float\n",
                )
            ]
        )
        text = _extract_quant_schema_signatures(bundle, project_type="quant")
        self.assertIn("Order", text)
        self.assertIn("symbol: str", text)

    def test_skips_non_schema_class_in_non_canonical_file(self) -> None:
        # A regular service class in backtest.py must NOT be published to the
        # schema block — only schema-shaped classes do.
        bundle = self._bundle(
            [
                (
                    "backtest.py",
                    "class BacktestRunner:\n"
                    "    def __init__(self, x):\n"
                    "        self.x = x\n"
                    "    def run(self):\n"
                    "        return 1\n",
                )
            ]
        )
        text = _extract_quant_schema_signatures(bundle, project_type="quant")
        self.assertNotIn("BacktestRunner", text)


# ─── failure_type strict enum + substring fallback removal ──────────────────


class TestFailureTypeStrict(unittest.TestCase):
    def test_coerce_none_passthrough(self) -> None:
        self.assertIsNone(_coerce_review_failure_type(None))

    def test_coerce_empty_string_to_none(self) -> None:
        self.assertIsNone(_coerce_review_failure_type(""))
        self.assertIsNone(_coerce_review_failure_type("   "))

    def test_coerce_known_marker_normalised(self) -> None:
        self.assertEqual(
            _coerce_review_failure_type("quality_loop_gave_up"),
            "QUALITY_LOOP_GAVE_UP",
        )
        self.assertEqual(
            _coerce_review_failure_type("QUALITY_LOOP_GAVE_UP"),
            "QUALITY_LOOP_GAVE_UP",
        )

    def test_coerce_typo_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _coerce_review_failure_type("QUALITY_LOOP_GIVE_UP")  # typo
        self.assertIn("QUALITY_LOOP_GAVE_UP", str(ctx.exception))

    def test_coerce_random_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            _coerce_review_failure_type("something_else")

    def test_coerce_enum_instance_accepted(self) -> None:
        out = _coerce_review_failure_type(FailureType.QUALITY_LOOP_GAVE_UP)
        self.assertEqual(out, "QUALITY_LOOP_GAVE_UP")

    def test_review_report_validates_at_construct_time(self) -> None:
        # Constructing with a typo raises a Pydantic validation error, NOT a
        # silent acceptance.
        with self.assertRaises(Exception):
            ReviewReport(
                passes=False,
                summary="x",
                issues=[],
                failure_type="QUALITY_LOOP_GIVE_UP",  # typo
            )

    def test_review_report_normalises_lowercase_marker(self) -> None:
        report = ReviewReport(
            passes=False,
            summary="x",
            issues=[],
            failure_type="quality_loop_gave_up",
        )
        self.assertEqual(report.failure_type, "QUALITY_LOOP_GAVE_UP")

    def test_allowed_set_is_subset_of_failure_type_enum(self) -> None:
        # Drift guard: every allowed value MUST also be a member of FailureType.
        enum_values = {m.value for m in FailureType}
        leftovers = _REVIEW_REPORT_ALLOWED_FAILURE_TYPES - enum_values
        self.assertEqual(
            leftovers,
            set(),
            msg=(
                "_REVIEW_REPORT_ALLOWED_FAILURE_TYPES drifted away from "
                f"FailureType. Orphans: {leftovers}"
            ),
        )


# ─── Cross-reference W001 / W002 ────────────────────────────────────────────


class TestEscapePathWarnings(unittest.TestCase):
    def test_w001_kwargs_unpack_emits_medium(self) -> None:
        files = [
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
                "def make(d):\n"
                "    return Trade(**d)\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        warnings = [i for i in report.issues if i.rule == "W001-kwargs-unpack-skipped-check"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].severity, "medium")

    def test_w001_does_not_fire_for_explicit_kwargs(self) -> None:
        files = [
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
                "    return Trade(symbol='BTC', qty=1.0)\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        warnings = [i for i in report.issues if i.rule == "W001-kwargs-unpack-skipped-check"]
        self.assertEqual(warnings, [])

    def test_w002_getattr_unverifiable_no_default(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return getattr(config, 'NONEXISTENT')\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        warnings = [i for i in report.issues if i.rule == "W002-getattr-dynamic-attr-unverifiable"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].severity, "high")  # no default → AttributeError

    def test_w002_getattr_with_default_drops_to_medium(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return getattr(config, 'NONEXISTENT', 0)\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        warnings = [i for i in report.issues if i.rule == "W002-getattr-dynamic-attr-unverifiable"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].severity, "medium")

    def test_w002_silent_for_known_attribute(self) -> None:
        files = [
            (
                "config.py",
                "class Config:\n"
                "    POSITION_SIZE = 100\n",
            ),
            (
                "strategy.py",
                "import config\n"
                "def go():\n"
                "    return getattr(config, 'POSITION_SIZE')\n",
            ),
        ]
        report = analyse_cross_references_from_files(files)
        warnings = [i for i in report.issues if i.rule == "W002-getattr-dynamic-attr-unverifiable"]
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
