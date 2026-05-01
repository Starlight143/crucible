"""
features/run_registry.py
=========================
SQLite-backed run history registry.

Indexes all completed runs in ``saved_projects/`` into a local SQLite
database, enabling cross-run queries: top scores, per-project trends,
risk distribution, etc.

The database file is stored at ``{workspace_dir}/run_registry.db``.

Usage::

    from crucible.features.run_registry import RunRegistry

    registry = RunRegistry("/path/to/workspace")
    registry.sync()  # scan saved_projects/ and index new runs

    top = registry.query_top_runs(limit=10)
    for run in top:
        print(f"{run.project_name}: {run.score}/100")
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    """One indexed run record."""
    run_id: str               # directory name
    run_dir: str              # absolute path
    project_name: str
    score: Optional[float]
    risk_level: Optional[str]
    mode: Optional[str]
    provider: Optional[str]
    timestamp: Optional[str]
    has_security_report: bool = False
    security_passed: bool = True
    has_validation_report: bool = False
    validation_verdict: Optional[str] = None


# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_dir TEXT NOT NULL,
    project_name TEXT NOT NULL DEFAULT '',
    score REAL,
    risk_level TEXT,
    mode TEXT,
    provider TEXT,
    timestamp TEXT,
    has_security_report INTEGER DEFAULT 0,
    security_passed INTEGER DEFAULT 1,
    has_validation_report INTEGER DEFAULT 0,
    validation_verdict TEXT,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_UPSERT = """\
INSERT INTO runs (
    run_id, run_dir, project_name, score, risk_level, mode, provider,
    timestamp, has_security_report, security_passed,
    has_validation_report, validation_verdict
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(run_id) DO UPDATE SET
    run_dir=excluded.run_dir,
    project_name=excluded.project_name,
    score=excluded.score,
    risk_level=excluded.risk_level,
    mode=excluded.mode,
    provider=excluded.provider,
    timestamp=excluded.timestamp,
    has_security_report=excluded.has_security_report,
    security_passed=excluded.security_passed,
    has_validation_report=excluded.has_validation_report,
    validation_verdict=excluded.validation_verdict,
    indexed_at=datetime('now')
"""

_SCHEMA_VERSION = 3

_CREATE_META_TABLE = """\
CREATE TABLE IF NOT EXISTS _schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_MIGRATE_SCHEMA = """\
INSERT OR IGNORE INTO _schema_meta (key, value) VALUES ('schema_version', '2')
"""

# ── v3: Run-tags table ────────────────────────────────────────────────────────

_CREATE_TAGS_TABLE = """\
CREATE TABLE IF NOT EXISTS run_tags (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, tag)
)
"""

_CREATE_TAGS_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_run_tags_tag ON run_tags(tag)
"""


# ── Registry ─────────────────────────────────────────────────────────────────

class RunRegistry:
    """
    SQLite-backed registry for pipeline run history.

    Thread-safe via per-instance Lock.
    """

    def __init__(self, workspace_dir: str) -> None:
        self._workspace_dir = str(workspace_dir)
        self._db_path = os.path.join(workspace_dir, "run_registry.db")
        self._saved_dir = os.path.join(workspace_dir, "saved_projects")
        self._conn: Optional[sqlite3.Connection] = None
        self._lock: threading.Lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        # Ensure parent directory exists; sqlite3 cannot create it automatically.
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_META_TABLE)
            conn.execute(_MIGRATE_SCHEMA)
            conn.commit()
            # ── v3 migration: add run_tags table ──────────────────────────
            try:
                conn.execute(_CREATE_TAGS_TABLE)
                conn.execute(_CREATE_TAGS_INDEX)
                conn.execute(
                    "INSERT OR REPLACE INTO _schema_meta (key, value) "
                    "VALUES ('schema_version', '3')"
                )
                conn.commit()
            except Exception as _v3_exc:
                # Log the failure rather than silently swallowing it.
                # Without this the run_tags table may be absent, causing
                # all subsequent add_tag/get_tags calls to raise
                # OperationalError with no indication of root cause.
                conn.rollback()
                import warnings as _warn_mod
                _warn_mod.warn(
                    f"RunRegistry: v3 schema migration failed — "
                    f"run_tags table may be absent: {_v3_exc}",
                    RuntimeWarning,
                    stacklevel=3,
                )
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

    # ── Indexing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _load_json(path: str) -> Dict[str, Any]:
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _index_run(self, run_dir: str) -> None:
        """Read run artifacts and upsert into the database."""
        run_id = os.path.basename(run_dir)
        analysis = self._load_json(os.path.join(run_dir, "analysis_result.json"))
        meta = self._load_json(os.path.join(run_dir, "run_meta.json"))

        project_name = str(
            analysis.get("project_name")
            or meta.get("project_name")
            or run_id
        ).strip()

        score: Optional[float] = None
        raw_score = analysis.get("score")
        if raw_score is not None:
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                pass

        risk_level = analysis.get("risk_level")
        mode = meta.get("mode") or analysis.get("mode_used")
        provider = meta.get("llm_provider")
        timestamp = meta.get("timestamp")

        # Security
        sec_path = os.path.join(run_dir, "security_report.json")
        has_sec = os.path.isfile(sec_path)
        sec_passed = True
        if has_sec:
            sec_data = self._load_json(sec_path)
            raw_passed = sec_data.get("passed", True)
            # bool("false") is True in Python because non-empty strings are truthy.
            # Handle str explicitly so a JSON "false" string is read correctly.
            if isinstance(raw_passed, str):
                sec_passed = raw_passed.strip().lower() not in ("false", "0", "no", "fail", "failed")
            else:
                sec_passed = bool(raw_passed)

        # Validation
        val_path = os.path.join(run_dir, "independent_validation_report.json")
        has_val = os.path.isfile(val_path)
        val_verdict: Optional[str] = None
        if has_val:
            val_data = self._load_json(val_path)
            val_verdict = val_data.get("overall_verdict")

        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(_UPSERT, (
                    run_id, run_dir, project_name, score, risk_level, mode, provider,
                    timestamp, int(has_sec), int(sec_passed),
                    int(has_val), val_verdict,
                ))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise RuntimeError(f"RunRegistry: failed to index run '{run_id}': {exc}") from exc

    def sync(self) -> int:
        """
        Scan ``saved_projects/`` and index all run directories.

        Returns the number of runs indexed (new + updated).
        """
        if not os.path.isdir(self._saved_dir):
            return 0
        count = 0
        try:
            entries = sorted(os.listdir(self._saved_dir))
        except OSError:
            return 0

        for entry in entries:
            full = os.path.join(self._saved_dir, entry)
            if not os.path.isdir(full):
                continue
            # Must have at least analysis_result.json to be a valid run
            if not os.path.isfile(os.path.join(full, "analysis_result.json")):
                continue
            try:
                self._index_run(full)
                count += 1
            except Exception as exc:
                import warnings
                warnings.warn(
                    f"RunRegistry.sync: failed to index '{entry}': {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        return count

    # ── Queries ──────────────────────────────────────────────────────────────

    def _rows_to_records(self, rows: List[tuple]) -> List[RunRecord]:
        records = []
        for row in rows:
            records.append(RunRecord(
                run_id=row[0],
                run_dir=row[1],
                project_name=row[2],
                score=row[3],
                risk_level=row[4],
                mode=row[5],
                provider=row[6],
                timestamp=row[7],
                has_security_report=bool(row[8]),
                security_passed=bool(row[9]),
                has_validation_report=bool(row[10]),
                validation_verdict=row[11],
            ))
        return records

    def query_top_runs(
        self,
        *,
        limit: int = 10,
        project_name: Optional[str] = None,
    ) -> List[RunRecord]:
        """Return top *limit* runs by score (descending)."""
        with self._lock:
            conn = self._get_conn()
            if project_name:
                cursor = conn.execute(
                    "SELECT * FROM runs WHERE LOWER(project_name)=LOWER(?) "
                    "AND score IS NOT NULL ORDER BY score DESC LIMIT ?",
                    (project_name, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM runs WHERE score IS NOT NULL "
                    "ORDER BY score DESC LIMIT ?",
                    (limit,),
                )
            return self._rows_to_records(cursor.fetchall())

    def query_recent_runs(self, *, limit: int = 10) -> List[RunRecord]:
        """Return *limit* most recent runs by timestamp."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return self._rows_to_records(cursor.fetchall())

    def query_project_history(
        self,
        project_name: str,
        *,
        limit: int = 20,
    ) -> List[RunRecord]:
        """Return all runs for *project_name*, newest first."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT * FROM runs WHERE LOWER(project_name)=LOWER(?) "
                "ORDER BY timestamp DESC LIMIT ?",
                (project_name, limit),
            )
            return self._rows_to_records(cursor.fetchall())

    def query_failed_security(self, *, limit: int = 20) -> List[RunRecord]:
        """Return runs where security scan failed."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT * FROM runs WHERE has_security_report=1 AND security_passed=0 "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return self._rows_to_records(cursor.fetchall())

    def count_runs(self) -> int:
        """Return total number of indexed runs."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM runs")
            return cursor.fetchone()[0]

    def summary_text(self) -> str:
        """Human-readable summary of the registry."""
        with self._lock:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            if total == 0:
                return "Run Registry: empty (run 'postprocess registry --update' to index)"
            avg_score = conn.execute(
                "SELECT AVG(score) FROM runs WHERE score IS NOT NULL"
            ).fetchone()[0]
            failed_sec = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE has_security_report=1 AND security_passed=0"
            ).fetchone()[0]
        lines = [
            f"Run Registry: {total} run(s) indexed",
            f"  Average score: {avg_score:.1f}" if avg_score is not None else "  Average score: N/A",
            f"  Failed security: {failed_sec}",
        ]
        return "\n".join(lines)

    # ── Tag management ────────────────────────────────────────────────────────

    def add_tag(self, run_id: str, tag: str) -> None:
        """Add *tag* to *run_id*.  Silently ignored if the tag already exists."""
        tag = tag.strip()
        if not tag:
            raise ValueError("Tag must be a non-empty string.")
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO run_tags (run_id, tag) VALUES (?, ?)",
                    (run_id, tag),
                )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise RuntimeError(f"RunRegistry.add_tag failed: {exc}") from exc

    def remove_tag(self, run_id: str, tag: str) -> None:
        """Remove *tag* from *run_id*.  No-op if the tag does not exist."""
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM run_tags WHERE run_id=? AND tag=?",
                    (run_id, tag),
                )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                raise RuntimeError(f"RunRegistry.remove_tag failed: {exc}") from exc

    def get_tags(self, run_id: str) -> List[str]:
        """Return a sorted list of tags for *run_id*."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT tag FROM run_tags WHERE run_id=? ORDER BY tag ASC",
                (run_id,),
            )
            return [row[0] for row in cursor.fetchall()]

    def query_by_tag(self, tag: str, *, limit: int = 50) -> List[RunRecord]:
        """Return up to *limit* RunRecords whose tag set contains *tag*."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(
                """
                SELECT r.* FROM runs r
                JOIN run_tags t ON r.run_id = t.run_id
                WHERE t.tag = ?
                ORDER BY r.timestamp DESC
                LIMIT ?
                """,
                (tag, limit),
            )
            return self._rows_to_records(cursor.fetchall())

    def query_by_tags(
        self,
        tags: List[str],
        *,
        require_all: bool = True,
        limit: int = 50,
    ) -> List[RunRecord]:
        """
        Return RunRecords filtered by multiple tags.

        require_all=True  → run must have ALL listed tags (AND semantics).
        require_all=False → run must have ANY of the listed tags (OR semantics).
        """
        if not tags:
            return []
        with self._lock:
            conn = self._get_conn()
            placeholders = ",".join("?" for _ in tags)
            if require_all:
                # Count matching tags per run; keep only those with all tags present
                cursor = conn.execute(
                    f"""
                    SELECT r.* FROM runs r
                    JOIN run_tags t ON r.run_id = t.run_id
                    WHERE t.tag IN ({placeholders})
                    GROUP BY r.run_id
                    HAVING COUNT(DISTINCT t.tag) = ?
                    ORDER BY r.timestamp DESC
                    LIMIT ?
                    """,
                    (*tags, len(tags), limit),
                )
            else:
                cursor = conn.execute(
                    f"""
                    SELECT DISTINCT r.* FROM runs r
                    JOIN run_tags t ON r.run_id = t.run_id
                    WHERE t.tag IN ({placeholders})
                    ORDER BY r.timestamp DESC
                    LIMIT ?
                    """,
                    (*tags, limit),
                )
            return self._rows_to_records(cursor.fetchall())

    # ── Fuzzy search (TF-IDF token matching) ─────────────────────────────────

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Lowercase and split on non-alphanumeric characters."""
        return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if tok]

    def _build_run_text(self, record: RunRecord, tags: List[str]) -> str:
        """Construct the searchable text blob for a RunRecord."""
        parts = [
            record.project_name or "",
            record.mode or "",
            record.risk_level or "",
            " ".join(tags),
        ]
        return " ".join(p for p in parts if p)

    def search_runs(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: Optional[str] = None,
    ) -> List[RunRecord]:
        """
        Fuzzy-search runs using a simple TF-IDF-style token matching score.

        The searchable text for each run is:
        ``"{project_name} {mode} {risk_level} {tags}"``.

        Parameters
        ----------
        query:  Free-text search string (tokenised on non-alphanumeric chars).
        limit:  Maximum number of results to return (sorted by score desc).
        mode:   If given, only consider runs whose mode matches (case-insensitive).

        Returns
        -------
        List[RunRecord] sorted by relevance (highest first).
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        with self._lock:
            conn = self._get_conn()

            if mode:
                cursor = conn.execute(
                    "SELECT * FROM runs WHERE LOWER(mode)=LOWER(?)", (mode,)
                )
            else:
                cursor = conn.execute("SELECT * FROM runs")

            all_records: List[RunRecord] = self._rows_to_records(cursor.fetchall())

            # Load all tags in one query to avoid N+1
            tags_cursor = conn.execute(
                "SELECT run_id, tag FROM run_tags ORDER BY run_id, tag"
            )
            tags_by_run: Dict[str, List[str]] = {}
            for run_id, tag in tags_cursor.fetchall():
                tags_by_run.setdefault(run_id, []).append(tag)

        # Score each record
        scored: List[tuple] = []  # (score, record)
        max_possible = len(query_tokens)

        for record in all_records:
            tags = tags_by_run.get(record.run_id, [])
            run_text = self._build_run_text(record, tags)
            run_tokens = set(self._tokenize(run_text))

            if not run_tokens:
                continue

            # Token overlap count (TF-IDF approximation: count query tokens in run)
            matched = sum(1 for t in query_tokens if t in run_tokens)
            if matched == 0:
                continue

            score = matched / max_possible   # normalised [0, 1]
            scored.append((score, record))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [rec for _, rec in scored[:limit]]
