"""
features/prompt_ab_test.py
===========================
Prompt A/B testing framework for the Crucible analysis pipeline.

Runs the core pipeline **twice** — once with Variant A prompt modifier and once
with Variant B — then compares the resulting ``analysis_result.json`` outputs
on key metrics:

  - analysis score  (0-100)
  - risk level
  - gate decision
  - consensus / disagreement length
  - number of blocking risks
  - number of experiments
  - codegen scope

Each variant is run as a **subprocess** via ``run_crucible_enhanced.py``, so
both runs are fully isolated (separate processes, separate LLM sessions, separate
run directories under ``saved_projects/``).

Variant context injection
-------------------------
Extra context for each variant is written to a temporary file and the env var
``PIPELINE_INTERACTIVE_CONTEXT`` is set to that path for the subprocess's
environment.  The file is cleaned up after the subprocess exits, regardless of
success or failure.

Results
-------
A structured ``ABTestReport`` is written to
``{config.output_dir}/ab_test_report.json``.

Usage::

    from crucible.features.prompt_ab_test import ABTestConfig, run_ab_test

    config = ABTestConfig(
        variant_a_label="baseline",
        variant_b_label="risk-focused",
        variant_b_extra_context="Emphasise irreversible risks and worst-case scenarios.",
        output_dir="/tmp/ab_output",
    )
    report = run_ab_test(config, workspace_dir="/path/to/repo")
    print(report.summary_text())

Or via the enhanced runner::

    python run_crucible_enhanced.py abtest \\
        --variant-b-context "Emphasise downside risks" \\
        --output-dir ./ab_output

Environment variables
---------------------
AB_TEST_TIMEOUT     Max seconds for each pipeline subprocess (default: 3600).
AB_TEST_PARALLEL    Set to 1 to run both variants in parallel threads (default: 0
                    — sequential).  Parallel mode uses more resources but halves
                    elapsed time.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Configuration ─────────────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


AB_TEST_TIMEOUT: int = _env_int("AB_TEST_TIMEOUT", 3600)
AB_TEST_PARALLEL: bool = bool(_env_int("AB_TEST_PARALLEL", 0))


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ABTestConfig:
    """Configuration for one A/B test run pair."""

    output_dir: str
    variant_a_label: str = "variant_a"
    variant_b_label: str = "variant_b"
    # Extra prompt context injected only for Variant A
    variant_a_extra_context: str = ""
    # Extra prompt context injected only for Variant B
    variant_b_extra_context: str = ""
    # Core-CLI flags forwarded to BOTH variants (e.g. ["--direction-debate"])
    shared_extra_args: List[str] = field(default_factory=list)
    # Variant-specific extra args: first list for A, second for B
    variant_a_extra_args: List[str] = field(default_factory=list)
    variant_b_extra_args: List[str] = field(default_factory=list)
    # Number of times to run each variant. Values > 1 enable statistical comparison.
    n_runs: int = 1


@dataclass
class VariantResult:
    """Analysis metrics extracted from one variant's output directory."""

    label: str
    run_dir: Optional[str]
    elapsed_seconds: float
    score: Optional[float]
    risk_level: Optional[str]
    gate_decision: Optional[str]
    consensus: str
    disagreement: str
    blocking_risks: List[str]
    experiments_count: int
    codegen_scope: str
    error: Optional[str] = None
    log_file: Optional[str] = None  # Path to captured subprocess output log

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "run_dir": self.run_dir,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "score": self.score,
            "risk_level": self.risk_level,
            "gate_decision": self.gate_decision,
            "consensus_length": len(self.consensus),
            "disagreement_length": len(self.disagreement),
            "blocking_risks_count": len(self.blocking_risks),
            "experiments_count": self.experiments_count,
            "codegen_scope": self.codegen_scope,
            "error": self.error,
            "log_file": self.log_file,
        }


@dataclass
class StatisticalSummary:
    """Statistical comparison of scores across multiple runs per variant."""
    n_runs: int
    a_scores: List[float]
    b_scores: List[float]
    a_mean: Optional[float]
    b_mean: Optional[float]
    a_std: Optional[float]
    b_std: Optional[float]
    # Mann-Whitney U test results (None if scipy unavailable or n < 2)
    p_value: Optional[float] = None
    statistic: Optional[float] = None
    significant: Optional[bool] = None  # True if p_value < 0.05
    test_name: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_runs": self.n_runs,
            "a_scores": self.a_scores,
            "b_scores": self.b_scores,
            "a_mean": round(self.a_mean, 3) if self.a_mean is not None else None,
            "b_mean": round(self.b_mean, 3) if self.b_mean is not None else None,
            "a_std": round(self.a_std, 3) if self.a_std is not None else None,
            "b_std": round(self.b_std, 3) if self.b_std is not None else None,
            "p_value": round(self.p_value, 4) if self.p_value is not None else None,
            "statistic": self.statistic,
            "significant": self.significant,
            "test_name": self.test_name,
        }


@dataclass
class ABTestReport:
    """Aggregated comparison of Variant A vs Variant B."""

    config_variant_a_label: str
    config_variant_b_label: str
    variant_a: VariantResult
    variant_b: VariantResult
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    stats: Optional[StatisticalSummary] = None

    def score_delta(self) -> Optional[float]:
        """Return B.score - A.score, or None if either score is missing."""
        if self.variant_a.score is None or self.variant_b.score is None:
            return None
        return self.variant_b.score - self.variant_a.score

    def summary_text(self) -> str:
        a, b = self.variant_a, self.variant_b
        lines = [
            "=" * 60,
            f"  A/B Test Report: {self.config_variant_a_label} vs {self.config_variant_b_label}",
            f"  Generated: {self.created_at}",
            "=" * 60,
            "",
            f"  Variant A — {self.config_variant_a_label}",
            f"    Score          : {a.score}",
            f"    Risk           : {a.risk_level}",
            f"    Gate           : {a.gate_decision}",
            f"    Blocking risks : {len(a.blocking_risks)}",
            f"    Experiments    : {a.experiments_count}",
            f"    Codegen scope  : {a.codegen_scope}",
            f"    Elapsed        : {a.elapsed_seconds:.1f}s",
            f"    Error          : {a.error or '—'}",
            f"    Log            : {a.log_file or '—'}",
            "",
            f"  Variant B — {self.config_variant_b_label}",
            f"    Score          : {b.score}",
            f"    Risk           : {b.risk_level}",
            f"    Gate           : {b.gate_decision}",
            f"    Blocking risks : {len(b.blocking_risks)}",
            f"    Experiments    : {b.experiments_count}",
            f"    Codegen scope  : {b.codegen_scope}",
            f"    Elapsed        : {b.elapsed_seconds:.1f}s",
            f"    Error          : {b.error or '—'}",
            f"    Log            : {b.log_file or '—'}",
            "",
        ]
        delta = self.score_delta()
        if delta is not None:
            direction = "B > A" if delta > 0 else ("A > B" if delta < 0 else "A == B")
            lines.append(f"  Score delta (B − A): {delta:+.1f}  [{direction}]")
        if a.risk_level != b.risk_level:
            lines.append(f"  Risk level changed : {a.risk_level} → {b.risk_level}")
        if a.gate_decision != b.gate_decision:
            lines.append(f"  Gate decision changed: {a.gate_decision} → {b.gate_decision}")
        risk_delta = len(b.blocking_risks) - len(a.blocking_risks)
        if risk_delta != 0:
            direction = "more" if risk_delta > 0 else "fewer"
            lines.append(
                f"  Blocking risks: B has {abs(risk_delta)} {direction} than A"
            )
        lines.append("=" * 60)
        if self.stats is not None and self.stats.n_runs > 1:
            lines.append("")
            lines.append(f"  Statistical Analysis (n={self.stats.n_runs} runs each):")
            if self.stats.a_mean is not None:
                lines.append(f"    A mean ± std : {self.stats.a_mean:.2f} ± {self.stats.a_std or 0:.2f}")
            if self.stats.b_mean is not None:
                lines.append(f"    B mean ± std : {self.stats.b_mean:.2f} ± {self.stats.b_std or 0:.2f}")
            if self.stats.p_value is not None:
                sig = "YES (p<0.05)" if self.stats.significant else "NO (p≥0.05)"
                lines.append(f"    {self.stats.test_name}: p={self.stats.p_value:.4f}  Significant: {sig}")
            else:
                lines.append(f"    Test: {self.stats.test_name}")
            lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "created_at": self.created_at,
            "variant_a_label": self.config_variant_a_label,
            "variant_b_label": self.config_variant_b_label,
            "variant_a": self.variant_a.to_dict(),
            "variant_b": self.variant_b.to_dict(),
            "score_delta": self.score_delta(),
        }
        if self.stats is not None:
            d["statistics"] = self.stats.to_dict()
        return d


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_analysis_result(run_dir: str) -> Dict[str, Any]:
    """Load analysis_result.json from *run_dir*; return {} on any error."""
    path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _find_latest_run_dir(workspace_dir: str, created_after: float) -> Optional[str]:
    """Return the most recently modified run directory created after *created_after*."""
    saved = os.path.join(workspace_dir, "saved_projects")
    if not os.path.isdir(saved):
        return None
    try:
        candidates = [
            os.path.join(saved, d)
            for d in os.listdir(saved)
            if os.path.isdir(os.path.join(saved, d))
        ]
    except OSError:
        return None
    candidates = [c for c in candidates if os.path.getmtime(c) > created_after]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _extract_variant_result(
    label: str,
    run_dir: Optional[str],
    elapsed: float,
    error: Optional[str],
    log_file: Optional[str] = None,
) -> VariantResult:
    data: Dict[str, Any] = _load_analysis_result(run_dir) if run_dir else {}
    return VariantResult(
        label=label,
        run_dir=run_dir,
        elapsed_seconds=elapsed,
        score=data.get("score"),
        risk_level=data.get("risk_level"),
        gate_decision=data.get("gate_decision"),
        consensus=str(data.get("consensus") or ""),
        disagreement=str(data.get("disagreement") or ""),
        blocking_risks=list(data.get("blocking_risks") or []),
        experiments_count=len(list(data.get("experiments") or [])),
        codegen_scope=str(data.get("codegen_scope") or ""),
        error=error,
        log_file=log_file,
    )


def _write_variant_context_file(
    label: str,
    extra_context: str,
    workspace_dir: str,
) -> Optional[str]:
    """Write the variant's extra context to a temp file; return its path or None."""
    if not extra_context.strip():
        return None
    path = os.path.join(workspace_dir, f"_ab_context_{label}.txt")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"=== A/B Test Variant: {label} ===\n")
            fh.write(extra_context.strip())
            fh.write("\n=== End Variant Context ===\n")
        os.replace(tmp, path)
        return path
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None


def _remove_file_safe(path: Optional[str]) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _run_variant(
    label: str,
    extra_context: str,
    extra_args: List[str],
    shared_extra_args: List[str],
    workspace_dir: str,
    output_dir: str,
) -> Tuple[Optional[str], float, Optional[str], Optional[str]]:
    """
    Run one variant pipeline as a child subprocess.

    Subprocess stdout and stderr are always written to a per-variant log file
    ``{output_dir}/{label}.log`` so that parallel runs do not interleave their
    output on the terminal and logs remain attributable to each variant.

    Returns ``(run_dir, elapsed_seconds, error_message_or_None, log_file_path)``.
    *run_dir* is the latest ``saved_projects/`` subdirectory created after the
    subprocess started, or ``None`` if no new directory was found.
    """
    context_path = _write_variant_context_file(label, extra_context, workspace_dir)
    env = os.environ.copy()
    if context_path:
        env["PIPELINE_INTERACTIVE_CONTEXT"] = context_path

    runner = str(Path(workspace_dir) / "run_crucible_enhanced.py")
    cmd = [sys.executable, runner, "run"] + list(shared_extra_args) + list(extra_args)

    # Per-variant log file — ensures parallel runs never produce interleaved output.
    os.makedirs(output_dir, exist_ok=True)
    log_path = os.path.join(output_dir, f"{label}.log")

    # Record wall-clock time for run-dir discovery (os.path.getmtime returns
    # epoch time, which is NOT comparable with time.monotonic()).
    t_wall = time.time()
    t0 = time.monotonic()
    error: Optional[str] = None
    try:
        with open(log_path, "w", encoding="utf-8") as log_fh:
            proc = subprocess.run(
                cmd,
                cwd=workspace_dir,
                env=env,
                timeout=AB_TEST_TIMEOUT,
                check=False,
                stdout=log_fh,
                stderr=log_fh,
            )
        if proc.returncode != 0:
            error = f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        error = f"timed out after {AB_TEST_TIMEOUT}s"
    except OSError as exc:
        error = str(exc)
        log_path = None  # type: ignore[assignment]
    finally:
        elapsed = time.monotonic() - t0
        _remove_file_safe(context_path)

    # Use wall-clock epoch time (t_wall) — not monotonic — for mtime comparison.
    run_dir = _find_latest_run_dir(workspace_dir, t_wall - 1)
    return run_dir, elapsed, error, log_path


# ── Statistical analysis ──────────────────────────────────────────────────────

def _compute_stats(
    a_results: List[VariantResult],
    b_results: List[VariantResult],
    n_runs: int,
) -> StatisticalSummary:
    """Compute statistical summary across multiple variant runs."""
    import math as _math

    a_scores = [r.score for r in a_results if r.score is not None]
    b_scores = [r.score for r in b_results if r.score is not None]

    def _mean(vals: List[float]) -> Optional[float]:
        return sum(vals) / len(vals) if vals else None

    def _std(vals: List[float]) -> Optional[float]:
        if len(vals) < 2:
            return None
        m = sum(vals) / len(vals)
        variance = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        return _math.sqrt(variance)

    summary = StatisticalSummary(
        n_runs=n_runs,
        a_scores=a_scores,
        b_scores=b_scores,
        a_mean=_mean(a_scores),
        b_mean=_mean(b_scores),
        a_std=_std(a_scores),
        b_std=_std(b_scores),
    )

    # Attempt Mann-Whitney U test if scipy is available and we have enough data
    if len(a_scores) >= 2 and len(b_scores) >= 2:
        try:
            from scipy import stats as _scipy_stats  # type: ignore[import]
            stat, pval = _scipy_stats.mannwhitneyu(
                a_scores, b_scores, alternative="two-sided"
            )
            summary.statistic = float(stat)
            summary.p_value = float(pval)
            summary.significant = pval < 0.05
            summary.test_name = "mann_whitney_u"
        except ImportError:
            # scipy not installed — fall back to descriptive stats only
            summary.test_name = "descriptive_only"
        except Exception:
            summary.test_name = "test_failed"
    else:
        summary.test_name = "insufficient_data"

    return summary


# ── Public API ────────────────────────────────────────────────────────────────

def run_ab_test(config: ABTestConfig, workspace_dir: str) -> ABTestReport:
    """
    Execute both pipeline variants and return a structured comparison report.

    The report is written to ``{config.output_dir}/ab_test_report.json``.

    Parameters
    ----------
    config:
        ``ABTestConfig`` describing the two variants.
    workspace_dir:
        Repository root (must contain ``run_crucible_enhanced.py`` and
        ``saved_projects/``).
    """
    os.makedirs(config.output_dir, exist_ok=True)

    n_runs = max(1, int(config.n_runs))

    # Accumulate results for each run (for multi-run statistical mode)
    all_results_a: List[VariantResult] = []
    all_results_b: List[VariantResult] = []

    def _run_one_pair(run_idx: int) -> None:
        """Run one A+B pair (used for both single and multi-run modes)."""
        _pair_results: Dict[str, Tuple[Optional[str], float, Optional[str], Optional[str]]] = {}

        label_a = f"{config.variant_a_label}_r{run_idx}" if n_runs > 1 else config.variant_a_label
        label_b = f"{config.variant_b_label}_r{run_idx}" if n_runs > 1 else config.variant_b_label

        def _run_a() -> None:
            _pair_results["a"] = _run_variant(
                label_a,
                config.variant_a_extra_context,
                list(config.variant_a_extra_args),
                list(config.shared_extra_args),
                workspace_dir,
                config.output_dir,
            )

        def _run_b() -> None:
            _pair_results["b"] = _run_variant(
                label_b,
                config.variant_b_extra_context,
                list(config.variant_b_extra_args),
                list(config.shared_extra_args),
                workspace_dir,
                config.output_dir,
            )

        if AB_TEST_PARALLEL:
            t_a = threading.Thread(target=_run_a, daemon=True)
            t_b = threading.Thread(target=_run_b, daemon=True)
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()
        else:
            _run_a()
            _run_b()

        run_dir_a, elapsed_a, error_a, log_a = _pair_results.get(
            "a", (None, 0.0, "not executed", None)
        )
        run_dir_b, elapsed_b, error_b, log_b = _pair_results.get(
            "b", (None, 0.0, "not executed", None)
        )

        all_results_a.append(_extract_variant_result(
            config.variant_a_label, run_dir_a, elapsed_a, error_a, log_a
        ))
        all_results_b.append(_extract_variant_result(
            config.variant_b_label, run_dir_b, elapsed_b, error_b, log_b
        ))

    for run_idx in range(n_runs):
        _run_one_pair(run_idx)

    # The primary (first-run) results are used for the top-level report fields
    result_a = all_results_a[0]
    result_b = all_results_b[0]

    report = ABTestReport(
        config_variant_a_label=config.variant_a_label,
        config_variant_b_label=config.variant_b_label,
        variant_a=result_a,
        variant_b=result_b,
    )

    # Compute statistical summary when n_runs > 1
    if n_runs > 1:
        report.stats = _compute_stats(all_results_a, all_results_b, n_runs)

    report_path = os.path.join(config.output_dir, "ab_test_report.json")
    _tmp_report = report_path + ".tmp"
    try:
        with open(_tmp_report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(_tmp_report, report_path)
    except OSError:
        try:
            os.unlink(_tmp_report)
        except OSError:
            pass

    return report
