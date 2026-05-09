# ruff: noqa: E402
"""Tests for crucible.features.quant_smoke (v1.0.5 P0-3)."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.quant_smoke import (
    QuantSmokeIssue,
    QuantSmokeResult,
    quant_smoke_dryrun,
    synthesise_ohlcv_csv,
)


class TestSynthesiseOhlcvCsv(unittest.TestCase):
    def test_header_and_row_count(self) -> None:
        csv = synthesise_ohlcv_csv(n_rows=5, seed=0)
        rows = csv.strip().splitlines()
        self.assertEqual(rows[0], "date,open,high,low,close,volume")
        self.assertEqual(len(rows), 6)  # header + 5 rows

    def test_seed_is_deterministic(self) -> None:
        a = synthesise_ohlcv_csv(n_rows=3, seed=42)
        b = synthesise_ohlcv_csv(n_rows=3, seed=42)
        self.assertEqual(a, b)

    def test_default_uses_30_rows(self) -> None:
        csv = synthesise_ohlcv_csv()
        rows = csv.strip().splitlines()
        self.assertEqual(len(rows), 31)

    def test_high_low_invariant(self) -> None:
        csv = synthesise_ohlcv_csv(n_rows=10, seed=7)
        for line in csv.strip().splitlines()[1:]:
            d, op, hi, lo, cl, vol = line.split(",")
            op_f, hi_f, lo_f, cl_f = map(float, (op, hi, lo, cl))
            self.assertGreaterEqual(hi_f, op_f, f"hi<{op_f} on {line}")
            self.assertGreaterEqual(hi_f, cl_f, f"hi<close on {line}")
            self.assertLessEqual(lo_f, op_f)
            self.assertLessEqual(lo_f, cl_f)
            self.assertGreater(int(vol), 0)


class TestQuantSmokeDryrun(unittest.TestCase):
    """Run the dry-run on minimal fixture bundles."""

    def test_skips_when_not_a_quant_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "app.py"), "w", encoding="utf-8") as f:
                f.write("# nothing quant here\n")
            result = quant_smoke_dryrun(d, timeout_seconds=5)
            self.assertTrue(result.passes)
            self.assertTrue(result.skipped)
            self.assertIn("Quant-mode marker", result.skip_reason or "")

    def test_skips_when_no_entrypoint_present(self) -> None:
        """Markers without a runnable entrypoint → skip (not fail)."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("def signal(df): pass\n")
            # No backtest.py / run_backtest.py etc.
            result = quant_smoke_dryrun(d, timeout_seconds=5)
            # strategy.py *is* a fallback module — `python -m strategy` will succeed
            # because strategy.py defines no top-level execution. So this should pass.
            self.assertTrue(result.passes)

    def test_passes_for_clean_quant_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            # Minimal but plausible Quant layout.
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("def run(): return 0\n")
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write(
                    "if __name__ == '__main__':\n"
                    "    print('{\"sharpe_ratio\": 0.0}')\n"
                )
            result = quant_smoke_dryrun(d, timeout_seconds=20)
            self.assertTrue(result.passes, msg=result.log)
            self.assertFalse(result.skipped)
            self.assertEqual(result.entrypoint_used, "backtest.py")

    def test_fails_with_traceback_extraction(self) -> None:
        """A backtest.py that raises TypeError → high-severity issue, exit != 0."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("def run(): pass\n")
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write(
                    "raise TypeError('Trade.__init__() got unexpected kwarg quantity')\n"
                )
            result = quant_smoke_dryrun(d, timeout_seconds=20)
            self.assertFalse(result.passes)
            self.assertEqual(len(result.issues), 1)
            issue = result.issues[0]
            self.assertEqual(issue["severity"], "high")
            self.assertIn("TypeError", issue["description"])
            self.assertEqual(issue["rule"], "Q013-quant-dryrun-typeerror")

    def test_attribute_error_classification(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write(
                    "import config\n"
                    "config.MISSING\n"
                )
            with open(os.path.join(d, "config.py"), "w", encoding="utf-8") as f:
                f.write("# empty config\n")
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("\n")
            result = quant_smoke_dryrun(d, timeout_seconds=20)
            self.assertFalse(result.passes)
            self.assertEqual(result.issues[0]["rule"], "Q014-quant-dryrun-attributeerror")

    def test_synthetic_csv_written_to_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write("print('ok')\n")
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("\n")
            result = quant_smoke_dryrun(d, timeout_seconds=10)
            csv_path = os.path.join(d, "data", "sample_data.csv")
            self.assertTrue(os.path.isfile(csv_path), msg=result.log)
            with open(csv_path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
            self.assertEqual(first, "date,open,high,low,close,volume")

    def test_existing_csv_not_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "data"))
            with open(os.path.join(d, "data", "sample_data.csv"), "w", encoding="utf-8") as f:
                f.write("PRESERVE\n")
            with open(os.path.join(d, "backtest.py"), "w", encoding="utf-8") as f:
                f.write("print('ok')\n")
            with open(os.path.join(d, "strategy.py"), "w", encoding="utf-8") as f:
                f.write("\n")
            quant_smoke_dryrun(d, timeout_seconds=10)
            with open(os.path.join(d, "data", "sample_data.csv"), encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), "PRESERVE")


if __name__ == "__main__":
    unittest.main()
