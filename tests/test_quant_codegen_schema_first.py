# ruff: noqa: E402
"""Tests for v1.0.5 round 2 P1-6 (b)(c): schema-first hoist + signature injection."""
import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.modules.section_03_models_and_context import (
    CodeBundle,
    CodegenBatchPlan,
    CodegenFilePlan,
    CodegenManifest,
    GeneratedFile,
)
from crucible.modules.section_05_analysis_and_codegen import (
    _extract_quant_schema_signatures,
    _normalize_codegen_manifest,
    _quant_hoist_schema_first,
)


class TestQuantSchemaHoist(unittest.TestCase):
    """P1-6 (b): trade.py / config.py must be in batch 0 for Quant manifests."""

    def _file_plan(self, path, depends_on=None):
        return CodegenFilePlan(
            path=path,
            purpose="",
            depends_on=list(depends_on or []),
            must_contain=[],
        )

    def test_hoist_lifts_late_trade_to_first_batch(self) -> None:
        batches = [
            CodegenBatchPlan(name="batch_1", objective="entry", files=["main.py"]),
            CodegenBatchPlan(
                name="batch_2", objective="exec", files=["backtest.py"]
            ),
            CodegenBatchPlan(name="batch_3", objective="schema", files=["trade.py"]),
        ]
        file_map = {
            "main.py": self._file_plan("main.py", ["trade.py"]),
            "backtest.py": self._file_plan("backtest.py", ["trade.py"]),
            "trade.py": self._file_plan("trade.py"),
        }
        out = _quant_hoist_schema_first(
            batches,
            known_paths={"main.py", "backtest.py", "trade.py"},
            file_map=file_map,
            batch_size=4,
        )
        self.assertIn("trade.py", out[0].files)
        # No batch may still hold trade.py beyond the first.
        for b in out[1:]:
            self.assertNotIn("trade.py", b.files)

    def test_hoist_no_op_when_already_first(self) -> None:
        batches = [
            CodegenBatchPlan(
                name="batch_1", objective="schema", files=["trade.py", "config.py"]
            ),
            CodegenBatchPlan(
                name="batch_2", objective="exec", files=["backtest.py"]
            ),
        ]
        file_map = {
            "trade.py": self._file_plan("trade.py"),
            "config.py": self._file_plan("config.py"),
            "backtest.py": self._file_plan("backtest.py", ["trade.py", "config.py"]),
        }
        out = _quant_hoist_schema_first(
            batches,
            known_paths=set(file_map),
            file_map=file_map,
            batch_size=5,
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].files, ["trade.py", "config.py"])

    def test_hoist_inserts_dedicated_batch_when_first_at_capacity(self) -> None:
        # First batch has 3 unrelated files at batch_size=3. Lifting trade.py
        # would exceed the cap → expect a new schema-only batch_0.
        batches = [
            CodegenBatchPlan(
                name="batch_1",
                objective="utils",
                files=["utils_a.py", "utils_b.py", "utils_c.py"],
            ),
            CodegenBatchPlan(name="batch_2", objective="exec", files=["trade.py"]),
        ]
        file_map = {
            "utils_a.py": self._file_plan("utils_a.py"),
            "utils_b.py": self._file_plan("utils_b.py"),
            "utils_c.py": self._file_plan("utils_c.py"),
            "trade.py": self._file_plan("trade.py"),
        }
        out = _quant_hoist_schema_first(
            batches,
            known_paths=set(file_map),
            file_map=file_map,
            batch_size=3,
        )
        self.assertEqual(out[0].name, "batch_schema")
        self.assertEqual(out[0].files, ["trade.py"])
        # The original first batch is preserved (schema removed if it was there).
        self.assertEqual(out[1].name, "batch_1")
        self.assertEqual(out[1].files, ["utils_a.py", "utils_b.py", "utils_c.py"])

    def test_hoist_skips_when_no_schema_in_bundle(self) -> None:
        batches = [
            CodegenBatchPlan(
                name="batch_1", objective="x", files=["main.py", "lib.py"]
            ),
        ]
        out = _quant_hoist_schema_first(
            batches,
            known_paths={"main.py", "lib.py"},
            file_map={
                "main.py": self._file_plan("main.py"),
                "lib.py": self._file_plan("lib.py"),
            },
            batch_size=4,
        )
        self.assertEqual(out, batches)

    def test_normalize_manifest_for_quant_calls_hoist(self) -> None:
        manifest = CodegenManifest(
            project_type="quant",
            architecture_summary="x",
            entrypoints=["backtest.py"],
            shared_constraints=[],
            files=[
                CodegenFilePlan(
                    path="trade.py", purpose="schema", depends_on=[], must_contain=[]
                ),
                CodegenFilePlan(
                    path="backtest.py",
                    purpose="exec",
                    depends_on=["trade.py"],
                    must_contain=[],
                ),
            ],
            generation_batches=[
                CodegenBatchPlan(name="b1", objective="ex", files=["backtest.py"]),
                CodegenBatchPlan(name="b2", objective="sc", files=["trade.py"]),
            ],
        )
        out = _normalize_codegen_manifest(manifest, mode="Quant", llm=None)
        self.assertIsNotNone(out)
        assert out is not None
        # First batch must contain trade.py after normalization.
        self.assertIn("trade.py", out.generation_batches[0].files)


class TestSchemaSignatureExtraction(unittest.TestCase):
    """P1-6 (c): parse already-emitted Trade dataclass fields into a prompt block."""

    def _bundle(self, files):
        return CodeBundle(
            project_type="quant",
            files=[GeneratedFile(path=p, content=c) for p, c in files],
        )

    def test_extracts_dataclass_fields(self) -> None:
        bundle = self._bundle(
            [
                (
                    "trade.py",
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n"
                    "    symbol: str\n"
                    "    side: str\n"
                    "    qty: float\n"
                    "    pnl: float = 0.0\n",
                )
            ]
        )
        text = _extract_quant_schema_signatures(bundle, project_type="quant")
        self.assertIn("Approved schema signatures", text)
        self.assertIn("Trade", text)
        self.assertIn("symbol: str", text)
        self.assertIn("side: str", text)
        self.assertIn("qty: float", text)

    def test_no_op_for_non_quant(self) -> None:
        bundle = self._bundle(
            [
                (
                    "trade.py",
                    "from dataclasses import dataclass\n"
                    "@dataclass\nclass Trade:\n"
                    "    symbol: str\n",
                )
            ]
        )
        self.assertEqual(
            _extract_quant_schema_signatures(bundle, project_type="saas"), ""
        )

    def test_empty_when_no_schema_files(self) -> None:
        bundle = self._bundle([("strategy.py", "x = 1\n")])
        self.assertEqual(
            _extract_quant_schema_signatures(bundle, project_type="quant"), ""
        )

    def test_empty_when_bundle_is_none(self) -> None:
        self.assertEqual(
            _extract_quant_schema_signatures(None, project_type="quant"), ""
        )

    def test_handles_syntax_error_gracefully(self) -> None:
        bundle = self._bundle([("trade.py", "@dataclass\nclass Trade:\n    not valid python")])
        # Should not raise; just returns empty (no parseable schema).
        result = _extract_quant_schema_signatures(bundle, project_type="quant")
        # Either empty or partial — must not raise.
        self.assertIsInstance(result, str)

    def test_extracts_config_class(self) -> None:
        bundle = self._bundle(
            [
                (
                    "config.py",
                    "class Config:\n"
                    "    POSITION_SIZE = 100\n"
                    "    LOOKBACK_DAYS = 30\n",
                )
            ]
        )
        text = _extract_quant_schema_signatures(bundle, project_type="quant")
        self.assertIn("Config", text)
        self.assertIn("POSITION_SIZE", text)
        self.assertIn("LOOKBACK_DAYS", text)


if __name__ == "__main__":
    unittest.main()
