"""
features/agent_metrics.py
===========================
Agent performance metrics dashboard for the Crucible pipeline.

Aggregates historical run data from the SQLite run registry (``run_registry.db``)
and per-run output files to compute per-project and per-pipeline-mode quality
metrics.  Generates both a JSON report and a human-readable terminal summary.

Metrics computed
----------------
Per-project:
  - run_count
  - avg_score / max_score / min_score
  - risk_distribution (count per risk level)
  - gate_pass_rate (fraction with gate_decision == "proceed")
  - avg_blocking_risks
  - hallucination_flag_count (total across all runs)
  - security_pass_rate (fraction where security scan passed)
  - avg_experiments

Trend:
  - last_5_scores (time-ordered, newest first) for sparkline-style view

Global:
  - Overall stats across all projects

Output
------
Results are written to ``{run_dir}/agent_metrics_report.json`` when called
via the feature registry, or returned as a ``AgentMetricsReport`` dataclass
when called programmatically.

Usage::

    from crucible.features.agent_metrics import compute_agent_metrics

    report = compute_agent_metrics("/path/to/workspace")
    print(report.summary_text())

    # Or persist to a file:
    report.save_json("/path/to/workspace/agent_metrics_report.json")
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ProjectMetrics:
    """Aggregated metrics for one project across all indexed runs."""

    project_name: str
    run_count: int
    avg_score: Optional[float]
    max_score: Optional[float]
    min_score: Optional[float]
    last_5_scores: List[Optional[float]] = field(default_factory=list)
    risk_distribution: Dict[str, int] = field(default_factory=dict)
    gate_pass_rate: Optional[float] = None   # fraction with gate == "proceed"
    avg_blocking_risks: Optional[float] = None
    hallucination_flag_count: int = 0
    security_pass_rate: Optional[float] = None
    avg_experiments: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_name": self.project_name,
            "run_count": self.run_count,
            "avg_score": self.avg_score,
            "max_score": self.max_score,
            "min_score": self.min_score,
            "last_5_scores": self.last_5_scores,
            "risk_distribution": self.risk_distribution,
            "gate_pass_rate": self.gate_pass_rate,
            "avg_blocking_risks": self.avg_blocking_risks,
            "hallucination_flag_count": self.hallucination_flag_count,
            "security_pass_rate": self.security_pass_rate,
            "avg_experiments": self.avg_experiments,
        }


@dataclass
class AgentMetricsReport:
    """Full agent performance report across all projects."""

    workspace_dir: str
    projects: List[ProjectMetrics] = field(default_factory=list)
    global_run_count: int = 0
    global_avg_score: Optional[float] = None
    global_security_pass_rate: Optional[float] = None
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace_dir": self.workspace_dir,
            "generated_at": self.generated_at,
            "global_run_count": self.global_run_count,
            "global_avg_score": self.global_avg_score,
            "global_security_pass_rate": self.global_security_pass_rate,
            "projects": [p.to_dict() for p in self.projects],
        }

    def save_json(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        _tmp = path + ".tmp"
        try:
            with open(_tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
            os.replace(_tmp, path)
        except OSError:
            try:
                os.unlink(_tmp)
            except OSError:
                pass
            raise

    def summary_text(self) -> str:
        lines = [
            "╔══════════════════════════════════════════════════════════╗",
            "║         Crucible Agent Performance Dashboard            ║",
            "╚══════════════════════════════════════════════════════════╝",
            f"  Total runs indexed : {self.global_run_count}",
        ]
        if self.global_avg_score is not None:
            lines.append(f"  Global avg score   : {self.global_avg_score:.1f}/100")
        if self.global_security_pass_rate is not None:
            lines.append(f"  Security pass rate : {self.global_security_pass_rate:.0%}")
        lines.append("")

        if not self.projects:
            lines.append("  No per-project data available.")
            return "\n".join(lines)

        # Header row
        hdr = f"  {'Project':<28} {'Runs':>5} {'AvgScore':>9} {'GatePass':>9} {'HallFlags':>10}"
        lines.append(hdr)
        lines.append("  " + "-" * 65)

        for p in sorted(self.projects, key=lambda x: (x.avg_score or 0), reverse=True):
            avg = f"{p.avg_score:.1f}" if p.avg_score is not None else " N/A "
            gate = f"{p.gate_pass_rate:.0%}" if p.gate_pass_rate is not None else " N/A "
            hall = str(p.hallucination_flag_count)
            name = p.project_name[:28]
            lines.append(
                f"  {name:<28} {p.run_count:>5} {avg:>9} {gate:>9} {hall:>10}"
            )

            # Sparkline of last 5 scores
            if p.last_5_scores:
                spark = "  " + " ".join(
                    f"{s:.0f}" if s is not None else "—"
                    for s in p.last_5_scores
                )
                lines.append(f"    last 5 scores: {spark.strip()}")

            # Risk distribution
            if p.risk_distribution:
                dist = ", ".join(
                    f"{k}:{v}" for k, v in sorted(p.risk_distribution.items())
                )
                lines.append(f"    risk dist: {dist}")

        lines.append("")
        if self.generated_at:
            lines.append(f"  Generated: {self.generated_at}")
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _count_hallucination_flags(analysis: Dict[str, Any]) -> int:
    """Count hallucination_flags entries in an analysis result."""
    flags = analysis.get("hallucination_flags") or []
    if isinstance(flags, list):
        return len(flags)
    # Some outputs store it as a dict with per-stage flags
    if isinstance(flags, dict):
        return sum(len(v) if isinstance(v, list) else 1 for v in flags.values())
    return 0


# ── Per-run detailed metrics (from saved_projects/*.json) ────────────────────

@dataclass
class _RunDetail:
    project_name: str
    score: Optional[float]
    risk_level: Optional[str]
    gate_decision: Optional[str]
    blocking_risk_count: int
    hallucination_flag_count: int
    security_passed: Optional[bool]
    experiment_count: int
    timestamp: Optional[str]


def _load_run_detail(run_dir: str) -> Optional[_RunDetail]:
    """Load detailed metrics from a single run directory."""
    analysis = _load_json(os.path.join(run_dir, "analysis_result.json"))
    meta = _load_json(os.path.join(run_dir, "run_meta.json"))

    if not analysis:
        return None

    project_name = str(
        analysis.get("project_name") or meta.get("project_name") or os.path.basename(run_dir)
    ).strip()

    risks = list(analysis.get("blocking_risks") or [])
    if not risks:
        gate_snap = analysis.get("gate_context_snapshot") or {}
        if isinstance(gate_snap, dict):
            risks = list(gate_snap.get("blocking_risks") or [])

    # Security
    sec_path = os.path.join(run_dir, "security_report.json")
    security_passed: Optional[bool] = None
    if os.path.isfile(sec_path):
        sec_data = _load_json(sec_path)
        security_passed = bool(sec_data.get("passed", True))

    return _RunDetail(
        project_name=project_name,
        score=_safe_float(analysis.get("score")),
        risk_level=analysis.get("risk_level"),
        gate_decision=analysis.get("gate_decision"),
        blocking_risk_count=len(risks),
        hallucination_flag_count=_count_hallucination_flags(analysis),
        security_passed=security_passed,
        experiment_count=len(list(analysis.get("experiments") or [])),
        timestamp=meta.get("timestamp"),
    )


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate_project_metrics(
    project_name: str,
    details: List[_RunDetail],
) -> ProjectMetrics:
    """Compute ProjectMetrics from a list of run details for one project."""
    scores = [d.score for d in details if d.score is not None]
    avg_score = round(sum(scores) / len(scores), 2) if scores else None
    max_score = max(scores) if scores else None
    min_score = min(scores) if scores else None

    # Last 5 scores (sorted by timestamp descending, fallback to list order reversed)
    sorted_details = sorted(
        details,
        key=lambda d: d.timestamp or "",
        reverse=True,
    )
    last_5 = [d.score for d in sorted_details[:5]]

    risk_dist: Dict[str, int] = {}
    for d in details:
        if d.risk_level:
            key = d.risk_level.lower()
            risk_dist[key] = risk_dist.get(key, 0) + 1

    # Gate pass rate: count gate_decision == "proceed" (case-insensitive)
    gate_counts = [d for d in details if d.gate_decision]
    proceed_count = sum(
        1 for d in gate_counts
        if str(d.gate_decision or "").lower() == "proceed"
    )
    gate_pass_rate = (proceed_count / len(gate_counts)) if gate_counts else None

    # Avg blocking risks
    risk_counts = [d.blocking_risk_count for d in details]
    avg_risks = round(sum(risk_counts) / len(risk_counts), 2) if risk_counts else None

    # Hallucination flags total
    hall_total = sum(d.hallucination_flag_count for d in details)

    # Security pass rate
    sec_runs = [d for d in details if d.security_passed is not None]
    if sec_runs:
        sec_pass_rate = round(
            sum(1 for d in sec_runs if d.security_passed) / len(sec_runs), 2
        )
    else:
        sec_pass_rate = None

    # Avg experiments
    exp_counts = [d.experiment_count for d in details]
    avg_exp = round(sum(exp_counts) / len(exp_counts), 2) if exp_counts else None

    return ProjectMetrics(
        project_name=project_name,
        run_count=len(details),
        avg_score=avg_score,
        max_score=max_score,
        min_score=min_score,
        last_5_scores=last_5,
        risk_distribution=risk_dist,
        gate_pass_rate=gate_pass_rate,
        avg_blocking_risks=avg_risks,
        hallucination_flag_count=hall_total,
        security_pass_rate=sec_pass_rate,
        avg_experiments=avg_exp,
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def compute_agent_metrics(workspace_dir: str) -> AgentMetricsReport:
    """
    Compute agent performance metrics from all indexed runs.

    Scans ``saved_projects/`` for completed run directories and aggregates
    per-project quality metrics.  Also queries ``run_registry.db`` if
    available for richer metadata.

    Parameters
    ----------
    workspace_dir:
        Repository root (must contain ``saved_projects/``).

    Returns
    -------
    AgentMetricsReport
        Aggregated metrics report, ready for JSON serialisation or terminal
        display.
    """
    from datetime import datetime, timezone

    saved_dir = os.path.join(workspace_dir, "saved_projects")
    report = AgentMetricsReport(
        workspace_dir=workspace_dir,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )

    if not os.path.isdir(saved_dir):
        return report

    # Collect all run details
    all_details: List[_RunDetail] = []
    try:
        entries = sorted(os.listdir(saved_dir))
    except OSError:
        return report

    for entry in entries:
        run_dir = os.path.join(saved_dir, entry)
        if not os.path.isdir(run_dir):
            continue
        if not os.path.isfile(os.path.join(run_dir, "analysis_result.json")):
            continue
        detail = _load_run_detail(run_dir)
        if detail:
            all_details.append(detail)

    if not all_details:
        return report

    # Group by project name
    by_project: Dict[str, List[_RunDetail]] = {}
    for d in all_details:
        key = d.project_name.lower()
        by_project.setdefault(key, [])
        by_project[key].append(d)

    # Per-project aggregation
    for project_name, details in sorted(by_project.items()):
        pm = _aggregate_project_metrics(
            project_name=details[0].project_name,  # preserve original casing
            details=details,
        )
        report.projects.append(pm)

    # Global stats
    report.global_run_count = len(all_details)

    all_scores = [d.score for d in all_details if d.score is not None]
    report.global_avg_score = (
        round(sum(all_scores) / len(all_scores), 2) if all_scores else None
    )

    sec_runs = [d for d in all_details if d.security_passed is not None]
    if sec_runs:
        report.global_security_pass_rate = round(
            sum(1 for d in sec_runs if d.security_passed) / len(sec_runs), 2
        )

    return report
