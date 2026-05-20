#!/usr/bin/env python
"""
run_crucible_enhanced.py
==========================
Enhanced entry point for the Crucible analysis pipeline.

Extends the core pipeline with:
  - Diff-aware context injection  (--diff-aware)
  - Persistent project memory     (--use-memory / --no-memory)
  - Post-run security scan        (--security-scan / --no-security-scan)
  - Deployment artifact gen       (--deployment-artifacts / --no-deployment-artifacts)
  - Test suite generation         (--generate-tests)
  - API version auto-patch        (--api-autopatch)
  - Independent validation agent  (--independent-validation)
  - CI/CD annotations output      (--ci-output)
  - Auto-remediation loop         (--auto-remediation)
  - Backtest runner (Quant mode)  (--backtest-runner)
  - Dependency vulnerability scan (--dependency-audit)
  - HTML report export            (--html-report)
  - Code quality metrics          (--code-quality)
  - Run registry indexing         (--run-registry)
  - Notification hooks            (--notify)
  - Interactive context session   (--interactive)
  - Semantic run deduplication    (--dedup-check)
  - External data source fetch    (--external-data / --external-symbols)
  - Watch mode                    (watch subcommand)
  - Multi-project batch           (batch subcommand)
  - Run comparison                (compare subcommand)
  - Post-process existing run     (postprocess subcommand)
  - Prompt A/B testing            (abtest subcommand)
  - Feature bundle                (--v169-features LIST)

When called WITHOUT a subcommand all arguments are forwarded to the original
``run_crucible.py`` pipeline unchanged (fully backward-compatible).

Examples
--------
# Standard run with security scan + deployment artifacts (defaults on):
python run_crucible_enhanced.py run

# Standard run with interactive guidance and dedup check:
python run_crucible_enhanced.py run --interactive --dedup-check

# Quant mode with backtest runner + external data (CoinGecko BTC):
python run_crucible_enhanced.py run --backtest-runner --external-data coingecko --external-symbols BTC

# Watch for changes (debounce 60s):
python run_crucible_enhanced.py watch . --watch-debounce 60

# Batch analyse three projects:
python run_crucible_enhanced.py batch ./projects

# Compare two saved runs:
python run_crucible_enhanced.py compare saved_projects/run_old saved_projects/run_new

# Post-process an existing run dir:
python run_crucible_enhanced.py postprocess saved_projects/20240101_120000_myproject

# A/B test two prompt variants:
python run_crucible_enhanced.py abtest --variant-b-context "Emphasise downside risks" --output-dir ./ab_out
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

# Module logger used to record otherwise-silent ``except Exception: pass``
# swallows at DEBUG level — operators investigating mysterious recoveries
# (e.g. "the analysis result was empty") can trace the swallowed traceback
# under ``CRUCIBLE_LOG_LEVEL=DEBUG`` without the swallow's user-visible
# semantics changing on the happy path.
LOGGER = logging.getLogger("crucible.runner")

# Ensure the package root is importable regardless of CWD
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_latest_run_dir(
    workspace_dir: str,
    created_after: Optional[float] = None,
) -> Optional[str]:
    """
    Return the most recently modified directory under saved_projects/.

    Args:
        workspace_dir:  Repository root.
        created_after:  If given (as a ``time.time()`` float), only directories
                        whose mtime is STRICTLY after this timestamp are
                        considered.  Use this to avoid returning a stale run
                        directory from a previous execution when the current
                        pipeline produced no output.
    """
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
    if not candidates:
        return None
    if created_after is not None:
        candidates = [c for c in candidates if os.path.getmtime(c) > created_after]
        if not candidates:
            return None
    return max(candidates, key=os.path.getmtime)


def _try_get_llm():
    """
    Attempt to initialise a primary LLM via the runtime namespace.
    Returns None if initialisation fails for any reason, including when
    API keys are not configured or the runtime calls sys.exit().
    """
    try:
        from crucible.runtime_api import get_runtime
        rt = get_runtime()
        init_llm = getattr(rt, "init_llm", None)
        if callable(init_llm):
            return init_llm()
    except (Exception, SystemExit):
        pass
    return None


# ── Environment variable helpers ─────────────────────────────────────────────
# These allow the documented ENHANCED_* env vars to override argparse defaults.

from crucible import _env


def _env_bool(var: str, default: bool) -> bool:
    """Read a boolean env var (whitelist: ``1/true/yes/on`` vs ``0/false/no/off``).

    Empty / missing / unrecognised values fall back to *default* (no silent
    coercion to truthy).
    """
    return _env.env_bool(var, default)


def _env_int(var: str, default: int) -> int:
    """Read an integer env var, returning *default* when absent or invalid."""
    return _env.env_int(var, default)


def _env_float(var: str, default: float) -> float:
    """Read a float env var, returning *default* when absent or invalid."""
    return _env.env_float(var, default)


# ── Subprocess feature-flag forwarding helper ─────────────────────────────────

def _build_feature_flag_args(args: argparse.Namespace) -> "list[str]":
    """Return CLI tokens to inject into a ``run`` subprocess invocation.

    Used by both :func:`cmd_watch` (``_trigger_run``) and :func:`cmd_batch`
    (``_run_project``) so that both modes always forward the same flag set.
    Missing attributes fall back to their documented env-var defaults so the
    subprocess inherits the caller's configuration even when the parent
    subcommand did not define a particular flag.
    """
    cmd: "list[str]" = []

    def _b(attr: str, flag: str, default: bool = False) -> None:
        """Append ``--flag`` or ``--no-flag`` based on *attr* in *args*."""
        if getattr(args, attr, default):
            cmd.append(flag)
        else:
            cmd.append("--no-" + flag.lstrip("-"))

    def _s(attr: str, flag: str) -> None:
        """Append ``--flag value`` when *attr* is a non-empty string."""
        val = (getattr(args, attr, None) or "").strip()
        if val:
            cmd.extend([flag, val])

    # ── Legacy flags (always forwarded explicitly) ───────────────────────────
    _b("security_scan",          "--security-scan",          default=True)
    _b("deployment_artifacts",   "--deployment-artifacts",   default=True)
    _b("use_memory",             "--use-memory",             default=True)
    _b("independent_validation", "--independent-validation", default=False)
    # ── Extended feature flags ───────────────────────────────────────────────
    _b("backtest_runner",   "--backtest-runner",   default=False)
    _b("ingest_docs",       "--ingest-docs",       default=False)
    _s("ingest_docs_dir",   "--ingest-docs-dir")
    _s("github_repo",       "--github-repo")
    _b("multilang_codegen", "--multilang-codegen", default=False)
    _s("multilang_langs",   "--multilang-langs")
    _b("agent_metrics",     "--agent-metrics",     default=False)
    _b("lockfile_gen",      "--lockfile-gen",       default=False)
    # ── Quant analytics suite ────────────────────────────────────────────────
    _b("quant_analytics",    "--quant-analytics",    default=False)
    _b("walk_forward",       "--walk-forward",       default=True)
    _b("significance_test",  "--significance-test",  default=True)
    _b("regime_detection",   "--regime-detection",   default=False)
    _b("factor_analysis",    "--factor-analysis",    default=False)
    _b("transaction_cost",   "--transaction-cost",   default=False)
    _b("monte_carlo",        "--monte-carlo",        default=False)
    _b("tearsheet",          "--tearsheet",          default=False)
    _b("signal_analysis",    "--signal-analysis",    default=False)
    _b("risk_attribution",   "--risk-attribution",   default=False)
    _b("cointegration",      "--cointegration",      default=False)
    _b("dynamic_correlation","--dynamic-correlation",default=False)
    # ── Per-stage model overrides ────────────────────────────────────────────
    _s("librarian_model",       "--librarian-model")
    _s("primary_model",         "--primary-model")
    _s("direction_judge_model", "--direction-judge-model")
    # ── Feature Bundle ───────────────────────────────────────────────────────
    _s("v169_features",         "--v169-features")

    return cmd


# ── Enhanced-only CLI flag names ──────────────────────────────────────────────
# These are stripped from sys.argv before the core CLI processes it.

_ENHANCED_FLAGS = {
    "--diff-aware", "--no-diff-aware",
    "--diff-base-ref",
    "--use-memory", "--no-use-memory",
    "--security-scan", "--no-security-scan",
    "--deployment-artifacts", "--no-deployment-artifacts",
    "--generate-tests", "--no-generate-tests",
    "--api-autopatch", "--no-api-autopatch",
    "--ci-output", "--no-ci-output",
    "--independent-validation", "--no-independent-validation",
    "--auto-remediation", "--no-auto-remediation",
    "--dependency-audit", "--no-dependency-audit",
    "--html-report", "--no-html-report",
    "--code-quality", "--no-code-quality",
    "--run-registry", "--no-run-registry",
    "--notify", "--no-notify",
    "--backtest-runner", "--no-backtest-runner",
    "--project-dir",
    "--interactive", "--no-interactive",
    "--dedup-check", "--no-dedup-check",
    "--external-data",
    "--external-symbols",
    "--external-start",
    "--external-end",
    # Extended features
    "--ingest-docs", "--no-ingest-docs",
    "--ingest-docs-dir",
    "--github-repo",
    "--multilang-codegen", "--no-multilang-codegen",
    "--multilang-langs",
    "--post-chat", "--no-post-chat",
    "--agent-metrics", "--no-agent-metrics",
    "--prompt-version-label",
    # Per-stage model overrides
    "--librarian-model",
    "--primary-model",
    "--direction-judge-model",
    # Quant analytics suite
    "--quant-analytics", "--no-quant-analytics",
    "--walk-forward", "--no-walk-forward",
    "--significance-test", "--no-significance-test",
    "--regime-detection", "--no-regime-detection",
    "--regime-method",
    "--factor-analysis", "--no-factor-analysis",
    "--transaction-cost", "--no-transaction-cost",
    "--monte-carlo", "--no-monte-carlo",
    "--tearsheet", "--no-tearsheet",
    "--signal-analysis", "--no-signal-analysis",
    "--risk-attribution", "--no-risk-attribution",
    # Quant analytics — remaining
    "--cointegration", "--no-cointegration",
    "--dynamic-correlation", "--no-dynamic-correlation",
    "--lockfile-gen", "--no-lockfile-gen",
    # Feature bundle
    "--v169-features",
}
# Flags in this set accept a value argument (next token)
_ENHANCED_VALUE_FLAGS = {
    "--diff-base-ref",
    "--project-dir",
    "--external-data",
    "--external-symbols",
    "--external-start",
    "--external-end",
    # Extended features
    "--ingest-docs-dir",
    "--github-repo",
    "--multilang-langs",
    "--prompt-version-label",
    # Per-stage model overrides
    "--librarian-model",
    "--primary-model",
    "--direction-judge-model",
    # Quant analytics suite (value-taking flags)
    "--regime-method",
    # Feature bundle
    "--v169-features",
}


def _build_core_argv(original_argv: List[str]) -> List[str]:
    """
    Rebuild sys.argv for the core CLI by removing:
    - The "run" subcommand token (argv[1] when present)
    - All enhanced-only flags and their values
    """
    result = [original_argv[0]]
    skip_next = False
    for token in original_argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token == "run":
            # Remove the subcommand; the core CLI has no subcommands
            continue
        base = token.split("=")[0]
        if base in _ENHANCED_FLAGS:
            if base in _ENHANCED_VALUE_FLAGS and "=" not in token:
                skip_next = True  # drop the following value token too
        else:
            result.append(token)
    return result


# ── Post-processing pipeline ──────────────────────────────────────────────────

def _run_postprocessing(run_dir: str, args: argparse.Namespace) -> None:
    """
    Apply requested post-processing features to a completed run directory.

    Execution order:
    External Data → Security Scan → Deployment Artifacts → (LLM init) →
    Test Generation → API Auto-patch → Independent Validation →
    Auto-Remediation → Backtest Runner → Project Memory → Dependency Audit →
    Code Quality → HTML Report → CI/CD Output → Run Registry → Notifications
    """
    print(f"\n[Enhanced] Post-processing: {run_dir}", flush=True)

    # External data connectors (before backtest so data files are ready)
    external_data = getattr(args, "external_data", None)
    if external_data:
        try:
            from crucible.features.external_data_connectors import (
                ExternalDataConfig,
                prepare_external_data,
            )
            sources_raw = str(external_data)
            sources = [s.strip() for s in sources_raw.split(",") if s.strip()]
            symbols_raw = getattr(args, "external_symbols", "") or ""
            symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()] or ["BTC"]
            ext_start = getattr(args, "external_start", "") or ""
            ext_end = getattr(args, "external_end", "") or ""
            ext_config = ExternalDataConfig(
                sources=sources,
                symbols=symbols,
                start_date=ext_start,
                end_date=ext_end,
            )
            print(
                f"[ExtData] Fetching {sources} × {symbols} ({ext_start or 'auto'}"
                f"..{ext_end or 'auto'})…",
                flush=True,
            )
            ext_result = prepare_external_data(run_dir, ext_config)
            print(
                f"[ExtData] {len(ext_result.files_written)} file(s) written  "
                f"rows={ext_result.total_rows}  errors={len(ext_result.errors)}",
                flush=True,
            )
            for err in ext_result.errors[:3]:
                print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[ExtData] Failed: {exc}", file=sys.stderr, flush=True)

    # Security scan
    security_scan = getattr(args, "security_scan", True)
    if security_scan:
        try:
            from crucible.features.security_scan import scan_run_directory
            print("[Security] Scanning…", flush=True)
            report = scan_run_directory(run_dir)
            high = len(report.high_severity_issues)
            status = "PASSED" if report.passed else f"FAILED ({high} HIGH issue(s))"
            print(f"[Security] {status}  scanner={report.scanner_used}", flush=True)
        except Exception as exc:
            print(f"[Security] Scan failed: {exc}", file=sys.stderr, flush=True)

    # Deployment artifacts
    deployment_artifacts = getattr(args, "deployment_artifacts", True)
    if deployment_artifacts:
        try:
            from crucible.features.deployment_artifacts import generate_deployment_artifacts
            print("[Deploy] Generating deployment artifacts…", flush=True)
            report = generate_deployment_artifacts(run_dir)
            print(
                f"[Deploy] framework={report.framework_detected}  "
                f"orm={report.has_orm}  "
                f"artifacts={len(report.artifacts_generated)}",
                flush=True,
            )
            for artifact in report.artifacts_generated:
                print(f"  + {artifact}", flush=True)
            if report.errors:
                for err in report.errors:
                    print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[Deploy] Failed: {exc}", file=sys.stderr, flush=True)

    # Test generation, API auto-patch, and independent validation all require an
    # LLM.  Initialise once here so repeated _try_get_llm() calls — each of
    # which hits the runtime initialisation path — are avoided when multiple
    # LLM-requiring features are enabled in the same run.
    generate_tests = getattr(args, "generate_tests", False)
    api_autopatch = getattr(args, "api_autopatch", False)
    independent_validation = getattr(args, "independent_validation", False)
    auto_remediation = getattr(args, "auto_remediation", False)
    backtest_runner = getattr(args, "backtest_runner", False)
    _iv_wants_llm = independent_validation and _env_bool("ENHANCED_INDEPENDENT_VALIDATION_LLM", True)
    _shared_llm = (
        _try_get_llm()
        if (generate_tests or api_autopatch or _iv_wants_llm or auto_remediation or backtest_runner)
        else None
    )

    # Test generation (requires LLM)
    if generate_tests:
        try:
            from crucible.features.test_generator import generate_tests_for_run
            print("[Tests] Generating test suite…", flush=True)
            if _shared_llm is None:
                print(
                    "[Tests] Could not initialise LLM — test generation skipped.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                report = generate_tests_for_run(
                    run_dir, _shared_llm,
                    max_files=_env_int("ENHANCED_GENERATE_TESTS_MAX_FILES", 20),
                )
                print(
                    f"[Tests] Generated {len(report.test_files)} test file(s)  "
                    f"errors={len(report.errors)}",
                    flush=True,
                )
                if report.errors:
                    for err in report.errors[:3]:
                        print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[Tests] Failed: {exc}", file=sys.stderr, flush=True)

    # API version auto-patch (requires LLM)
    if api_autopatch:
        try:
            from crucible.features.api_version_autopatch import run_api_version_autopatch
            print("[ApiPatch] Running API version auto-patch…", flush=True)
            if _shared_llm is None:
                print(
                    "[ApiPatch] Could not initialise LLM — auto-patch skipped.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                report = run_api_version_autopatch(run_dir, _shared_llm)
                print(
                    f"[ApiPatch] {report.patches_applied}/{report.patches_attempted} applied  "
                    f"failed={report.patches_failed}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[ApiPatch] Failed: {exc}", file=sys.stderr, flush=True)

    # Independent validation (Phase B: subprocess + Phase A: adversarial LLM)
    if independent_validation:
        try:
            from crucible.features.independent_validator import validate_run
            print("[Validator] Running independent validation…", flush=True)
            val_llm = _shared_llm if _iv_wants_llm else None
            val_timeout = _env_int("ENHANCED_INDEPENDENT_VALIDATION_TIMEOUT", 60)
            report = validate_run(run_dir, llm=val_llm, timeout=val_timeout)
            print(f"[Validator] Verdict: {report.overall_verdict.upper()}", flush=True)
            for phase in report.execution_phases:
                if phase.timed_out:
                    tag = "TIMEOUT"
                elif phase.passed:
                    tag = "PASS"
                else:
                    tag = "FAIL"
                print(f"  [{tag:7s}] {phase.phase}", flush=True)
            if report.adversarial_findings:
                high_count = sum(
                    1 for f in report.adversarial_findings
                    if f.severity in ("critical", "high")
                )
                print(
                    f"  [REVIEW] {len(report.adversarial_findings)} finding(s), "
                    f"{high_count} high/critical",
                    flush=True,
                )
            elif val_llm is None:
                if _iv_wants_llm:
                    # Feature was requested but LLM could not be initialised.
                    print(
                        "  [REVIEW] Skipped — LLM not available.",
                        flush=True,
                    )
                else:
                    # LLM review intentionally disabled via
                    # ENHANCED_INDEPENDENT_VALIDATION_LLM=false.
                    print(
                        "  [REVIEW] Skipped — LLM review disabled "
                        "(set ENHANCED_INDEPENDENT_VALIDATION_LLM=true to enable).",
                        flush=True,
                    )
            else:
                print("  [REVIEW] No issues found.", flush=True)
        except Exception as exc:
            print(f"[Validator] Failed: {exc}", file=sys.stderr, flush=True)

    # Auto-remediation (requires LLM + security/validation reports)
    if auto_remediation:
        try:
            from crucible.features.auto_remediator import remediate_run
            print("[Remediation] Running auto-remediation loop…", flush=True)
            _remed_llm = _shared_llm if _shared_llm is not None else _try_get_llm()
            if _remed_llm is None:
                print(
                    "[Remediation] Could not initialise LLM — skipped.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                max_rounds = _env_int("ENHANCED_AUTO_REMEDIATION_MAX_ROUNDS", 3)
                remed_report = remediate_run(
                    run_dir, _remed_llm, max_rounds=max_rounds,
                )
                print(
                    f"[Remediation] {remed_report.total_patches_applied}/"
                    f"{remed_report.total_patches_attempted} patches applied  "
                    f"issues: {remed_report.initial_issue_count} → "
                    f"{remed_report.final_issue_count}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[Remediation] Failed: {exc}", file=sys.stderr, flush=True)

    # Backtest runner (Quant mode only — after remediation so code is final)
    if backtest_runner:
        try:
            from crucible.features.backtest_runner import run_backtest_pipeline
            print("[Backtest] Running backtest pipeline…", flush=True)
            _bt_llm = _shared_llm if _shared_llm is not None else _try_get_llm()
            bt_report = run_backtest_pipeline(run_dir, llm=_bt_llm)
            if bt_report.success:
                base_sr = ""
                if bt_report.baseline_metrics and bt_report.baseline_metrics.sharpe_ratio is not None:
                    base_sr = f"  baseline_sharpe={bt_report.baseline_metrics.sharpe_ratio:.4f}"
                best_info = ""
                if bt_report.best_params:
                    best_info = f"  best_params={bt_report.best_params}"
                symbol_info = f"[{bt_report.data_symbol}]" if getattr(bt_report, "data_symbol", "") else ""
                print(
                    f"[Backtest] SUCCESS  data={bt_report.data_source}{symbol_info}({bt_report.data_rows})"
                    f"{base_sr}{best_info}",
                    flush=True,
                )
            else:
                print(
                    f"[Backtest] FAILED  errors={len(bt_report.errors)}  "
                    f"fix_rounds={bt_report.fix_rounds_used}",
                    flush=True,
                )
                for err in bt_report.errors[:3]:
                    print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[Backtest] Failed: {exc}", file=sys.stderr, flush=True)

    # ── Quant Analytics Suite ────────────────────────────────────────────────

    # Walk-Forward Validation + Statistical Significance (combined)
    quant_analytics = getattr(args, "quant_analytics", False)
    if quant_analytics:
        try:
            from crucible.features.quant_analytics import run_quant_analytics
            print("[QuantAnalytics] Running walk-forward validation + significance tests…", flush=True)
            qa_wf = getattr(args, "walk_forward", True)
            qa_sig = getattr(args, "significance_test", True)
            qa_result = run_quant_analytics(
                run_dir,
                walk_forward=qa_wf,
                significance_test=qa_sig,
            )
            wf = qa_result.get("walk_forward")
            sig = qa_result.get("significance")
            if wf and wf.get("avg_oos_sharpe") is not None:
                decay = wf.get("sharpe_decay_ratio")
                decay_str = f"{decay:.3f}" if decay is not None else "N/A"
                print(
                    f"[QuantAnalytics] WalkForward: OOS_Sharpe={(wf.get('avg_oos_sharpe') or 0):.4f}"
                    f"  IS_Sharpe={(wf.get('avg_is_sharpe') or 0):.4f}  decay_ratio={decay_str}"
                    f"  consistency={(wf.get('consistency_score') or 0):.1%}",
                    flush=True,
                )
            if sig and sig.get("p_value") is not None:
                print(
                    f"[QuantAnalytics] Significance: p={(sig.get('p_value') or 1.0):.4f}"
                    f"  significant={'Yes' if sig.get('is_significant') else 'No'}"
                    f"  DSR={(sig.get('deflated_sharpe_ratio') or 0):.4f}"
                    f"  CI=[{(sig.get('sharpe_ci_lower') or 0):.3f}, {(sig.get('sharpe_ci_upper') or 0):.3f}]",
                    flush=True,
                )
            if not qa_result.get("success"):
                errs = qa_result.get("errors", [])
                for err in errs[:3]:
                    print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[QuantAnalytics] Failed: {exc}", file=sys.stderr, flush=True)

    # Regime Detection
    regime_detection = getattr(args, "regime_detection", False)
    if regime_detection:
        try:
            from crucible.features.regime_detector import RegimeConfig, run_regime_detection
            print("[Regime] Detecting market regimes…", flush=True)
            method = getattr(args, "regime_method", None) or os.environ.get("REGIME_METHOD", "volatility")
            rd_config = RegimeConfig(method=method)
            rd_result = run_regime_detection(run_dir, config=rd_config)
            if rd_result.n_bars_total > 0:
                regime_counts = {r.label: 0 for r in rd_result.regimes}
                for r in rd_result.regimes:
                    regime_counts[r.label] = regime_counts.get(r.label, 0) + 1
                print(
                    f"[Regime] method={method}  bars={rd_result.n_bars_total}"
                    f"  current={rd_result.current_regime}"
                    f"  regimes={dict(regime_counts)}",
                    flush=True,
                )
            else:
                print("[Regime] No price data found — regime detection skipped.", flush=True)
        except Exception as exc:
            print(f"[Regime] Failed: {exc}", file=sys.stderr, flush=True)

    # Factor Analysis (Fama-French CAPM)
    factor_analysis = getattr(args, "factor_analysis", False)
    if factor_analysis:
        try:
            from crucible.features.factor_analyzer import run_factor_analysis
            print("[FactorAnalysis] Running factor exposure regression…", flush=True)
            fa_result = run_factor_analysis(run_dir)
            reg = fa_result.regression_result
            if reg is not None:
                sig_str = "significant" if reg.alpha_is_significant else "not significant"
                alpha_str = f"{reg.alpha:.6f}" if reg.alpha is not None else "N/A"
                t_str = f"{reg.alpha_t_stat:.2f}" if reg.alpha_t_stat is not None else "N/A"
                beta_str = f"{fa_result.market_beta:.4f}" if fa_result.market_beta is not None else "N/A"
                r2_str = f"{reg.r_squared:.4f}" if reg.r_squared is not None else "N/A"
                print(
                    f"[FactorAnalysis] alpha={alpha_str} (t={t_str}, {sig_str})"
                    f"  market_beta={beta_str}"
                    f"  R²={r2_str}",
                    flush=True,
                )
            else:
                print("[FactorAnalysis] Insufficient return data for regression.", flush=True)
        except Exception as exc:
            print(f"[FactorAnalysis] Failed: {exc}", file=sys.stderr, flush=True)

    # Transaction Cost Analysis
    transaction_cost = getattr(args, "transaction_cost", False)
    if transaction_cost:
        try:
            from crucible.features.transaction_cost_model import run_transaction_cost_analysis
            print("[TxCost] Running transaction cost sensitivity analysis…", flush=True)
            tc_result = run_transaction_cost_analysis(run_dir)
            if tc_result.base_config is not None:
                breakeven_comm = tc_result.breakeven_commission_pct
                be_str = f"{breakeven_comm:.4%}" if breakeven_comm is not None else "N/A"
                print(
                    f"[TxCost] scenarios={len(tc_result.scenarios)}"
                    f"  breakeven_commission={be_str}",
                    flush=True,
                )
            else:
                print("[TxCost] Insufficient trade data for cost analysis.", flush=True)
        except Exception as exc:
            print(f"[TxCost] Failed: {exc}", file=sys.stderr, flush=True)

    # Monte Carlo Simulation + Stress Testing
    monte_carlo = getattr(args, "monte_carlo", False)
    if monte_carlo:
        try:
            from crucible.features.monte_carlo import run_monte_carlo
            print("[MonteCarlo] Running simulation & stress tests…", flush=True)
            mc_result = run_monte_carlo(run_dir)
            stats = mc_result.simulation_stats
            if stats is not None:
                var_str  = f"{stats.var_5pct:.2%}" if stats.var_5pct is not None else "N/A"
                cvar_str = f"{stats.cvar_5pct:.2%}" if stats.cvar_5pct is not None else "N/A"
                pl_str   = f"{stats.prob_loss:.2%}" if stats.prob_loss is not None else "N/A"
                pdd_str  = f"{stats.prob_drawdown_gt_20pct:.2%}" if stats.prob_drawdown_gt_20pct is not None else "N/A"
                print(
                    f"[MonteCarlo] VaR(5%)={var_str}"
                    f"  CVaR(5%)={cvar_str}"
                    f"  P(loss)={pl_str}"
                    f"  P(DD>20%)={pdd_str}"
                    f"  stress_scenarios={len(mc_result.stress_results)}",
                    flush=True,
                )
            else:
                print("[MonteCarlo] Insufficient return data for simulation.", flush=True)
        except Exception as exc:
            print(f"[MonteCarlo] Failed: {exc}", file=sys.stderr, flush=True)

    # Strategy Tearsheet (integrates all available reports)
    tearsheet = getattr(args, "tearsheet", False)
    if tearsheet:
        try:
            from crucible.features.tearsheet import generate_tearsheet
            print("[Tearsheet] Generating strategy tearsheet…", flush=True)
            ts_result = generate_tearsheet(run_dir)
            if ts_result.report_path:
                print(f"[Tearsheet] Written to {ts_result.report_path}", flush=True)
            else:
                print("[Tearsheet] No report written — insufficient data.", flush=True)
        except Exception as exc:
            print(f"[Tearsheet] Failed: {exc}", file=sys.stderr, flush=True)

    # Signal Decay Analysis
    signal_analysis = getattr(args, "signal_analysis", False)
    if signal_analysis:
        try:
            from crucible.features.signal_analyzer import run_signal_analysis
            print("[SignalDecay] Running signal decay analysis…", flush=True)
            sa_result = run_signal_analysis(run_dir)
            if sa_result.effective_horizon_days is not None:
                half_life = sa_result.signal_half_life_days
                hl_str = f"{half_life:.1f}d" if half_life is not None else "N/A"
                print(
                    f"[SignalDecay] effective_horizon={sa_result.effective_horizon_days}d"
                    f"  half_life={hl_str}"
                    f"  horizons_tested={len(sa_result.horizon_stats)}",
                    flush=True,
                )
            else:
                print("[SignalDecay] No signal data available for decay analysis.", flush=True)
        except Exception as exc:
            print(f"[SignalDecay] Failed: {exc}", file=sys.stderr, flush=True)

    # Risk Attribution (single-run: treat current run as a single-asset portfolio)
    risk_attribution = getattr(args, "risk_attribution", False)
    if risk_attribution:
        try:
            from crucible.features.risk_attribution import run_risk_attribution
            print("[RiskAttrib] Computing component VaR and marginal VaR…", flush=True)
            ra_result = run_risk_attribution(
                run_dirs=[run_dir],
                weights=[1.0],
                output_dir=run_dir,
            )
            if ra_result.component_vars:
                c = ra_result.component_vars[0]
                pvar = ra_result.portfolio_var_pct
                pvar_str = f"{pvar:.4f}%" if pvar is not None else "N/A"
                cvar_str = f"{c.component_var_pct:.4f}%" if c.component_var_pct is not None else "N/A"
                mvar_str = f"{c.marginal_var_pct:.4f}%" if c.marginal_var_pct is not None else "N/A"
                contrib_str = f"{c.contribution_pct:.1f}%" if c.contribution_pct is not None else "N/A"
                print(
                    f"[RiskAttrib] portfolio_var={pvar_str}"
                    f"  component_var={cvar_str}"
                    f"  marginal_var={mvar_str}"
                    f"  contribution={contrib_str}",
                    flush=True,
                )
            else:
                print(f"[RiskAttrib] No attribution components computed."
                      f"  errors={len(ra_result.errors)}", flush=True)
        except Exception as exc:
            print(f"[RiskAttrib] Failed: {exc}", file=sys.stderr, flush=True)

    # Cointegration / Pairs Trading
    cointegration = getattr(args, "cointegration", False)
    if cointegration:
        try:
            from crucible.features.cointegration_analyzer import run_cointegration_analysis
            print("[Cointegration] Analyzing pairs for cointegration…", flush=True)
            ci_result = run_cointegration_analysis(run_dir)
            if ci_result.n_cointegrated > 0 and ci_result.best_pair is not None:
                bp = ci_result.best_pair
                hl_str = f"{bp.half_life_days:.1f}d" if bp.half_life_days is not None else "N/A"
                print(f"[Cointegration] {ci_result.n_cointegrated}/{ci_result.n_pairs_tested} pairs cointegrated"
                      f"  best={bp.asset_a}/{bp.asset_b}  half_life={hl_str}"
                      f"  signal={bp.signal}", flush=True)
            else:
                print(f"[Cointegration] {ci_result.n_pairs_tested} pairs tested, none cointegrated."
                      f"  errors={len(ci_result.errors)}", flush=True)
        except Exception as exc:
            print(f"[Cointegration] Failed: {exc}", file=sys.stderr, flush=True)

    # Dynamic Correlation + PCA
    dynamic_correlation = getattr(args, "dynamic_correlation", False)
    if dynamic_correlation:
        try:
            from crucible.features.dynamic_correlation import run_dynamic_correlation_single
            print("[DynCorr] Computing rolling correlation + PCA…", flush=True)
            dc_result = run_dynamic_correlation_single(run_dir)
            if dc_result.pca_components:
                pc1 = dc_result.pca_components[0]
                print(f"[DynCorr] snapshots={len(dc_result.snapshots)}"
                      f"  PC1_variance={pc1.explained_variance_ratio:.1%}"
                      f"  total_variance={dc_result.total_variance_explained:.1%}"
                      f"  diversification={dc_result.diversification_score:.3f}", flush=True)
            else:
                print(f"[DynCorr] snapshots={len(dc_result.snapshots)}  errors={len(dc_result.errors)}", flush=True)
        except Exception as exc:
            print(f"[DynCorr] Failed: {exc}", file=sys.stderr, flush=True)

    # Lockfile generation for generated code
    lockfile_gen = getattr(args, "lockfile_gen", False)
    if lockfile_gen:
        try:
            from crucible.features.code_lockfile_generator import generate_lockfiles
            print("[Lockfile] Generating pyproject.toml + requirements.txt…", flush=True)
            lf_result = generate_lockfiles(run_dir)
            print(f"[Lockfile] {len(lf_result.detected_deps)} deps detected"
                  f"  pyproject={bool(lf_result.pyproject_path)}"
                  f"  requirements={bool(lf_result.requirements_path)}", flush=True)
            if lf_result.errors:
                for err in lf_result.errors[:3]:
                    print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[Lockfile] Failed: {exc}", file=sys.stderr, flush=True)

    # Project memory update (after remediation so memory captures final state)
    use_memory = getattr(args, "use_memory", True)
    if use_memory:
        try:
            from crucible.features.project_memory import (
                ProjectMemoryStore,
                create_memory_entry_from_output,
            )
            workspace_dir = str(_REPO_ROOT)
            store = ProjectMemoryStore(workspace_dir)
            entry = create_memory_entry_from_output(run_dir)
            if entry:
                store.add_entry(entry)
                print(
                    f"[Memory] Saved memory entry for '{entry.project_name}'",
                    flush=True,
                )
        except Exception as exc:
            print(f"[Memory] Failed to save memory: {exc}", file=sys.stderr, flush=True)

    # Dependency audit
    dependency_audit = getattr(args, "dependency_audit", False)
    if dependency_audit:
        try:
            from crucible.features.dependency_auditor import audit_dependencies
            print("[DepAudit] Scanning dependencies…", flush=True)
            dep_report = audit_dependencies(run_dir)
            status = "PASS" if dep_report.passed else "FAIL"
            print(
                f"[DepAudit] {status}  scanner={dep_report.scanner_used}  "
                f"vulns={len(dep_report.vulnerabilities)}",
                flush=True,
            )
        except Exception as exc:
            print(f"[DepAudit] Failed: {exc}", file=sys.stderr, flush=True)

    # Code quality metrics
    code_quality = getattr(args, "code_quality", False)
    if code_quality:
        try:
            from crucible.features.code_quality import analyse_code_quality
            print("[Quality] Analysing code quality…", flush=True)
            cq_report = analyse_code_quality(run_dir)
            print(
                f"[Quality] files={cq_report.total_files}  "
                f"functions={cq_report.total_functions}  "
                f"avg_complexity={cq_report.avg_complexity:.1f}  "
                f"high_complexity={cq_report.high_complexity_functions}",
                flush=True,
            )
        except Exception as exc:
            print(f"[Quality] Failed: {exc}", file=sys.stderr, flush=True)

    # HTML report export
    html_report = getattr(args, "html_report", False)
    if html_report:
        try:
            from crucible.features.report_exporter import export_html_report
            print("[Report] Generating HTML report…", flush=True)
            report_path = export_html_report(run_dir)
            print(f"[Report] Written to {report_path}", flush=True)
        except Exception as exc:
            print(f"[Report] Failed: {exc}", file=sys.stderr, flush=True)

    # CI/CD output
    ci_output = getattr(args, "ci_output", False)
    if ci_output or os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        try:
            from crucible.features.ci_cd import write_github_outputs
            print("[CI] Writing CI/CD outputs…", flush=True)
            write_github_outputs(run_dir)
            print(f"[CI] ci_summary.md written to {run_dir}", flush=True)
        except Exception as exc:
            print(f"[CI] Failed: {exc}", file=sys.stderr, flush=True)

    # Run registry indexing
    run_registry = getattr(args, "run_registry", False)
    if run_registry:
        try:
            from crucible.features.run_registry import RunRegistry
            print("[Registry] Indexing run…", flush=True)
            registry = RunRegistry(str(_REPO_ROOT))
            count = registry.sync()
            print(f"[Registry] {count} run(s) indexed", flush=True)
            registry.close()
        except Exception as exc:
            print(f"[Registry] Failed: {exc}", file=sys.stderr, flush=True)

    # Multi-language code generation (TypeScript / Go) — requires LLM
    multilang_codegen = getattr(args, "multilang_codegen", False)
    if multilang_codegen:
        try:
            from crucible.features.multilang_codegen import (
                translate_run_to_languages,
                MultiLangConfig,
            )
            print("[MultiLang] Generating multi-language translations…", flush=True)
            _ml_llm = _shared_llm if _shared_llm is not None else _try_get_llm()
            if _ml_llm is None:
                print(
                    "[MultiLang] Could not initialise LLM — multilang codegen skipped.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                langs_raw = getattr(args, "multilang_langs", "") or ""
                langs = [l.strip() for l in langs_raw.split(",") if l.strip()] or ["typescript", "go"]
                ml_config = MultiLangConfig(languages=langs)
                ml_result = translate_run_to_languages(run_dir, config=ml_config, llm=_ml_llm)
                for lang, files in ml_result.files_written.items():
                    print(f"[MultiLang] {lang}: {len(files)} file(s) written", flush=True)
                if ml_result.errors:
                    for err in ml_result.errors[:3]:
                        print(f"  ! {err}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[MultiLang] Failed: {exc}", file=sys.stderr, flush=True)

    # Agent performance metrics dashboard
    agent_metrics = getattr(args, "agent_metrics", False)
    if agent_metrics:
        try:
            from crucible.features.agent_metrics import compute_agent_metrics
            print("[Metrics] Computing agent performance metrics…", flush=True)
            workspace_dir = str(_REPO_ROOT)
            metrics_report = compute_agent_metrics(workspace_dir)
            print(metrics_report.summary_text(), flush=True)
            metrics_path = os.path.join(run_dir, "agent_metrics_report.json")
            try:
                metrics_report.save_json(metrics_path)
                print(f"[Metrics] Written to {metrics_path}", flush=True)
            except OSError as exc:
                print(f"[Metrics] Write failed: {exc}", file=sys.stderr, flush=True)
        except Exception as exc:
            print(f"[Metrics] Failed: {exc}", file=sys.stderr, flush=True)

    # Prompt version tracking — record score for the current run
    prompt_version_label = getattr(args, "prompt_version_label", "") or ""
    if prompt_version_label.strip():
        try:
            from crucible.features.prompt_version_tracker import PromptVersionTracker
            workspace_dir = str(_REPO_ROOT)
            tracker = PromptVersionTracker(workspace_dir)
            # Auto-register if not already known
            versions = tracker.list_versions()
            vid = next((v.version_id for v in versions if v.label == prompt_version_label), None)
            if vid is None:
                vid = tracker.register_version(label=prompt_version_label)
            # Load run score from analysis_result.json
            _analysis = {}
            _ar_path = os.path.join(run_dir, "analysis_result.json")
            if os.path.isfile(_ar_path):
                try:
                    with open(_ar_path, "r", encoding="utf-8") as _fh:
                        _analysis = json.load(_fh)
                except Exception:
                    LOGGER.debug("[runner] swallowed exception", exc_info=True)
            _risks = list(_analysis.get("blocking_risks") or [])
            tracker.record_run_score(
                version_id=vid,
                run_id=os.path.basename(run_dir),
                score=_analysis.get("score"),
                risk_level=_analysis.get("risk_level"),
                gate_decision=_analysis.get("gate_decision"),
                blocking_risk_count=len(_risks),
            )
            print(f"[PromptTracker] Recorded score for version '{prompt_version_label}'", flush=True)
        except Exception as exc:
            print(f"[PromptTracker] Failed: {exc}", file=sys.stderr, flush=True)

    # Notification hooks
    notify = getattr(args, "notify", False)
    if notify:
        try:
            from crucible.features.notification_hooks import notify_run_complete
            print("[Notify] Sending notifications…", flush=True)
            # Derive actual pipeline success from analysis_result.json so that
            # NOTIFY_ON_FAIL_ONLY=1 works correctly.  A run is considered
            # successful when a score ≥ 50 was produced and the risk level is
            # not "critical".  Fail-closed on parse error — a missing or
            # unparseable analysis_result.json is itself a failure signal,
            # so the safer default is success=False (this is consistent with
            # the only consumer that cares about the boolean: NOTIFY_ON_FAIL_ONLY,
            # where the user wants to be alerted when something went wrong).
            _notify_success = False
            _analysis_path = os.path.join(run_dir, "analysis_result.json")
            if os.path.isfile(_analysis_path):
                try:
                    with open(_analysis_path, "r", encoding="utf-8") as _fh:
                        _analysis_data = json.load(_fh)
                    _score = float(_analysis_data.get("score") or 0)
                    _risk = str(_analysis_data.get("risk_level") or "").lower()
                    _notify_success = _score >= 50 and _risk != "critical"
                except Exception as _parse_exc:
                    print(
                        f"[Notify] analysis_result.json parse failed: {_parse_exc} — "
                        f"treating as failure for notification purposes.",
                        file=sys.stderr,
                        flush=True,
                    )
            notif_report = notify_run_complete(run_dir, success=_notify_success)
            if notif_report.notifications_sent or notif_report.notifications_failed:
                print(
                    f"[Notify] sent={notif_report.notifications_sent}  "
                    f"failed={notif_report.notifications_failed}",
                    flush=True,
                )
            else:
                print("[Notify] No webhook URLs configured — skipped.", flush=True)
        except Exception as exc:
            print(f"[Notify] Failed: {exc}", file=sys.stderr, flush=True)

    # ── Feature Bundle ───────────────────────────────────────────────────────
    # Accepts a comma-separated list of feature names, e.g.:
    #   --v169-features model_cascade,semantic_cache,citation_verifier
    # or via env var: V169_FEATURES=model_cascade,prometheus_exporter
    _v169_raw = (
        getattr(args, "v169_features", None)
        or os.environ.get("V169_FEATURES", "")
    ).strip()
    if _v169_raw:
        _v169_enabled = [f.strip() for f in _v169_raw.split(",") if f.strip()]
        # All bundled feature modules — import them so @register() decorators run
        _V169_MODULES = [
            "model_cascade", "semantic_cache", "few_shot_injector",
            "global_knowledge_base", "llm_quality_scorer",
            "options_analyzer", "alt_data_connectors", "market_stream",
            "trading_platform", "scheduler", "yaml_pipeline",
            "multi_project_compare", "webhook_templates",
            "prometheus_exporter", "grafana_dashboard", "redis_cache",
            "celery_worker", "auth_manager", "report_annotations",
            "notion_export", "chat_bot", "sandbox_executor",
            "type_coverage", "citation_verifier", "config_wizard",
        ]
        # Import ALL modules unconditionally so every @register() decorator runs
        # and the feature_registry has full dependency metadata before
        # run_features() resolves the execution order.  Filtering which features
        # actually execute is handled by run_features(enabled_features=...).
        import importlib as _importlib
        for _mod in _V169_MODULES:
            try:
                _importlib.import_module(f"crucible.features.{_mod}")
            except ImportError as _ie:
                print(f"[features] Could not import {_mod}: {_ie}", file=sys.stderr, flush=True)
        try:
            from crucible.feature_registry import (
                run_features as _run_v169,
                FeatureConfig as _FC169,
                format_results as _fmt169,
            )
            _v169_config = _FC169(llm=_shared_llm, args=args, env=dict(os.environ))
            print(f"[features] Running: {', '.join(_v169_enabled)}", flush=True)
            _v169_results = _run_v169(
                run_dir,
                enabled_features=_v169_enabled,
                config=_v169_config,
            )
            print(_fmt169(_v169_results), flush=True)
        except Exception as _v169_exc:
            print(f"[features] Feature pipeline failed: {_v169_exc}", file=sys.stderr, flush=True)

    # Post-analysis interactive Q&A (must be last — blocks until user exits)
    post_chat = getattr(args, "post_chat", False)
    if post_chat:
        try:
            from crucible.features.post_analysis_chat import start_post_analysis_chat
            _chat_llm = _shared_llm if _shared_llm is not None else _try_get_llm()
            if _chat_llm is None:
                print(
                    "[PostChat] Could not initialise LLM — post-analysis chat skipped.",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                start_post_analysis_chat(run_dir, _chat_llm)
        except Exception as exc:
            print(f"[PostChat] Failed: {exc}", file=sys.stderr, flush=True)


# ── Subcommand implementations ────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> None:
    """
    Run the core pipeline with optional pre-run context and post-processing.
    """
    workspace_dir = str(_REPO_ROOT)

    # v1.1.8 — Direction Debate Audit Mode CLI → env translation.  Each of
    # the four new flags maps onto a CRUCIBLE_DEBATE_* env var that
    # section_02:_run_single_direction_debate reads at runtime.  The
    # translation MUST happen BEFORE any section module is imported (which
    # currently is fine because section_02 reads env at call-time, not
    # import-time).  ``getattr(..., None)`` is used so missing args from
    # alternate subcommands don't crash this branch — the env override is
    # skipped silently when the flag is absent.  ``_b2e`` is local to avoid
    # polluting the module namespace.
    def _b2e(val: Any) -> Optional[str]:
        if val is True:
            return "1"
        if val is False:
            return "0"
        return None

    _audit_mode_arg = getattr(args, "audit_mode", None)
    if _audit_mode_arg is not None:
        os.environ["CRUCIBLE_DEBATE_AUDIT_MODE"] = _b2e(_audit_mode_arg) or "0"
    _isolation_arg = getattr(args, "debate_isolation", None)
    if _isolation_arg:
        os.environ["CRUCIBLE_DEBATE_ISOLATION_MODE"] = str(_isolation_arg).strip().lower()
    _critic_arg = getattr(args, "external_critic", None)
    if _critic_arg is not None:
        os.environ["CRUCIBLE_DEBATE_EXTERNAL_CRITIC"] = _b2e(_critic_arg) or "0"
    _critic_override_arg = getattr(args, "critic_can_override", None)
    if _critic_override_arg is not None:
        os.environ["CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED"] = (
            _b2e(_critic_override_arg) or "0"
        )
    # v1.1.8 extended — Direction Gate Tuning per-run flag.
    _tolerate_arg = getattr(args, "tolerate_unverifiable_evidence", None)
    if _tolerate_arg is not None:
        os.environ["CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE"] = (
            _b2e(_tolerate_arg) or "0"
        )

    # Pre-run: interactive context-gathering session
    _interactive_context_path: Optional[str] = None
    interactive = getattr(args, "interactive", False)
    if interactive:
        try:
            from crucible.features.interactive_mode import run_interactive_pre_run
            _interactive_context_path = run_interactive_pre_run(workspace_dir)
            if _interactive_context_path:
                print(
                    f"[Interactive] Guidance written to {_interactive_context_path}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[Interactive] Failed: {exc}", file=sys.stderr, flush=True)

    # Temp context file paths — initialised before the try so the finally block
    # can reference them regardless of which code paths execute inside the try.
    # Each is set to the actual path only if the file is successfully created.
    _profile_ctx_path: Optional[str] = None
    _doc_ctx_path: Optional[str] = None
    _gh_ctx_path: Optional[str] = None

    # Wrap the remainder of cmd_run in try/finally so ALL temp context files
    # are cleaned up on every exit path: normal return, early return (dedup
    # cancel, no run_dir found), and exceptions including SystemExit from the
    # core pipeline.
    try:
        # Pre-run: semantic deduplication check
        dedup_check = getattr(args, "dedup_check", False)
        if dedup_check:
            try:
                from crucible.features.run_deduplication import check_duplicate_run
                # Build a topic proxy by stripping subcommands, flags, and values of
                # known value-taking enhanced flags from sys.argv.  What remains are
                # positional tokens that actually describe the analysis subject.
                _KNOWN_SUBCOMMANDS = {
                    "run", "watch", "batch", "compare", "postprocess", "abtest"
                }
                _dedup_skip_next = False
                _topic_tokens: List[str] = []
                for _tok in sys.argv[1:]:
                    if _dedup_skip_next:
                        _dedup_skip_next = False
                        continue
                    if _tok in _KNOWN_SUBCOMMANDS:
                        continue
                    _base = _tok.split("=")[0]
                    if _base.startswith("-"):
                        # If this flag consumes the next token as its value, skip that too
                        if _base in _ENHANCED_VALUE_FLAGS and "=" not in _tok:
                            _dedup_skip_next = True
                        continue
                    _topic_tokens.append(_tok)
                dedup_topic = " ".join(_topic_tokens) or "unknown topic"
                dedup_result = check_duplicate_run(dedup_topic, workspace_dir)
                print(dedup_result.summary_text(), flush=True)
                if dedup_result.has_similar_runs and sys.stdin.isatty():
                    try:
                        answer = input(
                            "\n[Dedup] Similar run(s) found. Continue anyway? [y/N]: "
                        ).strip().lower()
                        if answer not in ("y", "yes"):
                            print("[Dedup] Run cancelled by user.", flush=True)
                            return  # finally block still runs → context cleaned up ✓
                    except (EOFError, KeyboardInterrupt):
                        pass
            except Exception as exc:
                print(f"[Dedup] Check failed: {exc}", file=sys.stderr, flush=True)

        # Pre-run: project profile loading
        try:
            from crucible.features.project_profile import load_project_profile
            workspace_dir = str(_REPO_ROOT)
            _profile = load_project_profile(workspace_dir)
            if _profile:
                prefix_text = _profile.as_prompt_prefix()
                # Write profile prefix to a temp context file for pipeline injection
                _profile_ctx_path = os.path.join(workspace_dir, "_project_profile_context.txt")  # noqa: F841 (used in finally)
                try:
                    with open(_profile_ctx_path, "w", encoding="utf-8") as _pf:
                        _pf.write(prefix_text)
                    # Only set env var if not already overridden by user
                    if not os.environ.get("PIPELINE_INTERACTIVE_CONTEXT"):
                        os.environ["PIPELINE_INTERACTIVE_CONTEXT"] = _profile_ctx_path
                    print(
                        f"[Profile] Project profile loaded from {_profile.source_path}",
                        flush=True,
                    )
                except OSError:
                    pass
        except Exception as exc:
            print(f"[Profile] Failed to load project profile: {exc}", file=sys.stderr, flush=True)

        # Pre-run: document ingestion
        ingest_docs = getattr(args, "ingest_docs", False)
        ingest_docs_dir = getattr(args, "ingest_docs_dir", "") or ""
        if ingest_docs and ingest_docs_dir.strip():
            try:
                from crucible.features.document_ingestion import (
                    ingest_documents_from_dir,
                )
                print(f"[IngestDocs] Ingesting documents from {ingest_docs_dir}…", flush=True)
                ingest_result = ingest_documents_from_dir(ingest_docs_dir)
                if ingest_result.context_text:
                    _doc_ctx_path = os.path.join(str(_REPO_ROOT), "_ingest_docs_context.txt")  # noqa: F841 (used in finally)
                    try:
                        with open(_doc_ctx_path, "w", encoding="utf-8") as _df:
                            _df.write(ingest_result.context_text)
                        if not os.environ.get("PIPELINE_INTERACTIVE_CONTEXT"):
                            os.environ["PIPELINE_INTERACTIVE_CONTEXT"] = _doc_ctx_path
                        print(
                            f"[IngestDocs] {ingest_result.success_count} file(s) injected "
                            f"({ingest_result.total_chars:,} chars)",
                            flush=True,
                        )
                    except OSError:
                        pass
                if ingest_result.errors:
                    for err in ingest_result.errors[:3]:
                        print(f"  ! {err}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"[IngestDocs] Failed: {exc}", file=sys.stderr, flush=True)

        # Pre-run: GitHub repo analysis
        github_repo = getattr(args, "github_repo", "") or ""
        if github_repo.strip():
            try:
                from crucible.features.github_repo_analyzer import (
                    analyze_github_repo_from_url,
                )
                print(f"[GitHub] Analysing repository: {github_repo}…", flush=True)
                gh_result = analyze_github_repo_from_url(github_repo)
                if gh_result.context_text and not gh_result.errors:
                    _gh_ctx_path = os.path.join(str(_REPO_ROOT), "_github_repo_context.txt")  # noqa: F841 (used in finally)
                    try:
                        with open(_gh_ctx_path, "w", encoding="utf-8") as _gf:
                            _gf.write(gh_result.context_text)
                        if not os.environ.get("PIPELINE_INTERACTIVE_CONTEXT"):
                            os.environ["PIPELINE_INTERACTIVE_CONTEXT"] = _gh_ctx_path
                        print(
                            f"[GitHub] {gh_result.full_name}  ⭐{gh_result.stars or 0:,}  "
                            f"issues={len(gh_result.issues)}  commits={len(gh_result.commits)}",
                            flush=True,
                        )
                    except OSError:
                        pass
                for err in gh_result.errors[:3]:
                    print(f"  ! {err}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"[GitHub] Failed: {exc}", file=sys.stderr, flush=True)

        # Pre-run: git diff awareness
        diff_aware = getattr(args, "diff_aware", False)
        project_dir = getattr(args, "project_dir", None)
        if diff_aware and project_dir:
            try:
                from crucible.features.diff_aware import build_diff_aware_prompt_prefix
                prefix = build_diff_aware_prompt_prefix(
                    project_dir,
                    getattr(args, "diff_base_ref", "HEAD~1"),
                )
                if prefix:
                    print("\n[DiffAware] Git diff context:", flush=True)
                    print(prefix, flush=True)
            except Exception as exc:
                print(f"[DiffAware] Failed: {exc}", file=sys.stderr, flush=True)

        # Pre-run: project memory display
        use_memory = getattr(args, "use_memory", True)
        if use_memory:
            try:
                from crucible.features.project_memory import ProjectMemoryStore
                store = ProjectMemoryStore(workspace_dir)
                # We show a generic message since we don't know the project name yet
                memory_file = os.path.join(workspace_dir, "project_memory.json")
                if os.path.isfile(memory_file):
                    print(
                        "[Memory] Project memory store found — context will be "
                        "saved after this run.",
                        flush=True,
                    )
            except Exception:
                LOGGER.debug("[runner] swallowed exception", exc_info=True)

        # Per-stage model overrides: inject as env vars so the core CLI
        # resolver picks them up regardless of which LLM provider is active.
        # These flags are stripped from sys.argv by _build_core_argv() below,
        # so they never reach the core CLI's strict argparse.
        _librarian_model = getattr(args, "librarian_model", "") or ""
        if _librarian_model:
            for _v in (
                "OPENROUTER_LIBRARIAN_MODEL",
                "LIBRARIAN_MODEL",
                "RESEARCH_MODEL",
                "ALIBABA_CODING_PLAN_LIBRARIAN_MODEL",
                "OLLAMA_LIBRARIAN_MODEL",
            ):
                os.environ[_v] = _librarian_model

        _primary_model = getattr(args, "primary_model", "") or ""
        if _primary_model:
            for _v in (
                "OPENROUTER_PRIMARY_MODEL",
                "PRIMARY_MODEL",
                "ALIBABA_CODING_PLAN_PRIMARY_MODEL",
                "OLLAMA_PRIMARY_MODEL",
            ):
                os.environ[_v] = _primary_model

        _direction_judge_model = getattr(args, "direction_judge_model", "") or ""
        if _direction_judge_model:
            for _v in (
                "OPENROUTER_DIRECTION_JUDGE_MODEL",
                "DIRECTION_JUDGE_MODEL",
                "ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL",
                "OLLAMA_DIRECTION_JUDGE_MODEL",
            ):
                os.environ[_v] = _direction_judge_model

        # Rebuild sys.argv for the core CLI (remove enhanced-only flags)
        sys.argv = _build_core_argv(sys.argv)

        # Record timestamp just before the run so we can distinguish a new run
        # directory from any pre-existing one.
        run_start_time = time.time()

        # ── Invoke the core pipeline ──────────────────────────────────────────
        from crucible.cli import main as _core_main
        _core_main()

        # ── Post-processing ───────────────────────────────────────────────────
        # Only accept a run directory that was actually created/updated after we
        # started, preventing accidental post-processing of a stale previous run.
        run_dir = _find_latest_run_dir(workspace_dir, created_after=run_start_time)
        if run_dir is None:
            print(
                "[Enhanced] No new run output directory found — skipping post-processing.",
                file=sys.stderr,
                flush=True,
            )
            return  # finally block still runs → context cleaned up ✓

        _run_postprocessing(run_dir, args)

    finally:
        # Post-run: clean up all temp context files.
        # Runs on ALL exit paths: normal completion, early return, and
        # exceptions (including SystemExit raised by _core_main()).
        if _interactive_context_path:
            try:
                from crucible.features.interactive_mode import cleanup_interactive_context
                cleanup_interactive_context(_interactive_context_path)
            except Exception:
                LOGGER.debug("[runner] swallowed exception", exc_info=True)
        # Remove the three workspace-local context files created before the run.
        # These are only written if the feature was active; the path variables
        # are None if the file was never created.
        for _ctx_path in (_profile_ctx_path, _doc_ctx_path, _gh_ctx_path):
            if _ctx_path and os.path.isfile(_ctx_path):
                try:
                    os.remove(_ctx_path)
                except OSError:
                    pass


def cmd_abtest(args: argparse.Namespace) -> None:
    """Run the pipeline twice with different prompt variants and compare results."""
    from crucible.features.prompt_ab_test import ABTestConfig, run_ab_test

    output_dir = getattr(args, "output_dir", None) or os.path.join(str(_REPO_ROOT), "ab_test_output")
    config = ABTestConfig(
        output_dir=output_dir,
        variant_a_label=getattr(args, "variant_a_label", "variant_a"),
        variant_b_label=getattr(args, "variant_b_label", "variant_b"),
        variant_a_extra_context=getattr(args, "variant_a_context", "") or "",
        variant_b_extra_context=getattr(args, "variant_b_context", "") or "",
        shared_extra_args=list(getattr(args, "shared_args", None) or []),
        variant_a_extra_args=list(getattr(args, "variant_a_args", None) or []),
        variant_b_extra_args=list(getattr(args, "variant_b_args", None) or []),
    )
    print(
        f"\n[ABTest] Running A ({config.variant_a_label}) vs "
        f"B ({config.variant_b_label})…",
        flush=True,
    )
    report = run_ab_test(config, workspace_dir=str(_REPO_ROOT))
    print(report.summary_text(), flush=True)
    report_path = os.path.join(output_dir, "ab_test_report.json")
    print(f"\n[ABTest] Full report: {report_path}", flush=True)


def cmd_watch(args: argparse.Namespace) -> None:
    """Watch a directory for changes and re-run the pipeline."""
    from crucible.features.watch_mode import WatchModeRunner

    watch_dir = getattr(args, "watch_dir", None) or os.getcwd()
    debounce = float(getattr(args, "watch_debounce", 30.0))
    run_immediately = bool(getattr(args, "run_immediately", True))

    def _trigger_run() -> None:
        # Re-invoke this script as a subprocess so each run gets a clean state
        cmd = [sys.executable, str(Path(__file__).resolve()), "run"]
        cmd.extend(_build_feature_flag_args(args))
        watch_timeout = _env_int("ENHANCED_WATCH_TIMEOUT", 3600)
        try:
            subprocess.run(cmd, check=False, timeout=watch_timeout)
        except subprocess.TimeoutExpired:
            print(
                "[WatchMode] Triggered run timed out — pipeline killed.",
                file=sys.stderr,
                flush=True,
            )
        except OSError as exc:
            print(f"[WatchMode] Subprocess error: {exc}", file=sys.stderr, flush=True)

    runner = WatchModeRunner(
        watch_dir=watch_dir,
        run_fn=_trigger_run,
        debounce_seconds=debounce,
    )
    runner.start(run_immediately=run_immediately)


def cmd_batch(args: argparse.Namespace) -> None:
    """Run the analysis pipeline on multiple project directories."""
    from crucible.features.batch_runner import run_batch

    batch_dir: str = args.batch_dir
    max_workers: int = getattr(args, "batch_workers", 1)

    def _run_project(project_dir: str) -> Optional[str]:
        """Run the pipeline for one project; return output dir path."""
        workspace_dir = str(_REPO_ROOT)
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
        ]
        # Forward all feature flags via shared helper
        cmd.extend(_build_feature_flag_args(args))

        print(
            f"[BatchRunner] Running pipeline for: {os.path.basename(project_dir)}",
            file=sys.stderr,
            flush=True,
        )
        run_start = time.time()
        batch_timeout = _env_int("ENHANCED_BATCH_TIMEOUT", 3600)
        try:
            subprocess.run(cmd, check=False, timeout=batch_timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Pipeline timed out for {project_dir}")
        except OSError as exc:
            raise RuntimeError(f"Subprocess error: {exc}")

        return _find_latest_run_dir(workspace_dir, created_after=run_start)

    run_batch(batch_dir, _run_project, max_workers=max_workers)


def cmd_compare(args: argparse.Namespace) -> None:
    """Compare two run output directories."""
    from crucible.features.run_diff import compare_runs

    run_a: str = args.run_a
    run_b: str = args.run_b

    if not os.path.isdir(run_a):
        print(f"[Compare] Error: run_a '{run_a}' is not a directory.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(run_b):
        print(f"[Compare] Error: run_b '{run_b}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    report = compare_runs(run_a, run_b)
    print(report.summary_text())
    print(
        f"\nFull report: {os.path.join(run_b, 'comparison_report.json')}",
        flush=True,
    )


def cmd_postprocess(args: argparse.Namespace) -> None:
    """Apply post-processing features to an existing run directory."""
    run_dir: str = args.run_dir
    if not os.path.isdir(run_dir):
        print(f"[PostProcess] Error: '{run_dir}' is not a directory.", file=sys.stderr)
        sys.exit(1)
    _run_postprocessing(run_dir, args)


def cmd_chat(args: argparse.Namespace) -> None:
    """Start an interactive post-analysis Q&A session for a completed run."""
    run_dir: str = args.run_dir
    if not os.path.isdir(run_dir):
        print(f"[Chat] Error: '{run_dir}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    llm = _try_get_llm()
    if llm is None:
        print(
            "[Chat] Could not initialise LLM. Ensure API keys are configured.",
            file=sys.stderr,
        )
        sys.exit(1)

    from crucible.features.post_analysis_chat import start_post_analysis_chat
    start_post_analysis_chat(run_dir, llm)


def cmd_metrics(args: argparse.Namespace) -> None:
    """Compute and display agent performance metrics across all indexed runs."""
    from crucible.features.agent_metrics import compute_agent_metrics

    workspace_dir = str(_REPO_ROOT)
    report = compute_agent_metrics(workspace_dir)
    print(report.summary_text(), flush=True)

    output_path = getattr(args, "metrics_output", None) or os.path.join(
        workspace_dir, "agent_metrics_report.json"
    )
    try:
        report.save_json(output_path)
        print(f"\n[Metrics] Report saved to {output_path}", flush=True)
    except OSError as exc:
        print(f"[Metrics] Failed to save report: {exc}", file=sys.stderr, flush=True)


# ── Argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_crucible_enhanced.py",
        description=(
            "Enhanced Crucible runner.\n"
            "Adds batch, watch, compare, and post-processing features on top of\n"
            "the core pipeline.  Run without a subcommand to use standard mode."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── run ──────────────────────────────────────────────────────────────────
    run_p = subparsers.add_parser(
        "run",
        help="Run core pipeline with optional pre/post features.",
        # allow_unknown_args so core CLI flags pass through
        add_help=False,
    )
    run_p.add_argument(
        "--project-dir",
        dest="project_dir",
        default=None,
        help="Project directory path (used only for diff-aware context).",
    )
    run_p.add_argument(
        "--diff-aware",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Display git-diff context before the run (informational).",
    )
    run_p.add_argument(
        "--diff-base-ref",
        dest="diff_base_ref",
        default="HEAD~1",
        metavar="REF",
        help="Git ref for diff comparison (default: HEAD~1).",
    )
    run_p.add_argument(
        "--use-memory",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_PROJECT_MEMORY", True),
        help="Save/load project memory across runs (default: on).",
    )
    run_p.add_argument(
        "--security-scan",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SECURITY_SCAN", True),
        help="Run security scan on generated code (default: on).",
    )
    run_p.add_argument(
        "--deployment-artifacts",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEPLOYMENT_ARTIFACTS", True),
        help="Generate Dockerfile / docker-compose / CI workflow (default: on).",
    )
    run_p.add_argument(
        "--generate-tests",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_GENERATE_TESTS", False),
        help="Generate pytest test suite via LLM (default: off).",
    )
    run_p.add_argument(
        "--api-autopatch",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_API_AUTOPATCH", False),
        help="Auto-patch deprecated API calls via LLM (default: off).",
    )
    run_p.add_argument(
        "--independent-validation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_INDEPENDENT_VALIDATION", False),
        help="Run independent subprocess + adversarial LLM validation (default: off).",
    )
    run_p.add_argument(
        "--ci-output",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_CI_OUTPUT", False),
        help="Write GitHub Actions annotations and step summary (default: off).",
    )
    run_p.add_argument(
        "--auto-remediation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_AUTO_REMEDIATION", False),
        help="Auto-fix HIGH+ security/validation findings via LLM (default: off).",
    )
    run_p.add_argument(
        "--dependency-audit",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEPENDENCY_AUDIT", False),
        help="Run pip-audit on generated requirements.txt (default: off).",
    )
    run_p.add_argument(
        "--html-report",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_HTML_REPORT", False),
        help="Generate self-contained HTML report (default: off).",
    )
    run_p.add_argument(
        "--code-quality",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_CODE_QUALITY", False),
        help="Run AST-based code quality analysis (default: off).",
    )
    run_p.add_argument(
        "--run-registry",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_RUN_REGISTRY", False),
        help="Index run into SQLite registry (default: off).",
    )
    # v1.1.8 — Direction Debate Audit Mode CLI flags.  Each translates into
    # an env var override in cmd_run BEFORE the core pipeline imports any
    # section module, so the section_02 orchestration sees the correct
    # values at run time.  All flags are additive — default behaviour is
    # off, preserving pre-v1.1.8 sequential debate flow.
    run_p.add_argument(
        "--audit-mode",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("CRUCIBLE_DEBATE_AUDIT_MODE", False),
        help=(
            "Enable Direction Debate Audit Mode: each specialist emits a "
            "structured AUDIT_FINDING block; the Judge emits a GATE_VERDICT "
            "(PROCEED/BRANCH/KILL/NEEDS_MORE_DATA).  v1.1.8 is observation-"
            "only — the audit ledger captures the disagreement trace but "
            "the legacy force-none flow is unchanged (default: off)."
        ),
    )
    run_p.add_argument(
        "--debate-isolation",
        choices=["sequential", "hybrid"],
        default=(
            os.environ.get("CRUCIBLE_DEBATE_ISOLATION_MODE", "sequential") or "sequential"
        ).strip().lower(),
        help=(
            "Direction-debate context-sharing mode.  sequential (default) = "
            "full prior task output passed via Task.context (legacy v1.1.7 "
            "behaviour).  hybrid = prior agents' free-form chain-of-thought "
            "is marked untrusted; only their structured AUDIT_FINDING blocks "
            "are authoritative — reduces sequential anchoring bias."
        ),
    )
    run_p.add_argument(
        "--external-critic",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("CRUCIBLE_DEBATE_EXTERNAL_CRITIC", False),
        help=(
            "Enable the External Critic (sixth agent) that re-judges the "
            "Judge's verdict using ONLY the raw research evidence + Judge's "
            "decision token.  Isolated from prior agents' chain-of-thought.  "
            "v1.1.8 uses the same model family as Judge (default: off)."
        ),
    )
    run_p.add_argument(
        "--critic-can-override",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED", False),
        help=(
            "Allow the External Critic's KILL verdict to override Judge "
            "PROCEED.  When off (default), Critic dissent is recorded in "
            "the audit trail but the Judge verdict stands.  Recommended to "
            "keep off until the Critic has been calibrated on real loads."
        ),
    )
    # v1.1.8 extended — Direction Gate Tuning per-run flag.  Allows the
    # direction-debate gate to degrade to low-confidence proceed instead
    # of force-none after N consecutive refinement iterations with the
    # same gate reason.  Orthogonal to --audit-mode (that one is
    # observation-only; this one changes the actual gate decision path).
    # Hard feasibility failures are NEVER downgraded.
    run_p.add_argument(
        "--tolerate-unverifiable-evidence",
        dest="tolerate_unverifiable_evidence",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE", False),
        help=(
            "Allow the direction-debate gate to degrade to low-confidence "
            "proceed instead of force-none after N consecutive refinement "
            "iterations with the same gate reason.  Hard feasibility "
            "failures are NEVER downgraded.  Useful for niche topics where "
            "Tier-1 cross-validated sources do not exist (default: off)."
        ),
    )
    run_p.add_argument(
        "--backtest-runner",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_BACKTEST_RUNNER", False),
        help=(
            "Run automated backtest pipeline for Quant mode projects: "
            "auto-prepare data, execute backtest, parameter sweep, "
            "and LLM-driven code fix loop (default: off)."
        ),
    )
    run_p.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Send post-run notifications via configured webhooks (default: off).",
    )
    run_p.add_argument(
        "--interactive",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_INTERACTIVE", False),
        help=(
            "Pause before the run and collect research guidance (focus areas, "
            "constraints, hypotheses) via stdin (default: off). "
            "No-op in non-TTY / CI environments."
        ),
    )
    run_p.add_argument(
        "--dedup-check",
        dest="dedup_check",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEDUP_CHECK", False),
        help=(
            "Check for semantically similar past runs before starting. "
            "Prompts for confirmation when a duplicate is found (default: off)."
        ),
    )
    run_p.add_argument(
        "--external-data",
        dest="external_data",
        default=None,
        metavar="SOURCES",
        help=(
            "Comma-separated external data source(s) to fetch before the backtest "
            "(e.g. coingecko,fred). Downloads CSV files to {run_dir}/code/data/ "
            "so backtest_runner uses real market data automatically."
        ),
    )
    run_p.add_argument(
        "--external-symbols",
        dest="external_symbols",
        default=None,
        metavar="SYMBOLS",
        help="Comma-separated symbols for --external-data (e.g. BTC,ETH,SP500).",
    )
    run_p.add_argument(
        "--external-start",
        dest="external_start",
        default="",
        metavar="DATE",
        help="Start date for --external-data in YYYY-MM-DD format (default: 1 year ago).",
    )
    run_p.add_argument(
        "--external-end",
        dest="external_end",
        default="",
        metavar="DATE",
        help="End date for --external-data in YYYY-MM-DD format (default: today).",
    )
    # Document ingestion
    run_p.add_argument(
        "--ingest-docs",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_INGEST_DOCS", False),
        help="Inject local documents from --ingest-docs-dir into pipeline context (default: off).",
    )
    run_p.add_argument(
        "--ingest-docs-dir",
        dest="ingest_docs_dir",
        default=os.environ.get("ENHANCED_INGEST_DOCS_DIR", ""),
        metavar="DIR",
        help="Directory of documents to inject (PDF/MD/TXT/DOCX).",
    )
    # GitHub repo analysis
    run_p.add_argument(
        "--github-repo",
        dest="github_repo",
        default=os.environ.get("ENHANCED_GITHUB_REPO", ""),
        metavar="URL",
        help="GitHub repository URL to analyse and inject as research context.",
    )
    # Multi-language codegen
    run_p.add_argument(
        "--multilang-codegen",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_MULTILANG_CODEGEN", False),
        help="Generate TypeScript/Go translations of Stage 4 Python output (default: off).",
    )
    run_p.add_argument(
        "--multilang-langs",
        dest="multilang_langs",
        default=os.environ.get("ENHANCED_MULTILANG_LANGS", "typescript,go"),
        metavar="LANGS",
        help="Comma-separated target languages for --multilang-codegen (default: typescript,go).",
    )
    # Post-analysis chat
    run_p.add_argument(
        "--post-chat",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_POST_CHAT", False),
        help="Start interactive Q&A about the analysis after the run completes (default: off).",
    )
    # Agent metrics
    run_p.add_argument(
        "--agent-metrics",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_AGENT_METRICS", False),
        help="Compute and display agent performance metrics after the run (default: off).",
    )
    # Prompt version tracking
    run_p.add_argument(
        "--prompt-version-label",
        dest="prompt_version_label",
        default=os.environ.get("ENHANCED_PROMPT_VERSION_LABEL", ""),
        metavar="LABEL",
        help="Prompt version label to record the run's quality score against.",
    )
    # Per-stage model overrides
    run_p.add_argument(
        "--librarian-model",
        dest="librarian_model",
        default="",
        metavar="MODEL_ID",
        help=(
            "Override the librarian/research-agent model ID for this run. "
            "Injected as env vars (OPENROUTER_LIBRARIAN_MODEL, LIBRARIAN_MODEL, "
            "RESEARCH_MODEL, and provider-specific variants) before the core "
            "pipeline starts."
        ),
    )
    run_p.add_argument(
        "--primary-model",
        dest="primary_model",
        default="",
        metavar="MODEL_ID",
        help=(
            "Override the primary/coding-agent model ID for this run. "
            "Injected as env vars (OPENROUTER_PRIMARY_MODEL, PRIMARY_MODEL, "
            "and provider-specific variants) before the core pipeline starts."
        ),
    )
    run_p.add_argument(
        "--direction-judge-model",
        dest="direction_judge_model",
        default="",
        metavar="MODEL_ID",
        help=(
            "Override the direction-judge model ID for this run. "
            "Injected as env vars (OPENROUTER_DIRECTION_JUDGE_MODEL, "
            "DIRECTION_JUDGE_MODEL, and provider-specific variants) before "
            "the core pipeline starts."
        ),
    )
    # Quant analytics suite
    run_p.add_argument(
        "--quant-analytics",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_QUANT_ANALYTICS", False),
        help=(
            "Run Walk-Forward Validation and Statistical Significance Testing "
            "after a Quant mode backtest (default: off)."
        ),
    )
    run_p.add_argument(
        "--walk-forward",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_WALK_FORWARD", True),
        help="Enable walk-forward validation within --quant-analytics (default: on).",
    )
    run_p.add_argument(
        "--significance-test",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SIGNIFICANCE_TEST", True),
        help="Enable permutation/bootstrap significance test within --quant-analytics (default: on).",
    )
    run_p.add_argument(
        "--regime-detection",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_REGIME_DETECTION", False),
        help="Detect market regimes (bull/bear/sideways) from backtest price data (default: off).",
    )
    run_p.add_argument(
        "--regime-method",
        dest="regime_method",
        default=os.environ.get("REGIME_METHOD", "volatility"),
        metavar="METHOD",
        help="Regime detection method: volatility | trend | hmm (default: volatility).",
    )
    run_p.add_argument(
        "--factor-analysis",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_FACTOR_ANALYSIS", False),
        help="Run CAPM/Fama-French factor exposure regression (default: off).",
    )
    run_p.add_argument(
        "--transaction-cost",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_TRANSACTION_COST", False),
        help="Run transaction cost sensitivity analysis (default: off).",
    )
    run_p.add_argument(
        "--monte-carlo",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_MONTE_CARLO", False),
        help="Run Monte Carlo simulation and stress tests (default: off).",
    )
    run_p.add_argument(
        "--tearsheet",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_TEARSHEET", False),
        help="Generate rich Markdown strategy tearsheet integrating all reports (default: off).",
    )
    run_p.add_argument(
        "--signal-analysis",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SIGNAL_ANALYSIS", False),
        help="Run signal decay analysis to measure edge half-life (default: off).",
    )
    run_p.add_argument(
        "--risk-attribution",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_RISK_ATTRIBUTION", False),
        help="Compute component VaR and marginal VaR risk attribution for the run (default: off).",
    )
    # Quant analytics — remaining features
    run_p.add_argument(
        "--cointegration",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_COINTEGRATION", False),
        help="Run cointegration + pairs trading analysis on multi-asset data (default: off).",
    )
    run_p.add_argument(
        "--dynamic-correlation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DYNAMIC_CORRELATION", False),
        help="Compute rolling correlation matrix and PCA decomposition (default: off).",
    )
    run_p.add_argument(
        "--lockfile-gen",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_LOCKFILE_GEN", False),
        help="Generate pyproject.toml + pinned requirements.txt for generated code (default: off).",
    )
    # Feature bundle
    run_p.add_argument(
        "--v169-features",
        dest="v169_features",
        default=os.environ.get("V169_FEATURES", ""),
        metavar="FEATURES",
        help=(
            "Comma-separated list of post-processing features to run. "
            "Available: model_cascade, semantic_cache, few_shot_injector, "
            "global_knowledge_base, llm_quality_scorer, options_analyzer, "
            "alt_data_connectors, market_stream, trading_platform, scheduler, "
            "yaml_pipeline, multi_project_compare, webhook_templates, "
            "prometheus_exporter, grafana_dashboard, redis_cache, celery_worker, "
            "auth_manager, report_annotations, notion_export, chat_bot, "
            "sandbox_executor, type_coverage, citation_verifier, config_wizard. "
            "Example: --v169-features model_cascade,prometheus_exporter,citation_verifier"
        ),
    )
    run_p.set_defaults(func=cmd_run)

    # ── watch ────────────────────────────────────────────────────────────────
    watch_p = subparsers.add_parser(
        "watch",
        help="Watch a directory for changes and re-run the pipeline.",
    )
    watch_p.add_argument(
        "watch_dir",
        nargs="?",
        default=os.getcwd(),
        help="Directory to watch (default: current directory).",
    )
    watch_p.add_argument(
        "--watch-debounce",
        dest="watch_debounce",
        type=float,
        default=_env_float("ENHANCED_WATCH_DEBOUNCE_SECONDS", 30.0),
        metavar="SECONDS",
        help="Debounce delay in seconds (default: 30).",
    )
    watch_p.add_argument(
        "--run-immediately",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Trigger one run immediately on start (default: on).",
    )
    watch_p.add_argument(
        "--security-scan",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SECURITY_SCAN", True),
    )
    watch_p.add_argument(
        "--deployment-artifacts",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEPLOYMENT_ARTIFACTS", True),
    )
    watch_p.add_argument(
        "--use-memory",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_PROJECT_MEMORY", True),
    )
    watch_p.add_argument(
        "--independent-validation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_INDEPENDENT_VALIDATION", False),
    )
    # Per-stage model overrides (kept consistent with run subcommand)
    watch_p.add_argument(
        "--librarian-model",
        dest="librarian_model",
        default="",
        metavar="MODEL_ID",
        help="Override the librarian/research-agent model ID for triggered runs.",
    )
    watch_p.add_argument(
        "--primary-model",
        dest="primary_model",
        default="",
        metavar="MODEL_ID",
        help="Override the primary/coding-agent model ID for triggered runs.",
    )
    watch_p.add_argument(
        "--direction-judge-model",
        dest="direction_judge_model",
        default="",
        metavar="MODEL_ID",
        help="Override the direction-judge model ID for triggered runs.",
    )
    # Extended feature flags (forwarded to each triggered run)
    watch_p.add_argument("--backtest-runner", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_BACKTEST_RUNNER", False))
    watch_p.add_argument("--ingest-docs", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_INGEST_DOCS", False))
    watch_p.add_argument("--ingest-docs-dir", dest="ingest_docs_dir",
                         default=os.environ.get("ENHANCED_INGEST_DOCS_DIR", ""), metavar="DIR")
    watch_p.add_argument("--github-repo", dest="github_repo",
                         default=os.environ.get("ENHANCED_GITHUB_REPO", ""), metavar="URL")
    watch_p.add_argument("--multilang-codegen", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_MULTILANG_CODEGEN", False))
    watch_p.add_argument("--multilang-langs", dest="multilang_langs",
                         default=os.environ.get("ENHANCED_MULTILANG_LANGS", "typescript,go"), metavar="LANGS")
    watch_p.add_argument("--agent-metrics", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_AGENT_METRICS", False))
    watch_p.add_argument("--lockfile-gen", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_LOCKFILE_GEN", False))
    # Quant analytics suite (forwarded to each triggered run)
    watch_p.add_argument("--quant-analytics", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_QUANT_ANALYTICS", False))
    watch_p.add_argument("--walk-forward", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_WALK_FORWARD", True))
    watch_p.add_argument("--significance-test", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_SIGNIFICANCE_TEST", True))
    watch_p.add_argument("--regime-detection", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_REGIME_DETECTION", False))
    watch_p.add_argument("--factor-analysis", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_FACTOR_ANALYSIS", False))
    watch_p.add_argument("--transaction-cost", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_TRANSACTION_COST", False))
    watch_p.add_argument("--monte-carlo", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_MONTE_CARLO", False))
    watch_p.add_argument("--tearsheet", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_TEARSHEET", False))
    watch_p.add_argument("--signal-analysis", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_SIGNAL_ANALYSIS", False))
    watch_p.add_argument("--risk-attribution", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_RISK_ATTRIBUTION", False))
    watch_p.add_argument("--cointegration", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_COINTEGRATION", False))
    watch_p.add_argument("--dynamic-correlation", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_DYNAMIC_CORRELATION", False))
    watch_p.set_defaults(func=cmd_watch)

    # ── batch ────────────────────────────────────────────────────────────────
    batch_p = subparsers.add_parser(
        "batch",
        help="Run the pipeline for all projects in a directory.",
    )
    batch_p.add_argument(
        "batch_dir",
        help="Root directory containing project sub-directories.",
    )
    batch_p.add_argument(
        "--batch-workers",
        dest="batch_workers",
        type=int,
        default=_env_int("ENHANCED_BATCH_MAX_WORKERS", 1),
        metavar="N",
        help="Max parallel workers (default: 1 — sequential).",
    )
    batch_p.add_argument(
        "--security-scan",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SECURITY_SCAN", True),
    )
    batch_p.add_argument(
        "--deployment-artifacts",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEPLOYMENT_ARTIFACTS", True),
    )
    batch_p.add_argument(
        "--independent-validation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_INDEPENDENT_VALIDATION", False),
    )
    batch_p.add_argument(
        "--use-memory",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_PROJECT_MEMORY", True),
        help="Persist and load project memory across batch runs (default: on).",
    )
    # Per-stage model overrides (kept consistent with run subcommand)
    batch_p.add_argument(
        "--librarian-model",
        dest="librarian_model",
        default="",
        metavar="MODEL_ID",
        help="Override the librarian/research-agent model ID for each batch run.",
    )
    batch_p.add_argument(
        "--primary-model",
        dest="primary_model",
        default="",
        metavar="MODEL_ID",
        help="Override the primary/coding-agent model ID for each batch run.",
    )
    batch_p.add_argument(
        "--direction-judge-model",
        dest="direction_judge_model",
        default="",
        metavar="MODEL_ID",
        help="Override the direction-judge model ID for each batch run.",
    )
    # Extended feature flags (forwarded to each project run)
    batch_p.add_argument("--backtest-runner", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_BACKTEST_RUNNER", False))
    batch_p.add_argument("--ingest-docs", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_INGEST_DOCS", False))
    batch_p.add_argument("--ingest-docs-dir", dest="ingest_docs_dir",
                         default=os.environ.get("ENHANCED_INGEST_DOCS_DIR", ""), metavar="DIR")
    batch_p.add_argument("--github-repo", dest="github_repo",
                         default=os.environ.get("ENHANCED_GITHUB_REPO", ""), metavar="URL")
    batch_p.add_argument("--multilang-codegen", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_MULTILANG_CODEGEN", False))
    batch_p.add_argument("--multilang-langs", dest="multilang_langs",
                         default=os.environ.get("ENHANCED_MULTILANG_LANGS", "typescript,go"), metavar="LANGS")
    batch_p.add_argument("--agent-metrics", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_AGENT_METRICS", False))
    batch_p.add_argument("--lockfile-gen", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_LOCKFILE_GEN", False))
    # Quant analytics suite (forwarded to each project run)
    batch_p.add_argument("--quant-analytics", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_QUANT_ANALYTICS", False))
    batch_p.add_argument("--walk-forward", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_WALK_FORWARD", True))
    batch_p.add_argument("--significance-test", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_SIGNIFICANCE_TEST", True))
    batch_p.add_argument("--regime-detection", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_REGIME_DETECTION", False))
    batch_p.add_argument("--factor-analysis", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_FACTOR_ANALYSIS", False))
    batch_p.add_argument("--transaction-cost", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_TRANSACTION_COST", False))
    batch_p.add_argument("--monte-carlo", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_MONTE_CARLO", False))
    batch_p.add_argument("--tearsheet", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_TEARSHEET", False))
    batch_p.add_argument("--signal-analysis", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_SIGNAL_ANALYSIS", False))
    batch_p.add_argument("--risk-attribution", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_RISK_ATTRIBUTION", False))
    batch_p.add_argument("--cointegration", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_COINTEGRATION", False))
    batch_p.add_argument("--dynamic-correlation", action=argparse.BooleanOptionalAction,
                         default=_env_bool("ENHANCED_DYNAMIC_CORRELATION", False))
    batch_p.set_defaults(func=cmd_batch)

    # ── compare ──────────────────────────────────────────────────────────────
    compare_p = subparsers.add_parser(
        "compare",
        help="Compare two run output directories.",
    )
    compare_p.add_argument(
        "run_a",
        help="Baseline run directory (older).",
    )
    compare_p.add_argument(
        "run_b",
        help="Candidate run directory (newer).",
    )
    compare_p.set_defaults(func=cmd_compare)

    # ── postprocess ──────────────────────────────────────────────────────────
    pp_p = subparsers.add_parser(
        "postprocess",
        help="Apply post-processing features to an existing run directory.",
    )
    pp_p.add_argument(
        "run_dir",
        help="Path to a saved run output directory.",
    )
    pp_p.add_argument(
        "--security-scan",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SECURITY_SCAN", True),
    )
    pp_p.add_argument(
        "--deployment-artifacts",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEPLOYMENT_ARTIFACTS", True),
    )
    pp_p.add_argument(
        "--generate-tests",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_GENERATE_TESTS", False),
    )
    pp_p.add_argument(
        "--api-autopatch",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_API_AUTOPATCH", False),
    )
    pp_p.add_argument(
        "--independent-validation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_INDEPENDENT_VALIDATION", False),
    )
    pp_p.add_argument(
        "--ci-output",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_CI_OUTPUT", False),
    )
    pp_p.add_argument(
        "--use-memory",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_PROJECT_MEMORY", False),
        help="Save a memory entry after post-processing (default: off).",
    )
    pp_p.add_argument(
        "--auto-remediation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_AUTO_REMEDIATION", False),
    )
    pp_p.add_argument(
        "--dependency-audit",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DEPENDENCY_AUDIT", False),
    )
    pp_p.add_argument(
        "--html-report",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_HTML_REPORT", False),
    )
    pp_p.add_argument(
        "--code-quality",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_CODE_QUALITY", False),
    )
    pp_p.add_argument(
        "--run-registry",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_RUN_REGISTRY", False),
    )
    pp_p.add_argument(
        "--backtest-runner",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_BACKTEST_RUNNER", False),
    )
    pp_p.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    # Extended additions for postprocess
    pp_p.add_argument(
        "--multilang-codegen",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_MULTILANG_CODEGEN", False),
    )
    pp_p.add_argument(
        "--multilang-langs",
        dest="multilang_langs",
        default=os.environ.get("ENHANCED_MULTILANG_LANGS", "typescript,go"),
        metavar="LANGS",
    )
    pp_p.add_argument(
        "--agent-metrics",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_AGENT_METRICS", False),
    )
    pp_p.add_argument(
        "--prompt-version-label",
        dest="prompt_version_label",
        default=os.environ.get("ENHANCED_PROMPT_VERSION_LABEL", ""),
        metavar="LABEL",
    )
    pp_p.add_argument(
        "--post-chat",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_POST_CHAT", False),
        help="Start interactive Q&A about the analysis after post-processing (default: off).",
    )
    # Quant analytics for postprocess subcommand
    pp_p.add_argument(
        "--quant-analytics",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_QUANT_ANALYTICS", False),
    )
    pp_p.add_argument(
        "--walk-forward",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_WALK_FORWARD", True),
    )
    pp_p.add_argument(
        "--significance-test",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SIGNIFICANCE_TEST", True),
    )
    pp_p.add_argument(
        "--regime-detection",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_REGIME_DETECTION", False),
    )
    pp_p.add_argument(
        "--regime-method",
        dest="regime_method",
        default=os.environ.get("REGIME_METHOD", "volatility"),
        metavar="METHOD",
    )
    pp_p.add_argument(
        "--factor-analysis",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_FACTOR_ANALYSIS", False),
    )
    pp_p.add_argument(
        "--transaction-cost",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_TRANSACTION_COST", False),
    )
    pp_p.add_argument(
        "--monte-carlo",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_MONTE_CARLO", False),
    )
    pp_p.add_argument(
        "--tearsheet",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_TEARSHEET", False),
    )
    pp_p.add_argument(
        "--signal-analysis",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_SIGNAL_ANALYSIS", False),
    )
    pp_p.add_argument(
        "--risk-attribution",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_RISK_ATTRIBUTION", False),
    )
    # Quant analytics — remaining features
    pp_p.add_argument(
        "--cointegration",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_COINTEGRATION", False),
    )
    pp_p.add_argument(
        "--dynamic-correlation",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_DYNAMIC_CORRELATION", False),
    )
    pp_p.add_argument(
        "--lockfile-gen",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("ENHANCED_LOCKFILE_GEN", False),
    )
    # Feature Bundle for postprocess subcommand
    pp_p.add_argument(
        "--v169-features",
        dest="v169_features",
        default=os.environ.get("V169_FEATURES", ""),
        metavar="FEATURES",
        help=(
            "Comma-separated list of post-processing features to run. "
            "Example: --v169-features model_cascade,prometheus_exporter"
        ),
    )
    pp_p.set_defaults(func=cmd_postprocess)

    # ── abtest ───────────────────────────────────────────────────────────────
    ab_p = subparsers.add_parser(
        "abtest",
        help=(
            "Run the pipeline twice with different prompt modifiers and compare "
            "the two analysis results (A/B test)."
        ),
    )
    ab_p.add_argument(
        "--variant-a-label",
        dest="variant_a_label",
        default="variant_a",
        help="Label for Variant A (default: variant_a).",
    )
    ab_p.add_argument(
        "--variant-b-label",
        dest="variant_b_label",
        default="variant_b",
        help="Label for Variant B (default: variant_b).",
    )
    ab_p.add_argument(
        "--variant-a-context",
        dest="variant_a_context",
        default="",
        metavar="TEXT",
        help="Extra context / prompt modifier injected only for Variant A.",
    )
    ab_p.add_argument(
        "--variant-b-context",
        dest="variant_b_context",
        default="",
        metavar="TEXT",
        help="Extra context / prompt modifier injected only for Variant B.",
    )
    ab_p.add_argument(
        "--output-dir",
        dest="output_dir",
        default=None,
        metavar="DIR",
        help=(
            "Directory to write ab_test_report.json "
            "(default: ./ab_test_output)."
        ),
    )
    ab_p.add_argument(
        "--variant-a-args",
        dest="variant_a_args",
        nargs="*",
        default=[],
        metavar="ARG",
        help=(
            "Extra core-CLI flags forwarded ONLY to Variant A "
            "(e.g. --variant-a-args --direction-debate)."
        ),
    )
    ab_p.add_argument(
        "--variant-b-args",
        dest="variant_b_args",
        nargs="*",
        default=[],
        metavar="ARG",
        help=(
            "Extra core-CLI flags forwarded ONLY to Variant B "
            "(e.g. --variant-b-args --api-autopatch)."
        ),
    )
    ab_p.add_argument(
        "--shared-args",
        dest="shared_args",
        nargs=argparse.REMAINDER,
        default=[],
        help="Additional core-CLI flags forwarded to BOTH variants.",
    )
    ab_p.set_defaults(func=cmd_abtest)

    # ── chat ─────────────────────────────────────────────────────────────────
    chat_p = subparsers.add_parser(
        "chat",
        help="Interactive post-analysis Q&A for a completed run directory.",
    )
    chat_p.add_argument(
        "run_dir",
        help="Path to a completed run output directory.",
    )
    chat_p.set_defaults(func=cmd_chat)

    # ── metrics ──────────────────────────────────────────────────────────────
    metrics_p = subparsers.add_parser(
        "metrics",
        help="Compute and display agent performance metrics across all runs.",
    )
    metrics_p.add_argument(
        "--output",
        dest="metrics_output",
        default=None,
        metavar="PATH",
        help="Path to write agent_metrics_report.json (default: workspace root).",
    )
    metrics_p.set_defaults(func=cmd_metrics)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    # v1.1.0: bind the run-correlation contextvar at the very start so every
    # downstream emit (telemetry, structured logs, run_insights ledger)
    # carries a consistent run_id.  When the WebUI spawned this process, the
    # WebUI's own run_id was passed via CRUCIBLE_RUN_ID so its `sess.run_id`
    # matches the ledger entries and the per-run Insights tab populates.
    # Direct CLI invocations fall back to a fresh UUID4.
    try:
        try:
            from crucible.run_correlation import set_run_id as _set_run_id
        except ImportError:
            from run_correlation import set_run_id as _set_run_id  # type: ignore[no-redef]
        # v1.1.2 (sixth-pass H-3): strip before ``or None`` to reject
        # whitespace-only ``CRUCIBLE_RUN_ID`` values that would otherwise
        # bypass set_run_id's own ``.strip()`` defence.
        _set_run_id((os.environ.get("CRUCIBLE_RUN_ID") or "").strip() or None)
    except Exception:
        # Correlation-id binding must never break the pipeline boot.
        pass

    parser = _build_parser()

    # Use parse_known_args so that core CLI flags (--provider, --dry-run, etc.)
    # are not rejected when the "run" subcommand is active.
    known, _unknown = parser.parse_known_args()

    if known.command is None:
        # No subcommand → full pass-through to the original core pipeline.
        # Rebuild sys.argv from the unrecognised tokens so that core CLI flags
        # (--provider, --dry-run, --idea, etc.) are forwarded correctly.
        # Without this, parse_known_args() would silently discard _unknown and
        # the core pipeline would see only sys.argv[0], breaking backward compat.
        sys.argv = [sys.argv[0]] + _unknown
        from crucible.cli import main as _core_main
        _core_main()
        return

    if not hasattr(known, "func"):
        parser.print_help()
        sys.exit(1)

    known.func(known)


if __name__ == "__main__":
    main()
