"""
features/batch_runner.py
========================
Multi-project batch execution.

Scans a directory for Python project sub-directories and runs the analysis
pipeline on each one.  Results are collected into a ``batch_summary.json``
saved in *output_dir* (defaults to *batch_dir*).

Projects are detected by the presence of ``.py`` files or common Python
project manifests (``requirements.txt``, ``pyproject.toml``, etc.).

Parallelism is controlled via *max_workers*.  Default is 1 (sequential) which
is the safe default for LLM rate-limit sensitive workflows.  Set to 2–4 for
providers with generous quotas.

Usage::

    from crucible.features.batch_runner import run_batch

    def my_run_fn(project_dir: str):
        # run pipeline, return output dir path or None
        ...

    report = run_batch("/path/to/batch_dir", my_run_fn, max_workers=1)
    print(f"{report.successful}/{report.total_projects} succeeded")
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

if __package__ == "crucible.features":
    from ..cancellation import OperationCancelledError as _OperationCancelledError
else:  # pragma: no cover
    from cancellation import OperationCancelledError as _OperationCancelledError  # type: ignore[no-redef]

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class BatchProjectResult:
    project_dir: str
    project_name: str
    success: bool
    run_dir: Optional[str] = None
    score: Optional[float] = None
    risk_level: Optional[str] = None
    duration_seconds: float = 0.0
    error: str = ""


@dataclass
class BatchSummaryReport:
    batch_dir: str
    timestamp: str
    total_projects: int
    successful: int
    failed: int
    results: List[BatchProjectResult] = field(default_factory=list)
    total_duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_dir": self.batch_dir,
            "timestamp": self.timestamp,
            "total_projects": self.total_projects,
            "successful": self.successful,
            "failed": self.failed,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "results": [
                {
                    "project_dir": os.path.basename(r.project_dir),
                    "project_name": r.project_name,
                    "success": r.success,
                    "run_dir": r.run_dir,
                    "score": r.score,
                    "risk_level": r.risk_level,
                    "duration_seconds": round(r.duration_seconds, 2),
                    "error": r.error,
                }
                for r in self.results
            ],
        }

    def print_summary(self, file: Any = None) -> None:
        out = file or sys.stderr
        print(
            f"\n[BatchRunner] Completed: "
            f"{self.successful}/{self.total_projects} succeeded "
            f"({self.failed} failed, {self.total_duration_seconds:.1f}s total)",
            file=out,
            flush=True,
        )
        for r in self.results:
            status = "✓" if r.success else "✗"
            score_str = f"  score={r.score}" if r.score is not None else ""
            risk_str = f"  risk={r.risk_level}" if r.risk_level else ""
            print(
                f"  {status} {r.project_name}"
                f"  ({r.duration_seconds:.1f}s{score_str}{risk_str})"
                + (f"  ERROR: {r.error[:80]}" if r.error else ""),
                file=out,
                flush=True,
            )


# ── Project discovery ─────────────────────────────────────────────────────────

_PROJECT_MANIFESTS = {
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "package.json",  # allow JS projects too
}


def _looks_like_project(directory: str) -> bool:
    """Heuristic check: does *directory* contain Python code or a manifest?"""
    try:
        entries = set(os.listdir(directory))
    except OSError:
        return False

    if entries & _PROJECT_MANIFESTS:
        return True

    # Walk up to 2 levels for .py files.
    # Use dirnames[:] = [] to prune the walk at depth 2 so we never descend
    # into depth-3+ directories, and never miss depth-≤2 siblings that follow
    # a deep directory in iteration order.
    for dirpath, dirnames, filenames in os.walk(directory):
        depth = dirpath[len(directory):].count(os.sep)
        if depth >= 2:
            dirnames[:] = []  # stop os.walk from recursing deeper
        if any(f.endswith(".py") for f in filenames):
            return True
    return False


def discover_projects(batch_dir: str) -> List[str]:
    """
    Return sorted list of absolute paths to project sub-directories in
    *batch_dir*.  Only immediate children (depth 1) are considered.
    """
    batch_dir = os.path.abspath(batch_dir)
    projects: List[str] = []
    try:
        entries = sorted(os.listdir(batch_dir))
    except OSError as exc:
        print(f"[BatchRunner] Cannot read batch_dir: {exc}", file=sys.stderr)
        return []

    for entry in entries:
        if entry.startswith("."):
            continue
        full_path = os.path.join(batch_dir, entry)
        if os.path.isdir(full_path) and _looks_like_project(full_path):
            projects.append(full_path)
    return projects


# ── Per-project runner ────────────────────────────────────────────────────────

def _load_run_results(run_dir: Optional[str]) -> tuple:
    """Return ``(score, risk_level)`` from *run_dir/analysis_result.json*."""
    if not run_dir or not os.path.isdir(run_dir):
        return None, None
    path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("score"), data.get("risk_level")
    except (json.JSONDecodeError, OSError):
        return None, None


def _run_single_project(
    project_dir: str,
    run_fn: Callable[[str], Optional[str]],
) -> BatchProjectResult:
    """
    Call *run_fn(project_dir)* and wrap the result in a BatchProjectResult.

    *run_fn* must return the run output directory path (str) or None.
    Any exception raised by *run_fn* is caught and recorded as a failure.
    """
    project_name = os.path.basename(project_dir)
    t0 = time.monotonic()

    try:
        run_dir_raw = run_fn(project_dir)
        run_dir = str(run_dir_raw) if run_dir_raw else None
        duration = time.monotonic() - t0
        score, risk_level = _load_run_results(run_dir)
        return BatchProjectResult(
            project_dir=project_dir,
            project_name=project_name,
            success=True,
            run_dir=run_dir,
            score=score,
            risk_level=risk_level,
            duration_seconds=duration,
        )
    except _OperationCancelledError:
        # Cooperative cancellation must abort the entire batch — do not
        # record it as a per-project failure and continue to the next project.
        raise
    except Exception as exc:
        duration = time.monotonic() - t0
        return BatchProjectResult(
            project_dir=project_dir,
            project_name=project_name,
            success=False,
            duration_seconds=duration,
            error=str(exc)[:500],
        )


# ── Main entry point ──────────────────────────────────────────────────────────

def run_batch(
    batch_dir: str,
    run_fn: Callable[[str], Optional[str]],
    *,
    max_workers: int = 1,
    output_dir: Optional[str] = None,
) -> BatchSummaryReport:
    """
    Discover projects in *batch_dir* and run *run_fn* for each.

    Args:
        batch_dir:    Root directory containing project sub-directories.
        run_fn:       ``(project_dir) -> run_output_dir | None``.
                      Must be thread-safe when *max_workers* > 1.
        max_workers:  Maximum parallel workers.  Default 1 = sequential.
                      Capped at 4 to avoid overwhelming LLM rate limits.
        output_dir:   Where to write ``batch_summary.json``.
                      Defaults to *batch_dir*.

    Returns:
        BatchSummaryReport with per-project results.
    """
    batch_dir = os.path.abspath(batch_dir)
    projects = discover_projects(batch_dir)

    if not projects:
        print(
            f"[BatchRunner] No Python projects discovered in {batch_dir}",
            file=sys.stderr,
        )
    else:
        print(
            f"[BatchRunner] {len(projects)} project(s) discovered in {batch_dir}",
            file=sys.stderr,
        )

    timestamp = datetime.now(timezone.utc).isoformat()
    t_start = time.monotonic()
    results: List[BatchProjectResult] = []

    # Cap concurrency conservatively
    workers = max(1, min(max_workers, len(projects), 4))

    if workers == 1 or not projects:
        # Sequential — simplest, safest for LLM rate limits
        for proj_dir in projects:
            print(
                f"[BatchRunner] → {os.path.basename(proj_dir)}",
                file=sys.stderr,
                flush=True,
            )
            result = _run_single_project(proj_dir, run_fn)
            results.append(result)
            status = "✓" if result.success else "✗"
            score_str = f"  score={result.score}" if result.score is not None else ""
            print(
                f"[BatchRunner] {status} {result.project_name}"
                f"  ({result.duration_seconds:.1f}s{score_str})",
                file=sys.stderr,
                flush=True,
            )
    else:
        # Parallel via thread pool
        print(
            f"[BatchRunner] Running with {workers} parallel workers",
            file=sys.stderr,
        )
        # Use explicit executor lifecycle so cancellation does not block on
        # shutdown(wait=True).  The `with` statement calls shutdown(wait=True)
        # on __exit__, which hangs waiting for all running threads to finish —
        # exactly what we want to avoid on OperationCancelledError.
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        _cancelled = False
        try:
            future_to_proj = {
                executor.submit(_run_single_project, proj, run_fn): proj
                for proj in projects
            }
            for future in concurrent.futures.as_completed(future_to_proj):
                try:
                    result = future.result()
                except _OperationCancelledError:
                    # Cancellation must abort the entire batch — cancel all
                    # pending futures, release the pool without waiting for
                    # already-running workers, and propagate immediately.
                    for f in future_to_proj:
                        f.cancel()
                    _cancelled = True
                    raise
                results.append(result)
                status = "✓" if result.success else "✗"
                print(
                    f"[BatchRunner] {status} {result.project_name}",
                    file=sys.stderr,
                    flush=True,
                )
        finally:
            # On cancellation use wait=False so we don't block on still-running workers,
            # negating the fast-path shutdown above.  On normal exit wait=True is correct.
            executor.shutdown(wait=not _cancelled)

    total_duration = time.monotonic() - t_start
    successful = sum(1 for r in results if r.success)
    failed = len(results) - successful

    summary = BatchSummaryReport(
        batch_dir=batch_dir,
        timestamp=timestamp,
        total_projects=len(projects),
        successful=successful,
        failed=failed,
        results=results,
        total_duration_seconds=total_duration,
    )

    save_dir = output_dir or batch_dir
    os.makedirs(save_dir, exist_ok=True)
    summary_path = os.path.join(save_dir, "batch_summary.json")
    _tmp_summary = summary_path + ".tmp"
    try:
        with open(_tmp_summary, "w", encoding="utf-8") as fh:
            json.dump(summary.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(_tmp_summary, summary_path)
        print(
            f"[BatchRunner] Summary saved → {summary_path}",
            file=sys.stderr,
        )
    except OSError as exc:
        try:
            os.unlink(_tmp_summary)
        except OSError:
            pass
        print(
            f"[BatchRunner] Could not write batch_summary.json: {exc}",
            file=sys.stderr,
        )

    summary.print_summary()
    return summary
