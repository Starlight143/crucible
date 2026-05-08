"""
crucible.features
========================
Optional post-processing and automation features that extend the core pipeline.

Features are designed to be additive – they never modify the core pipeline
sections and always have safe fallbacks when optional dependencies are absent.

Modules
-------
diff_aware               – Git-diff context injection for incremental analysis.
project_memory           – Persistent cross-run memory (direction history, risks) +
                           optional ChromaDB semantic search backend.
test_generator           – LLM-powered pytest test suite generation.
security_scan            – Static security analysis (bandit + pattern fallback).
api_version_autopatch    – Auto-patch deprecated API calls using LLM.
deployment_artifacts     – Dockerfile / docker-compose / CI / K8s / Helm generation.
run_diff                 – Side-by-side comparison of two run output directories.
ci_cd                    – GitHub Actions annotations and step-summary output.
watch_mode               – File-change watcher that re-triggers analysis.
batch_runner             – Parallel/sequential multi-project batch execution.
independent_validator    – Independent subprocess + adversarial LLM validation.
auto_remediator          – Closed-loop auto-remediation for security/validation findings.
dependency_auditor       – Dependency vulnerability scanning (pip-audit).
checkpoint               – Stage-level checkpointing and resume.
notification_hooks       – Post-run notification hooks (webhook/Slack/Discord).
report_exporter          – Self-contained HTML + PDF report generation.
run_registry             – SQLite-backed run history registry.
code_quality             – AST-based code quality metrics (complexity, LOC, nesting).
interactive_mode         – Pre-run interactive context-gathering session (--interactive).
prompt_ab_test           – Prompt A/B testing: compare two pipeline variant outputs.
external_data_connectors – External market data connectors (Alpha Vantage, CoinGecko, FRED).
run_deduplication        – Semantic run deduplication via TF-IDF cosine similarity.

Extended modules
----------------
document_ingestion       – RAG-style injection of local PDF/Markdown/TXT/DOCX files
                           into pipeline research context.
github_repo_analyzer     – Deep GitHub repository analysis (README, issues, PRs, commits)
                           for grounding pipeline research in real-world signals.
project_profile          – YAML/JSON project profile loader: pre-fills project context
                           (tech stack, constraints, decisions) for every run.
post_analysis_chat       – Interactive post-run Q&A: ask follow-up questions about
                           analysis results without re-running the full pipeline.
multilang_codegen        – Multi-language code translation: generates TypeScript and Go
                           equivalents from Stage 4 Python output using the LLM.
prompt_version_tracker   – SQLite-backed prompt version registry with per-run score
                           recording and best-version selection.
agent_metrics            – Agent performance metrics dashboard: aggregates historical
                           run quality stats per project with sparkline scores.

Portfolio + tracking modules
----------------------------
portfolio_backtest       – Portfolio-level backtesting: combine multiple strategy runs
                           into a weighted portfolio and compute aggregate risk metrics
                           (Sharpe, Sortino, Calmar, max drawdown, correlation matrix).
                           Exports ``portfolio_report.json``. Pure stdlib; no external deps.
mlflow_sink              – MLflow experiment tracking sink. Implements the
                           ``TelemetrySink`` protocol and auto-logs each pipeline run to
                           an MLflow experiment when ``MLFLOW_TRACKING_URI`` is set.
                           Requires ``pip install mlflow``.
backtest_runner          – Backtest parameter search with grid, random, and Bayesian
                           (Optuna TPE) optimisation. Bayesian mode requires
                           ``pip install optuna``.
"""
from __future__ import annotations

__all__ = [
    "diff_aware",
    "project_memory",
    "test_generator",
    "security_scan",
    "api_version_autopatch",
    "deployment_artifacts",
    "run_diff",
    "ci_cd",
    "watch_mode",
    "batch_runner",
    "independent_validator",
    "auto_remediator",
    "dependency_auditor",
    "checkpoint",
    "notification_hooks",
    "report_exporter",
    "run_registry",
    "code_quality",
    "interactive_mode",
    "prompt_ab_test",
    "external_data_connectors",
    "run_deduplication",
    # Extended modules
    "document_ingestion",
    "github_repo_analyzer",
    "project_profile",
    "post_analysis_chat",
    "multilang_codegen",
    "prompt_version_tracker",
    "agent_metrics",
    # Portfolio + tracking
    "portfolio_backtest",
    "mlflow_sink",
    "backtest_runner",
]
