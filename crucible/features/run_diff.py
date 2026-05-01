"""
features/run_diff.py
====================
Side-by-side comparison of two completed run output directories.

Computes:
- Score / risk / confidence deltas (with regression detection)
- New vs. resolved blocking risks
- Direction changes
- Unified diff for every generated code file

A ``comparison_report.json`` is written to *run_b_dir* (the "newer" run).

Usage::

    from crucible.features.run_diff import compare_runs
    report = compare_runs("saved_projects/run_old", "saved_projects/run_new")
    print(report.summary_text())
"""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# ── Data models ───────────────────────────────────────────────────────────────

# Ordered risk levels: higher index = higher risk (worse).
_RISK_ORDER = ["low", "medium", "high", "critical"]


@dataclass
class ScoreDelta:
    field_name: str
    old_value: Any
    new_value: Any

    @property
    def changed(self) -> bool:
        return self.old_value != self.new_value

    @property
    def regressed(self) -> bool:
        """
        True when the value worsened.
        - For numeric fields (score, confidence): decreased is worse.
        - For risk_level: increased rank is worse.
        """
        if not self.changed:
            return False
        # Numeric comparison
        try:
            return float(self.new_value) < float(self.old_value)
        except (TypeError, ValueError):
            pass
        # Risk-level string comparison
        if self.field_name == "risk_level":
            old_lower = str(self.old_value).lower()
            new_lower = str(self.new_value).lower()
            old_rank = _RISK_ORDER.index(old_lower) if old_lower in _RISK_ORDER else -1
            new_rank = _RISK_ORDER.index(new_lower) if new_lower in _RISK_ORDER else -1
            if old_rank >= 0 and new_rank >= 0:
                return new_rank > old_rank
        return False

    @property
    def improved(self) -> bool:
        """
        True when the value improved.
        - For numeric fields: increased is better.
        - For risk_level: decreased rank is better.
        """
        if not self.changed:
            return False
        # Numeric comparison
        try:
            return float(self.new_value) > float(self.old_value)
        except (TypeError, ValueError):
            pass
        # Risk-level string comparison
        if self.field_name == "risk_level":
            old_lower = str(self.old_value).lower()
            new_lower = str(self.new_value).lower()
            old_rank = _RISK_ORDER.index(old_lower) if old_lower in _RISK_ORDER else -1
            new_rank = _RISK_ORDER.index(new_lower) if new_lower in _RISK_ORDER else -1
            if old_rank >= 0 and new_rank >= 0:
                return new_rank < old_rank
        return False


@dataclass
class FileDiff:
    path: str
    status: str            # "added" | "removed" | "modified" | "unchanged"
    diff_lines: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return self.status != "unchanged"


@dataclass
class ComparisonReport:
    run_a_dir: str
    run_b_dir: str
    score_deltas: List[ScoreDelta] = field(default_factory=list)
    new_blocking_risks: List[str] = field(default_factory=list)
    resolved_blocking_risks: List[str] = field(default_factory=list)
    direction_changed: bool = False
    direction_a: Optional[str] = None
    direction_b: Optional[str] = None
    code_diffs: List[FileDiff] = field(default_factory=list)
    regressions: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_a": os.path.basename(self.run_a_dir),
            "run_b": os.path.basename(self.run_b_dir),
            "score_deltas": [
                {
                    "field": d.field_name,
                    "old": d.old_value,
                    "new": d.new_value,
                    "regressed": d.regressed,
                    "improved": d.improved,
                }
                for d in self.score_deltas
                if d.changed
            ],
            "new_blocking_risks": self.new_blocking_risks,
            "resolved_blocking_risks": self.resolved_blocking_risks,
            "direction_changed": self.direction_changed,
            "direction_a": self.direction_a,
            "direction_b": self.direction_b,
            "code_changes": [
                {
                    "path": d.path,
                    "status": d.status,
                    # Truncate large diffs to keep the report readable
                    "diff_lines": d.diff_lines[:100],
                }
                for d in self.code_diffs
                if d.has_changes
            ],
            "regressions": self.regressions,
            "improvements": self.improvements,
        }

    def summary_text(self) -> str:
        a_name = os.path.basename(self.run_a_dir)
        b_name = os.path.basename(self.run_b_dir)
        lines = [
            "Run Comparison",
            f"  Baseline : {a_name}",
            f"  Candidate: {b_name}",
            "",
        ]

        for delta in self.score_deltas:
            if not delta.changed:
                continue
            if delta.regressed:
                lines.append(f"  ▼ {delta.field_name}: {delta.old_value} → {delta.new_value}  [REGRESSION]")
            elif delta.improved:
                lines.append(f"  ▲ {delta.field_name}: {delta.old_value} → {delta.new_value}  [IMPROVEMENT]")
            else:
                lines.append(f"  ~ {delta.field_name}: {delta.old_value} → {delta.new_value}")

        if self.direction_changed:
            lines.append(
                f"\n  ↩ Direction changed: "
                f"{self.direction_a or '?'} → {self.direction_b or '?'}"
            )

        if self.new_blocking_risks:
            lines.append(f"\nNew blocking risks ({len(self.new_blocking_risks)}):")
            for r in self.new_blocking_risks[:5]:
                lines.append(f"  ⛔ {r}")

        if self.resolved_blocking_risks:
            lines.append(f"\nResolved risks ({len(self.resolved_blocking_risks)}):")
            for r in self.resolved_blocking_risks[:5]:
                lines.append(f"  ✓ {r}")

        changed_files = [d for d in self.code_diffs if d.has_changes]
        if changed_files:
            lines.append(f"\nCode changes ({len(changed_files)} file(s)):")
            for d in changed_files[:15]:
                lines.append(f"  [{d.status.upper():8s}] {d.path}")

        if self.regressions:
            lines.append("\nRegressions:")
            for r in self.regressions:
                lines.append(f"  ⚠ {r}")

        if self.improvements:
            lines.append("\nImprovements:")
            for i in self.improvements:
                lines.append(f"  ✓ {i}")

        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


# Directories generated at runtime that produce meaningless diffs.
_IGNORED_CODE_DIRS: Set[str] = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    ".tox", "dist", "build", ".eggs",
}

# Extensions that are known binary — skip these to avoid garbled diffs.
_BINARY_EXTENSIONS: Set[str] = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".whl", ".egg",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp",
    ".pdf", ".docx", ".xlsx", ".db", ".sqlite", ".sqlite3",
}


def _collect_code_files(run_dir: str) -> Dict[str, str]:
    """Return ``{relative_path: content}`` for every text file under ``code/``.

    Skips known-binary extensions and prunes runtime-generated directories
    (``__pycache__``, ``.git``, etc.) so diffs remain meaningful.
    """
    code_dir = os.path.join(run_dir, "code")
    result: Dict[str, str] = {}
    if not os.path.isdir(code_dir):
        return result
    for dirpath, dirnames, filenames in os.walk(code_dir):
        # Prune ignored dirs in-place so os.walk never recurses into them.
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_CODE_DIRS]
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            # Skip files with known binary extensions; no-extension files
            # (Makefile, Dockerfile, etc.) are kept.
            if ext and ext in _BINARY_EXTENSIONS:
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, code_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    result[rel] = fh.read()
            except OSError:
                result[rel] = ""
    return result


def _extract_blocking_risks(analysis: Dict[str, Any]) -> Set[str]:
    risks: Set[str] = set()
    gate_snap = analysis.get("gate_context_snapshot") or {}
    if isinstance(gate_snap, dict):
        for r in gate_snap.get("blocking_risks") or []:
            risks.add(str(r))
    for r in analysis.get("blocking_risks") or []:
        risks.add(str(r))
    return risks


def _compute_code_diffs(
    files_a: Dict[str, str],
    files_b: Dict[str, str],
    context_lines: int = 3,
) -> List[FileDiff]:
    all_paths = sorted(set(files_a) | set(files_b))
    diffs: List[FileDiff] = []
    for path in all_paths:
        in_a = path in files_a
        in_b = path in files_b
        if not in_a:
            diffs.append(FileDiff(path=path, status="added"))
        elif not in_b:
            diffs.append(FileDiff(path=path, status="removed"))
        elif files_a[path] == files_b[path]:
            diffs.append(FileDiff(path=path, status="unchanged"))
        else:
            diff_lines = list(
                difflib.unified_diff(
                    files_a[path].splitlines(keepends=True),
                    files_b[path].splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                    n=context_lines,
                )
            )
            diffs.append(FileDiff(path=path, status="modified", diff_lines=diff_lines))
    return diffs


# ── Main entry point ──────────────────────────────────────────────────────────

def compare_runs(run_a_dir: str, run_b_dir: str) -> ComparisonReport:
    """
    Compare two run output directories.

    *run_a_dir* is the baseline (older); *run_b_dir* is the candidate (newer).
    A ``comparison_report.json`` is saved in *run_b_dir*.

    Returns a ComparisonReport with delta information.
    """
    report = ComparisonReport(run_a_dir=run_a_dir, run_b_dir=run_b_dir)

    analysis_a = _load_json(os.path.join(run_a_dir, "analysis_result.json"))
    analysis_b = _load_json(os.path.join(run_b_dir, "analysis_result.json"))
    snapshot_a = _load_json(os.path.join(run_a_dir, "run_snapshot.json"))
    snapshot_b = _load_json(os.path.join(run_b_dir, "run_snapshot.json"))

    # ── Score deltas ──────────────────────────────────────────────────────────
    for field_name in ("score", "risk_level", "confidence"):
        # Use `in` + direct access rather than `or` to avoid the falsy-zero
        # problem: analysis["score"] = 0 is a valid score but `0 or fallback`
        # would incorrectly fall through to the snapshot value.
        val_a = analysis_a[field_name] if field_name in analysis_a else snapshot_a.get(field_name)
        val_b = analysis_b[field_name] if field_name in analysis_b else snapshot_b.get(field_name)
        delta = ScoreDelta(field_name=field_name, old_value=val_a, new_value=val_b)
        report.score_deltas.append(delta)
        if delta.regressed:
            # Use "worsened" for risk_level (higher risk = worse, not "decreased")
            verb = "worsened" if field_name == "risk_level" else "decreased"
            report.regressions.append(f"{field_name} {verb}: {val_a} → {val_b}")
        elif delta.improved:
            verb = "improved" if field_name == "risk_level" else "increased"
            report.improvements.append(f"{field_name} {verb}: {val_a} → {val_b}")

    # ── Blocking risks ────────────────────────────────────────────────────────
    risks_a = _extract_blocking_risks(analysis_a)
    risks_b = _extract_blocking_risks(analysis_b)

    report.new_blocking_risks = sorted(risks_b - risks_a)
    report.resolved_blocking_risks = sorted(risks_a - risks_b)

    if report.new_blocking_risks:
        report.regressions.append(
            f"{len(report.new_blocking_risks)} new blocking risk(s) introduced."
        )
    if report.resolved_blocking_risks:
        report.improvements.append(
            f"{len(report.resolved_blocking_risks)} blocking risk(s) resolved."
        )

    # ── Direction change ──────────────────────────────────────────────────────
    dd_a = snapshot_a.get("direction_decision") or {}
    dd_b = snapshot_b.get("direction_decision") or {}
    if isinstance(dd_a, dict) and isinstance(dd_b, dict):
        report.direction_a = dd_a.get("selected_direction")
        report.direction_b = dd_b.get("selected_direction")
        report.direction_changed = (
            report.direction_a is not None
            and report.direction_b is not None
            and report.direction_a != report.direction_b
        )

    # ── Code diffs ────────────────────────────────────────────────────────────
    files_a = _collect_code_files(run_a_dir)
    files_b = _collect_code_files(run_b_dir)
    report.code_diffs = _compute_code_diffs(files_a, files_b)

    # ── Persist ───────────────────────────────────────────────────────────────
    report_path = os.path.join(run_b_dir, "comparison_report.json")
    _tmp_report = report_path + ".tmp"
    try:
        with open(_tmp_report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(_tmp_report, report_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_report)
        except OSError:
            pass
        report.regressions.append(f"WARNING: could not write comparison_report.json: {exc}")

    return report
