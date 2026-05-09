# ruff: noqa: E402
"""Regression tests for v1.0.5 round 4 cost surfacing.

Three layers under test
-----------------------
1. **section_07.save_project_output**: must promote ``total_cost`` /
   ``total_cost_usd`` / ``total_tokens`` / ``cost_source`` from
   ``run_snapshot.cost_summary`` (or the live cost accountant) into
   the top level of ``run_meta.json``.

2. **webui._extract_run_row**: must read those promoted fields into
   the ``cost`` / ``tokens`` columns, prefer ``total_cost_usd`` over
   the legacy ``total_cost`` units, and fall back to
   ``run_snapshot.cost_summary`` for older saved_projects/ that
   predate the promotion.

3. **frontend precision**: ``app.js`` and ``app.py`` round/format cost
   to 6 decimals to match cost_tracker's persistence precision.

Why each layer matters
----------------------
Pre-fix, the dashboard displayed $0.00 for every saved run because
``run_meta.json`` had no cost field at all.  OpenRouter per-call costs
reach the 6th decimal place (e.g. $0.000003 for cached cheap-model
tokens) — any rounding tighter than 6 silently truncates real money to
$0 once summed across many runs.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — section_07 cost promotion
# ─────────────────────────────────────────────────────────────────────────────


class TestSaveProjectOutputCostPromotion(unittest.TestCase):
    """``save_project_output`` must write cost fields into run_meta.json."""

    def setUp(self) -> None:
        from crucible.modules import section_07_selfcheck_output_main as s07
        from crucible.modules.section_03_models_and_context import RunSnapshot

        self.s07 = s07
        self.RunSnapshot = RunSnapshot

    def _build_snapshot(self, **cost_summary: Any) -> Any:
        snap = self.RunSnapshot(run_id="test-run-id", mode="Quant")
        snap.cost_summary = dict(cost_summary)
        return snap

    def test_run_meta_promotes_total_cost_usd_from_snapshot(self) -> None:
        """A non-zero USD cost must reach run_meta.json with full precision."""
        # Use a value at the 6th decimal — the exact precision that prior
        # rounding-to-5 was silently truncating to $0.
        per_call_usd = 0.000003
        n_calls = 7
        expected_total = per_call_usd * n_calls  # = 0.000021

        snap = self._build_snapshot(
            total_cost_usd=expected_total,
            total_cost=12.5,
            total_tokens=1234,
            cost_source="openrouter_api",
            total_executions=n_calls,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.s07, "_REPO_ROOT", tmp):
                self.s07.save_project_output(
                    result=None,
                    code=None,
                    review=None,
                    runtime_log=None,
                    run_meta={"mode": "Quant", "llm_provider": "openrouter"},
                    run_snapshot=snap,
                )
            # Locate the written run_meta.json (timestamped subdir).
            saved_root = Path(tmp) / "saved_projects"
            run_dirs = [p for p in saved_root.iterdir() if p.is_dir()]
            self.assertEqual(len(run_dirs), 1, msg=str(run_dirs))
            meta = json.loads((run_dirs[0] / "run_meta.json").read_text(encoding="utf-8"))
        # Full precision preserved — no rounding below the 6th decimal.
        self.assertAlmostEqual(meta["total_cost_usd"], expected_total, places=12)
        self.assertEqual(meta["total_cost"], 12.5)
        self.assertEqual(meta["total_tokens"], 1234)
        self.assertEqual(meta["cost_source"], "openrouter_api")

    def test_run_meta_promotes_zero_usd_when_cost_source_is_estimated(self) -> None:
        """Cost = 0.0 with cost_source='estimated' must still be written.

        Truthful $0 is correct when OpenRouter did not return a cost and
        the model was not in the local pricing table — the dashboard
        should render $0.00, not silently fall back to a misleading
        cost-units value.
        """
        snap = self._build_snapshot(
            total_cost_usd=0.0,
            total_cost=8523.36,
            total_tokens=8500000,
            cost_source="estimated",
            total_executions=41,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.s07, "_REPO_ROOT", tmp):
                self.s07.save_project_output(
                    result=None,
                    code=None,
                    run_meta={"mode": "Quant"},
                    run_snapshot=snap,
                )
            saved_root = Path(tmp) / "saved_projects"
            run_dirs = list(saved_root.iterdir())
            meta = json.loads((run_dirs[0] / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["total_cost_usd"], 0.0)
        self.assertEqual(meta["total_cost"], 8523.36)
        self.assertEqual(meta["total_tokens"], 8500000)
        self.assertEqual(meta["cost_source"], "estimated")

    def test_existing_run_meta_cost_keys_are_not_overwritten(self) -> None:
        """If the caller already populated cost in run_meta, leave it alone.

        Mirrors the ``setdefault`` pattern used by the quality_passed
        promotion so explicit caller overrides survive.
        """
        snap = self._build_snapshot(
            total_cost_usd=99.0,
            total_tokens=99,
            cost_source="openrouter_api",
            total_executions=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.s07, "_REPO_ROOT", tmp):
                self.s07.save_project_output(
                    result=None,
                    code=None,
                    run_meta={
                        "mode": "Quant",
                        "total_cost_usd": 0.000123,  # caller's value
                        "total_tokens": 7777,
                    },
                    run_snapshot=snap,
                )
            saved_root = Path(tmp) / "saved_projects"
            run_dirs = list(saved_root.iterdir())
            meta = json.loads((run_dirs[0] / "run_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["total_cost_usd"], 0.000123)
        self.assertEqual(meta["total_tokens"], 7777)

    def test_run_meta_skips_promotion_when_no_snapshot_and_empty_accountant(self) -> None:
        """If neither a snapshot nor a non-empty accountant is available,
        run_meta.json must NOT gain phantom cost keys with 0.0 — that
        would mask a real "no data" state with a misleading $0.00.
        """
        # Reset the global cost accountant so its summary returns the
        # zero-execution skeleton.
        from crucible.modules.section_03_models_and_context import reset_cost_accountant
        reset_cost_accountant()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(self.s07, "_REPO_ROOT", tmp):
                self.s07.save_project_output(
                    result=None,
                    code=None,
                    run_meta={"mode": "Quant"},
                    run_snapshot=None,
                )
            saved_root = Path(tmp) / "saved_projects"
            run_dirs = list(saved_root.iterdir())
            meta = json.loads((run_dirs[0] / "run_meta.json").read_text(encoding="utf-8"))
        # The 4 cost keys must be absent — no false "$0" reading.
        for key in ("total_cost_usd", "total_cost", "total_tokens", "cost_source"):
            self.assertNotIn(key, meta, msg=f"phantom {key!r} key in run_meta")


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — webui extraction
# ─────────────────────────────────────────────────────────────────────────────


class TestExtractRunRowCostFields(unittest.TestCase):
    """``_extract_run_row`` must read the new cost fields, prefer USD over
    the legacy units, and fall back to run_snapshot for older runs."""

    def setUp(self) -> None:
        from webui import app as webui
        self.webui = webui

    def _make_run(
        self,
        tmp_root: Path,
        run_id: str,
        meta: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> Path:
        d = tmp_root / run_id
        d.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            (d / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if snapshot is not None:
            (d / "run_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        return d

    def test_prefers_total_cost_usd_over_legacy_total_cost_units(self) -> None:
        """When both fields are present, USD wins — the legacy field is a
        token-derived synthetic units value, not real USD billing."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_run(
                Path(tmp), "run_a",
                meta={
                    "total_cost_usd": 0.000123,
                    "total_cost": 8523.36,  # legacy units
                    "total_tokens": 8500000,
                },
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertEqual(row["cost"], 0.000123)
        self.assertEqual(row["tokens"], 8500000)

    def test_preserves_full_precision_at_sixth_decimal(self) -> None:
        """A per-call OpenRouter cost at the 6th decimal must survive
        end-to-end with no precision loss at the extraction layer."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_run(
                Path(tmp), "run_precise",
                meta={"total_cost_usd": 0.000003},
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertAlmostEqual(row["cost"], 0.000003, places=12)

    def test_falls_back_to_run_snapshot_for_legacy_runs(self) -> None:
        """Older saved_projects/ that predate the run_meta promotion must
        still surface real cost via the run_snapshot.json fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_run(
                Path(tmp), "run_legacy",
                meta={"mode": "Quant"},  # no cost keys at all
                snapshot={
                    "cost_summary": {
                        "total_cost_usd": 0.000456,
                        "total_cost": 1500.0,
                        "total_tokens": 1500000,
                        "cost_source": "openrouter_api",
                    },
                },
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertAlmostEqual(row["cost"], 0.000456, places=12)
        self.assertEqual(row["tokens"], 1500000)

    def test_row_cost_is_none_when_no_data_present(self) -> None:
        """No meta cost + no snapshot → row.cost stays None so the
        dashboard renders "—" instead of a misleading $0.00."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_run(
                Path(tmp), "run_blank",
                meta={"mode": "Quant"},
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertIsNone(row["cost"])
        self.assertIsNone(row["tokens"])

    def test_zero_usd_with_legacy_units_present_resolves_to_zero(self) -> None:
        """When total_cost_usd is explicitly 0.0 (e.g. cost_source='estimated'
        with no pricing table entry), the dashboard must show 0.0 rather
        than silently fall through to the misleading legacy units value.

        This mirrors the saved_projects/ state for the user's three
        existing runs after migration: USD=0.0 (model not priced),
        legacy=8523.36 (token units).  Showing $8523.36 in the "Total
        Cost (USD)" column would be flat-out wrong.
        """
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._make_run(
                Path(tmp), "run_estimated",
                meta={"total_cost_usd": 0.0, "total_cost": 8523.36},
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertEqual(row["cost"], 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — frontend / API precision
# ─────────────────────────────────────────────────────────────────────────────


class TestDashboardApiPrecision(unittest.TestCase):
    """The /api/dashboard endpoint must round to 6 decimals (matches
    cost_tracker's persistence precision) and expose total_cost_usd."""

    def test_api_dashboard_rounds_to_six_decimals(self) -> None:
        from webui import app as webui

        # Patch _scan_saved_runs so the test does not depend on the live
        # saved_projects/ dir and we can drive the math deterministically.
        fake_runs = [
            {"id": "r1", "cost": 0.000003, "quality": 80.0,
             "name": "r1", "mtime": 1.0, "tokens": 1000, "mode": "Quant",
             "provider": "openrouter", "timestamp": "t", "has_backtest": 0,
             "sharpe": None, "drawdown": None, "total_return": None,
             "quality_passed": True, "quality_loop_failure_type": None},
            {"id": "r2", "cost": 0.000004, "quality": 70.0,
             "name": "r2", "mtime": 2.0, "tokens": 2000, "mode": "Quant",
             "provider": "openrouter", "timestamp": "t", "has_backtest": 0,
             "sharpe": None, "drawdown": None, "total_return": None,
             "quality_passed": True, "quality_loop_failure_type": None},
        ]
        with mock.patch.object(webui, "_scan_saved_runs", return_value=fake_runs):
            client = webui.app.test_client()
            resp = client.get("/api/dashboard")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # 0.000003 + 0.000004 = 0.000007 — must NOT round to 0.0
        self.assertEqual(body["total_cost"], 0.000007)
        self.assertEqual(body["total_cost_usd"], 0.000007)


class TestFrontendCostPrecision(unittest.TestCase):
    """app.js must read meta.total_cost_usd (with legacy fallback) and
    render with toFixed(6) — toFixed(4) / toFixed(5) silently truncated
    real per-call OpenRouter cost to $0.0000."""

    def setUp(self) -> None:
        # Read app.js directly — the WebUI integration test fixture handles
        # the Flask test_client variant.  Here we just confirm the source
        # does not regress the precision contract.
        self.app_js = (ROOT / "webui" / "static" / "js" / "app.js").read_text(
            encoding="utf-8"
        )

    def test_dashboard_stat_card_uses_six_decimal_cost(self) -> None:
        # The widget reading data.total_cost_usd / data.total_cost must
        # toFixed(6).  Pre-fix it was toFixed(4).
        self.assertIn("toFixed(6)", self.app_js)
        # Negative pin: ensure the prior bug is not re-introduced for
        # the dashboard stat card or the runs table cost cell.
        self.assertNotIn("toFixed(4) : '—'", self.app_js)

    def test_app_js_prefers_total_cost_usd_over_total_cost(self) -> None:
        """Frontend must check data.total_cost_usd before data.total_cost
        so legacy cost-units values never get shown as USD."""
        self.assertIn("total_cost_usd", self.app_js)
        # The runs table / stat card / detail modal all participate in
        # the USD-priority pattern — at least one site must explicitly
        # `meta.total_cost_usd != null ? meta.total_cost_usd : ...`
        # (allow optional surrounding parentheses around the test).
        self.assertRegex(
            self.app_js,
            r"\(?\s*meta\.total_cost_usd\s*!=\s*null\s*\)?\s*\?\s*meta\.total_cost_usd\s*:",
        )


if __name__ == "__main__":
    unittest.main()
