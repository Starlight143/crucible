# Architecture

This document covers the internal structure of Crucible for contributors and integrators. For a high-level overview and quick start, see [README.md](README.md).

---

## Pipeline Internals (`crucible/modules/`)

The runtime namespace is assembled by `module_runtime.py` exclusively from the `modules/` directory. Each section file maps to one or more pipeline stages.

| File | Responsibility |
|------|---------------|
| `section_00_bootstrap_and_utils.py` | Environment initialisation, shared utilities, logging bootstrap |
| `section_01_extraction_and_reformat.py` | Input extraction, schema reformatting, LLM output normalisation |
| `section_02_research_and_llm.py` | LLM client wrappers, provider routing, cost tracking integration |
| `section_03_models_and_context.py` | Pydantic models, `ResearchContext`, context assembly |
| `section_04_web_research_and_direction.py` | Librarian, Research Swarm lanes, Synthesizer, Direction Debate |
| `section_05_analysis_and_codegen.py` | Analysis Crew, Gate Controller, CodeGen pipeline, auto-optimize loop |
| `section_06_runtime_quality_api.py` | Runtime validation, quality loop, API version check, Review & Fix |
| `section_07_selfcheck_output_main.py` | Self-check, output assembly, project-fix entry point, main dispatch |

> Static analysis and code review should focus on `modules/`, `web_research/`, `runtime_logging.py`, and `resilience.py` as the primary source of truth.

---

## Infrastructure Modules (`crucible/`)

Cross-cutting concerns shared by all pipeline stages. None contain business logic.

| Module | Capability | Key API |
|--------|-----------|---------|
| `context_budget.py` | Context window budget management. Estimates message token counts; auto-compresses early messages into structured summaries when the threshold is reached. Supports `prune_raw_tool_results()` to truncate large tool return values. | `ContextBudgetManager.compact()`, `compact_if_needed()`, `prune_raw_tool_results()` |
| `cost_tracker.py` | Per-stage LLM cost accumulation. `cost_context()` context manager records `StageUsage` on stage exit; raises `StageBudgetExceededError` when the budget is exceeded. | `CostTracker`, `cost_context()`, `get_tracker()` |
| `progress.py` | Event-driven progress reporting. `ProgressReporter` emits typed `ProgressEvent` (STARTED / STEP / PROGRESS / WARNING / FINISHED / FAILED). `OperationCancelledError` does not trigger a FAILED event — it propagates directly to the caller. | `ProgressReporter`, `stage_context()`, `ConsoleProgressListener` |
| `feature_registry.py` | Feature registration and topological sort. `@register` declares a feature and its dependencies; `resolve_order()` uses Kahn's algorithm; `run_features()` executes in order. | `@register`, `resolve_order()`, `run_features()` |
| `_file_cache.py` | LRU file content cache with `mtime_ns`-based invalidation. Thread-safe. `read_text` / `read_bytes` / `get_or_read`; `stats()` / `hit_rate` for observability. | `FileCache`, `get_default_cache()` |
| `convergence_guard.py` | Feedback loop convergence protection. `LoopConvergenceGuard` sets an iteration cap, a wall-clock timeout, and detects stale state (repeated signatures) to prevent infinite LLM-driven loops. | `LoopConvergenceGuard`, `ConvergenceError`, `StaleLoopWarning` |
| `hooks.py` | Post-stage hook system. `register_stage_hook` / `hook_for` inject callbacks after stage completion; `execute_stage_hooks` runs them with a per-stage sequential lock and full exception isolation. | `register_stage_hook()`, `hook_for()`, `execute_stage_hooks()`, `HookContext` |
| `http_retry.py` | HTTP retry decorator. `@with_http_retry` adds retry logic to any HTTP call function; `is_http_retryable` identifies transient errors from httpx / requests / urllib3; `safe_get` / `safe_post` add retry + size protection for one-shot calls. | `@with_http_retry`, `safe_get()`, `safe_post()`, `HttpRetryConfig` |
| `error_budget.py` | Per-stage error budget tracking. Each stage configures a maximum failure count; exceeding it raises `BudgetExhaustedError`. All error events are written to a JSONL audit log. | `configure_budget()`, `record_error()`, `ErrorAuditLog`, `BudgetExhaustedError` |
| `telemetry.py` | Non-blocking structured telemetry. `emit()` returns immediately; a background daemon thread dispatches events asynchronously to registered sinks (`JsonlFileSink` built-in). `flush()` drains the queue before process exit. | `emit()`, `add_sink()`, `JsonlFileSink`, `TelemetryEvent` |
| `resilience.py` | Circuit breaker + retry with exponential backoff. Circuit key does not include `id(crew)` — rebuilding a crew does not reset the failure count. | `CircuitBreaker`, `with_retry()` |
| `runtime_logging.py` | Structured logging with context propagation. JSON log format toggle; per-request correlation ID via `ContextVar`. | `get_logger()`, `log_context()` |
| `cancellation.py` | Cooperative cancellation token. `OperationCancelledError` is propagated through all stage boundaries without being misclassified as a pipeline failure. | `CancellationToken`, `OperationCancelledError` |
| `streaming.py` | SSE streaming bridge between the pipeline subprocess and the WebUI backend. | `StreamBridge`, `stream_run_output()` |

---

## Feature Modules (`crucible/features/`)

Post-processing and enhancement modules invoked by the Enhanced Runner after Stage 4 completes, or before the pipeline starts (pre-processing). All are opt-in via CLI flags or environment variables.

### Pre-processing

| Module | Flag | Description |
|--------|------|-------------|
| `interactive_mode.py` | `--interactive` | Guided pre-pipeline context collection: focus areas, constraints, risk preference, hypotheses. Injects result via `PIPELINE_INTERACTIVE_CONTEXT`. |
| `run_deduplication.py` | `--dedup-check` | Semantic duplicate run detection using TF-IDF cosine similarity against historical `analysis_result.json` summaries. Upgrades to scikit-learn bigrams when available. |
| `document_ingestion.py` | `--ingest-docs` | RAG-style local document injection. Scans PDF / Markdown / TXT / DOCX; extracts and truncates to character budget; injects into research context. |
| `github_repo_analyzer.py` | `--github-repo` | Fetches README, issues, closed PRs, commits, and repo metadata from a GitHub repository and injects as research context. Memory-cached with TTL. |
| `project_profile.py` | auto | Loads `project_profile.yaml` / `.json` from CWD or `PIPELINE_PROJECT_PROFILE`. Injects project name, tech stack, known constraints, and prior decisions into context. |
| `diff_aware.py` | `--diff-aware` | Runs `git diff` against a base ref before pipeline start; displays changed files. Informational only — does not alter pipeline behaviour. |

### Post-processing

| Module | Flag | Description |
|--------|------|-------------|
| `security_scan.py` | `--security-scan` | Static security scan of generated `code/`. Uses `bandit` when installed; falls back to 14 built-in regex rules (eval, hardcoded credentials, SQL injection, unsafe deserialization, etc.). |
| `deployment_artifacts.py` | `--deployment-artifacts` | AST-detects web framework (FastAPI / Flask / Django / aiohttp / Streamlit) and ORM (SQLAlchemy / Alembic). Generates multi-stage Dockerfile, docker-compose, `.env.example`, GitHub Actions CI, K8s Deployment + Service manifests, Helm chart. Auto-detects a free local port shared across all artifacts. |
| `test_generator.py` | `--generate-tests` | LLM-generates pytest test files for each Python source file in `code/`. Includes syntax validation and one LLM retry on `SyntaxError`. |
| `api_version_autopatch.py` | `--api-autopatch` | Reads API version report from `run_snapshot.json`; LLM-generates patches for deprecated API calls and applies them directly to source files. |
| `independent_validator.py` | `--independent-validation` | **Phase B** (no LLM): `py_compile` syntax check, `pytest` execution, `main.py --help` smoke check. **Phase A** (LLM): adversarial code review using an independent persona. Strips credentials from subprocess env before execution. |
| `auto_remediator.py` | `--auto-remediation` | Closed-loop remediation: collect HIGH+ issues from security scan and validation → LLM patch → syntax check → apply → re-scan. Up to N rounds. |
| `backtest_runner.py` | `--backtest-runner` | Quant mode only. Detects backtest entry point; fetches real OHLCV data (project `data_provider.py` → yfinance → Binance public API → synthetic GBM fallback); runs in isolated subprocess; parses Sharpe / Drawdown / Win Rate; LLM-fixes failures; grid/random search for parameter optimisation. |
| `dependency_auditor.py` | `--dependency-audit` | Runs `pip-audit` against `code/requirements.txt`. Detects CVE / PYSEC vulnerabilities and lists fix versions. Gracefully skips if `pip-audit` is not installed. |
| `code_quality.py` | `--code-quality` | AST-based metrics: McCabe cyclomatic complexity, LOC breakdown, function length, nesting depth, parameter count. Warns on values exceeding thresholds. |
| `report_exporter.py` | `--html-report` | Merges all run artifacts into a single self-contained HTML report (dark theme, inline CSS, no external dependencies). |
| `run_registry.py` | `--run-registry` | SQLite-backed run index. Upserts completed runs; supports cross-run queries (highest score, project history, failed security scans). WAL journal mode; thread-safe with `threading.Lock`. |
| `notification_hooks.py` | `--notify` | Post-run webhook notifications. Supports generic HTTP POST, Slack Incoming Webhook, Discord Webhook. `NOTIFY_ON_FAIL_ONLY=1` sends only on failure. |
| `ci_cd.py` | `--ci-output` | Outputs GitHub Actions workflow commands (`::error`, `::warning`, `::notice`) to `github_annotations.txt` and a Markdown step summary to `ci_summary.md`. Auto-enabled when `GITHUB_ACTIONS=true`. |
| `multilang_codegen.py` | `--multilang-codegen` | Translates Stage 4 Python output to TypeScript 5.x and Go 1.21+ (Rust 2021 optional via `MULTILANG_ENABLE_RUST=1`). Outputs to `code_<lang>/` directories. |
| `checkpoint.py` | auto | Stage-level checkpointing and resume. `StageState` enum (PENDING / RUNNING / COMPLETED / FAILED / SKIPPED). `OperationCancelledError` does not write FAILED — stage remains RUNNING for resume. |
| `project_memory.py` | `--use-memory` | Cross-run persistent memory (JSONL append-only ledger). Records direction decisions, confirmed tech choices, failed experiments, and blocking risks. Rolling token-budget context window injected into analyst prompts. |
| `run_diff.py` | `compare` subcommand | Compares two run directories: score delta, added/resolved blocking risks, direction changes, unified code diff. |
| `watch_mode.py` | `watch` subcommand | File-change monitoring with debounce timer. Uses `watchdog` when available; falls back to polling. Non-blocking lock prevents duplicate triggers. |
| `batch_runner.py` | `batch` subcommand | Scans a directory for Python sub-projects; runs each with `subprocess`; supports limited parallelism (up to 4 workers). Produces `batch_summary.json`. |
| `prompt_ab_test.py` | `abtest` subcommand | Runs two pipeline variants in fully isolated subprocesses. Compares score delta, risk level, gate decision, consensus, blocking risks. Supports `n_runs` multi-round mode with Mann-Whitney U significance testing. |
| `external_data_connectors.py` | `--external-data` | Fetches real market data before pipeline execution. Sources: Alpha Vantage (daily/intraday), CoinGecko (free-tier OHLCV), FRED (economic series). Writes to `code/data/` with manifest. Preflight validates API key existence. |
| `post_analysis_chat.py` | `--post-chat` | Interactive Q&A after run completes. Grounds answers on `analysis_result.json`, `run_snapshot.json`, and generated code (up to 5 files × 2 000 chars). Conversation history persisted to `.postchat_history.json`. |
| `agent_metrics.py` | `--agent-metrics` | Scans all historical runs in `saved_projects/`; computes per-project avg/max/min score, risk distribution, gate pass rate, hallucination flag count, security pass rate. Outputs formatted terminal dashboard and `agent_metrics_report.json`. |
| `prompt_version_tracker.py` | `--prompt-version-label` | SQLite-backed prompt version management. Records per-run scores against a version label. `get_best_version()` returns the label with the highest average score. |

### Mode-specific Validation Matrix (v1.0.5 round 3 final)

`section_06_runtime_quality_api` runs different defence layers per pipeline mode. The canonical mapping lives in `crucible/features/mode_validation_matrix.py`; the rendered table below is a snapshot of `mode_validation_summary_markdown()` and is regenerated whenever the matrix changes.

| Mode | Defence | Status | Rules | Notes |
|------|---------|--------|-------|-------|
| `quant` | import_smoke | active | Q010, Q011 | Subprocess import of every Quant entrypoint; surfaces import-time errors. |
| `quant` | cross_reference | active | X001, X002, X003, X004, W001, W002, W003 | AST cross-file consistency: dataclass kwargs, config attrs, missing imports, positional types, escape paths. |
| `quant` | domain_lint | active | Q001, Q002, Q003, Q004 | Lookahead bias (4 escape paths), off-by-one stop window, Trade(spread=0), fixed slippage with dynamic flag. |
| `quant` | synthetic_dryrun | active | Q012, Q013, Q014, Q015 | GBM OHLCV subprocess run of backtest entrypoint; opt-in dirty-data fixture via env var. |
| `quant` | live_trader_smoke | active | Q020, Q021, Q022, Q023, Q024 | ccxt-stubbed import + behavioural SL assertion (40% drawdown ramp). |
| `quant` | production_tests | opt-in | X005 | Enforces tests/*.py when CRUCIBLE_QUANT_REQUIRE_TESTS=1 or codegen_scope='production'. |
| `saas` | web_smoke | active | HTTP-smoke | Existing ASGI/WSGI app start + GET / probe (legacy section_06 path). |
| `saas` | cross_reference | active | X001, X002, X003, X004, W001, W002, W003 | v1.0.5 round 3 final: now runs on all non-Quant modes too. |
| `saas` | mode_specific_lint | active | H001 | Web framework imported but missing from requirements.txt declaration. |
| `saas` | dependency_audit | opt-in | dep-audit | pip-audit via --dependency-audit flag. |
| `saas` | openapi_consistency | deferred | — | OpenAPI spec ↔ route handler consistency — planned for a future minor release (v1.0.6 was skipped — the v1.0.5 → v1.1.0 jump rolled all in-flight items into v1.1.x; these three remain on the roadmap without a pinned version). |
| `agent` | cross_reference | active | X001, X002, X003, X004, W001, W002, W003 | v1.0.5 round 3 final. |
| `agent` | mode_specific_lint | active | A001, A002 | Agent(...) missing role/goal/backstory; Tool/BaseTool missing description. |
| `agent` | tool_use_smoke | deferred | — | Stubbed tool-use round-trip — planned for a future minor release (v1.0.6 was skipped — the v1.0.5 → v1.1.0 jump rolled all in-flight items into v1.1.x; these three remain on the roadmap without a pinned version). |
| `scientist` | cross_reference | active | X001, X002, X003, X004, W001, W002, W003 | v1.0.5 round 3 final. |
| `scientist` | mode_specific_lint | active | S001, S002 | Numerical work without explicit seed/RandomState; missing requirements.txt. |
| `scientist` | data_leakage_check | deferred | — | Train/test split leakage detection — planned for a future minor release (v1.0.6 was skipped — the v1.0.5 → v1.1.0 jump rolled all in-flight items into v1.1.x; these three remain on the roadmap without a pinned version). |

`active` defences run by default. `opt-in` requires an env var or codegen scope flag. `deferred` is tracked-but-unimplemented debt — listed here so it stays visible until the rule lands. Set `CRUCIBLE_UNIVERSAL_CROSSREF=0` to opt out of the universal cross-reference layer for legacy callers (default ON).

---

### Quant Analytics Suite

Activated via `--quant-analytics` and related flags. All are Quant mode only.

| Module | Flag | Description |
|--------|------|-------------|
| `walk_forward_validator.py` | `--walk-forward` | Walk-forward cross-validation. Splits data into N folds; runs OOS evaluation on each; aggregates IS vs OOS Sharpe comparison. |
| `signal_analyzer.py` | `--signal-analysis` | Signal decay and IC (Information Coefficient) analysis. Computes signal half-life and forward return correlations. |
| `regime_detector.py` | `--regime-detection` | Market regime detection via volatility clustering, trend-following SMA, or HMM. Annotates backtest periods by regime. |
| `monte_carlo.py` | `--monte-carlo` | Monte Carlo simulation and stress testing. Generates N paths via bootstrapped returns; computes VaR, CVaR, and probability of ruin. |
| `factor_analyzer.py` | `--factor-analysis` | CAPM and Fama-French factor exposure regression. Reports alpha, beta, and factor loadings. |
| `transaction_cost_model.py` | `--transaction-cost` | Transaction cost sensitivity analysis. Sweeps commission and slippage assumptions; reports break-even cost level. |
| `tearsheet_generator.py` | `--tearsheet` | Generates a strategy tearsheet in Markdown and HTML with Sharpe, Sortino, Calmar, CAGR, max drawdown, and monthly returns heatmap. |
| `cointegration_analyzer.py` | `--cointegration` | Cointegrated pair-trading analysis (Engle-Granger + Johansen tests). Requires ≥ 2 asset CSVs. |
| `dynamic_correlation.py` | `--dynamic-correlation` | Rolling correlation matrix and PCA decomposition. Identifies regime shifts in cross-asset correlations. |

---

## Web Research (`crucible/web_research/`)

| File | Description |
|------|-------------|
| `http_clients.py` | Low-level HTTP helpers with retry, size cap, and bot-detection handling |
| `crew_factory.py` | Factory for constructing CrewAI `Crew` and `Agent` objects from specs |
| `swarm_specs.py` | Agent and task specs for the Research Swarm (Market / Technical / Competitor lanes) and Synthesizer |
| `analysis_specs.py` | Agent and task specs for the Analysis Crew (Research / Risk / Ops / Biz / Critic) and Gate Controller |
