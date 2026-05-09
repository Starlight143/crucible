# ruff: noqa: E402
"""Backend tests for v1.0.5 quality-loop status surfacing.

Backend changes (webui/app.py)
------------------------------
1. ``_bootstrap_db_schema`` adds two columns to the ``runs`` SQLite table:
   ``quality_passed`` (INTEGER, 0/1/None) and ``quality_loop_failure_type``
   (TEXT).  ALTER TABLE is idempotent so repeated worker bootstrap on an
   already-migrated DB is a no-op.
2. ``_extract_run_row`` reads ``run_meta.quality_passed`` (canonical,
   v1.0.5 round 2 promotion) and falls back to ``review_report.passes`` /
   ``review_report.failure_type`` for older runs.
3. ``_scan_saved_runs`` (both SQLite + filesystem branches) emits the
   new fields in API responses as JSON booleans (or null) for
   ``quality_passed`` and the strictly-validated enum string for
   ``quality_loop_failure_type``.

These tests pin all three behaviours so a refactor cannot silently
regress the dashboard quality badge.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _import_webui_module():
    """Importing webui.app initialises Flask routes and SQLite paths.

    We snapshot the original constants before the test so we can monkeypatch
    them per-test against an isolated temp directory and restore them in
    ``tearDown``.
    """
    from webui import app as webui_module
    return webui_module


class TestExtractRunRowQualityFields(unittest.TestCase):
    """``_extract_run_row`` reads quality_passed / quality_loop_failure_type."""

    def setUp(self) -> None:
        self.webui = _import_webui_module()

    def _write_run(
        self,
        tmp_root: Path,
        run_id: str,
        meta: dict[str, Any] | None = None,
        review: dict[str, Any] | None = None,
    ) -> Path:
        run_dir = tmp_root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            (run_dir / "run_meta.json").write_text(
                json.dumps(meta), encoding="utf-8"
            )
        if review is not None:
            (run_dir / "review_report.json").write_text(
                json.dumps(review), encoding="utf-8"
            )
        return run_dir

    def test_reads_quality_passed_true_from_run_meta(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = self._write_run(
                tmp_root, "run_a",
                meta={
                    "mode": "Quant",
                    "llm_provider": "openrouter",
                    "timestamp": "20260509_120000_000000",
                    "quality_passed": True,
                },
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertEqual(row["quality_passed"], 1)
        self.assertIsNone(row["quality_loop_failure_type"])

    def test_reads_quality_passed_false_and_failure_type_from_run_meta(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = self._write_run(
                tmp_root, "run_b",
                meta={
                    "mode": "Quant",
                    "quality_passed": False,
                    "quality_loop_failure_type": "QUALITY_LOOP_GAVE_UP",
                },
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertEqual(row["quality_passed"], 0)
        self.assertEqual(row["quality_loop_failure_type"], "QUALITY_LOOP_GAVE_UP")

    def test_falls_back_to_review_report_when_meta_predates_promotion(self) -> None:
        """Older runs whose run_meta.json lacks the structured fields
        must still surface the badge by reading review_report.json."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = self._write_run(
                tmp_root, "run_legacy",
                meta={"mode": "Quant"},  # no quality_passed key at all
                review={
                    "passes": False,
                    "failure_type": "QUALITY_LOOP_GAVE_UP",
                    "summary": "Quality loop exhausted retries.",
                    "issues": [],
                },
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertEqual(row["quality_passed"], 0)
        self.assertEqual(row["quality_loop_failure_type"], "QUALITY_LOOP_GAVE_UP")

    def test_quality_passed_normalises_truthy_strings_from_legacy_meta(self) -> None:
        """Operator-edited meta files may have stringified booleans; we
        accept the canonical truthy/falsy spellings so the badge does not
        silently disappear."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = self._write_run(
                tmp_root, "run_str",
                meta={"mode": "Quant", "quality_passed": "true"},
            )
            row = self.webui._extract_run_row(run_dir)
        self.assertEqual(row["quality_passed"], 1)

    def test_unknown_failure_type_value_is_dropped_not_passed_through(self) -> None:
        """Strict validation parity with backend section_07: only the
        canonical enum value is persisted.  An unknown failure_type
        string is treated as None so the frontend does not render a
        phantom badge for an unrecognised state."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = self._write_run(
                tmp_root, "run_unknown",
                meta={
                    "mode": "Quant",
                    "quality_passed": False,
                    "quality_loop_failure_type": "SOMETHING_NEW_IN_FUTURE_VERSION",
                },
            )
            row = self.webui._extract_run_row(run_dir)
        # quality_passed survived the False; failure_type was rejected.
        self.assertEqual(row["quality_passed"], 0)
        self.assertIsNone(row["quality_loop_failure_type"])

    def test_no_meta_no_review_yields_null_quality_fields(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            run_dir = self._write_run(tmp_root, "run_empty")
            row = self.webui._extract_run_row(run_dir)
        self.assertIsNone(row["quality_passed"])
        self.assertIsNone(row["quality_loop_failure_type"])


class TestSqliteIndexQualityColumns(unittest.TestCase):
    """``_bootstrap_db_schema`` migrates the runs table to include the
    new columns and the migration is idempotent."""

    def setUp(self) -> None:
        self.webui = _import_webui_module()

    def test_bootstrap_adds_quality_columns_on_fresh_db(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "fresh.sqlite3"
            conn = sqlite3.connect(str(db_path))
            try:
                self.webui._bootstrap_db_schema(conn)
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                }
            finally:
                conn.close()
        self.assertIn("quality_passed", cols)
        self.assertIn("quality_loop_failure_type", cols)

    def test_bootstrap_is_idempotent_on_already_migrated_db(self) -> None:
        """Running bootstrap twice on the same DB must not raise
        ``OperationalError: duplicate column`` — idempotency is the
        contract for cross-thread re-bootstrap."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "twice.sqlite3"
            conn = sqlite3.connect(str(db_path))
            try:
                self.webui._bootstrap_db_schema(conn)
                # Second call must not raise.
                self.webui._bootstrap_db_schema(conn)
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                }
            finally:
                conn.close()
        self.assertIn("quality_passed", cols)
        self.assertIn("quality_loop_failure_type", cols)

    def test_bootstrap_migrates_pre_v105_db_in_place(self) -> None:
        """A DB created before v1.0.5 (lacking the two new columns) must
        be migrated by ALTER TABLE without losing any existing rows."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            conn = sqlite3.connect(str(db_path))
            try:
                # Recreate the pre-v1.0.5 schema (no quality columns).
                conn.execute("""
                    CREATE TABLE runs (
                        run_id TEXT PRIMARY KEY,
                        mtime REAL,
                        cost REAL,
                        tokens INTEGER,
                        quality REAL,
                        mode TEXT,
                        provider TEXT,
                        timestamp TEXT,
                        has_backtest INTEGER DEFAULT 0,
                        sharpe REAL,
                        drawdown REAL,
                        total_return REAL,
                        schema_version TEXT
                    )
                """)
                conn.execute(
                    "INSERT INTO runs (run_id, mtime, cost) VALUES (?, ?, ?)",
                    ("legacy_run_1", 1.0, 0.001),
                )
                conn.commit()
                # Apply the v1.0.5 migration.
                self.webui._bootstrap_db_schema(conn)
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(runs)").fetchall()
                }
                # Existing row survives.
                row = conn.execute(
                    "SELECT run_id, quality_passed, quality_loop_failure_type "
                    "FROM runs WHERE run_id = ?",
                    ("legacy_run_1",),
                ).fetchone()
            finally:
                conn.close()
        self.assertIn("quality_passed", cols)
        self.assertIn("quality_loop_failure_type", cols)
        self.assertEqual(row[0], "legacy_run_1")
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])


class TestScanSavedRunsEmitsQualityFields(unittest.TestCase):
    """Both the SQLite and filesystem code paths in ``_scan_saved_runs``
    must include the new fields with consistent JSON-serialisable shape
    (true/false/null — never int)."""

    def setUp(self) -> None:
        self.webui = _import_webui_module()

    def _write_run_dir(
        self,
        root: Path,
        run_id: str,
        meta: dict[str, Any],
        review: dict[str, Any] | None = None,
    ) -> None:
        d = root / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if review is not None:
            (d / "review_report.json").write_text(
                json.dumps(review), encoding="utf-8"
            )

    def test_filesystem_fallback_emits_bool_quality_passed(self) -> None:
        """Force the SQLite path to fail so we exercise the FS fallback.

        We patch ``_sync_run_index`` to raise so ``_scan_saved_runs``
        falls through to the filesystem branch — that branch must emit
        the same JSON shape as the SQLite branch.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            self._write_run_dir(
                tmp_root, "run_pass",
                meta={"mode": "Quant", "quality_passed": True, "timestamp": "t1"},
            )
            self._write_run_dir(
                tmp_root, "run_giveup",
                meta={
                    "mode": "Quant",
                    "quality_passed": False,
                    "quality_loop_failure_type": "QUALITY_LOOP_GAVE_UP",
                    "timestamp": "t2",
                },
            )
            with mock.patch.object(self.webui, "SAVED_PROJECTS_DIR", tmp_root), \
                 mock.patch.object(
                     self.webui, "_sync_run_index",
                     side_effect=RuntimeError("force fs fallback"),
                 ):
                runs = self.webui._scan_saved_runs(limit=10)

        by_id = {r["id"]: r for r in runs}
        self.assertIn("run_pass", by_id)
        self.assertIn("run_giveup", by_id)
        # Strictly typed: must be Python bool (-> JSON true/false), not int.
        self.assertIs(by_id["run_pass"]["quality_passed"], True)
        self.assertIsNone(by_id["run_pass"]["quality_loop_failure_type"])
        self.assertIs(by_id["run_giveup"]["quality_passed"], False)
        self.assertEqual(
            by_id["run_giveup"]["quality_loop_failure_type"],
            "QUALITY_LOOP_GAVE_UP",
        )

    def test_filesystem_fallback_omits_field_for_pre_v105_runs(self) -> None:
        """Older runs without the structured fields must surface as
        ``None`` so the frontend renders no badge (instead of a phantom
        ✗ Failed)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            self._write_run_dir(
                tmp_root, "run_legacy",
                meta={"mode": "Quant", "timestamp": "t0"},
            )
            with mock.patch.object(self.webui, "SAVED_PROJECTS_DIR", tmp_root), \
                 mock.patch.object(
                     self.webui, "_sync_run_index",
                     side_effect=RuntimeError("force fs fallback"),
                 ):
                runs = self.webui._scan_saved_runs(limit=10)
        self.assertEqual(len(runs), 1)
        self.assertIsNone(runs[0]["quality_passed"])
        self.assertIsNone(runs[0]["quality_loop_failure_type"])


if __name__ == "__main__":
    unittest.main()
