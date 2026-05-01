"""
features/prompt_version_tracker.py
=====================================
Prompt version management and performance tracking for the Crucible pipeline.

Records a versioned history of prompt variants (from A/B tests, manual edits,
or automated tuning) and links each version to its observed analysis quality
metrics.  Enables evidence-based prompt improvement over time.

Storage
-------
SQLite database at ``{workspace_dir}/prompt_versions.db``.

Schema
------
Two tables:

``prompt_versions``
    One row per registered prompt version.  Includes version ID, label,
    variant context text, creation timestamp, and free-text notes.

``prompt_scores``
    One row per run that used a tracked prompt version.  Links a
    ``version_id`` to a run's quality metrics (score, risk_level,
    gate_decision, blocking_risk_count).

Promotion
---------
``get_best_version()`` returns the version with the highest mean analysis
score over its recorded runs.  Use this to automatically adopt the best-
performing prompt variant.

Usage::

    from crucible.features.prompt_version_tracker import PromptVersionTracker

    tracker = PromptVersionTracker("/path/to/workspace")

    # Register a new variant after crafting it
    vid = tracker.register_version(
        label="risk-focused-v2",
        variant_context="Emphasise irreversible risks and worst-case scenarios.",
        notes="Trying harder emphasis on kill-criteria",
    )

    # After a run completes, record its metrics
    tracker.record_run_score(
        version_id=vid,
        run_id="20240401_120000_my_project",
        score=78.5,
        risk_level="medium",
        gate_decision="proceed",
        blocking_risk_count=1,
    )

    # Find the best-performing version
    best = tracker.get_best_version()
    if best:
        print(f"Best: {best['label']}  avg_score={best['avg_score']:.1f}")

    # Print a performance table
    print(tracker.summary_text())
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_int(v: Any) -> Optional[int]:
    """Convert *v* to int, returning None on any conversion failure."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ── Schema ────────────────────────────────────────────────────────────────────

_CREATE_VERSIONS = """\
CREATE TABLE IF NOT EXISTS prompt_versions (
    version_id    TEXT PRIMARY KEY,
    label         TEXT NOT NULL DEFAULT '',
    variant_context TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_SCORES = """\
CREATE TABLE IF NOT EXISTS prompt_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      TEXT NOT NULL,
    run_id          TEXT NOT NULL DEFAULT '',
    score           REAL,
    risk_level      TEXT,
    gate_decision   TEXT,
    blocking_risk_count INTEGER,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (version_id) REFERENCES prompt_versions(version_id)
)
"""

_UPSERT_VERSION = """\
INSERT INTO prompt_versions (version_id, label, variant_context, notes, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(version_id) DO UPDATE SET
    label=excluded.label,
    variant_context=excluded.variant_context,
    notes=excluded.notes
"""

_INSERT_SCORE = """\
INSERT INTO prompt_scores
    (version_id, run_id, score, risk_level, gate_decision,
     blocking_risk_count, recorded_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class PromptVersion:
    """One registered prompt version."""
    version_id: str
    label: str
    variant_context: str
    notes: str
    created_at: str


@dataclass
class PromptRunScore:
    """One recorded run score for a prompt version."""
    version_id: str
    run_id: str
    score: Optional[float]
    risk_level: Optional[str]
    gate_decision: Optional[str]
    blocking_risk_count: Optional[int]
    recorded_at: str


# ── Tracker ───────────────────────────────────────────────────────────────────

class PromptVersionTracker:
    """
    SQLite-backed prompt version registry with per-run score recording.

    Thread-safe within a single process.  Not safe for concurrent
    multi-process write access.
    """

    def __init__(self, workspace_dir: str) -> None:
        self._workspace_dir = str(workspace_dir)
        self._db_path = os.path.join(workspace_dir, "prompt_versions.db")
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ── Connection management ─────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(_CREATE_VERSIONS)
                conn.execute(_CREATE_SCORES)
                conn.commit()
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ── Version management ────────────────────────────────────────────────────

    def register_version(
        self,
        *,
        label: str,
        variant_context: str = "",
        notes: str = "",
        version_id: Optional[str] = None,
    ) -> str:
        """
        Register a new prompt version (or update an existing one by ID).

        Parameters
        ----------
        label:
            Short human-readable name (e.g. ``"risk-focused-v2"``).
        variant_context:
            The extra context string injected into the pipeline for this variant.
        notes:
            Free-text notes about this version.
        version_id:
            Explicit ID string.  If omitted, a timestamp-based ID is generated.

        Returns
        -------
        str
            The ``version_id`` assigned to this version.
        """
        if not version_id:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:24]
            version_id = f"pv_{ts}_{safe_label}"

        created_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._get_conn()
            conn.execute(_UPSERT_VERSION, (
                version_id, str(label), str(variant_context),
                str(notes), created_at,
            ))
            conn.commit()

        return version_id

    def get_version(self, version_id: str) -> Optional[PromptVersion]:
        """Return the PromptVersion for *version_id*, or None."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT version_id, label, variant_context, notes, created_at "
                "FROM prompt_versions WHERE version_id=?",
                (version_id,),
            ).fetchone()
        if row is None:
            return None
        return PromptVersion(
            version_id=row[0], label=row[1], variant_context=row[2],
            notes=row[3], created_at=row[4],
        )

    def list_versions(self, *, limit: int = 50) -> List[PromptVersion]:
        """Return all registered versions ordered by creation time (newest first)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT version_id, label, variant_context, notes, created_at "
                "FROM prompt_versions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            PromptVersion(
                version_id=r[0], label=r[1], variant_context=r[2],
                notes=r[3], created_at=r[4],
            )
            for r in rows
        ]

    # ── Score recording ───────────────────────────────────────────────────────

    def record_run_score(
        self,
        *,
        version_id: str,
        run_id: str = "",
        score: Optional[float] = None,
        risk_level: Optional[str] = None,
        gate_decision: Optional[str] = None,
        blocking_risk_count: Optional[int] = None,
    ) -> None:
        """
        Record the quality metrics of a run that used *version_id*.

        Parameters
        ----------
        version_id:
            ID of the prompt version used in the run.
        run_id:
            Run directory name (from saved_projects/).
        score:
            Analysis score (0-100).
        risk_level:
            Risk classification string.
        gate_decision:
            Gate Controller decision (e.g. ``"proceed"``).
        blocking_risk_count:
            Number of blocking risks identified.
        """
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            conn.execute(_INSERT_SCORE, (
                version_id, str(run_id), score, risk_level,
                gate_decision, blocking_risk_count, recorded_at,
            ))
            conn.commit()

    def get_scores_for_version(
        self, version_id: str, *, limit: int = 50
    ) -> List[PromptRunScore]:
        """Return all recorded run scores for *version_id* (newest first)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT version_id, run_id, score, risk_level, gate_decision, "
                "blocking_risk_count, recorded_at "
                "FROM prompt_scores WHERE version_id=? "
                "ORDER BY recorded_at DESC LIMIT ?",
                (version_id, limit),
            ).fetchall()
        return [
            PromptRunScore(
                version_id=r[0], run_id=r[1], score=r[2],
                risk_level=r[3], gate_decision=r[4],
                blocking_risk_count=r[5], recorded_at=r[6],
            )
            for r in rows
        ]

    # ── Analytics ─────────────────────────────────────────────────────────────

    def get_performance_stats(self) -> List[Dict[str, Any]]:
        """
        Return per-version performance statistics.

        Each dict contains:
        ``version_id``, ``label``, ``run_count``, ``avg_score``,
        ``max_score``, ``min_score``, ``proceed_rate``.
        """
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                """
                SELECT
                    pv.version_id,
                    pv.label,
                    COUNT(ps.id)            AS run_count,
                    AVG(ps.score)           AS avg_score,
                    MAX(ps.score)           AS max_score,
                    MIN(ps.score)           AS min_score,
                    SUM(CASE WHEN LOWER(ps.gate_decision)='proceed'
                             THEN 1 ELSE 0 END) AS proceed_count
                FROM prompt_versions pv
                LEFT JOIN prompt_scores ps USING (version_id)
                GROUP BY pv.version_id
                ORDER BY avg_score IS NULL, avg_score DESC
                """,
            ).fetchall()

        stats: List[Dict[str, Any]] = []
        for r in rows:
            run_count = r[2] or 0
            proceed_count = r[6] or 0
            proceed_rate = (proceed_count / run_count) if run_count > 0 else None
            stats.append({
                "version_id": r[0],
                "label": r[1],
                "run_count": run_count,
                "avg_score": round(float(r[3]), 2) if r[3] is not None else None,
                "max_score": round(float(r[4]), 2) if r[4] is not None else None,
                "min_score": round(float(r[5]), 2) if r[5] is not None else None,
                "proceed_rate": round(proceed_rate, 2) if proceed_rate is not None else None,
            })
        return stats

    def get_best_version(self) -> Optional[Dict[str, Any]]:
        """
        Return the version with the highest mean analysis score.

        Only considers versions with at least one recorded run score.
        Returns None when no scores have been recorded.
        """
        stats = [s for s in self.get_performance_stats() if s["run_count"] > 0]
        if not stats:
            return None
        # Sort by avg_score DESC (treat None as -1)
        stats.sort(key=lambda s: s["avg_score"] if s["avg_score"] is not None else float("-inf"), reverse=True)
        return stats[0]

    def import_from_ab_report(
        self,
        ab_report_path: str,
        *,
        register_if_missing: bool = True,
    ) -> int:
        """
        Import scores from an A/B test report JSON file.

        Reads ``{output_dir}/ab_test_report.json`` produced by
        ``features/prompt_ab_test.py`` and records scores for each variant.

        Parameters
        ----------
        ab_report_path:
            Path to ``ab_test_report.json``.
        register_if_missing:
            If True (default), auto-register a version for each variant label
            if it doesn't already exist.

        Returns
        -------
        int
            Number of score records inserted.
        """
        if not os.path.isfile(ab_report_path):
            return 0
        try:
            with open(ab_report_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return 0

        inserted = 0
        for key in ("variant_a", "variant_b"):
            variant = data.get(key) or {}
            if not isinstance(variant, dict):
                continue
            label = str(variant.get("label") or key)
            score = variant.get("score")
            run_id = str(variant.get("run_dir") or "")
            risk_level = variant.get("risk_level")
            gate_decision = variant.get("gate_decision")
            blocking_risks = variant.get("blocking_risks_count")

            # Find or register version
            versions = self.list_versions()
            vid: Optional[str] = None
            for v in versions:
                if v.label == label:
                    vid = v.version_id
                    break
            if vid is None and register_if_missing:
                vid = self.register_version(label=label)

            if vid:
                try:
                    score_float = float(score) if score is not None else None
                except (TypeError, ValueError):
                    score_float = None
                self.record_run_score(
                    version_id=vid,
                    run_id=os.path.basename(run_id) if run_id else "",
                    score=score_float,
                    risk_level=risk_level,
                    gate_decision=gate_decision,
                    blocking_risk_count=(
                        _safe_int(blocking_risks)
                        if blocking_risks is not None
                        else None
                    ),
                )
                inserted += 1

        return inserted

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary_text(self) -> str:
        """Human-readable performance table for all prompt versions."""
        stats = self.get_performance_stats()
        if not stats:
            return "Prompt Version Tracker: no versions registered yet."

        lines = [
            "Prompt Version Performance",
            f"{'Label':<30} {'Runs':>5} {'AvgScore':>9} {'MaxScore':>9} {'ProceedRate':>12}",
            "-" * 70,
        ]
        for s in stats:
            avg = f"{s['avg_score']:.1f}" if s["avg_score"] is not None else "N/A"
            mx = f"{s['max_score']:.1f}" if s["max_score"] is not None else "N/A"
            pr = f"{s['proceed_rate']:.0%}" if s["proceed_rate"] is not None else "N/A"
            lines.append(
                f"{s['label']:<30} {s['run_count']:>5} {avg:>9} {mx:>9} {pr:>12}"
            )
        lines.append("-" * 70)
        return "\n".join(lines)
