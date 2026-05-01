# Auto-generated from OLD_version/crucible_v14.py.
# Import-based section module. Do not edit manually; regenerate from V14.
from __future__ import annotations

from . import section_00_bootstrap_and_utils as _prev_00
globals().update({k: v for k, v in _prev_00.__dict__.items() if not k.startswith('__')})
from . import section_01_extraction_and_reformat as _prev_01
globals().update({k: v for k, v in _prev_01.__dict__.items() if not k.startswith('__')})
from . import section_02_research_and_llm as _prev_02
globals().update({k: v for k, v in _prev_02.__dict__.items() if not k.startswith('__')})
from . import section_03_models_and_context as _prev_03
globals().update({k: v for k, v in _prev_03.__dict__.items() if not k.startswith('__')})
# threading is not in the globals chain; import after globals.update() to
# avoid shadowing anything from earlier sections.
import threading as _threading_s04  # used only for per-query search cache lock
if __package__ == "crucible.modules":
    from ..web_research.analysis_specs import (
        build_analysis_specs as _build_analysis_specs_from_module,
    )
    from ..web_research.analysis_specs import (
        normalize_rerun_agent_keys as _normalize_rerun_agent_keys_from_module,
    )
    from ..web_research.crew_factory import aggregate_retry_policy as _aggregate_retry_policy
    from ..web_research.crew_factory import build_task_from_spec as _build_task_from_module
    from ..web_research.crew_factory import create_agent_from_spec as _create_agent_from_module
    from ..web_research.http_clients import append_url_query as _append_url_query_from_module
    from ..web_research.http_clients import safe_http_json as _safe_http_json_from_module
    from ..web_research.http_clients import safe_http_text as _safe_http_text_from_module
    from ..web_research.swarm_specs import (
        build_research_swarm_specs as _build_research_swarm_specs_from_module,
    )
else:  # pragma: no cover - direct script fallback
    from web_research.analysis_specs import (
        build_analysis_specs as _build_analysis_specs_from_module,
    )
    from web_research.analysis_specs import (
        normalize_rerun_agent_keys as _normalize_rerun_agent_keys_from_module,
    )
    from web_research.crew_factory import aggregate_retry_policy as _aggregate_retry_policy
    from web_research.crew_factory import build_task_from_spec as _build_task_from_module
    from web_research.crew_factory import create_agent_from_spec as _create_agent_from_module
    from web_research.http_clients import append_url_query as _append_url_query_from_module
    from web_research.http_clients import safe_http_json as _safe_http_json_from_module
    from web_research.http_clients import safe_http_text as _safe_http_text_from_module
    from web_research.swarm_specs import (
        build_research_swarm_specs as _build_research_swarm_specs_from_module,
    )

def _get_mode_config(mode: Optional[str]) -> "ModeConfig":
    normalized = str(mode or "").strip()
    if not normalized:
        raise ValueError(
            f"Mode is required. Available modes: {', '.join(sorted(ModeRegistry.list_names()))}"
        )
    exact = ModeRegistry.get(normalized)
    if exact is not None:
        return exact
    lowered = normalized.lower()
    for name, cfg in ModeRegistry.all_modes().items():
        if name.lower() == lowered:
            return cfg
    raise ValueError(
        f"Unsupported mode {normalized!r}. Available modes: {', '.join(sorted(ModeRegistry.list_names()))}"
    )


_VALID_PROJECT_TYPES: frozenset = frozenset({"quant", "saas", "agent", "scientist"})
_CANONICAL_MODE_NAMES: dict = {
    "quant": "Quant",
    "saas": "SaaS",
    "agent": "Agent",
    "scientist": "Scientist",
}


def _project_type_for_mode(mode: Optional[str]) -> str:
    mode_cfg = _get_mode_config(mode)
    project_type = mode_cfg.name.strip().lower()
    if project_type not in _VALID_PROJECT_TYPES:
        raise ValueError(
            f"Resolved mode config produced invalid project type {project_type!r}. "
            f"Expected one of: {', '.join(sorted(_VALID_PROJECT_TYPES))}"
        )
    return project_type


def _validated_mode_name(mode: Optional[str]) -> str:
    project_type = _project_type_for_mode(mode)
    return _CANONICAL_MODE_NAMES[project_type]


def _validated_mode_project_type(mode_config: "ModeConfig") -> str:
    project_type = str(getattr(mode_config, "name", "") or "").strip().lower()
    if project_type not in _VALID_PROJECT_TYPES:
        raise ValueError(
            f"Resolved mode config produced invalid project type {project_type!r}. "
            f"Expected one of: {', '.join(sorted(_VALID_PROJECT_TYPES))}"
        )
    return project_type


def _mode_code_fix_rule_lines(mode_config: "ModeConfig") -> List[str]:
    # Intentionally does NOT accept a scope parameter.
    # Code fixes are always minimal and conservative regardless of the original
    # generation scope — the goal is to fix the smallest root cause, not to
    # regenerate or expand the codebase.  This prevents full/production-scope
    # rules from accidentally instructing the fixer to add new modules.
    project_type = _validated_mode_project_type(mode_config)
    if project_type == "saas":
        return [
            "- Preserve the existing web entrypoint and request/response contract unless the bug requires a minimal compatibility fix",
            "- Keep framework wiring detectable for runtime validation (for example FastAPI/Flask app objects)",
        ]
    if project_type == "agent":
        return [
            "- Preserve headless execution semantics; do not introduce UI, dashboards, or interactive flows",
            "- Keep startup safe for daemon/systemd execution; avoid long-running side effects at import time",
            "- Preserve deterministic machine-consumable outputs and existing orchestration contracts",
        ]
    if project_type == "scientist":
        return [
            "- Preserve the experiment entrypoint and hyperparameter interface; do not change CLI flags or config keys",
            "- Keep random seed initialisation intact to maintain reproducibility across runs",
            "- Do not alter metric computation or benchmark comparison logic unless the bug is in those routines",
        ]
    return [
        "- Preserve pure-Python execution flow and strategy/runtime semantics",
        "- Avoid introducing web-only abstractions unless the bug explicitly requires them",
    ]


def _mode_codegen_rule_lines(mode_config: "ModeConfig", scope: str = "mvp") -> List[str]:
    project_type = _validated_mode_project_type(mode_config)
    scope = str(scope or "mvp").strip().lower()
    if scope not in ("mvp", "full", "production"):
        scope = "mvp"
    common = [
        "- Output CodeBundle JSON only",
        "- Paths must be relative and must not start with 'code/'",
    ]
    if scope == "mvp":
        common.append("- Keep implementation minimal and executable")
    else:
        common.append(
            "- This is a "
            + scope
            + "-scope build: generate every module completely — no stubs, no placeholders, no 'TODO' comments"
        )
    if project_type == "saas":
        return common + _saas_codegen_rules(scope)
    if project_type == "agent":
        return common + _agent_codegen_rules(scope)
    if project_type == "scientist":
        return common + _scientist_codegen_rules(scope)
    # quant
    return common + _quant_codegen_rules(scope)


def _quant_codegen_rules(scope: str) -> List[str]:
    """Return quant-mode codegen rules for the given scope (mvp | full | production)."""
    # ── Core rules present in ALL scopes ──────────────────────────────────────
    base = [
        "- Build a pure-Python strategy/execution project",
        "- Quant mode must include strategy logic, a backtest runner, a trading/execution module, and a signals/results export module",
        "- Prefer concrete filenames such as strategy.py, backtest.py, trade.py, export.py, and config.py unless the prompt requires equivalent names",
        "- Do not introduce a web framework unless explicitly required by the prompt",
        # ── Data preparation ──
        "- Include a data_provider.py module that fetches real historical OHLCV data from the internet: "
        "(a) PRIMARY: use yfinance (for stocks/ETFs) or ccxt/Binance public API (for crypto) to download real market data, "
        "(b) CACHE: save downloaded data to data/sample_data.csv for reuse, "
        "(c) FALLBACK: load from existing CSV if network is unavailable, "
        "(d) LAST RESORT: generate synthetic GBM sample data only if all fetch methods fail, "
        "(e) VALIDATE: check data integrity (no NaN, monotonic dates, OHLCV > 0)",
        "- The data_provider.py symbol, date range, and data source must be configurable via environment variables "
        "(BACKTEST_SYMBOL, BACKTEST_PERIOD, BACKTEST_DATA_SOURCE) with sensible defaults",
        "- The backtest.py must auto-invoke data_provider to prepare data before running — "
        "the user should never need to manually create or download data for a first run",
        # ── Backtest runner interface (must match automated runner contract) ──
        "- backtest.py: CSV from BACKTEST_DATA_FILE (default data/sample_data.csv)",
        "- backtest.py: last stdout line is JSON with sharpe_ratio, total_return_pct, "
        "max_drawdown_pct, win_rate, trade_count",
        "- data_provider.py: CSV cols date,open,high,low,close,volume (YYYY-MM-DD; float)",
        "- Tunable: '# tunable:NAME=[v1,v2]'; read from BACKTEST_PARAM_<NAME> env",
        # ── Live trading ──
        "- Include a live_trader.py module with a production-ready live/paper trading loop: "
        "connect to broker API, submit orders, track positions, handle fills, and log P&L",
        "- live_trader.py must read all exchange/broker credentials and trading parameters from environment variables "
        "(loaded via python-dotenv from a .env file)",
        "- Include a .env.example file documenting every required and optional environment variable "
        "with safe placeholder values and inline comments",
        # ── Configuration ──
        "- Include a config.py that centralises all configurable parameters: "
        "strategy params, data paths, broker settings, risk limits — loaded from .env via os.environ with sensible defaults",
        "- Strategy parameters (lookback, thresholds, stop-loss, take-profit) must be overridable via "
        "BACKTEST_PARAM_<NAME> environment variables so the backtest runner can perform parameter sweeps",
        # ── Project README ──
        "- Include a README.md with: project description, quick-start instructions, "
        "file-by-file explanation, all CLI flags and environment variables, "
        "backtest usage examples, live trading setup guide, and .env configuration reference",
    ]
    if scope == "mvp":
        return base
    # ── full / production: complete quantitative trading system ───────────────
    full_extra = [
        # Risk management
        "- Include a risk_manager.py module with portfolio-level risk controls: "
        "position sizing (Kelly Criterion, fixed-fraction, and volatility-targeting variants), "
        "max drawdown protection with automatic position reduction, daily loss limits, "
        "and correlation-aware exposure caps across multiple positions",
        # Portfolio management
        "- Include a portfolio.py module for multi-asset portfolio tracking: "
        "real-time position ledger, cash balance, gross/net exposure calculation, "
        "rebalancing logic, and trade-level P&L attribution",
        # Performance analytics
        "- Include a performance.py module with a complete analytics suite: "
        "Sharpe ratio, Sortino ratio, Calmar ratio, CAGR, annualised volatility, "
        "maximum drawdown (value + duration), alpha and beta vs a configurable benchmark, "
        "win rate, profit factor, average win/loss, expectancy, and rolling Sharpe/drawdown windows; "
        "export results to both CSV and an HTML report",
        # CLI
        "- Include a cli.py (or main.py with argparse subcommands): "
        "subcommands must include 'backtest' (run historical simulation), "
        "'live' (start live/paper trading), 'report' (generate performance report from saved results), "
        "and 'optimize' (grid-search over BACKTEST_PARAM_<NAME> environment variables); "
        "all subcommands must accept --symbol, --period, --dry-run, and --log-level flags",
        # Structured logging
        "- Use Python's standard logging module throughout with a structured formatter; "
        "log level must be configurable via LOG_LEVEL environment variable; "
        "emit machine-readable JSON log lines when LOG_FORMAT=json",
        # Code quality
        "- Add complete type annotations (PEP 484) to all public functions and class attributes",
        "- Add docstrings to all public classes and functions explaining purpose, parameters, and return values",
    ]
    if scope == "full":
        return base + full_extra
    # production: full + tests + Docker + CI
    production_extra = full_extra + [
        # Tests
        "- Include a tests/ directory with a complete pytest suite: "
        "tests/test_strategy.py (signal generation correctness), "
        "tests/test_backtest.py (engine P&L arithmetic, position tracking, transaction costs), "
        "tests/test_data_provider.py (data integrity checks, fallback chain), "
        "tests/test_risk_manager.py (limit enforcement, sizing calculations), "
        "tests/test_performance.py (metric calculations vs known values), "
        "tests/conftest.py with shared fixtures (synthetic OHLCV DataFrame, mock broker); "
        "every test must be runnable with 'pytest tests/' and must pass without network access",
        # Docker
        "- Include a Dockerfile (multi-stage: builder + final slim image) and a docker-compose.yml "
        "with services for the backtest runner and an optional data-cache volume; "
        "the image must be startable with 'docker compose run backtest'",
        # CI
        "- Include .github/workflows/ci.yml: "
        "runs on push and pull_request to main; "
        "jobs: lint (ruff or flake8), type-check (mypy --ignore-missing-imports), test (pytest); "
        "uses ubuntu-latest and Python 3.11",
        # requirements
        "- Include a requirements.txt with all runtime dependencies pinned to exact versions "
        "(use == not >=); also include a requirements-dev.txt for test/lint tools",
        # Makefile
        "- Include a Makefile with targets: install, test, lint, typecheck, backtest, live, docker-build",
    ]
    return base + production_extra


def _saas_codegen_rules(scope: str) -> List[str]:
    """Return SaaS-mode codegen rules for the given scope (mvp | full | production)."""
    base = [
        "- Build a FastAPI + Pydantic service by default",
        "- Include a detectable web entrypoint such as app.py or main.py with an importable FastAPI/Flask app",
        "- Prefer a health endpoint when practical",
    ]
    if scope == "mvp":
        return base
    # full / production: complete SaaS service
    full_extra = [
        # Database
        "- Include a database layer using SQLAlchemy 2.0 async (asyncpg driver) with: "
        "database.py (engine, async session factory, Base), "
        "models.py (ORM models), "
        "and an alembic/ directory with alembic.ini and a first migration that creates all tables",
        # Auth
        "- Include JWT-based authentication: "
        "auth.py with token creation (python-jose) and password hashing (passlib bcrypt), "
        "a /auth/register and /auth/login endpoint, "
        "and a FastAPI dependency get_current_user that validates Bearer tokens on protected routes",
        # Schemas and CRUD
        "- Include schemas.py with Pydantic v2 request/response models for every resource "
        "(Create, Update, and Read variants); "
        "include crud.py with async CRUD functions for every model covering create, read, update, delete",
        # Config
        "- Include a settings.py using pydantic-settings BaseSettings; "
        "all secrets (DATABASE_URL, SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES) must come from .env; "
        "include a .env.example with safe placeholders",
        # Error handling
        "- Register FastAPI exception handlers for HTTPException, RequestValidationError, and a custom AppError base class; "
        "error responses must follow {detail: str, code: str} JSON format",
        # Logging
        "- Configure structured logging in a logging_config.py; "
        "emit JSON lines when LOG_FORMAT=json; "
        "include a correlation ID middleware that injects request_id into every log record",
        # Background tasks
        "- Include at least one example of an async background task using FastAPI BackgroundTasks "
        "to demonstrate async job dispatch (e.g. send welcome email, trigger async report)",
    ]
    if scope == "full":
        return base + full_extra
    # production: full + tests + Docker + CI
    production_extra = full_extra + [
        # Tests
        "- Include a tests/ directory with a complete pytest-asyncio suite: "
        "tests/conftest.py with async engine, test DB creation/teardown, and AsyncClient fixture; "
        "tests/test_auth.py (register, login, token validation, protected route access); "
        "tests/test_api.py (CRUD happy-path and error cases for every resource); "
        "every test must be runnable with 'pytest tests/' against a SQLite in-memory test database",
        # Docker
        "- Include a Dockerfile (multi-stage: builder + final slim image) and a docker-compose.yml "
        "with services for the API and a PostgreSQL database; "
        "include a healthcheck on the /health endpoint",
        # CI
        "- Include .github/workflows/ci.yml: "
        "runs on push and pull_request to main; "
        "jobs: lint (ruff), type-check (mypy), test (pytest with PostgreSQL service container); "
        "uses ubuntu-latest and Python 3.11",
        # requirements
        "- Include requirements.txt (pinned == versions) and requirements-dev.txt",
    ]
    return base + production_extra


def _agent_codegen_rules(scope: str) -> List[str]:
    """Return Agent-mode codegen rules for the given scope (mvp | full | production)."""
    base = [
        "- Build a headless Python service/daemon for automation or orchestration",
        "- Prefer a main.py entrypoint suitable for CLI or systemd execution",
        "- Include runtime/orchestration/config modules when needed",
        "- Keep outputs deterministic and machine-consumable; no UI or human-facing flows",
    ]
    if scope == "mvp":
        return base
    # full / production: complete agent/automation service
    full_extra = [
        # Task/job management
        "- Include a job_queue.py module with an async job queue (asyncio.Queue): "
        "JobSpec dataclass (job_id, payload, max_retries, created_at), "
        "JobResult dataclass (job_id, status, output, error, attempts, duration_ms), "
        "and a JobWorker class that processes jobs with configurable concurrency and back-pressure",
        # Retry / circuit breaker
        "- Include a retry.py module with: "
        "an async retry decorator with configurable max_attempts, base_delay, max_delay, and jitter; "
        "a CircuitBreaker class (closed/open/half-open states) with configurable failure_threshold and recovery_timeout; "
        "wrap every external tool call with both retry and circuit breaker",
        # Tool registry
        "- Include a tools.py (or tool_registry.py) with a ToolRegistry class: "
        "tools are registered by name with a callable and a JSON Schema for input validation; "
        "ToolRegistry.execute(name, payload) validates input, calls the tool, and returns a typed ToolResult; "
        "unregistered or invalid-input calls raise descriptive ToolError exceptions",
        # Config
        "- Include a config.py using pydantic-settings BaseSettings: "
        "all tuneable parameters (concurrency, retry limits, tool endpoints, credentials) loaded from .env; "
        "include a .env.example with safe placeholders and inline comments; "
        "validate config on startup and fail fast with a clear error if required values are missing",
        # Structured logging
        "- Emit structured JSON log lines using Python's standard logging with a JSON formatter; "
        "every log record must include: timestamp, level, correlation_id, job_id (if available), message; "
        "LOG_LEVEL must be configurable via environment variable",
        # Graceful shutdown
        "- Implement graceful shutdown: "
        "register SIGINT and SIGTERM handlers; "
        "on signal, stop accepting new jobs, wait for in-flight jobs to complete (with a configurable drain_timeout), "
        "then exit cleanly; "
        "log shutdown progress at each step",
        # Type annotations and docstrings
        "- Add complete type annotations (PEP 484) and docstrings to all public classes and functions",
    ]
    if scope == "full":
        return base + full_extra
    # production: full + tests + Docker + systemd
    production_extra = full_extra + [
        # Tests
        "- Include a tests/ directory with a complete pytest-asyncio suite: "
        "tests/conftest.py with event loop fixture and mock tool stubs; "
        "tests/test_job_queue.py (enqueue, worker processing, retry logic, concurrency limit); "
        "tests/test_tools.py (registry lookup, input validation, circuit breaker transitions); "
        "tests/test_retry.py (backoff timing, max attempts, jitter); "
        "every test must be runnable with 'pytest tests/' without network access",
        # Docker
        "- Include a Dockerfile (multi-stage: builder + final slim image) and a docker-compose.yml; "
        "the service must start with 'docker compose up agent' and honour all env vars from .env",
        # systemd
        "- Include a deploy/agent.service systemd unit file with: "
        "Restart=on-failure, RestartSec=5, EnvironmentFile=/etc/agent/.env, "
        "StandardOutput=journal, StandardError=journal",
        # CI
        "- Include .github/workflows/ci.yml: "
        "runs on push and pull_request to main; "
        "jobs: lint (ruff), type-check (mypy), test (pytest); "
        "uses ubuntu-latest and Python 3.11",
        # requirements
        "- Include requirements.txt (pinned == versions) and requirements-dev.txt",
    ]
    return base + production_extra


def _scientist_codegen_rules(scope: str) -> List[str]:
    """Return Scientist-mode codegen rules for the given scope (mvp | full | production)."""
    base = [
        "- Build a pure-Python research implementation project that faithfully reproduces the algorithm or method described in the referenced paper(s)",
        "- The project must NOT use web frameworks, trading libraries, or automation daemons; keep the dependency footprint minimal and research-focused",
        "- Include an experiment.py (or main.py) as the primary entry point; it must be runnable directly with 'python experiment.py'",
        # Reproducibility
        "- Set and expose a global random seed in config.py (seed=42 default) used by ALL stochastic components (numpy, random, torch, tensorflow); "
        "every run with the same seed must produce bit-for-bit identical outputs",
        # Paper fidelity
        "- Include a references.py (or REFERENCES section in README.md) that lists the full citation(s) of the paper(s) being implemented, "
        "including authors, title, venue/journal, year, and DOI/URL",
        "- Variable names, function names, and comments must align with the notation used in the paper where practical "
        "(e.g. if the paper uses 'alpha' for learning rate, use alpha not lr)",
        # Config
        "- Include a config.py that exposes ALL hyperparameters as named constants with the paper's default values; "
        "hyperparameters must be overridable via environment variables (EXPERIMENT_<PARAM_NAME>) so that grid-search wrappers can sweep them without code changes",
        # Data
        "- Include a data.py (or dataset.py) that loads or generates the dataset used in the paper; "
        "if the dataset is publicly available, fetch it automatically (e.g. via torchvision, sklearn.datasets, uci_datasets, or direct URL download); "
        "if synthetic, generate it with the same distribution described in the paper and cache to data/synthetic_data.npy",
        # Metrics
        "- Include a metrics.py module that computes every quantitative metric reported in the paper "
        "(e.g. accuracy, F1, RMSE, BLEU, FID, AUC-ROC); results must be printed to stdout in a structured table and saved to results/metrics.json",
    ]
    if scope == "mvp":
        return base
    # full / production: ablations, baselines, full experiment harness
    full_extra = [
        # Baseline comparison
        "- Include a baselines.py module that implements at least two competing methods or ablation variants for comparison; "
        "each baseline must expose the same interface as the main method (fit/predict or train/evaluate) so they are drop-in replaceable",
        # Ablation harness
        "- Include an ablation.py module that systematically disables or swaps key components of the algorithm "
        "(guided by the paper's ablation study if present, otherwise by the algorithm's major design choices); "
        "each ablation variant must be registered by name and runnable via '--ablation <name>'",
        # Experiment runner
        "- Include a run_experiments.py script that sweeps hyperparameters over a configurable grid "
        "(read from a YAML/JSON config file), executes each configuration, collects results, and saves a consolidated results/all_runs.csv",
        # Visualisation
        "- Include a plot.py module that reproduces the key figures from the paper "
        "(learning curves, metric tables, comparison bar charts) using matplotlib; "
        "figures are saved to results/figures/ as PNG files with 150 dpi minimum",
        # Logging
        "- Use Python's standard logging module with a structured formatter; "
        "emit experiment metadata (seed, hyperparameters, start/end time, git commit hash if available) at INFO level at the start of every run; "
        "LOG_LEVEL must be configurable via environment variable",
        # Type annotations and docstrings
        "- Add complete type annotations (PEP 484) and docstrings to all public classes and functions",
    ]
    if scope == "full":
        return base + full_extra
    # production: full + tests + Docker + CI
    production_extra = full_extra + [
        # Tests
        "- Include a tests/ directory with a complete pytest suite: "
        "tests/test_experiment.py (verify training converges to expected metric within tolerance, seeded for determinism); "
        "tests/test_metrics.py (metric computations against known reference values); "
        "tests/test_data.py (data loading/generation: shape checks, value range, reproducibility with fixed seed); "
        "tests/test_baselines.py (baseline methods produce plausible outputs without errors); "
        "tests/conftest.py with shared fixtures (tiny synthetic dataset, fixed seed); "
        "every test must run without network access using cached/synthetic data",
        # Docker
        "- Include a Dockerfile (multi-stage: builder + final slim image) and a docker-compose.yml "
        "with a service for the experiment runner and a results/ volume mount; "
        "the experiment must be runnable with 'docker compose run experiment'",
        # CI
        "- Include .github/workflows/ci.yml: "
        "runs on push and pull_request to main; "
        "jobs: lint (ruff), type-check (mypy --ignore-missing-imports), test (pytest); "
        "uses ubuntu-latest and Python 3.11",
        # requirements
        "- Include requirements.txt (pinned == versions) and requirements-dev.txt (test/lint tools)",
        # Makefile
        "- Include a Makefile with targets: install, test, lint, typecheck, experiment, ablation, plot",
    ]
    return base + production_extra


def _mode_gate_controller_guidance(mode_config: "ModeConfig") -> List[str]:
    project_type = _validated_mode_project_type(mode_config)
    common = [
        "- Set ready_for_codegen=false only when the current evidence shows code generation would be misleading, unsafe, or structurally blocked",
        "- Use blocking_risks only for risks that truly prevent an executable baseline from being generated",
        "- Unknowns that can be isolated behind assumptions, config, or TODO-free stubs should reduce score/confidence before they block code generation",
    ]
    if project_type == "agent":
        return common + [
            "- In Agent mode, prioritize technical executability, deterministic behavior, safe orchestration boundaries, and operational safety over PMF-style market validation",
            "- Do NOT block code generation only because protocol demand, pricing, monetization, or operator adoption is not yet validated",
            "- For Agent mode, block code generation only when there is a hard technical contradiction (for example impossible state source, non-deterministic core requirement, undefined critical trust boundary, or unsafe execution semantics)",
            "- Economic uncertainty may remain in blocking_risks only if it makes the proposed agent unsafe to operate by default, not merely commercially uncertain",
        ]
    if project_type == "saas":
        return common + [
            "- In SaaS mode, PMF, distribution, and monetization risks may justify ready_for_codegen=false when they invalidate the product direction",
        ]
    if project_type == "scientist":
        return common + [
            "- In Scientist mode, prioritize algorithmic correctness, reproducibility, and fidelity to the referenced paper over commercial or market validation",
            "- Do NOT block code generation because the algorithm lacks commercial adoption, market demand, or monetisation potential",
            "- Block code generation only when the core algorithm described in the paper is fundamentally ambiguous, self-contradictory, or depends on proprietary data/hardware that cannot be approximated",
            "- If hyperparameters or dataset details are underspecified, allow codegen with documented assumptions rather than blocking",
        ]
    return common + [
        "- In Quant mode, block code generation when the strategy cannot be expressed coherently or when execution assumptions are internally contradictory",
        "- If the user is explicitly asking for a validation/calibration/measurement framework, allow codegen_scope='validation' instead of blocking solely because thresholds, semantics, or evidence are not yet proven",
    ]


def _output_model_by_name(model_name: Optional[str]) -> Optional[Any]:
    mapping = {
        "AnalysisReport": AnalysisReport,
        "CodeBundle": CodeBundle,
        "GateDecision": GateDecision,
        "GateContextBundle": GateContextBundle,
        "ReviewReport": ReviewReport,
        "DirectionDecision": DirectionDecision,
        "ResearchLaneReport": ResearchLaneReport,
        "ResearchContext": ResearchContext,
    }
    if not model_name:
        return None
    return mapping.get(model_name)


def _dedupe_text_items(items: List[str], limit: Optional[int] = None) -> List[str]:
    output: List[str] = []
    seen: Set[str] = set()
    for item in items or []:
        text = re.sub(r"\s+", " ", str(item or "").strip())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
        if limit is not None and len(output) >= limit:
            break
    return output


def _cache_window_bucket(hours: Optional[int]) -> Optional[int]:
    if hours is None or hours <= 0:
        return None
    return int(time.time() // max(1, hours * 3600))


def _current_context7_api_url() -> str:
    return (
        os.environ.get("CONTEXT7_API_URL") or "https://context7.com/api/v1/search"
    ).strip()


def _librarian_provider_fingerprint() -> Dict[str, Any]:
    fingerprint: Dict[str, Any] = {
        "providers": list(LIBRARIAN_SEARCH_PROVIDERS),
        "results_per_query": LIBRARIAN_MAX_RESULTS_PER_QUERY,
        "max_citations": LIBRARIAN_MAX_CITATIONS,
        "max_queries_per_lane": LIBRARIAN_MAX_QUERIES_PER_LANE,
        "http_max_bytes": LIBRARIAN_HTTP_MAX_BYTES,
        "verify_citations": bool(LIBRARIAN_VERIFY_CITATIONS),
        "max_verified_citations": LIBRARIAN_MAX_VERIFIED_CITATIONS,
        "query_plan_version": LIBRARIAN_QUERY_PLAN_VERSION,
    }
    if "context7" in LIBRARIAN_SEARCH_PROVIDERS:
        fingerprint["context7_api_url"] = _current_context7_api_url()
    return fingerprint


def _append_url_query(
    base_url: str, params: Dict[str, Any], *, doseq: bool = False
) -> str:
    return _append_url_query_from_module(base_url, params, doseq=doseq)


def _safe_http_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    return _safe_http_json_from_module(
        url,
        timeout_seconds=LIBRARIAN_HTTP_TIMEOUT_SECONDS,
        max_bytes=LIBRARIAN_HTTP_MAX_BYTES,
        user_agent=LIBRARIAN_HTTP_USER_AGENT,
        method=method,
        headers=headers,
        payload=payload,
    )


def _safe_http_text(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    timeout_seconds: Optional[int] = None,
) -> str:
    resolved_timeout = (
        int(timeout_seconds)
        if timeout_seconds is not None and int(timeout_seconds) > 0
        else LIBRARIAN_HTTP_TIMEOUT_SECONDS
    )
    return _safe_http_text_from_module(
        url,
        timeout_seconds=resolved_timeout,
        max_bytes=LIBRARIAN_HTTP_MAX_BYTES,
        user_agent=LIBRARIAN_HTTP_USER_AGENT,
        method=method,
        headers=headers,
    )


def _citation_source_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    return (parsed.netloc or "").lower()


def _is_public_http_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "localhost.localdomain"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return ip.is_global
    if "." not in host:
        return False
    if host.endswith((".local", ".internal", ".lan", ".home", ".corp", ".localdomain")):
        return False
    return True


def _snippet_hash(snippet: str) -> str:
    normalized = re.sub(r"\s+", " ", str(snippet or "").strip())
    if not normalized:
        return ""
    return _text_sha256(normalized)


def _extract_html_text_excerpt(html_text: str) -> str:
    meta_match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        html_text or "",
        flags=re.IGNORECASE,
    )
    if meta_match:
        meta_text = re.sub(r"\s+", " ", unescape(meta_match.group(1) or "")).strip()
        if len(meta_text) >= 40:
            return meta_text[:500]
    without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    text = re.sub(r"\s+", " ", unescape(text)).strip()
    return text[:500]


def _fetch_citation_excerpt(url: str) -> str:
    if not _is_public_http_url(url):
        return ""
    try:
        raw_text = _safe_http_text(url)
    except _OperationCancelledError:
        raise
    except Exception:
        return ""
    if not raw_text:
        return ""
    if "<html" in raw_text.lower() or "<body" in raw_text.lower():
        return _extract_html_text_excerpt(raw_text)
    normalized = re.sub(r"\s+", " ", raw_text).strip()
    return normalized[:500]


def _citation_from_payload(
    provider: str,
    query: str,
    *,
    title: Any,
    url: Any,
    snippet: Any = "",
    evidence_type: str = "web_result",
) -> Optional[ResearchCitation]:
    title_text = re.sub(r"\s+", " ", str(title or "").strip())
    url_text = str(url or "").strip()
    snippet_text = re.sub(r"\s+", " ", str(snippet or "").strip())
    if not title_text or not url_text:
        return None
    return ResearchCitation(
        provider=provider,
        title=title_text[:240],
        url=url_text[:1000],
        snippet=snippet_text[:500],
        query=query[:240],
        source_domain=_citation_source_domain(url_text),
        snippet_hash=_snippet_hash(snippet_text[:500]),
        verification_status="search_snippet" if snippet_text else "metadata_only",
        evidence_type=str(evidence_type or "web_result")[:64],
    )


def _dedupe_citations(
    citations: List[ResearchCitation], limit: Optional[int] = None
) -> List[ResearchCitation]:
    output: List[ResearchCitation] = []
    seen: Set[Tuple[str, str]] = set()
    for citation in citations or []:
        key = (citation.provider.lower(), citation.url.strip().lower())
        if not citation.url or key in seen:
            continue
        seen.add(key)
        output.append(citation)
        if limit is not None and len(output) >= limit:
            break
    return output


def _verify_research_citation(
    citation: ResearchCitation, *, fetch_excerpt: bool
) -> ResearchCitation:
    snippet = re.sub(r"\s+", " ", citation.snippet or "").strip()
    verification_status = "search_snippet" if snippet else "metadata_only"
    if fetch_excerpt:
        fetched_excerpt = _fetch_citation_excerpt(citation.url)
        if fetched_excerpt:
            snippet = fetched_excerpt
            verification_status = "fetched_excerpt"
        elif not snippet:
            verification_status = "unverified"
    elif not snippet:
        verification_status = "unverified"
    return _model_copy_compat(
        citation,
        update={
            "snippet": snippet[:500],
            "source_domain": _citation_source_domain(citation.url),
            "snippet_hash": _snippet_hash(snippet[:500]),
            "verification_status": verification_status,
        },
    )


def _verify_research_citations(
    citations: List[ResearchCitation],
) -> List[ResearchCitation]:
    verified: List[ResearchCitation] = []
    deduped = _dedupe_citations(citations, limit=LIBRARIAN_MAX_CITATIONS)
    high_value: List[Tuple[int, ResearchCitation]] = []
    other: List[Tuple[int, ResearchCitation]] = []
    for idx, citation in enumerate(deduped):
        if _is_high_value_source(citation.url or ""):
            high_value.append((idx, citation))
        else:
            other.append((idx, citation))
    prioritized = high_value + other
    verified_count = 0
    verified_by_idx: Dict[int, ResearchCitation] = {}
    for original_idx, citation in prioritized:
        should_fetch = (
            bool(LIBRARIAN_VERIFY_CITATIONS)
            and verified_count < LIBRARIAN_MAX_VERIFIED_CITATIONS
        )
        result = _verify_research_citation(citation, fetch_excerpt=should_fetch)
        verified_by_idx[original_idx] = result
        if should_fetch:
            verified_count += 1
    for idx, citation in enumerate(deduped):
        if idx in verified_by_idx:
            verified.append(verified_by_idx[idx])
        else:
            verified.append(_verify_research_citation(citation, fetch_excerpt=False))
    return verified


class _DuckDuckGoHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: List[Dict[str, str]] = []
        self._current_href: Optional[str] = None
        self._capture_title = False
        self._capture_snippet = False
        self._current_title_parts: List[str] = []
        self._current_snippet_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = {k: v or "" for k, v in attrs}
        class_attr = attr_map.get("class", "")
        data_testid = attr_map.get("data-testid", "")
        href = attr_map.get("href", "")
        # Require a real DuckDuckGo result-anchor CSS class or data-testid.
        # Earlier revisions also matched `rel="nofollow"` links with an
        # HTTP(S) href, which false-positived on CAPTCHA / bot-detection
        # pages (202 responses) whose privacy/about/settings links carry
        # `rel="nofollow"` and were being harvested as fake search results.
        is_result_anchor = (
            "result__a" in class_attr
            or data_testid == "result-title-a"
            or "result-link" in class_attr
        )
        if tag == "a" and href and is_result_anchor:
            self._flush_current()
            self._current_href = href
            self._capture_title = True
            self._current_title_parts = []
            self._current_snippet_parts = []
            return
        if self._current_href and (
            "result__snippet" in class_attr
            or "result-snippet" in class_attr
            or data_testid == "result-snippet"
        ):
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            self._capture_title = False
        if self._capture_snippet and tag in {"a", "div", "span"}:
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", unescape(data or "")).strip()
        if not text:
            return
        if self._capture_title:
            self._current_title_parts.append(text)
        elif self._capture_snippet and self._current_href:
            self._current_snippet_parts.append(text)

    def close(self) -> None:
        super().close()
        self._flush_current()

    def _flush_current(self) -> None:
        if not self._current_href:
            return
        title = re.sub(r"\s+", " ", " ".join(self._current_title_parts)).strip()
        snippet = re.sub(r"\s+", " ", " ".join(self._current_snippet_parts)).strip()
        if title:
            self.results.append(
                {
                    "title": title,
                    "url": self._current_href,
                    "snippet": snippet,
                }
            )
        self._current_href = None
        self._current_title_parts = []
        self._current_snippet_parts = []
        self._capture_title = False
        self._capture_snippet = False


def _extract_websearch_citations_from_html(
    html_text: str, *, query: str
) -> List[ResearchCitation]:
    parser = _DuckDuckGoHtmlParser()
    parser.feed(html_text or "")
    parser.close()
    citations: List[ResearchCitation] = []
    for item in parser.results:
        url = item.get("url", "")
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urllib.parse.urljoin("https://duckduckgo.com", url)
        # DuckDuckGo often returns redirect wrappers; keep direct URL when present.
        if "duckduckgo.com/l/?" in url:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            uddg = qs.get("uddg")
            if uddg:
                url = urllib.parse.unquote(uddg[0])
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            continue
        citation = _citation_from_payload(
            "websearch",
            query,
            title=item.get("title"),
            url=url,
            snippet=item.get("snippet", ""),
        )
        if citation is not None:
            citations.append(citation)
        if len(citations) >= LIBRARIAN_MAX_RESULTS_PER_QUERY:
            break
    return _dedupe_citations(citations, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY)


RESEARCH_GROUNDING_STOPWORDS: Set[str] = {
    "about",
    "after",
    "again",
    "against",
    "agent",
    "agents",
    "architecture",
    "because",
    "before",
    "between",
    "build",
    "could",
    "enterprise",
    "feature",
    "features",
    "from",
    "have",
    "into",
    "market",
    "might",
    "mode",
    "needs",
    "only",
    "pattern",
    "patterns",
    "platform",
    "pricing",
    "product",
    "query",
    "risk",
    "risks",
    "saas",
    "should",
    "technical",
    "than",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "tool",
    "tools",
    "very",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
}


def _tokenize_for_grounding(text: str) -> Set[str]:
    """Extract grounding tokens from *text*.

    Two parallel tokenizers run on every input:

    1. ASCII technical identifiers (e.g. ``ohlcv``, ``binance``, ``ccxt``)
       via the ``[a-z0-9][a-z0-9_./+-]{2,}`` pattern — finds library names,
       URLs, indicator codes, etc.  Behaviour is unchanged from older
       releases so that English / mixed-language runs keep their existing
       grounding scores.
    2. CJK shingles (added in v16.9.71): Chinese / Japanese / Korean text
       has no whitespace word boundaries, so the ASCII regex above produces
       an empty set for any pure-CJK claim.  Without this pass a Traditional
       Chinese run produced ``grounded_claims=0`` for every Chinese claim,
       which cascaded into ``_filter_claims_by_citations`` flagging
       everything as a hallucination, ``_should_force_direction_none``
       force-returning ``"none"`` from the judge, and the entire
       direction-debate phase emitting the
       ``[Warn] Direction debate could not produce a valid decision``
       message even though the synthesizer had legitimate Chinese-language
       evidence.  We extract the full CJK run plus overlapping 2-char and
       3-char windows so that two claims sharing any meaningful Chinese
       substring overlap in token-set space (the existing
       ``_citation_support_score`` requires ``overlap >= 2`` *or* a single
       ``len >= 5`` token; CJK shingles satisfy the first branch when even
       one short Chinese term is shared).

    The covered Unicode ranges are:

    * ``U+3400–U+9FFF`` – CJK Unified Ideographs (Trad./Simp. Chinese, Kanji)
    * ``U+F900–U+FAFF`` – CJK Compatibility Ideographs
    * ``U+3040–U+309F`` – Hiragana
    * ``U+30A0–U+30FF`` – Katakana
    * ``U+AC00–U+D7AF`` – Hangul Syllables (Korean)
    """
    if not text:
        return set()
    lowered = text.lower()
    tokens: Set[str] = set()

    # 1) ASCII identifiers — preserves the legacy behaviour exactly.
    raw_ascii = set(re.findall(r"[a-z0-9][a-z0-9_./+-]{2,}", lowered))
    for token in raw_ascii:
        tokens.add(token)
        alpha_only = re.sub(r"[^a-z]+", "", token)
        if len(alpha_only) >= 3:
            tokens.add(alpha_only)

    # 2) CJK shingles — only contribute when the input actually contains CJK
    #    characters, so English-only / mixed runs see no behaviour change.
    cjk_runs = re.findall(
        r"[㐀-鿿豈-﫿぀-ゟ゠-ヿ가-힯]+",
        lowered,
    )
    for run in cjk_runs:
        run_len = len(run)
        if run_len < 2:
            # Single CJK characters are far too generic to ground claims
            # ("的", "是", "了", …) — drop them rather than poison the token set.
            continue
        tokens.add(run)
        # Sliding 2-char window — captures 2-character compounds like
        # "資金", "費率", "波動" that occur as substrings of longer phrases.
        for i in range(run_len - 1):
            tokens.add(run[i : i + 2])
        # 3-char window — disambiguates 2-char shingles when the run is
        # long enough (e.g., "資金費" vs unrelated "資金流").
        if run_len >= 3:
            for i in range(run_len - 2):
                tokens.add(run[i : i + 3])

    filtered = {
        token
        for token in tokens
        if token not in RESEARCH_GROUNDING_STOPWORDS and not token.isdigit()
    }
    return filtered


def _citation_support_score(claim: str, citation: ResearchCitation) -> int:
    claim_tokens = _tokenize_for_grounding(claim)
    if not claim_tokens:
        return 0
    evidence_text = " ".join(
        [
            citation.title or "",
            citation.snippet or "",
            citation.url or "",
        ]
    )
    evidence_tokens = _tokenize_for_grounding(evidence_text)
    overlap = claim_tokens & evidence_tokens
    if len(overlap) >= 2:
        return len(overlap)
    if len(overlap) == 1:
        token = next(iter(overlap))
        if len(token) >= 5:
            return 1
    lowered_claim = (claim or "").lower()
    lowered_title = (citation.title or "").lower()
    lowered_snippet = (citation.snippet or "").lower()
    if lowered_claim and (
        lowered_claim in lowered_title or lowered_claim in lowered_snippet
    ):
        return max(1, len(overlap))
    return 0


def _citation_evidence_weight(citation: ResearchCitation) -> int:
    provider = (citation.provider or "").strip().lower()
    evidence_type = (getattr(citation, "evidence_type", "") or "").strip().lower()
    verification_status = (citation.verification_status or "").strip().lower()

    weight = 1
    if provider == "context7" or evidence_type == "docs":
        weight = 4
    elif provider == "arxiv" or evidence_type == "paper":
        weight = 4
    elif provider == "grep_app" or evidence_type == "code_search":
        weight = 4
    elif provider == "github":
        if evidence_type == "code_search":
            weight = 4
        elif evidence_type == "repo_search":
            weight = 2
    elif provider == "websearch":
        weight = 2
    elif provider == "paperswithcode":
        weight = 1 if evidence_type in {"discovery_only", "site_search"} else 2

    if verification_status == "fetched_excerpt":
        weight += 1
    elif verification_status in {"metadata_only", "unverified"}:
        weight = max(1, weight - 1)
    return max(1, weight)


def _filter_claims_by_citations(
    claims: List[str],
    citations: List[ResearchCitation],
    *,
    category: str,
    hallucination_flags: List[str],
) -> Tuple[List[str], List[ClaimAttribution]]:
    grounded: List[str] = []
    attributions: List[ClaimAttribution] = []
    for claim in _dedupe_text_items(claims):
        scored: List[Tuple[int, int]] = []
        for idx, citation in enumerate(citations):
            score = _citation_support_score(claim, citation)
            if score > 0:
                scored.append((score * _citation_evidence_weight(citation), idx))
        scored.sort(reverse=True)
        best_score = scored[0][0] if scored else 0
        if best_score > 0:
            grounded.append(claim)
            top_indices = [idx for _, idx in scored[:3]]
            attributions.append(
                ClaimAttribution(
                    category=category,
                    claim=claim,
                    citation_indices=top_indices,
                    citation_urls=[
                        citations[idx].url
                        for idx in top_indices
                        if idx < len(citations)
                    ],
                    support_score=best_score,
                )
            )
        else:
            hallucination_flags.append(f"{category}:{claim}")
    return grounded, attributions


def _split_summary_claims(summary: str) -> List[str]:
    text = re.sub(r"\s+", " ", (summary or "")).strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?;])\s+|\s+\|\s+|\s+(?:\u2022|\u00b7)\s+|\s+-\s+", text)
    cleaned: List[str] = []
    for part in parts:
        candidate = re.sub(r"\s+", " ", part).strip(" \t\r\n-;,.")
        if len(candidate) >= 8:
            cleaned.append(candidate)
    if not cleaned and text:
        cleaned.append(text)
    return _dedupe_text_items(cleaned, limit=6)


def _infer_quant_field_capability_matrix(
    research_context: ResearchContext,
) -> List[DataFieldCapability]:
    corpus = " ".join(
        [
            str(research_context.user_problem or ""),
            " ".join(list(research_context.suggested_search_queries or [])),
            " ".join(list(research_context.technical_patterns or [])),
            " ".join(list(research_context.market_examples or [])),
            " ".join(list(research_context.existing_tools or [])),
            str(research_context.synthesized_summary or ""),
        ]
    ).lower()
    quant_markers = (
        "binance",
        "perpetual",
        "futures",
        "funding",
        "open interest",
        "taker",
        "ccxt",
        "backtest",
        "strategy",
        "quant",
        "ohlcv",
        "mark price",
        "index price",
        "basis",
        "long-short",
        "永續",
        "合約",
        "量化",
        "回測",
        "資金費率",
        "未平倉",
        "主動買賣",
    )
    if not any(marker in corpus for marker in quant_markers):
        return []

    definitions = [
        {
            "field_name": "ohlcv",
            "tier": "tier_1_core",
            "availability_class": "stable_long_history",
            "recommended_lane": "production",
            "recommended_horizons": ["intraday", "swing", "position"],
            "hard_gate_rule": "Baseline field for production lane. Reject directions that cannot be expressed from OHLCV plus explicit costs.",
            "soft_preference_rule": "Prefer when the thesis can be backtested on long history without special vendor dependence.",
            "notes": "Core exchange-native candles; safest default for initial production research.",
        },
        {
            "field_name": "mark_price_kline",
            "tier": "tier_1_core",
            "availability_class": "stable_long_history",
            "recommended_lane": "production",
            "recommended_horizons": ["intraday", "swing"],
            "hard_gate_rule": "Use when liquidation, premium divergence, or mark-trigger logic matters.",
            "soft_preference_rule": "Prefer over last-price-only studies when execution or liquidation logic depends on mark price.",
            "notes": "Useful for derivatives-specific trigger and risk modeling.",
        },
        {
            "field_name": "index_price_kline",
            "tier": "tier_1_core",
            "availability_class": "stable_long_history",
            "recommended_lane": "production",
            "recommended_horizons": ["intraday", "swing"],
            "hard_gate_rule": "Required when the thesis depends on basis or mark-vs-index divergence.",
            "soft_preference_rule": "Rank higher when index/mark spread is central but still exchange-native.",
            "notes": "Supports basis and fair-value style features.",
        },
        {
            "field_name": "premium_index_kline",
            "tier": "tier_1_core",
            "availability_class": "stable_long_history",
            "recommended_lane": "production",
            "recommended_horizons": ["intraday", "swing"],
            "hard_gate_rule": "Treat as production-feasible when the venue exposes historical premium/index klines directly.",
            "soft_preference_rule": "Strong production candidate for premium mean-reversion and derivatives regime filters.",
            "notes": "Often safer than microstructure fields for regime classification.",
        },
        {
            "field_name": "funding_rate",
            "tier": "tier_1_extended",
            "availability_class": "paged_history",
            "recommended_lane": "production",
            "recommended_horizons": ["intraday", "swing"],
            "hard_gate_rule": "Allowed in production only when historical pagination or backfill is implemented for the target venue.",
            "soft_preference_rule": "Rank below pure candle studies if the same thesis works without paginated funding history.",
            "notes": "Good production input, but history collection is usually paginated rather than one-shot.",
        },
        {
            "field_name": "open_interest",
            "tier": "tier_2_short_window",
            "availability_class": "short_window",
            "recommended_lane": "exploration",
            "recommended_horizons": ["intraday", "short_swing"],
            "hard_gate_rule": "Do not approve for long-horizon production studies unless independent historical coverage is verified.",
            "soft_preference_rule": "Keep as backup or exploration candidate when it sharpens short-cycle regime detection.",
            "notes": "Historical coverage is often materially shorter than candle history.",
        },
        {
            "field_name": "taker_buy_sell_volume",
            "tier": "tier_2_short_window",
            "availability_class": "short_window",
            "recommended_lane": "exploration",
            "recommended_horizons": ["intraday", "short_swing"],
            "hard_gate_rule": "Fail the production gate for medium or long horizon studies if only short-window history is available.",
            "soft_preference_rule": "Useful for short-term exploration; demote for production unless the horizon matches the short window.",
            "notes": "Microstructure-rich, but commonly constrained by short history windows.",
        },
        {
            "field_name": "long_short_ratio_or_basis",
            "tier": "tier_2_short_window",
            "availability_class": "conditional",
            "recommended_lane": "exploration",
            "recommended_horizons": ["intraday", "short_swing"],
            "hard_gate_rule": "Treat as conditional: verify venue-specific depth and history before approving production use.",
            "soft_preference_rule": "Exploration-first field; keep as backup when the thesis survives without it.",
            "notes": "Venue semantics and coverage differ enough that hard-feasibility must stay conservative.",
        },
    ]
    return [DataFieldCapability(**item) for item in definitions]


def _stabilize_research_context(research_context: ResearchContext) -> ResearchContext:
    citations = _dedupe_citations(
        list(research_context.citations or []), limit=LIBRARIAN_MAX_CITATIONS
    )
    hallucination_flags: List[str] = []

    claim_attributions: List[ClaimAttribution] = []

    market_examples, market_attr = _filter_claims_by_citations(
        list(research_context.market_examples or []),
        citations,
        category="market_examples",
        hallucination_flags=hallucination_flags,
    )
    claim_attributions.extend(market_attr)
    existing_tools, tool_attr = _filter_claims_by_citations(
        list(research_context.existing_tools or []),
        citations,
        category="existing_tools",
        hallucination_flags=hallucination_flags,
    )
    claim_attributions.extend(tool_attr)
    technical_patterns, pattern_attr = _filter_claims_by_citations(
        list(research_context.technical_patterns or []),
        citations,
        category="technical_patterns",
        hallucination_flags=hallucination_flags,
    )
    claim_attributions.extend(pattern_attr)
    key_risks, risk_attr = _filter_claims_by_citations(
        list(research_context.key_risks or []),
        citations,
        category="key_risks",
        hallucination_flags=hallucination_flags,
    )
    claim_attributions.extend(risk_attr)
    grounded_summary_claims, summary_attr = _filter_claims_by_citations(
        _split_summary_claims(research_context.synthesized_summary),
        citations,
        category="summary",
        hallucination_flags=hallucination_flags,
    )
    claim_attributions.extend(summary_attr)
    unknowns = _dedupe_text_items(list(research_context.unknowns or []), limit=8)
    for claim in [flag.split(":", 1)[1] for flag in hallucination_flags if ":" in flag]:
        if claim not in unknowns:
            unknowns.append(f"Need evidence validation: {claim}")

    grounded_claims = sum(
        len(items)
        for items in (
            market_examples,
            existing_tools,
            technical_patterns,
            key_risks,
        )
    )
    evidence_coverage = {
        "citations": len(citations),
        "grounded_claims": grounded_claims,
        "grounded_summary_claims": len(grounded_summary_claims),
        "hallucination_flags": len(hallucination_flags),
        "providers_used": len(
            _dedupe_text_items(list(research_context.providers_used or []))
        ),
    }

    summary_candidates = _dedupe_text_items(
        [
            " ".join(grounded_summary_claims[:2]).strip(),
            *market_examples[:1],
            *existing_tools[:1],
            *technical_patterns[:2],
            *key_risks[:2],
        ],
        limit=4,
    )
    summary = " ".join(summary_candidates[:3]).strip()
    if not summary:
        summary = "Research evidence is sparse; downstream debate should treat unresolved claims as unknowns."
    field_capability_matrix = _infer_quant_field_capability_matrix(research_context)

    return _model_copy_compat(
        research_context,
        update={
            "providers_used": _dedupe_text_items(
                list(research_context.providers_used or []), limit=8
            ),
            "suggested_search_queries": _dedupe_text_items(
                list(research_context.suggested_search_queries or []), limit=12
            ),
            "market_examples": market_examples,
            "existing_tools": existing_tools,
            "technical_patterns": technical_patterns,
            "key_risks": key_risks,
            "unknowns": unknowns[:8],
            "citations": citations,
            "synthesized_summary": summary,
            "evidence_coverage": evidence_coverage,
            "hallucination_flags": hallucination_flags[:12],
            "claim_attributions": claim_attributions[:24],
            "field_capability_matrix": field_capability_matrix,
        },
    )


def _extract_context7_library_candidates(
    user_problem: str,
    mode: str,
    *,
    problem_breakdown: Optional[Dict[str, Any]] = None,
    lane_queries: Optional[List[str]] = None,
) -> List[str]:
    known_tokens = {
        "fastapi": "fastapi",
        "flask": "flask",
        "django": "django",
        "react": "react",
        "next.js": "nextjs",
        "nextjs": "nextjs",
        "supabase": "supabase",
        "stripe": "stripe",
        "postgres": "postgresql",
        "postgresql": "postgresql",
        "prisma": "prisma",
        "redis": "redis",
        "bullmq": "bullmq",
        "crewai": "crewai",
        "langchain": "langchain",
        "docker": "docker",
        "kubernetes": "kubernetes",
        "ccxt": "ccxt",
        "binance": "binance-connector",
        "ta-lib": "talib",
        "talib": "talib",
        "pandas": "pandas",
        "numpy": "numpy",
        "scikit-learn": "scikit-learn",
        "sklearn": "scikit-learn",
        "tensorflow": "tensorflow",
        "pytorch": "pytorch",
        "backtrader": "backtrader",
        "zipline": "zipline",
        "quantlib": "quantlib",
        "vectorbt": "vectorbt",
        "freqtrade": "freqtrade",
        "jesse": "jesse",
        "vnpy": "vnpy",
        "scipy": "scipy",
        "matplotlib": "matplotlib",
        "plotly": "plotly",
        "web3": "web3.py",
        "web3.py": "web3.py",
        "ethers": "ethers",
        "solana": "solana-py",
        "requests": "requests",
        "aiohttp": "aiohttp",
        "httpx": "httpx",
        "sqlalchemy": "sqlalchemy",
        "alembic": "alembic",
        "celery": "celery",
        "prefect": "prefect",
        "airflow": "airflow",
        "temporal": "temporal",
    }
    breakdown = problem_breakdown or {}
    lane_focus = " ".join(
        str(item)
        for item in (breakdown.get("lane_focus") or {}).get("technical", [])
        if str(item).strip()
    )
    entities = " ".join(
        str(item) for item in (breakdown.get("entities") or []) if str(item).strip()
    )
    constraints = " ".join(
        str(item) for item in (breakdown.get("constraints") or []) if str(item).strip()
    )
    query_terms = " ".join(
        str(item) for item in lane_queries or [] if str(item).strip()
    )
    text = " ".join(
        item
        for item in [
            mode,
            user_problem,
            str(breakdown.get("core_objective") or ""),
            entities,
            constraints,
            lane_focus,
            query_terms,
        ]
        if item
    ).lower()
    candidates: List[str] = []
    for token, library in known_tokens.items():
        if token in text and library not in candidates:
            candidates.append(library)
    return candidates[:4]


LIBRARIAN_QUERY_STOPWORDS: Set[str] = {
    "a",
    "an",
    "and",
    "api",
    "app",
    "application",
    "apps",
    "build",
    "create",
    "develop",
    "for",
    "from",
    "help",
    "into",
    "make",
    "need",
    "platform",
    "product",
    "project",
    "service",
    "system",
    "that",
    "the",
    "this",
    "tool",
    "tools",
    "use",
    "using",
    "with",
    "without",
}


def _normalize_problem_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _strip_problem_action_prefix(problem: str) -> str:
    stripped = re.sub(
        r"^(build|create|design|develop|make|launch|plan|research|analyze|improve|optimize|automate)\s+",
        "",
        problem,
        flags=re.IGNORECASE,
    ).strip()
    stripped = re.sub(r"^(an?|the)\s+", "", stripped, flags=re.IGNORECASE).strip()
    return stripped or problem


def _extract_problem_entities(problem: str) -> List[str]:
    entities: List[str] = []
    quoted = re.findall(r"['\"]([^'\"]{3,80})['\"]", problem or "")
    entities.extend(quoted)
    tokens = re.findall(r"\b[A-Za-z0-9][A-Za-z0-9+.#/_-]{2,}\b", problem or "")
    for token in tokens:
        normalized = token.strip(".,:;!?()[]{}")
        lowered = normalized.lower()
        if lowered in LIBRARIAN_QUERY_STOPWORDS:
            continue
        if (
            any(ch.isupper() for ch in normalized[1:])
            or any(ch in normalized for ch in "+.#/_-")
            or lowered
            in {
                "ai",
                "b2b",
                "b2c",
                "smb",
                "erp",
                "ocr",
                "mcp",
                "rag",
                "etl",
                "crm",
                "erp",
                "quickbooks",
                "xero",
                "shopify",
                "stripe",
                "postgres",
                "redis",
                "fastapi",
            }
        ):
            entities.append(normalized)
    return _dedupe_text_items(entities, limit=8)


def _extract_constraint_phrases(problem: str) -> List[str]:
    text = _normalize_problem_text(problem)
    patterns = [
        r"\bfor\s+([^,.;&]{3,80})",
        r"\bwith\s+([^,.;&]{3,80})",
        r"\busing\s+([^,.;&]{3,80})",
        r"\bwithout\s+([^,.;&]{3,80})",
        r"\bvia\s+([^,.;&]{3,80})",
        r"\bmust\s+([^,.;&]{3,80})",
        r"\bshould\s+([^,.;&]{3,80})",
        r"\bneeds?\s+to\s+([^,.;&]{3,80})",
        r"\bsupport(?:s|ing)?\s+([^,.;&]{3,80})",
    ]
    constraints: List[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            candidate = re.sub(r"\s+", " ", match).strip(" ,.;:")
            if len(candidate) >= 3:
                constraints.append(candidate)
    return _dedupe_text_items(constraints, limit=8)


def _mode_specific_query_terms(mode_name: str) -> Dict[str, List[str]]:
    lowered = _project_type_for_mode(mode_name)
    if lowered == "agent":
        return {
            "market": ["workflow pain", "automation use cases", "operator friction"],
            "technical": [
                "deterministic orchestration",
                "tool reliability",
                "state machine",
                "retries",
                "idempotency",
            ],
            "competitor": [
                "agent frameworks",
                "automation platforms",
                "open source alternatives",
            ],
        }
    if lowered == "quant":
        return {
            "market": ["strategy precedent", "alpha source", "execution constraints"],
            "technical": [
                "backtest reliability",
                "slippage",
                "look-ahead bias",
                "risk controls",
            ],
            "competitor": [
                "open source quant frameworks",
                "research platforms",
                "broker tooling",
            ],
        }
    if lowered == "saas":
        return {
            "market": ["competitors", "pricing", "workflow pain", "adoption blockers"],
            "technical": [
                "architecture",
                "reliability",
                "idempotency",
                "audit trail",
                "integration patterns",
            ],
            "competitor": [
                "alternatives",
                "incumbents",
                "open source",
                "workflow substitutes",
            ],
        }
    if lowered == "scientist":
        return {
            "market": [
                "paper reproducibility",
                "research replication",
                "algorithm evaluation",
                "benchmark dataset",
            ],
            "technical": [
                "paper implementation",
                "algorithm details",
                "ablation study",
                "benchmark comparison",
                "reproducibility",
            ],
            "competitor": [
                "open source baseline",
                "existing implementations",
                "paperswithcode",
                "related work",
            ],
        }
    raise ValueError(
        f"Unsupported mode {mode_name!r}. Expected one of: quant, saas, agent, scientist"
    )


def _build_librarian_problem_breakdown(user_problem: str, mode: str) -> Dict[str, Any]:
    problem = _normalize_problem_text(user_problem)
    mode_name = _validated_mode_name(mode)
    action_stripped = _strip_problem_action_prefix(problem)
    core_objective = re.split(
        r"\b(for|with|using|without|that|which|who|via|while|where|when|because|so that)\b",
        action_stripped,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.;:")
    if not core_objective:
        core_objective = action_stripped or problem
    entities = _extract_problem_entities(problem)
    constraints = _extract_constraint_phrases(problem)
    mode_terms = _mode_specific_query_terms(mode_name)
    market_focus = _dedupe_text_items(
        [core_objective, *constraints[:2], *mode_terms["market"][:3], *entities[:2]],
        limit=6,
    )
    technical_focus = _dedupe_text_items(
        [core_objective, *constraints[:3], *mode_terms["technical"][:4], *entities[:4]],
        limit=8,
    )
    competitor_focus = _dedupe_text_items(
        [
            core_objective,
            *constraints[:2],
            *mode_terms["competitor"][:4],
            *entities[:3],
        ],
        limit=7,
    )
    return {
        "normalized_problem": problem,
        "mode_name": mode_name,
        "core_objective": core_objective,
        "entities": entities,
        "constraints": constraints,
        "lane_focus": {
            "market": market_focus,
            "technical": technical_focus,
            "competitor": competitor_focus,
        },
    }


# ============================================================
# Mode-specific Search Templates
# ============================================================

QUANT_SEARCH_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "market": {
        "en": [
            "{objective} crypto trading strategy backtest performance",
            "{objective} site:binance.com {entity}",
            "{entities} trading strategy alpha source execution",
            "{objective} quantitative trading {constraints}",
        ],
        "zh": [
            "{objective} 加密貨幣 交易策略 回測 績效",
            "{entities} 幣安 永續合約 策略",
            "{objective} 量化交易 {constraints} 實作",
            "{entities} site:github.com trading bot",
        ],
    },
    "technical": {
        "en": [
            "site:github.com {entities} trading bot python",
            "{entities} API implementation example",
            "{objective} backtest slippage look-ahead bias",
            "site:pypi.org {entities}",
        ],
        "zh": [
            "site:github.com {entities} 交易機器人 python",
            "{entities} API 實作 範例 教程",
            "{objective} 回測 滑點 前視偏差",
            "site:pypi.org {entities}",
        ],
    },
    "competitor": {
        "en": [
            "site:github.com crypto trading bot stars:>100",
            "{entities} vs alternatives comparison",
            "open source quantitative trading framework python",
            "{objective} existing tools frameworks",
        ],
        "zh": [
            "site:github.com 加密貨幣 交易機器人 stars:>100",
            "{entities} vs 替代方案 比較",
            "開源 量化交易 框架 python",
            "{objective} 現有工具 框架",
        ],
    },
}

SAAS_SEARCH_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "market": {
        "en": [
            "{objective} SaaS competitors pricing",
            "{objective} market size TAM SAM SOM",
            "{entities} workflow pain points adoption blockers",
            "{objective} buyer demand case study",
        ],
        "zh": [
            "{objective} SaaS 競爭對手 定價",
            "{objective} 市場規模 TAM SAM SOM",
            "{entities} 工作流程 痛點 採用障礙",
            "{objective} 買家需求 案例",
        ],
    },
    "technical": {
        "en": [
            "site:github.com {entities} fastapi flask",
            "{objective} architecture reliability patterns",
            "site:stackoverflow.com {objective} implementation",
            "{entities} API integration production examples",
        ],
        "zh": [
            "site:github.com {entities} fastapi flask",
            "{objective} 架構 可靠性 模式",
            "site:stackoverflow.com {objective} 實作",
            "{entities} API 整合 生產範例",
        ],
    },
    "competitor": {
        "en": [
            "{entities} alternatives incumbents open source",
            "site:github.com {objective} stars:>50",
            "{objective} SaaS tools comparison",
            "{entities} vs competitor analysis",
        ],
        "zh": [
            "{entities} 替代方案 競爭對手 開源",
            "site:github.com {objective} stars:>50",
            "{objective} SaaS 工具 比較",
            "{entities} vs 競爭分析",
        ],
    },
}

AGENT_SEARCH_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "market": {
        "en": [
            "{objective} automation use cases workflow",
            "{entities} agent orchestration market demand",
            "{objective} operator friction pain points",
            "{entities} headless service adoption",
        ],
        "zh": [
            "{objective} 自動化 用例 工作流程",
            "{entities} 代理 編排 市場需求",
            "{objective} 運營者 摩擦 痛點",
            "{entities} 無頭服務 採用",
        ],
    },
    "technical": {
        "en": [
            "site:github.com {entities} agent daemon python",
            "{objective} deterministic orchestration retry",
            "{entities} state machine idempotency pattern",
            "site:pypi.org {entities}",
        ],
        "zh": [
            "site:github.com {entities} 代理 守護進程 python",
            "{objective} 確定性 編排 重試",
            "{entities} 狀態機 冪等 模式",
            "site:pypi.org {entities}",
        ],
    },
    "competitor": {
        "en": [
            "site:github.com agent framework python stars:>100",
            "{entities} open source automation platform",
            "{objective} workflow substitutes tools",
            "{entities} vs temporal airflow comparison",
        ],
        "zh": [
            "site:github.com 代理框架 python stars:>100",
            "{entities} 開源 自動化 平台",
            "{objective} 工作流 替代 工具",
            "{entities} vs temporal airflow 比較",
        ],
    },
}

SCIENTIST_SEARCH_TEMPLATES: Dict[str, Dict[str, List[str]]] = {
    "market": {
        "en": [
            "site:arxiv.org {objective} {entities}",
            "site:semanticscholar.org {objective} {entities}",
            "{entities} paper survey benchmark comparison",
            "{objective} research reproducibility replication study",
        ],
        "zh": [
            "site:arxiv.org {objective} {entities}",
            "site:semanticscholar.org {objective} {entities}",
            "{entities} 論文 綜述 基準比較",
            "{objective} 可重現性 複現研究",
        ],
    },
    "technical": {
        "en": [
            "site:paperswithcode.com {entities} {objective}",
            "site:github.com {entities} paper implementation python",
            "{objective} {entities} ablation study implementation details",
            "{entities} algorithm pseudocode python reproduce",
        ],
        "zh": [
            "site:paperswithcode.com {entities} {objective}",
            "site:github.com {entities} 論文 實作 python",
            "{objective} {entities} 消融實驗 實作細節",
            "{entities} 演算法 偽碼 python 複現",
        ],
    },
    "competitor": {
        "en": [
            "site:github.com {entities} baseline implementation stars:>50",
            "{entities} vs {objective} benchmark dataset comparison",
            "{objective} open source code reproducible",
            "{entities} related work alternatives",
        ],
        "zh": [
            "site:github.com {entities} 基線 實作 stars:>50",
            "{entities} vs {objective} 基準數據集比較",
            "{objective} 開源代碼 可重現",
            "{entities} 相關工作 替代方法",
        ],
    },
}


def _resolve_query_helper_mode_name(
    explicit_mode_name: str = "",
    breakdown_mode_name: str = "",
) -> str:
    explicit = str(explicit_mode_name or "").strip()
    breakdown = str(breakdown_mode_name or "").strip()
    canonical_explicit = _get_mode_config(explicit).name if explicit else ""
    canonical_breakdown = _get_mode_config(breakdown).name if breakdown else ""
    if canonical_explicit and canonical_breakdown and canonical_explicit != canonical_breakdown:
        raise ValueError(
            "Breakdown mode_name conflicted with the explicit mode. "
            f"Expected {canonical_explicit!r}, got {canonical_breakdown!r}."
        )
    return canonical_explicit or canonical_breakdown or _get_mode_config(explicit or breakdown).name


def _get_search_templates_for_mode(mode_name: str) -> Dict[str, Dict[str, List[str]]]:
    """Get mode-specific search templates."""
    lowered = _get_mode_config(mode_name).name.strip().lower()
    if lowered == "quant":
        return QUANT_SEARCH_TEMPLATES
    elif lowered == "agent":
        return AGENT_SEARCH_TEMPLATES
    elif lowered == "saas":
        return SAAS_SEARCH_TEMPLATES
    elif lowered == "scientist":
        return SCIENTIST_SEARCH_TEMPLATES
    raise ValueError(
        f"Unsupported mode {mode_name!r}. Expected one of: quant, saas, agent, scientist"
    )


def _detect_search_language(language_hint: str, user_problem: str) -> str:
    """
    Determine the language for search queries.
    Returns 'zh' for Chinese input, 'en' otherwise.
    """
    lowered = (language_hint or "").strip().lower()
    if (
        "chinese" in lowered
        or "中文" in lowered
        or "繁體" in lowered
        or "简体" in lowered
    ):
        return "zh"
    # Fallback: detect CJK characters in user problem
    if contains_cjk(user_problem):
        return "zh"
    return "en"


def _render_search_templates(
    templates: List[str],
    objective: str,
    entities: List[str],
    constraints: List[str],
) -> List[str]:
    """Render search template placeholders with actual values."""
    entity_clause = " ".join(entities[:3]).strip()
    constraint_clause = " ".join(constraints[:2]).strip()
    rendered = []
    for template in templates:
        query = template.format(
            objective=objective,
            entities=entity_clause,
            entity=entity_clause,
            constraints=constraint_clause,
        )
        # Clean up multiple spaces
        query = re.sub(r"\s+", " ", query).strip()
        if query:
            rendered.append(query)
    return rendered


# ============================================================
# LLM-based Problem Decomposition
# ============================================================


class LLMProblemBreakdown(BaseModel):
    """LLM-generated problem breakdown with extracted entities and constraints."""

    core_objective: str = Field(
        default="", description="The main objective/goal extracted from user problem"
    )
    entities: List[str] = Field(
        default_factory=list,
        description="Key entities mentioned (libraries, platforms, tools)",
    )
    constraints: List[str] = Field(
        default_factory=list, description="Constraints and requirements"
    )
    technical_stack: List[str] = Field(
        default_factory=list, description="Technical stack components"
    )
    domain_keywords: List[str] = Field(
        default_factory=list, description="Domain-specific keywords for search"
    )


def _build_llm_problem_breakdown(
    user_problem: str,
    mode: str,
    language_hint: str,
) -> Optional[Dict[str, Any]]:
    """
    Use LLM to extract structured entities, constraints, and keywords from user problem.
    This provides better decomposition than regex-based extraction, especially for non-English input.
    """
    if not LIBRARIAN_ENABLED:
        return None

    cache_payload = {
        "user_problem_sha256": _text_sha256(user_problem),
        "mode": mode,
        "language_hint": language_hint,
        "version": "v1_llm_breakdown",
    }
    cached = _cache_get_pydantic(
        "llm_problem_breakdown", cache_payload, LLMProblemBreakdown
    )
    if cached is not None:
        return {
            "core_objective": cached.core_objective,
            "entities": list(cached.entities or []),
            "constraints": list(cached.constraints or []),
            "technical_stack": list(cached.technical_stack or []),
            "domain_keywords": list(cached.domain_keywords or []),
        }

    librarian_llm = _get_librarian_llm()
    mode_config = _get_mode_config(mode)

    prompt = f"""You are an expert at analyzing user problems and extracting structured information.

USER PROBLEM:
{user_problem}

MODE: {mode_config.name}
LANGUAGE: {language_hint}

TASK: Extract structured information from the user problem. Focus on:
1. core_objective: The main goal/objective (be concise, remove "Idea:" prefix if present)
2. entities: Key entities mentioned (libraries, platforms, exchanges, tools, frameworks)
3. constraints: Requirements and constraints mentioned
4. technical_stack: Technical components mentioned (programming languages, databases, APIs)
5. domain_keywords: Domain-specific keywords useful for search

RULES:
- Extract specific names (CCXT, Binance, USDT, Python, etc.)
- Quant mode: look for trading terms, exchanges, strategies, timeframes
- SaaS mode: look for business terms, platforms, integrations
- Agent mode: look for automation, orchestration, daemon terms
- Be specific, not generic
- Output JSON only, no markdown

OUTPUT FORMAT:
{{
  "core_objective": "concise goal description",
  "entities": ["CCXT", "Binance", "永續合約"],
  "constraints": ["市值前30", "短線", "高勝率低回撤"],
  "technical_stack": ["Python", "機器學習", "多時框"],
  "domain_keywords": ["量化交易", "backtest", "perpetual futures"]
}}
"""

    formatter = Agent(
        role="Problem Analyst",
        goal="Extract structured entities and constraints from user problems.",
        backstory="You are an expert at parsing user requirements and extracting actionable search keywords.",
        allow_delegation=False,
        verbose=False,
        llm=librarian_llm,
    )
    task = Task(
        description=prompt,
        agent=formatter,
        expected_output="JSON with core_objective, entities, constraints, technical_stack, domain_keywords.",
    )
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )

    try:
        _cost_trace(
            "llm_problem_breakdown.kickoff", user_problem_chars=len(user_problem)
        )
        result = kickoff_crew_with_retry(
            crew,
            crew_name="llm_problem_breakdown",
            logger=LOGGER,
            log_fields={"user_problem_chars": len(user_problem or "")},
        )
        raw_text = _extract_text_from_result(result) or ""

        parsed = _extract_first_json_object(raw_text)
        if parsed is None:
            return None

        breakdown = LLMProblemBreakdown(
            core_objective=str(parsed.get("core_objective") or "").strip(),
            entities=list(parsed.get("entities") or []),
            constraints=list(parsed.get("constraints") or []),
            technical_stack=list(parsed.get("technical_stack") or []),
            domain_keywords=list(parsed.get("domain_keywords") or []),
        )

        _cache_set_pydantic("llm_problem_breakdown", cache_payload, breakdown)

        return {
            "core_objective": breakdown.core_objective,
            "entities": breakdown.entities,
            "constraints": breakdown.constraints,
            "technical_stack": breakdown.technical_stack,
            "domain_keywords": breakdown.domain_keywords,
        }
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — returning None would allow
        # the pipeline to continue running after the user cancelled.
        raise
    except Exception as e:
        print(f"[Warn] LLM problem breakdown failed: {e}", file=sys.stderr)
        return None


def _build_lane_queries_from_breakdown(
    breakdown: Dict[str, Any],
    lane: str,
    *,
    mode_name: str = "",
    language: str = "en",
) -> List[str]:
    core_objective = str(
        breakdown.get("core_objective") or breakdown.get("normalized_problem") or ""
    ).strip()
    entities = [
        str(item).strip()
        for item in breakdown.get("entities") or []
        if str(item).strip()
    ]
    constraints = [
        str(item).strip()
        for item in breakdown.get("constraints") or []
        if str(item).strip()
    ]
    domain_keywords = [
        str(item).strip()
        for item in breakdown.get("domain_keywords") or []
        if str(item).strip()
    ]
    lane_focus = [
        str(item).strip()
        for item in (breakdown.get("lane_focus") or {}).get(lane, [])
        if str(item).strip()
    ]
    mode_name = _resolve_query_helper_mode_name(
        explicit_mode_name=mode_name,
        breakdown_mode_name=str(breakdown.get("mode_name") or "").strip(),
    )

    templates = _get_search_templates_for_mode(mode_name)
    lane_templates = templates.get(lane, {})
    lang_templates = lane_templates.get(language, lane_templates.get("en", []))

    if lang_templates:
        seeds = _render_search_templates(
            lang_templates, core_objective, entities, constraints
        )
    else:
        if lane == "market":
            seeds = [
                f"{core_objective} competitors pricing {' '.join(entities[:3])}".strip(),
                f"{core_objective} workflow pain points adoption blockers".strip(),
                f"{core_objective} buyer demand case study {mode_name}".strip(),
                f"{core_objective} alternatives incumbents {' '.join(entities[:3])}".strip(),
            ]
        elif lane == "technical":
            seeds = [
                f"{core_objective} architecture reliability patterns {' '.join(entities[:3])}".strip(),
                f"{core_objective} implementation risks idempotency concurrency".strip(),
                f"{core_objective} audit trail integration production examples".strip(),
                f"{core_objective} failure modes observability validation".strip(),
            ]
        else:
            seeds = [
                f"{core_objective} alternatives incumbents open source {' '.join(entities[:3])}".strip(),
                f"{core_objective} github repositories tools frameworks".strip(),
                f"{core_objective} workflow substitutes pricing comparison".strip(),
                f"{core_objective} paperswithcode arxiv research systems".strip(),
            ]

    seeds.extend(lane_focus[:2])
    seeds.extend(domain_keywords[:2])

    cleaned = [
        re.sub(r"\s+", " ", seed).strip(" ,.;:")
        for seed in seeds
        if seed and seed.strip()
    ]
    cleaned = [seed for seed in cleaned if seed]
    return _dedupe_text_items(cleaned, limit=LIBRARIAN_MAX_QUERIES_PER_LANE)


class SmartSearchQueries(BaseModel):
    """LLM-generated search queries for each research lane."""

    market: List[str] = Field(
        default_factory=list, description="Search queries for market research"
    )
    technical: List[str] = Field(
        default_factory=list, description="Search queries for technical research"
    )
    competitor: List[str] = Field(
        default_factory=list, description="Search queries for competitor research"
    )
    rationale: str = Field(
        default="", description="Brief explanation of query strategy"
    )


def _build_smart_search_queries(
    user_problem: str,
    mode: str,
    breakdown: Dict[str, Any],
    *,
    language_hint: str = "English",
) -> Optional[Dict[str, List[str]]]:
    if not LIBRARIAN_ENABLED:
        return None

    cache_payload = {
        "user_problem_sha256": _text_sha256(user_problem),
        "mode": mode,
        "language_hint": language_hint,
        "breakdown_sha256": _text_sha256(
            json.dumps(breakdown, sort_keys=True, default=str)
        ),
        "version": "v2_smart_queries_i18n",
    }
    cached = _cache_get_pydantic(
        "smart_search_queries", cache_payload, SmartSearchQueries
    )
    if cached is not None:
        return {
            "market": list(cached.market or []),
            "technical": list(cached.technical or []),
            "competitor": list(cached.competitor or []),
        }

    librarian_llm = _get_librarian_llm()
    mode_config = _get_mode_config(mode)
    mode_name = _validated_mode_name(mode)
    mode_project_type = _validated_mode_project_type(mode_config)

    is_chinese = (
        "chinese" in language_hint.lower()
        or "中文" in language_hint
        or contains_cjk(user_problem)
    )
    language_guidance = ""
    if is_chinese:
        language_guidance = """
LANGUAGE STRATEGY (Chinese input detected):
- Generate a MIX of Chinese and English search queries
- For technical content: prefer English queries (better coverage on GitHub, Stack Overflow, PyPI)
- For market/local content: Chinese queries are acceptable
- Example patterns:
  - "CCXT binance perpetual futures API" (English for technical docs)
  - "幣安永續合約 交易策略 回測" (Chinese for local market info)
  - "site:github.com CCXT binance trading bot" (English with site operator)
"""
    else:
        language_guidance = """
LANGUAGE STRATEGY:
- Generate search queries in English (best coverage for technical content)
- Use site: operators for authoritative sources when appropriate
"""

    mode_guidance = ""
    if mode_project_type == "quant":
        mode_guidance = """
QUANT MODE SPECIFIC GUIDANCE:
- Technical lane: focus on backtesting, slippage, look-ahead bias, risk controls
- Use site:github.com for trading bot examples
- Use site:pypi.org for library documentation
- Include keywords: backtest, strategy, alpha, execution, slippage, drawdown
"""
    elif mode_project_type == "agent":
        mode_guidance = """
AGENT MODE SPECIFIC GUIDANCE:
- Technical lane: focus on deterministic execution, retries, idempotency, state machines
- Use site:github.com for daemon/agent examples
- Include keywords: daemon, orchestration, retry, idempotent, state machine
"""
    elif mode_project_type == "saas":
        mode_guidance = """
SAAS MODE SPECIFIC GUIDANCE:
- Market lane: focus on TAM/SAM/SOM, pricing models, competitors
- Technical lane: focus on FastAPI/Flask patterns, database schemas, API design
- Use site:stackoverflow.com for implementation questions
"""
    elif mode_project_type == "scientist":
        mode_guidance = """
SCIENTIST MODE SPECIFIC GUIDANCE:
- Technical lane: focus on paper algorithm details, implementation pitfalls, reproducibility, benchmark datasets
- Use site:arxiv.org and site:semanticscholar.org for primary literature
- Use site:paperswithcode.com for benchmark comparisons and existing implementations
- Use site:github.com for reference implementations and open-source baselines
- Include keywords: reproduce, implementation, ablation, benchmark, dataset, baseline, replication
"""

    entities = breakdown.get("entities", [])
    constraints = breakdown.get("constraints", [])
    domain_keywords = breakdown.get("domain_keywords", [])

    prompt = f"""You are an expert search query optimizer. Generate targeted search queries for a multi-lane research system.

USER PROBLEM:
{user_problem}

MODE: {mode_name}
LANGUAGE: {language_hint}

PROBLEM BREAKDOWN:
{json.dumps(breakdown, indent=2, ensure_ascii=False)}

EXTRACTED ENTITIES: {json.dumps(entities, ensure_ascii=False)}
EXTRACTED CONSTRAINTS: {json.dumps(constraints, ensure_ascii=False)}
DOMAIN KEYWORDS: {json.dumps(domain_keywords, ensure_ascii=False)}

{language_guidance}

{mode_guidance}

TASK:
Generate 2-4 highly targeted search queries for each research lane. Each query should:
- Be specific enough to find relevant results
- Use technical terms and industry vocabulary
- Focus on actionable information
- Avoid vague or overly broad terms
- Incorporate extracted entities and keywords where relevant

LANE DEFINITIONS:
- market: User pain points, buyer context, pricing models, adoption blockers, market size, demand validation
- technical: Architecture patterns, implementation approaches, failure modes, reliability constraints, production examples
- competitor: Existing tools, alternatives, open-source projects, positioning, feature comparison

RULES:
- Output JSON only, no markdown or explanation
- Each lane must have 2-4 queries
- Queries should be diverse and complementary
- Prioritize queries likely to surface concrete evidence over marketing content
- Use site: operators when targeting specific authoritative sources (github, pypi, stackoverflow)
- For Chinese input: include both Chinese and English queries for broader coverage

OUTPUT FORMAT:
{{
  "market": ["query1", "query2", ...],
  "technical": ["query1", "query2", ...],
  "competitor": ["query1", "query2", ...],
  "rationale": "Brief explanation of query strategy"
}}
"""

    formatter = Agent(
        role="Search Query Optimizer",
        goal="Generate targeted search queries for multi-lane research.",
        backstory="You are an expert at crafting precise search queries that surface actionable information.",
        allow_delegation=False,
        verbose=False,
        llm=librarian_llm,
    )
    task = Task(
        description=prompt,
        agent=formatter,
        expected_output="JSON with market, technical, competitor query lists.",
    )
    crew = Crew(
        agents=[formatter], tasks=[task], process=Process.sequential, verbose=False
    )

    try:
        _cost_trace(
            "smart_search_queries.kickoff", user_problem_chars=len(user_problem)
        )
        result = kickoff_crew_with_retry(
            crew,
            crew_name="smart_search_queries",
            logger=LOGGER,
            log_fields={
                "user_problem_chars": len(user_problem or ""),
                "mode": mode,
            },
        )
        raw_text = _extract_text_from_result(result) or ""

        # Try to parse the JSON
        parsed = _extract_first_json_object(raw_text)
        if parsed is None:
            return None

        smart_queries = SmartSearchQueries(
            market=parsed.get("market", []),
            technical=parsed.get("technical", []),
            competitor=parsed.get("competitor", []),
            rationale=parsed.get("rationale", ""),
        )

        # Validate: each lane should have at least 2 non-empty queries
        def _count_valid_queries(queries: List[str]) -> int:
            return len([q for q in queries if q and str(q).strip()])

        if (
            _count_valid_queries(smart_queries.market) < 2
            or _count_valid_queries(smart_queries.technical) < 2
            or _count_valid_queries(smart_queries.competitor) < 2
        ):
            return None

        # Filter out empty/whitespace-only queries
        smart_queries.market = [q for q in smart_queries.market if q and str(q).strip()]
        smart_queries.technical = [
            q for q in smart_queries.technical if q and str(q).strip()
        ]
        smart_queries.competitor = [
            q for q in smart_queries.competitor if q and str(q).strip()
        ]

        # Cache the result
        _cache_set_pydantic("smart_search_queries", cache_payload, smart_queries)

        return {
            "market": list(smart_queries.market)[:LIBRARIAN_MAX_QUERIES_PER_LANE],
            "technical": list(smart_queries.technical)[:LIBRARIAN_MAX_QUERIES_PER_LANE],
            "competitor": list(smart_queries.competitor)[
                :LIBRARIAN_MAX_QUERIES_PER_LANE
            ],
        }
    except _OperationCancelledError:
        # Cooperative cancellation must propagate — returning None would allow
        # the pipeline to continue running after the user cancelled.
        raise
    except Exception as e:
        print(f"[Warn] Smart search query generation failed: {e}", file=sys.stderr)
        return None


def _build_librarian_query_plan(
    user_problem: str,
    mode: str,
    *,
    language_hint: str = "English",
    direction_seed_plan: Optional["DirectionSeedPlan"] = None,
) -> Dict[str, Any]:
    search_language = _detect_search_language(language_hint, user_problem)
    mode_config = _get_mode_config(mode)
    mode_name = _validated_mode_name(mode)
    mode_project_type = _validated_mode_project_type(mode_config)
    seed_directions = list(getattr(direction_seed_plan, "directions", []) or [])
    query_budget_per_lane = max(
        LIBRARIAN_MAX_QUERIES_PER_LANE,
        len(seed_directions) + 2 if seed_directions else LIBRARIAN_MAX_QUERIES_PER_LANE,
    )

    def _seed_query_map() -> Dict[str, List[str]]:
        if not seed_directions:
            return {"market": [], "technical": [], "competitor": []}
        lane_suffixes = {
            "market": "market demand workflow pain",
            "technical": (
                "backtest implementation slippage risk"
                if mode_project_type == "quant"
                else (
                    "paper algorithm reproduce implementation benchmark"
                    if mode_project_type == "scientist"
                    else "implementation architecture risk"
                )
            ),
            "competitor": "alternatives competitors open source",
        }
        seed_query_map: Dict[str, List[str]] = {
            "market": [],
            "technical": [],
            "competitor": [],
        }
        for direction in seed_directions:
            label = str(getattr(direction, "label", "") or "").strip()
            thesis = str(getattr(direction, "thesis", "") or "").strip()
            search_terms = _dedupe_text_items(
                list(getattr(direction, "search_terms", []) or []) + [label, thesis],
                limit=4,
            )
            base_query = " ".join(term for term in search_terms if term).strip()
            if not base_query:
                continue
            for lane, suffix in lane_suffixes.items():
                seed_query_map[lane].append(f"{base_query} {suffix}".strip())
        return {
            lane: _dedupe_text_items(values, limit=query_budget_per_lane)
            for lane, values in seed_query_map.items()
        }

    def _merge_query_map(
        base_map: Dict[str, List[str]], extra_map: Dict[str, List[str]]
    ) -> Dict[str, List[str]]:
        merged: Dict[str, List[str]] = {}
        for lane in ("market", "technical", "competitor"):
            merged[lane] = _dedupe_text_items(
                list(base_map.get(lane, []) or []) + list(extra_map.get(lane, []) or []),
                limit=query_budget_per_lane,
            )
        return merged

    seed_query_map = _seed_query_map()

    llm_breakdown = _build_llm_problem_breakdown(user_problem, mode, language_hint)
    if llm_breakdown is not None:
        breakdown = llm_breakdown
        breakdown["mode_name"] = mode_name
        breakdown["normalized_problem"] = user_problem
        if seed_directions:
            breakdown["seed_directions"] = [
                {
                    "label": str(getattr(direction, "label", "") or "").strip(),
                    "thesis": str(getattr(direction, "thesis", "") or "").strip(),
                    "search_terms": list(getattr(direction, "search_terms", []) or []),
                }
                for direction in seed_directions
            ]
        lane_focus: Dict[str, List[str]] = {
            "market": list(
                breakdown.get("entities", [])[:3]
                + breakdown.get("domain_keywords", [])[:3]
            ),
            "technical": list(
                breakdown.get("technical_stack", [])[:3]
                + breakdown.get("domain_keywords", [])[:3]
            ),
            "competitor": list(
                breakdown.get("entities", [])[:2]
                + breakdown.get("domain_keywords", [])[:2]
            ),
        }
        breakdown["lane_focus"] = lane_focus
    else:
        breakdown = _build_librarian_problem_breakdown(user_problem, mode)

    smart_queries = _build_smart_search_queries(
        user_problem, mode, breakdown, language_hint=language_hint
    )
    if smart_queries is not None:
        merged_query_map = _merge_query_map(smart_queries, seed_query_map)
        print(
            "[Info] Using LLM-generated smart search queries for: "
            f"market={len(merged_query_map['market'])}, "
            f"technical={len(merged_query_map['technical'])}, "
            f"competitor={len(merged_query_map['competitor'])} "
            f"(language={search_language})"
        )
        return {
            "problem_breakdown": breakdown,
            "query_map": merged_query_map,
            "query_source": "smart_llm",
            "search_language": search_language,
            "query_budget_per_lane": query_budget_per_lane,
            "direction_seed_count": len(seed_directions),
        }

    query_map = {
        "market": _build_lane_queries_from_breakdown(
            breakdown, "market", mode_name=mode_config.name, language=search_language
        ),
        "technical": _build_lane_queries_from_breakdown(
            breakdown, "technical", mode_name=mode_config.name, language=search_language
        ),
        "competitor": _build_lane_queries_from_breakdown(
            breakdown,
            "competitor",
            mode_name=mode_config.name,
            language=search_language,
        ),
    }
    query_map = _merge_query_map(query_map, seed_query_map)
    return {
        "problem_breakdown": breakdown,
        "query_map": query_map,
        "query_source": "template_fallback",
        "search_language": search_language,
        "query_budget_per_lane": query_budget_per_lane,
        "direction_seed_count": len(seed_directions),
    }


def _build_librarian_query_map(
    user_problem: str,
    mode: str,
    *,
    language_hint: str = "English",
    direction_seed_plan: Optional["DirectionSeedPlan"] = None,
) -> Dict[str, List[str]]:
    return dict(
        _build_librarian_query_plan(
            user_problem,
            mode,
            language_hint=language_hint,
            direction_seed_plan=direction_seed_plan,
        )["query_map"]
    )


# ── Per-query search result cache ────────────────────────────────────────────
# Caches individual (provider, query) → List[ResearchCitation] results within
# the process lifetime using a TTL of 1 hour.  This prevents redundant HTTP
# round-trips when different user problems produce overlapping query strings,
# or when a run is retried shortly after a prior failure.
#
# The whole-context cache in run_librarian_research (keyed by full
# user_problem SHA256) already short-circuits re-running the entire research
# pipeline for identical inputs.  This per-query cache is a complementary
# layer that saves individual HTTP fetches for partially-overlapping query sets.

_SEARCH_QUERY_CACHE_TTL_SECONDS: float = 3600.0   # 1 hour
_SEARCH_QUERY_CACHE: Dict[str, Tuple[float, List["ResearchCitation"]]] = {}
_SEARCH_QUERY_CACHE_LOCK: _threading_s04.Lock = _threading_s04.Lock()


def _qcache_key(provider: str, query: str) -> str:
    """Return a short stable cache key for (provider, query)."""
    # hashlib is available from section_00 via globals().update()
    raw = f"{provider}||{query.strip()}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def _qcache_get(provider: str, query: str) -> Optional[List["ResearchCitation"]]:
    """Return cached results for *(provider, query)* if still within TTL, else None.

    The TTL check and eviction are performed inside the same lock acquisition
    as the dict read so that no other thread can refresh or evict the entry
    between the read and the expiry decision (avoids returning a stale result
    that expired milliseconds before the check).
    """
    key = _qcache_key(provider, query)
    with _SEARCH_QUERY_CACHE_LOCK:
        entry = _SEARCH_QUERY_CACHE.get(key)
        if entry is None:
            return None
        ts, results = entry
        if time.time() - ts > _SEARCH_QUERY_CACHE_TTL_SECONDS:
            # Expired — evict atomically while the lock is still held
            _SEARCH_QUERY_CACHE.pop(key, None)
            return None
        # Capture a copy while the lock is held so the caller gets a
        # consistent snapshot even if _qcache_set is called concurrently.
        return list(results)


def _qcache_set(provider: str, query: str, results: List["ResearchCitation"]) -> None:
    """Store *results* in the per-query cache for *(provider, query)*."""
    if not results:
        return   # do not cache empty result sets — provider may have been unavailable
    key = _qcache_key(provider, query)
    with _SEARCH_QUERY_CACHE_LOCK:
        _SEARCH_QUERY_CACHE[key] = (time.time(), list(results))


def clear_search_query_cache() -> None:
    """Evict all entries from the per-query search result cache."""
    with _SEARCH_QUERY_CACHE_LOCK:
        _SEARCH_QUERY_CACHE.clear()


def _search_websearch(
    query: str, *, timeout_seconds: Optional[int] = None
) -> List[ResearchCitation]:
    params = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{params}"
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": LIBRARIAN_WEBSEARCH_USER_AGENT,
    }
    try:
        html_text = _safe_http_text(
            url, headers=headers, timeout_seconds=timeout_seconds
        )
        citations = _extract_websearch_citations_from_html(html_text, query=query)
        if citations:
            return citations
    except _OperationCancelledError:
        raise
    except Exception:
        pass
    lite_url = f"https://lite.duckduckgo.com/lite/?{params}"
    try:
        html_text = _safe_http_text(
            lite_url, headers=headers, timeout_seconds=timeout_seconds
        )
        return _extract_websearch_citations_from_html(html_text, query=query)
    except _OperationCancelledError:
        raise
    except Exception:
        # Both primary and lite endpoints failed.  Return an empty citation
        # list so the caller can record the provider as empty and move on
        # rather than aborting the whole lane.
        return []


def _search_context7(
    query: str,
    *,
    user_problem: str,
    mode: str,
    problem_breakdown: Optional[Dict[str, Any]] = None,
    lane_queries: Optional[List[str]] = None,
) -> List[ResearchCitation]:
    libraries = _extract_context7_library_candidates(
        user_problem,
        mode,
        problem_breakdown=problem_breakdown,
        lane_queries=lane_queries,
    )
    if not libraries:
        raise RuntimeError("No obvious library candidates were found for Context7.")
    # Strip any `site:` qualifiers that may have been injected by the LLM or
    # copied from another search lane; the Context7 API treats them as literal
    # text, which distorts results and can exceed query-length limits.
    import re as _re
    clean_query = _re.sub(r"\bsite:\S+\s*", "", query, flags=_re.IGNORECASE).strip()
    if not clean_query:
        return []
    base_url = _current_context7_api_url()
    citations: List[ResearchCitation] = []
    for library in libraries:
        url = _append_url_query(base_url, {"query": f"{library} {clean_query}"})
        response = _safe_http_json(url)
        results = response.get("results", []) if isinstance(response, dict) else []
        for item in results[:LIBRARIAN_MAX_RESULTS_PER_QUERY]:
            citation = _citation_from_payload(
                "context7",
                clean_query,
                title=item.get("title") or library,
                url=item.get("url") or item.get("canonicalUrl"),
                snippet=item.get("snippet") or item.get("text") or "",
                evidence_type="docs",
            )
            if citation is not None:
                citations.append(citation)
    return citations


def _resolve_github_token() -> str:
    """Return a GitHub API token from the environment, or "" when absent.

    Checked in order: GITHUB_TOKEN, GH_TOKEN, GITHUB_API_TOKEN.
    Whitespace-stripped; placeholder sentinels (your_*, xxx*) are ignored.
    """
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_API_TOKEN"):
        raw = str(os.environ.get(key) or "").strip()
        if not raw:
            continue
        lowered = raw.lower()
        if lowered.startswith(("your_", "xxxx", "placeholder", "changeme")):
            continue
        return raw
    return ""


def _github_api_headers(*, accept: str) -> Dict[str, str]:
    headers: Dict[str, str] = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _resolve_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _search_github_repositories(query: str) -> List[ResearchCitation]:
    # v16.9.72: skip entirely when no GITHUB_TOKEN is configured.  The
    # ``/search/repositories`` endpoint allows anonymous calls but the
    # quota is only **10 req/hr** (vs 30 req/min when authenticated), and
    # a single librarian run easily exhausts it — every subsequent
    # repo-search call returns 403 ``rate limit exceeded`` and the
    # transport error string previously polluted ``key_risks`` (see fix
    # #4 in v16.9.72).  Mirroring the guard already in
    # :func:`_search_github_code`.
    if not _resolve_github_token():
        return []
    # The GitHub Repositories API does not support web-search-style `site:`
    # qualifiers (e.g. "site:github.com …").  Passing them through causes a
    # 422 Unprocessable Entity response.  Strip them before constructing the
    # request URL.
    import re as _re
    clean_query = _re.sub(r"\bsite:\S+\s*", "", query, flags=_re.IGNORECASE).strip()
    # GitHub's search API returns 422 when the query string is too long (>150
    # chars).  Truncate on a word boundary to stay within the safe limit.
    if len(clean_query) > 150:
        clean_query = clean_query[:150].rsplit(" ", 1)[0].strip()
    if not clean_query:
        return []
    url = _append_url_query(
        "https://api.github.com/search/repositories",
        {
            "q": clean_query,
            "sort": "stars",
            "order": "desc",
            "per_page": LIBRARIAN_MAX_RESULTS_PER_QUERY,
        },
    )
    response = _safe_http_json(
        url,
        headers=_github_api_headers(accept="application/vnd.github+json"),
    )
    items = response.get("items", []) if isinstance(response, dict) else []
    citations: List[ResearchCitation] = []
    for item in items[:LIBRARIAN_MAX_RESULTS_PER_QUERY]:
        description = item.get("description") or ""
        topics = item.get("topics") or []
        topic_text = ", ".join(
            str(topic).strip() for topic in topics[:5] if str(topic).strip()
        )
        snippet = description.strip()
        if topic_text:
            snippet = f"{snippet} Topics: {topic_text}".strip()
        citation = _citation_from_payload(
            "github",
            query,
            title=item.get("full_name") or item.get("name"),
            url=item.get("html_url"),
            snippet=snippet,
            evidence_type="repo_search",
        )
        if citation is not None:
            citations.append(citation)
    return _dedupe_citations(citations, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY)


def _search_github_code(query: str) -> List[ResearchCitation]:
    # GitHub's search/code endpoint REQUIRES authentication (returns 401
    # for anonymous callers).  Skip entirely when no token is configured
    # rather than burn an unauthenticated request that is guaranteed to
    # 401 and surface as a provider error in the librarian output.
    if not _resolve_github_token():
        return []
    # Apply the same sanitisation as _search_github_repositories: strip any
    # web-search-style `site:` qualifiers (GitHub API rejects them with 422)
    # and truncate to ≤150 chars on a word boundary (long queries also 422).
    import re as _re
    clean_query = _re.sub(r"\bsite:\S+\s*", "", query, flags=_re.IGNORECASE).strip()
    if len(clean_query) > 150:
        clean_query = clean_query[:150].rsplit(" ", 1)[0].strip()
    if not clean_query:
        return []
    url = _append_url_query(
        "https://api.github.com/search/code",
        {
            "q": clean_query,
            "per_page": LIBRARIAN_MAX_RESULTS_PER_QUERY,
        },
    )
    response = _safe_http_json(
        url,
        headers=_github_api_headers(
            accept="application/vnd.github.text-match+json, application/vnd.github+json",
        ),
    )
    items = response.get("items", []) if isinstance(response, dict) else []
    citations: List[ResearchCitation] = []
    for item in items[:LIBRARIAN_MAX_RESULTS_PER_QUERY]:
        repo_info = item.get("repository") or {}
        repo_name = str(repo_info.get("full_name") or "").strip()
        path = str(item.get("path") or "").strip()
        html_url = str(item.get("html_url") or "").strip()
        text_matches = item.get("text_matches") or []

        snippet_parts: List[str] = []
        for match in text_matches:
            fragment = re.sub(
                r"\s+", " ", str((match or {}).get("fragment") or "")
            ).strip()
            if fragment:
                snippet_parts.append(fragment)

        if not snippet_parts and repo_name:
            snippet_parts.append(f"Repository: {repo_name}")
        if path:
            snippet_parts.append(f"Path: {path}")

        citation = _citation_from_payload(
            "github",
            clean_query,
            title=f"{repo_name}:{path}".strip(":")
            or item.get("name")
            or "GitHub code result",
            url=html_url,
            snippet=" | ".join(_dedupe_text_items(snippet_parts, limit=3)),
            evidence_type="code_search",
        )
        if citation is not None:
            citations.append(citation)

    return _dedupe_citations(citations, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY)


def _search_github(query: str, *, lane: Optional[str] = None) -> List[ResearchCitation]:
    normalized_lane = str(lane or "").strip().lower()
    if normalized_lane == "technical":
        try:
            code_hits = _search_github_code(query)
        except _OperationCancelledError:
            raise
        except Exception:
            code_hits = []
        if code_hits:
            return code_hits
    return _search_github_repositories(query)


def _search_arxiv(query: str) -> List[ResearchCitation]:
    encoded_query = urllib.parse.quote(query)
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query=all:{encoded_query}&start=0&max_results={LIBRARIAN_MAX_RESULTS_PER_QUERY}"
        "&sortBy=relevance&sortOrder=descending"
    )
    raw_xml = _safe_http_text(
        url,
        headers={"Accept": "application/atom+xml, text/xml, application/xml"},
    )
    # arxiv occasionally returns an HTML error page or truncated Atom feed —
    # `ET.fromstring` then raises `ParseError`.  Treat as empty result rather
    # than propagating to the librarian loop (which would record a provider
    # error and skip the rest of the lane).
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    citations: List[ResearchCitation] = []
    for entry in root.findall("atom:entry", ns)[:LIBRARIAN_MAX_RESULTS_PER_QUERY]:
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        summary = re.sub(
            r"\s+",
            " ",
            (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip(),
        )
        url_text = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        citation = _citation_from_payload(
            "arxiv",
            query,
            title=title,
            url=url_text,
            snippet=summary,
            evidence_type="paper",
        )
        if citation is not None:
            citations.append(citation)
    return _dedupe_citations(citations, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY)


def _search_paperswithcode(query: str) -> List[ResearchCitation]:
    # Strip any existing `site:` qualifiers from the incoming query before
    # prepending our own `site:paperswithcode.com`.  Competitor-lane queries
    # can arrive with `site:github.com` already embedded (injected by the LLM
    # or by _search_github_repositories callers), which causes DuckDuckGo to
    # receive contradictory `site:` directives and return a non-retryable 403.
    import re as _re
    clean_query = _re.sub(r"\bsite:\S+\s*", "", query, flags=_re.IGNORECASE).strip()
    if not clean_query:
        return []
    # Truncate to 150 chars on a word boundary.  LLM-generated queries can be
    # excessively verbose (160+ chars with duplicate keywords); long queries are
    # more likely to trigger DuckDuckGo bot-detection and produce noisy results.
    if len(clean_query) > 150:
        clean_query = clean_query[:150].rsplit(" ", 1)[0].strip()
    if not clean_query:
        return []
    citations = _search_websearch(f"site:paperswithcode.com {clean_query}")
    rewritten: List[ResearchCitation] = []
    for citation in citations:
        rewritten.append(
            _model_copy_compat(
                citation,
                update={
                    "provider": "paperswithcode",
                    "query": clean_query[:240],
                    "evidence_type": "discovery_only",
                },
            )
        )
    return _dedupe_citations(rewritten, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY)


def _build_grep_app_search_params(
    *,
    query: str,
    page: int = 1,
    repo_filter: Optional[str] = None,
    path_filter: Optional[str] = None,
    language_filter: Optional[List[str]] = None,
    use_regex: bool = False,
    whole_words: bool = False,
    case_sensitive: bool = False,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "q": query,
        "page": max(1, int(page)),
        "regexp": "true" if use_regex else "false",
        "words": "true" if whole_words else "false",
        "case": "true" if case_sensitive else "false",
    }
    if repo_filter:
        params["r"] = repo_filter
        params["f.repo.pattern"] = repo_filter
    if path_filter:
        params["path"] = path_filter
        params["f.path.pattern"] = path_filter
    if language_filter:
        normalized_languages = [
            str(item).strip().lower() for item in language_filter if str(item).strip()
        ]
        if normalized_languages:
            params["l"] = ",".join(normalized_languages)
            params["f.lang"] = normalized_languages
    return params


def _grep_app_result_url(
    query: str, *, repo: Optional[str] = None, path: Optional[str] = None
) -> str:
    params: Dict[str, Any] = {"q": query}
    if repo:
        params["f.repo.pattern"] = repo
    if path:
        params["f.path.pattern"] = path
    return "https://grep.app/search?" + urllib.parse.urlencode(params)


def _normalize_grep_app_snippet(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, list):
        parts = [
            re.sub(r"\s+", " ", str(item)).strip()
            for item in value
            if str(item).strip()
        ]
        return " ... ".join(parts)
    if isinstance(value, dict):
        parts: List[str] = []
        for key in ("text", "snippet", "lines", "value"):
            candidate = value.get(key)
            normalized = _normalize_grep_app_snippet(candidate)
            if normalized:
                parts.append(normalized)
        return " ... ".join(_dedupe_text_items(parts, limit=4))
    return re.sub(r"\s+", " ", str(value)).strip()


_CJK_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _is_grep_app_compatible_query(query: str) -> bool:
    """grep.app is a *code* search engine — only feed it code-like queries.

    Skip when the query contains CJK characters or reads as natural-language
    prose (many short ASCII words without code tokens).  This prevents
    persistent 4xx/5xx responses that would burn retry budget and surface
    as provider_errors in the librarian output.
    """
    text = str(query or "").strip()
    if not text:
        return False
    if _CJK_PATTERN.search(text):
        return False
    # Natural-language heuristic: more than 8 whitespace-separated tokens
    # and no obvious code tokens (symbols, camelCase, snake_case, dotted names).
    tokens = text.split()
    if len(tokens) > 8:
        has_code_token = any(
            re.search(r"[_\.\(\)\[\]{}:;=<>/\\]|[A-Z][a-z]+[A-Z]|[a-z]_[a-z]", tok)
            for tok in tokens
        )
        if not has_code_token:
            return False
    return True


def _search_grep_app(query: str) -> List[ResearchCitation]:
    # grep.app is a *code* search engine and does not understand web-search-style
    # `site:` qualifiers (e.g. "site:github.com …").  LLM-generated queries or
    # queries forwarded from other search lanes can arrive with these embedded;
    # passing them through causes HTTP 4xx errors that exhaust the retry budget
    # and open the circuit breaker, blocking all subsequent grep.app calls for
    # the rest of the session.  Strip before any further processing.
    import re as _re
    clean_query = _re.sub(r"\bsite:\S+\s*", "", query, flags=_re.IGNORECASE).strip()
    if not clean_query:
        return []
    # Truncate to 150 chars on a word boundary — excessively long queries
    # produce poor code-search results and are more likely to trigger rejections.
    if len(clean_query) > 150:
        clean_query = clean_query[:150].rsplit(" ", 1)[0].strip()
    if not clean_query:
        return []
    # Compatibility check runs on the *stripped* query: a query that was only
    # "code-like" due to the `site:` token should not be forwarded to grep.app.
    if not _is_grep_app_compatible_query(clean_query):
        return []
    citations: List[ResearchCitation] = []
    pages_to_fetch = max(1, min(3, (LIBRARIAN_MAX_RESULTS_PER_QUERY + 9) // 10))
    for page in range(1, pages_to_fetch + 1):
        params = _build_grep_app_search_params(query=clean_query, page=page)
        url = "https://grep.app/api/search?" + urllib.parse.urlencode(
            params, doseq=True
        )
        response = _safe_http_json(url)
        hits = response.get("hits", {}) if isinstance(response, dict) else {}
        results = hits.get("hits", []) if isinstance(hits, dict) else []
        for item in results:
            repo = item.get("repo", {})
            path = item.get("path", {})
            snippets = (
                item.get("content", {}).get("snippet")
                if isinstance(item.get("content"), dict)
                else item.get("content")
            )
            title = (
                f"{repo.get('raw', '')}:{path.get('raw', '')}".strip(":")
                or "grep.app result"
            )
            citation = _citation_from_payload(
                "grep_app",
                clean_query,
                title=title,
                url=_grep_app_result_url(
                    clean_query, repo=repo.get("raw"), path=path.get("raw")
                ),
                snippet=_normalize_grep_app_snippet(snippets),
                evidence_type="code_search",
            )
            if citation is not None:
                citations.append(citation)
            if len(citations) >= LIBRARIAN_MAX_RESULTS_PER_QUERY:
                return _dedupe_citations(
                    citations, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY
                )
    return _dedupe_citations(citations, limit=LIBRARIAN_MAX_RESULTS_PER_QUERY)


def _collect_librarian_search_materials(
    user_problem: str,
    *,
    mode: str,
    language_hint: str = "English",
    direction_seed_plan: Optional["DirectionSeedPlan"] = None,
) -> Dict[str, Any]:
    query_plan = _build_librarian_query_plan(
        user_problem,
        mode,
        language_hint=language_hint,
        direction_seed_plan=direction_seed_plan,
    )
    problem_breakdown = dict(query_plan.get("problem_breakdown") or {})
    query_map = {
        str(lane): list(queries or [])
        for lane, queries in (query_plan.get("query_map") or {}).items()
    }
    search_language = str(query_plan.get("search_language") or "en")
    query_budget_per_lane = int(
        query_plan.get("query_budget_per_lane") or LIBRARIAN_MAX_QUERIES_PER_LANE
    )
    lane_citations: Dict[str, List[ResearchCitation]] = {lane: [] for lane in query_map}
    providers_used: List[str] = []
    provider_errors: Dict[str, str] = {}
    provider_lane_allowlist: Dict[str, Set[str]] = {
        "context7": {"technical"},
        "arxiv": {"technical"},
        "github": {"technical", "competitor"},
        "paperswithcode": {"technical", "competitor"},
    }

    for provider_name in LIBRARIAN_SEARCH_PROVIDERS:
        provider_had_success = False
        staged_lane_citations: Dict[str, List[ResearchCitation]] = {
            lane: [] for lane in query_map
        }
        provider_error_details: List[str] = []
        for lane, lane_queries in query_map.items():
            if lane not in provider_lane_allowlist.get(
                provider_name, set(query_map.keys())
            ):
                continue
            last_query: Optional[str] = None
            try:
                lane_results: List[ResearchCitation] = []
                # Tracks whether a real HTTP search request has been issued in
                # this lane yet.  Cache hits do NOT count — a cache hit costs
                # no network round-trip, so it must not consume the "first
                # request is exempt from rate-limit delay" token.  Using a
                # separate flag (_is_first_http_in_lane) instead of
                # _is_first_query_in_lane prevents the bug where a leading
                # cache hit would cause the first actual HTTP request to
                # incorrectly wait for LIBRARIAN_INTER_QUERY_DELAY_SECONDS.
                _is_first_http_in_lane = True
                for query in lane_queries[:query_budget_per_lane]:
                    # Per-query cache hit: skip HTTP fetch and rate-limit delay
                    # entirely — the cache serves a copy of the prior result.
                    # context7 queries depend on extra contextual arguments that
                    # vary per-call, so they are excluded from caching.
                    _cached_qresult: Optional[List[ResearchCitation]] = (
                        None
                        if provider_name == "context7"
                        else _qcache_get(provider_name, query)
                    )
                    if _cached_qresult is not None:
                        lane_results.extend(_cached_qresult)
                        last_query = query
                        # Do NOT clear _is_first_http_in_lane: this was a
                        # cache hit, no HTTP request was made, so the next
                        # real request is still the first and must not be
                        # delayed by the rate-limit guard.
                        continue

                    # Rate-limit guard: pause between consecutive HTTP search
                    # requests to avoid 429 / block responses from DuckDuckGo
                    # and other search endpoints.  The first HTTP request in
                    # each lane is exempt; subsequent ones always wait.
                    if not _is_first_http_in_lane:
                        time.sleep(LIBRARIAN_INTER_QUERY_DELAY_SECONDS)
                    _is_first_http_in_lane = False
                    last_query = query
                    _query_results: List[ResearchCitation] = []
                    if provider_name == "websearch":
                        _query_results = _search_websearch(query)
                    elif provider_name == "context7":
                        _query_results = _search_context7(
                            query,
                            user_problem=user_problem,
                            mode=mode,
                            problem_breakdown=problem_breakdown,
                            lane_queries=lane_queries,
                        )
                    elif provider_name == "grep_app":
                        _query_results = _search_grep_app(query)
                    elif provider_name == "github":
                        _query_results = _search_github(query, lane=lane)
                    elif provider_name == "arxiv":
                        _query_results = _search_arxiv(query)
                    elif provider_name == "paperswithcode":
                        _query_results = _search_paperswithcode(query)
                    # Store successful result in the per-query cache
                    if provider_name != "context7":
                        _qcache_set(provider_name, query, _query_results)
                    lane_results.extend(_query_results)
                staged_lane_citations[lane].extend(lane_results)
                provider_had_success = True
            except _OperationCancelledError:
                # Cooperative cancellation must abort the entire search — do not
                # record as a per-query provider error and continue searching.
                raise
            except Exception as exc:
                query_label = repr(last_query) if last_query else "(no query)"
                provider_error_details.append(
                    f"[{lane}] query={query_label} error={exc}"
                )
                continue
        if provider_error_details:
            provider_errors[provider_name] = " | ".join(provider_error_details)[:600]
        for lane, lane_results in staged_lane_citations.items():
            lane_citations[lane].extend(lane_results)
        if provider_had_success and provider_name not in providers_used:
            providers_used.append(provider_name)

    for lane in list(lane_citations.keys()):
        lane_citations[lane] = _dedupe_citations(
            lane_citations[lane], limit=LIBRARIAN_MAX_CITATIONS
        )

    all_citations: List[ResearchCitation] = []
    for citations in lane_citations.values():
        all_citations.extend(citations)
    all_citations = _verify_research_citations(
        _dedupe_citations(all_citations, limit=LIBRARIAN_MAX_CITATIONS)
    )
    verified_by_key = {
        (citation.provider.lower(), citation.url.strip().lower()): citation
        for citation in all_citations
    }
    for lane in list(lane_citations.keys()):
        normalized_lane: List[ResearchCitation] = []
        for citation in lane_citations[lane]:
            key = (citation.provider.lower(), citation.url.strip().lower())
            normalized_lane.append(verified_by_key.get(key, citation))
        lane_citations[lane] = _dedupe_citations(
            normalized_lane, limit=LIBRARIAN_MAX_CITATIONS
        )

    lane_materials: Dict[str, str] = {}
    for lane, lane_queries in query_map.items():
        lane_focus = (problem_breakdown.get("lane_focus") or {}).get(lane, [])
        lines = [
            f"Lane: {lane}",
            f"Core objective: {problem_breakdown.get('core_objective') or user_problem}",
            f"Entities: {json.dumps(problem_breakdown.get('entities') or [], ensure_ascii=False)}",
            f"Constraints: {json.dumps(problem_breakdown.get('constraints') or [], ensure_ascii=False)}",
            f"Lane focus: {json.dumps(lane_focus or [], ensure_ascii=False)}",
            f"Queries: {json.dumps(lane_queries, ensure_ascii=False)}",
        ]
        if lane_citations[lane]:
            lines.append("Evidence:")
            for citation in lane_citations[lane][:6]:
                lines.append(
                    f"- [{citation.provider}] {citation.title} | {citation.url} | verify={citation.verification_status or 'n/a'} | domain={citation.source_domain or 'n/a'} | hash={(citation.snippet_hash or '')[:12]} | {citation.snippet}"
                )
        else:
            lines.append("Evidence: none retrieved.")
        lane_materials[lane] = "\n".join(lines)

    suggested_queries: List[str] = []
    for lane_queries in query_map.values():
        suggested_queries.extend(lane_queries)

    return {
        "problem_breakdown": problem_breakdown,
        "query_map": query_map,
        "search_language": search_language,
        "suggested_search_queries": _dedupe_text_items(suggested_queries, limit=12),
        "search_strategy": "+".join(LIBRARIAN_SEARCH_PROVIDERS),
        "direction_seed_count": int(query_plan.get("direction_seed_count") or 0),
        "providers_used": providers_used,
        "provider_errors": provider_errors,
        "citations": all_citations,
        "lane_materials": lane_materials,
    }


def _build_fallback_research_context(
    user_problem: str,
    materials: Dict[str, Any],
) -> ResearchContext:
    citations: List[ResearchCitation] = list(materials.get("citations") or [])
    snippet_samples = [citation.snippet for citation in citations if citation.snippet]
    summary_bits = _dedupe_text_items(snippet_samples, limit=3) or [
        "No external evidence retrieved; downstream debate should treat unknowns as high-risk."
    ]
    # v16.9.72: previously we populated ``key_risks`` from
    # ``provider_errors.values()`` — that meant raw HTTP error strings
    # (e.g. ``"Client error '429 Too Many Requests'…"``,
    # ``"Circuit breaker '…' is open after 3 failures."``) were injected as
    # product-level risks and read verbatim by every downstream debate
    # agent (Explorer, Comparator, Skeptic, Auditor, Judge).  HTTP
    # transport errors have no bearing on product risk and were
    # confusing the agents into surfacing infrastructure noise as
    # business-level concerns.  ``provider_errors`` is still preserved
    # below as a separate field so observability tooling can surface
    # transport failures without contaminating the prompt.
    return ResearchContext(
        user_problem=user_problem,
        search_strategy=str(materials.get("search_strategy") or ""),
        providers_used=list(materials.get("providers_used") or []),
        suggested_search_queries=list(materials.get("suggested_search_queries") or []),
        market_examples=[],
        existing_tools=[],
        technical_patterns=_dedupe_text_items(summary_bits, limit=6),
        key_risks=[],
        unknowns=[
            "Validate retrieved evidence before committing to a single product direction."
        ],
        synthesized_summary=" ".join(summary_bits[:3]),
        citations=citations[:LIBRARIAN_MAX_CITATIONS],
        provider_errors=dict(materials.get("provider_errors") or {}),
    )


def _render_research_context_for_prompt(
    research_context: Optional[ResearchContext],
) -> str:
    if research_context is None:
        return "No research context available."
    decision_critical_unknowns = _extract_decision_critical_unknowns(research_context)
    lines = [
        f"Search strategy: {research_context.search_strategy or 'n/a'}",
        f"Providers used: {', '.join(research_context.providers_used) if research_context.providers_used else 'none'}",
        "Suggested queries are a decomposed search plan across market, technical, and competitor lanes.",
        f"Suggested queries: {json.dumps(research_context.suggested_search_queries[:8], ensure_ascii=False)}",
        f"Evidence coverage: {json.dumps(research_context.evidence_coverage or {}, ensure_ascii=False)}",
        f"Market examples: {json.dumps(research_context.market_examples[:5], ensure_ascii=False)}",
        f"Existing tools: {json.dumps(research_context.existing_tools[:5], ensure_ascii=False)}",
        f"Technical patterns: {json.dumps(research_context.technical_patterns[:5], ensure_ascii=False)}",
        f"Key risks: {json.dumps(research_context.key_risks[:5], ensure_ascii=False)}",
        f"Unknowns: {json.dumps(research_context.unknowns[:5], ensure_ascii=False)}",
    ]
    if decision_critical_unknowns:
        lines.append(
            f"Decision-critical unknowns: {json.dumps(decision_critical_unknowns[:5], ensure_ascii=False)}"
        )
    if research_context.hallucination_flags:
        lines.append(
            f"Unsupported claims removed: {json.dumps(research_context.hallucination_flags[:8], ensure_ascii=False)}"
        )
    if research_context.field_capability_matrix:
        lines.append("Field capability matrix:")
        for item in research_context.field_capability_matrix[:8]:
            lines.append(
                "- "
                + f"{item.field_name}: tier={item.tier}, availability={item.availability_class}, "
                + f"lane={item.recommended_lane}, horizons={json.dumps(item.recommended_horizons, ensure_ascii=False)}, "
                + f"hard_gate={item.hard_gate_rule}, soft_preference={item.soft_preference_rule}"
            )
    if research_context.claim_attributions:
        lines.append("Claim attributions (PRIMARY EVIDENCE):")
        for item in research_context.claim_attributions[:10]:
            lines.append(
                f"- [{item.category}] {item.claim} <= {json.dumps(item.citation_urls[:3], ensure_ascii=False)}"
            )
    if research_context.citations:
        lines.append("Citations (PRIMARY EVIDENCE):")
        for citation in research_context.citations[:8]:
            lines.append(
                f"- [{citation.provider}] {citation.title} | {citation.url} | domain={citation.source_domain or 'n/a'} | verify={citation.verification_status or 'n/a'} | hash={(citation.snippet_hash or '')[:12]}"
            )
    lines.append(
        f"Summary (SECONDARY COMPRESSED NARRATIVE ONLY): {research_context.synthesized_summary or 'n/a'}"
    )
    return limit_text("\n".join(lines), 4000)


def _is_decision_critical_unknown(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered.strip():
        return False
    critical_markers = (
        "legal",
        "regulat",
        "compliance",
        "license",
        "feasible",
        "viable",
        "possible",
        "possible to",
        "allowed",
        "permission",
        "access",
        "availability",
        "whether",
        "unclear if",
        "cannot",
        "can't",
        "data source",
        "api access",
        "technical constraint",
        "hard dependency",
        "security boundary",
        "trust boundary",
    )
    return any(marker in lowered for marker in critical_markers)


def _extract_decision_critical_unknowns(
    research_context: Optional[ResearchContext],
) -> List[str]:
    if research_context is None:
        return []
    return _dedupe_text_items(
        [
            item
            for item in list(research_context.unknowns or [])
            if _is_decision_critical_unknown(item)
        ],
        limit=5,
    )


def _score_to_0_5(value: int, *, step: int = 1) -> int:
    value = max(0, int(value or 0))
    step = max(1, int(step or 1))
    return max(0, min(5, value // step))


def _direction_confidence_rank(confidence: str) -> int:
    normalized = str(confidence or "").strip().lower()
    if normalized == "high":
        return 2
    if normalized == "medium":
        return 1
    return 0


def _direction_confidence_from_rank(rank: int) -> str:
    if rank >= 2:
        return "high"
    if rank == 1:
        return "medium"
    return "low"


def _grounding_text_overlap_score(left: str, right: str) -> int:
    left_tokens = _tokenize_for_grounding(left)
    right_tokens = _tokenize_for_grounding(right)
    if not left_tokens or not right_tokens:
        return 0
    overlap = left_tokens & right_tokens
    if len(overlap) >= 2:
        return len(overlap)
    if len(overlap) == 1:
        token = next(iter(overlap))
        if len(token) >= 5:
            return 1
    left_lower = str(left or "").lower()
    right_lower = str(right or "").lower()
    if left_lower and (left_lower in right_lower or right_lower in left_lower):
        return 1
    return 0


def _direction_claim_support_score(
    text: str,
    research_context: Optional[ResearchContext],
) -> int:
    if research_context is None or not text:
        return 0
    best_score = 0
    for attribution in list(research_context.claim_attributions or []):
        overlap_score = _grounding_text_overlap_score(text, attribution.claim)
        if overlap_score <= 0:
            continue
        best_score = max(
            best_score, overlap_score * int(attribution.support_score or 0)
        )
    return best_score


def _direction_penalty_overlap_count(text: str, candidates: List[str]) -> int:
    if not text:
        return 0
    return sum(
        1
        for candidate in candidates
        if _grounding_text_overlap_score(text, candidate) > 0
    )


def _research_context_is_validation_first(
    research_context: Optional[ResearchContext],
) -> bool:
    if research_context is None:
        return False
    text = str(getattr(research_context, "user_problem", "") or "").strip()
    if not text:
        return False
    if _text_contains_any_marker(text, _VALIDATION_FIRST_REQUEST_MARKERS):
        return True
    return (
        _count_text_markers(text, _VALIDATION_FIRST_TOPIC_MARKERS) >= 2
        and _count_text_markers(text, _VALIDATION_SCOPE_DELIVERABLE_MARKERS) >= 1
    )


def _direction_option_looks_validation_first(
    option: Optional[DirectionOption],
) -> bool:
    if option is None:
        return False
    text = " ".join(
        [
            str(getattr(option, "name", "") or ""),
            str(getattr(option, "thesis", "") or ""),
            str(getattr(option, "primary_metric", "") or ""),
            str(getattr(option, "fastest_test", "") or ""),
            str(getattr(option, "major_risk", "") or ""),
        ]
    ).strip()
    if not text:
        return False
    return (
        _count_text_markers(text, _VALIDATION_FIRST_TOPIC_MARKERS) >= 2
        and (
            _count_text_markers(text, _VALIDATION_SCOPE_DELIVERABLE_MARKERS) >= 1
            or any(
                marker in text.lower()
                for marker in ("compare", "comparison", "benchmark", "methodology", "對照", "比較")
            )
        )
    )


def _validation_first_prompt_guidance(user_problem: str) -> str:
    text = str(user_problem or "").strip()
    if not text:
        return ""
    if not (
        _text_contains_any_marker(text, _VALIDATION_FIRST_REQUEST_MARKERS)
        or (
            _count_text_markers(text, _VALIDATION_FIRST_TOPIC_MARKERS) >= 2
            and _count_text_markers(text, _VALIDATION_SCOPE_DELIVERABLE_MARKERS) >= 1
        )
    ):
        return ""
    return (
        "VALIDATION-FIRST ROUTING:\n"
        "- The user is asking for a validation/calibration/measurement-first scope, not a final production module.\n"
        "- Prefer reversible validation frameworks, semantic checks, threshold calibration, measurement harnesses, and comparison pipelines over production alpha logic.\n"
        "- If the current blockers are evidence gaps, missing semantics, uncalibrated thresholds, or unresolved data validity, favor directions that directly measure those unknowns.\n"
        "- Reward directions whose fastest_test produces machine-readable evidence, reports, or calibration outputs.\n"
    )


def _deterministic_direction_option_score(
    option: DirectionOption,
    research_context: Optional[ResearchContext],
) -> int:
    if research_context is None:
        return 0
    thesis_score = _direction_claim_support_score(option.thesis, research_context)
    metric_score = _direction_claim_support_score(
        option.primary_metric, research_context
    )
    test_score = _direction_claim_support_score(option.fastest_test, research_context)
    risk_score = _direction_claim_support_score(option.major_risk, research_context)
    critical_unknowns = _extract_decision_critical_unknowns(research_context)
    hallucination_claims = [
        item.split(":", 1)[1].strip()
        for item in list(research_context.hallucination_flags or [])
        if ":" in item
    ]
    option_text = " ".join(
        [
            option.name,
            option.thesis,
            option.primary_metric,
            option.fastest_test,
            option.major_risk,
        ]
    ).strip()
    critical_penalty = 10 * _direction_penalty_overlap_count(
        option_text, critical_unknowns
    )
    hallucination_penalty = 12 * _direction_penalty_overlap_count(
        option_text, hallucination_claims
    )
    validation_bonus = (
        18
        if _research_context_is_validation_first(research_context)
        and _direction_option_looks_validation_first(option)
        else 0
    )
    return max(
        0,
        (thesis_score * 4)
        + (metric_score * 3)
        + (test_score * 2)
        + risk_score
        + validation_bonus
        - critical_penalty
        - hallucination_penalty,
    )


def _find_direction_option(
    decision: Optional[DirectionDecision],
    key: str,
) -> Optional[DirectionOption]:
    normalized_key = _canonical_report_direction_key(key)
    if decision is None or not normalized_key:
        return None
    return next(
        (
            option
            for option in list(decision.options or [])
            if str(option.key or "").strip().upper() == normalized_key
        ),
        None,
    )


def _infer_option_horizon_class(option: Optional[DirectionOption]) -> str:
    if option is None:
        return "unknown"
    text = " ".join(
        [
            str(option.name or ""),
            str(option.thesis or ""),
            str(option.primary_metric or ""),
            str(option.fastest_test or ""),
        ]
    ).lower()
    short_markers = (
        "intraday",
        "short-term",
        "short term",
        "scalp",
        "scalping",
        "minute",
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "短線",
        "日內",
        "分鐘",
    )
    long_markers = (
        "swing",
        "multi-day",
        "multi day",
        "weekly",
        "position",
        "1d",
        "daily",
        "long horizon",
        "mid-term",
        "medium-term",
        "中週期",
        "中周期",
        "波段",
        "日線",
        "週線",
        "长周期",
        "長週期",
    )
    if any(marker in text for marker in short_markers):
        return "short"
    if any(marker in text for marker in long_markers):
        return "long"
    return "unknown"


def _capability_matrix_gate_for_option(
    option: Optional[DirectionOption],
    research_context: Optional[ResearchContext],
) -> Tuple[bool, List[str], str]:
    if option is None or research_context is None:
        return True, [], "production"
    matrix = list(getattr(research_context, "field_capability_matrix", []) or [])
    if not matrix:
        return True, [], "production"

    text = " ".join(
        [
            str(option.name or ""),
            str(option.thesis or ""),
            str(option.primary_metric or ""),
            str(option.fastest_test or ""),
            str(option.major_risk or ""),
        ]
    ).lower()
    horizon = _infer_option_horizon_class(option)
    field_aliases = {
        "ohlcv": (
            "ohlcv",
            "candle",
            "candlestick",
            "kline",
            "price action",
            "成交量",
            "k 線",
            "k线",
        ),
        "mark_price_kline": ("mark price", "mark-price", "標記價格", "标记价格"),
        "index_price_kline": ("index price", "指數價格", "指数价格"),
        "premium_index_kline": ("premium", "premium index", "溢價", "溢价"),
        "funding_rate": ("funding", "funding rate", "資金費率", "资金费率"),
        "open_interest": ("open interest", "oi", "未平倉", "未平仓"),
        "taker_buy_sell_volume": (
            "taker",
            "aggressive buy",
            "aggressive sell",
            "buy/sell volume",
            "主動買賣",
            "主动买卖",
        ),
        "long_short_ratio_or_basis": (
            "long short",
            "long-short",
            "basis",
            "基差",
            "多空比",
        ),
    }
    mentioned: List[DataFieldCapability] = []
    for item in matrix:
        aliases = field_aliases.get(item.field_name, (item.field_name,))
        if any(alias in text for alias in aliases):
            mentioned.append(item)
    if not mentioned:
        return True, [], "production"

    blockers: List[str] = []
    recommended_lane = "production"
    for item in mentioned:
        lane = str(item.recommended_lane or "production").strip().lower()
        if lane == "exploration":
            recommended_lane = "exploration"
        elif lane == "conditional" and recommended_lane != "exploration":
            recommended_lane = "conditional"
        if (
            horizon == "long"
            and str(item.availability_class or "").strip().lower() == "short_window"
        ):
            blockers.append(
                f"{item.field_name} is short-window data but the direction reads as medium/long-horizon."
            )
    return (not blockers), blockers[:4], recommended_lane


def _derive_rule_backed_direction_signals(
    option: Optional[DirectionOption],
    research_context: Optional[ResearchContext],
    audit_item: Optional["EvidenceAuditItem"],
) -> Dict[str, int]:
    if option is None:
        return {
            "feasibility_score": 0,
            "reversibility_score": 0,
            "speed_to_test_score": 0,
            "evidence_strength_score": 0,
            "downside_severity_score": 5,
            "unresolved_unknown_dependency_score": 5,
        }

    option_text = " ".join(
        [
            option.name,
            option.thesis,
            option.primary_metric,
            option.fastest_test,
            option.major_risk,
        ]
    ).strip()
    deterministic_score = _deterministic_direction_option_score(
        option, research_context
    )
    critical_unknowns = _extract_decision_critical_unknowns(research_context)
    critical_overlap = _direction_penalty_overlap_count(option_text, critical_unknowns)
    hallucination_claims = [
        item.split(":", 1)[1].strip()
        for item in list(getattr(research_context, "hallucination_flags", []) or [])
        if ":" in item
    ]
    hallucination_overlap = _direction_penalty_overlap_count(
        option_text, hallucination_claims
    )
    risk_overlap = _direction_penalty_overlap_count(
        option.major_risk, list(getattr(research_context, "key_risks", []) or [])
    )
    supported_count = len(list(getattr(audit_item, "supported_fields", []) or []))
    summary_only_count = len(list(getattr(audit_item, "summary_only_fields", []) or []))
    unsupported_count = int(getattr(audit_item, "unsupported_count", 0) or 0)
    audit_critical_count = len(
        list(getattr(audit_item, "decision_critical_unknowns", []) or [])
    )
    evidence_score = int(getattr(audit_item, "evidence_score", 0) or 0)

    test_lower = str(option.fastest_test or "").lower()
    speed_bonus = 0
    if any(
        marker in test_lower
        for marker in (
            "manual",
            "pilot",
            "prototype",
            "landing page",
            "interview",
            "shadow",
            "smoke",
        )
    ):
        speed_bonus += 2
    if any(
        marker in test_lower
        for marker in (
            "api",
            "integration",
            "compliance",
            "regulator",
            "data partnership",
            "migration",
            "rewrite",
        )
    ):
        speed_bonus -= 2

    thesis_lower = str(option.thesis or "").lower()
    reversibility = 3
    if any(
        marker in thesis_lower
        for marker in ("pilot", "assistant", "copilot", "workflow", "overlay")
    ):
        reversibility += 1
    if any(
        marker in thesis_lower
        for marker in (
            "platform",
            "exchange",
            "marketplace",
            "infrastructure",
            "core system",
            "rewrite",
        )
    ):
        reversibility -= 1

    feasibility = 5 - min(
        5,
        critical_overlap
        + hallucination_overlap
        + max(0, unsupported_count - supported_count),
    )
    feasibility = max(0, min(5, feasibility))

    return {
        "feasibility_score": max(0, min(5, feasibility)),
        "reversibility_score": max(0, min(5, reversibility)),
        "speed_to_test_score": max(
            0, min(5, 2 + _score_to_0_5(deterministic_score, step=12) + speed_bonus)
        ),
        "evidence_strength_score": max(
            0,
            min(
                5, _score_to_0_5(evidence_score, step=2) + min(2, supported_count // 2)
            ),
        ),
        "downside_severity_score": max(
            0,
            min(
                5,
                1
                + risk_overlap
                + hallucination_overlap
                + min(2, summary_only_count // 2),
            ),
        ),
        "unresolved_unknown_dependency_score": max(
            0,
            min(
                5,
                critical_overlap
                + audit_critical_count
                + min(2, unsupported_count // 2),
            ),
        ),
    }


def _calibrate_direction_comparator_report(
    comparator_report: Optional["DirectionComparatorReport"],
    *,
    decision: Optional[DirectionDecision],
    research_context: Optional[ResearchContext],
    audit_report: Optional["EvidenceAuditReport"],
) -> Optional["DirectionComparatorReport"]:
    if comparator_report is None:
        return None
    audit_by_key = {
        item.key: item
        for item in list(getattr(audit_report, "items", []) or [])
        if _canonical_report_direction_key(getattr(item, "key", "")) == item.key
    }
    calibrated_items: List[DirectionComparatorItem] = []
    for item in list(comparator_report.items or []):
        option = _find_direction_option(decision, item.key)
        rule_scores = _derive_rule_backed_direction_signals(
            option, research_context, audit_by_key.get(item.key)
        )
        hard_pass, hard_blockers, recommended_lane = _capability_matrix_gate_for_option(
            option, research_context
        )
        item.feasibility_score = max(
            0,
            min(
                5,
                min(
                    int(item.feasibility_score or 0),
                    rule_scores["feasibility_score"] + 1,
                ),
            ),
        )
        item.reversibility_score = max(
            0,
            min(
                5,
                min(
                    int(item.reversibility_score or 0),
                    rule_scores["reversibility_score"] + 1,
                ),
            ),
        )
        item.speed_to_test_score = max(
            0,
            min(
                5,
                min(
                    int(item.speed_to_test_score or 0),
                    rule_scores["speed_to_test_score"] + 1,
                ),
            ),
        )
        item.evidence_strength_score = max(
            int(item.evidence_strength_score or 0),
            rule_scores["evidence_strength_score"],
        )
        item.downside_severity_score = max(
            int(item.downside_severity_score or 0),
            rule_scores["downside_severity_score"],
        )
        item.unresolved_unknown_dependency_score = max(
            int(item.unresolved_unknown_dependency_score or 0),
            rule_scores["unresolved_unknown_dependency_score"],
        )
        item.hard_feasibility_pass = bool(
            getattr(item, "hard_feasibility_pass", True) and hard_pass
        )
        item.hard_blockers = _normalize_text_list(
            list(getattr(item, "hard_blockers", []) or []) + list(hard_blockers or [])
        )[:4]
        existing_lane = (
            str(getattr(item, "recommended_lane", "production") or "").strip().lower()
        )
        lane_priority = {"production": 0, "conditional": 1, "exploration": 2}
        if existing_lane not in lane_priority:
            existing_lane = "production"
        if lane_priority.get(recommended_lane, 0) > lane_priority.get(existing_lane, 0):
            item.recommended_lane = recommended_lane
        else:
            item.recommended_lane = existing_lane
        item.composite_score = max(
            0,
            (item.feasibility_score * 2)
            + (item.reversibility_score * 2)
            + (item.speed_to_test_score * 2)
            + (item.evidence_strength_score * 3)
            - (item.downside_severity_score * 2)
            - (item.unresolved_unknown_dependency_score * 2),
        )
        if not item.hard_feasibility_pass:
            item.composite_score = max(0, item.composite_score - 6)
        calibrated_items.append(item)
    comparator_report.items = _normalize_direction_comparator_items(calibrated_items)
    comparator_report = _normalize_direction_comparator_report_instance(
        comparator_report
    )
    return comparator_report


def _build_deterministic_direction_ranking(
    decision: Optional[DirectionDecision],
    research_context: Optional[ResearchContext],
) -> List[Tuple[str, int]]:
    if decision is None or research_context is None:
        return []
    scored: List[Tuple[str, int]] = []
    for option in list(decision.options or []):
        scored.append(
            (
                option.key,
                _deterministic_direction_option_score(option, research_context),
            )
        )
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _structured_direction_shortlist(
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
) -> List[str]:
    ordered: List[str] = []
    for key in list(getattr(comparator_report, "top_keys", []) or []):
        normalized = _canonical_report_direction_key(key)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    for key in list(getattr(audit_report, "top_keys", []) or []):
        normalized = _canonical_report_direction_key(key)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return ordered[:3]


def _structured_direction_option_score(
    key: str,
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
) -> int:
    normalized_key = _canonical_report_direction_key(key)
    if not normalized_key:
        return 0
    comparator_item = None
    if comparator_report is not None:
        comparator_item = next(
            (
                item
                for item in list(comparator_report.items or [])
                if item.key == normalized_key
            ),
            None,
        )
    audit_item = None
    if audit_report is not None:
        audit_item = next(
            (
                item
                for item in list(audit_report.items or [])
                if item.key == normalized_key
            ),
            None,
        )

    score = 0
    if comparator_item is not None:
        score += int(comparator_item.composite_score or 0) * 3
        score += int(comparator_item.evidence_strength_score or 0) * 2
        score -= int(comparator_item.downside_severity_score or 0)
        score -= int(comparator_item.unresolved_unknown_dependency_score or 0)
        if not bool(getattr(comparator_item, "hard_feasibility_pass", True)):
            score -= 20
    if audit_item is not None:
        score += int(audit_item.evidence_score or 0) * 4
        score -= int(audit_item.unsupported_count or 0) * 5
        score -= len(list(audit_item.decision_critical_unknowns or [])) * 4
        score -= len(list(audit_item.summary_only_fields or [])) * 2
    return max(0, score)


def _derive_backup_candidates(
    decision: Optional[DirectionDecision],
    *,
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
    deterministic_ranking: Optional[List[Tuple[str, int]]] = None,
) -> List[str]:
    if decision is None or decision.selected_direction == "none":
        return []
    ordered: List[str] = []
    ordered.extend(list(getattr(decision, "backup_candidates", []) or []))
    ordered.extend(_structured_direction_shortlist(comparator_report, audit_report))
    for key, _score in list(deterministic_ranking or [])[:4]:
        ordered.append(key)
    return _normalize_direction_key_list(
        ordered,
        exclude={decision.selected_direction},
        limit=2,
    )


def _selected_audit_item(
    decision: Optional[DirectionDecision],
    audit_report: Optional["EvidenceAuditReport"],
) -> Optional["EvidenceAuditItem"]:
    selected_key = _canonical_report_direction_key(
        getattr(decision, "selected_direction", "")
    )
    if not selected_key or audit_report is None:
        return None
    return next(
        (item for item in list(audit_report.items or []) if item.key == selected_key),
        None,
    )


def _align_direction_decision_summary_with_selection(
    decision: Optional[DirectionDecision],
) -> Optional[DirectionDecision]:
    if decision is None or decision.selected_direction == "none":
        return decision
    selected_key = _canonical_report_direction_key(decision.selected_direction)
    if not selected_key:
        return decision
    option_name = ""
    for option in list(getattr(decision, "options", []) or []):
        option_key = _canonical_report_direction_key(getattr(option, "key", ""))
        if option_key == selected_key:
            option_name = str(getattr(option, "name", "") or "").strip()
            break
    canonical_intro = (
        f"選擇 {selected_key}（{option_name}）作為首選方向。"
        if option_name
        else f"選擇 {selected_key} 作為首選方向。"
    )
    summary = str(getattr(decision, "summary", "") or "").strip()
    if not summary:
        decision.summary = canonical_intro
        return decision

    override_markers = (
        "deterministic evidence rerank",
        "structured comparator/auditor",
        "deterministic abstain gate",
        "override reason:",
    )
    summary_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？.!?])\s+", summary)
        if sentence and sentence.strip()
    ]
    override_sentences = [
        sentence
        for sentence in summary_sentences
        if any(marker in sentence.lower() for marker in override_markers)
    ]
    if override_sentences:
        rebuilt_parts = [canonical_intro, *override_sentences]
        backup_candidates = list(getattr(decision, "backup_candidates", []) or [])
        if backup_candidates:
            rebuilt_parts.append("備選方向：" + "；".join(backup_candidates) + "。")
        decision.summary = " ".join(part for part in rebuilt_parts if part).strip()
        return decision

    intro_patterns = (
        r"^\s*選擇\s*[A-G](?:（[^）]+）)?\s*作為首選方向。?\s*",
        r"^\s*Choose\s*[A-G](?:\s*\([^)]+\))?\s*as the primary direction\.?\s*",
        r"^\s*Selected\s+direction\s*:\s*[A-G](?:\s*\([^)]+\))?\.?\s*",
    )
    for pattern in intro_patterns:
        if re.match(pattern, summary, flags=re.IGNORECASE):
            decision.summary = re.sub(
                pattern,
                canonical_intro + " ",
                summary,
                count=1,
                flags=re.IGNORECASE,
            ).strip()
            return decision

    if not summary.startswith(canonical_intro):
        decision.summary = f"{canonical_intro} {summary}".strip()
    return decision


def _direction_summary_has_override_reason(summary: str) -> bool:
    lowered = str(summary or "").lower()
    markers = (
        "override",
        "overrode",
        "contradict",
        "contradicted",
        "outside the defended short-list",
        "outside the short-list",
        "short-list",
        "auditor",
        "comparator",
    )
    return any(marker in lowered for marker in markers)


def _apply_structured_direction_funnel(
    decision: Optional[DirectionDecision],
    *,
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
) -> Optional[DirectionDecision]:
    if decision is None:
        return None
    if decision.selected_direction == "none":
        return decision

    shortlist = _structured_direction_shortlist(comparator_report, audit_report)
    if not shortlist:
        return decision

    scored_shortlist = [
        (key, _structured_direction_option_score(key, comparator_report, audit_report))
        for key in shortlist
    ]
    scored_shortlist = [(key, score) for key, score in scored_shortlist if score > 0]
    if not scored_shortlist:
        return decision

    scored_shortlist.sort(key=lambda item: (-item[1], item[0]))
    best_key, best_score = scored_shortlist[0]
    selected_key = _canonical_report_direction_key(decision.selected_direction)
    selected_score = _structured_direction_option_score(
        selected_key, comparator_report, audit_report
    )

    if selected_key == best_key:
        return decision

    if selected_key not in shortlist and best_score >= max(
        selected_score + 15, (selected_score * 2) + 1
    ):
        decision.selected_direction = best_key
        decision.summary = (
            f"{decision.summary} Structured comparator/auditor funnel elevated {best_key} "
            f"because the original choice sat outside the defended short-list."
        ).strip()
        decision.confidence = "low"
        return decision

    if selected_key in shortlist and best_score >= selected_score + 18:
        decision.selected_direction = best_key
        decision.summary = (
            f"{decision.summary} Structured comparator/auditor adjudication elevated {best_key} "
            f"over {selected_key} due to materially stronger scored support."
        ).strip()
        decision.confidence = "low"
        return decision

    if selected_key not in shortlist:
        decision.confidence = "low"
    return decision


def _should_force_direction_none(
    decision: Optional[DirectionDecision],
    *,
    research_context: Optional[ResearchContext],
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
    deterministic_ranking: Optional[List[Tuple[str, int]]] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    判斷是否應該強制選擇 'none'，並回傳缺口資訊供研究精煉使用。

    Returns:
        Tuple[bool, str, Dict[str, Any]]: (是否強制none, 原因, 缺口資訊字典)
    """
    gap_info: Dict[str, Any] = {
        "missing_evidence_areas": [],
        "critical_unknowns": [],
        "weak_directions": [],
        "grounded_claims_needed": 0,
        "citations_needed": 0,
        "research_queries": [],
    }
    if decision is None or research_context is None:
        return False, "", gap_info
    coverage = dict(research_context.evidence_coverage or {})
    grounded_claims = int(coverage.get("grounded_claims") or 0)
    grounded_summary_claims = int(coverage.get("grounded_summary_claims") or 0)
    citation_count = len(list(research_context.citations or []))
    claim_attribution_count = len(list(research_context.claim_attributions or []))
    # "Near-zero grounded evidence" used to trigger whenever
    # ``grounded_claims <= 0 OR citation_count <= 0`` — a single OR meant any
    # synthesizer hiccup that left structured claim arrays empty (most
    # commonly when the grounding tokenizer mis-handled CJK text and rejected
    # every Chinese claim) would force the judge to "none" even though the
    # librarian had returned a healthy citation set.
    #
    # The new condition treats the citation pool itself as one form of
    # grounding signal: if the librarian collected at least three citations,
    # has any claim_attributions OR any grounded_summary_claims, that is
    # enough material for the judge to make a low-confidence call.  We only
    # short-circuit to "none" when the evidence pool is genuinely empty.
    has_grounded_claims = grounded_claims > 0
    has_summary_claims = grounded_summary_claims > 0
    has_attributions = claim_attribution_count > 0
    has_citations = citation_count > 0
    near_zero_citations = citation_count < 3
    near_zero_evidence = (
        not has_grounded_claims
        and not has_summary_claims
        and not has_attributions
        and (near_zero_citations or not has_citations)
    )
    if near_zero_evidence:
        gap_info["grounded_claims_needed"] = max(3, 5 - grounded_claims)
        gap_info["citations_needed"] = max(3, 5 - citation_count)
        gap_info["missing_evidence_areas"] = list(research_context.unknowns or [])[:5]
        gap_info["research_queries"] = list(
            research_context.suggested_search_queries or []
        )[:3]
        return True, "near-zero grounded evidence", gap_info

    shortlist = _structured_direction_shortlist(comparator_report, audit_report)
    if not shortlist:
        return False, "", gap_info
    scores = [
        _structured_direction_option_score(key, comparator_report, audit_report)
        for key in shortlist
    ]
    scores = [score for score in scores if score > 0]
    ranking = list(deterministic_ranking or [])
    top_deterministic_score = ranking[0][1] if ranking else 0
    if top_deterministic_score >= 12 and grounded_claims > 0 and citation_count > 0:
        return False, "", gap_info
    if not scores:
        gap_info["weak_directions"] = shortlist[:3]
        gap_info["missing_evidence_areas"] = list(research_context.unknowns or [])[:5]
        gap_info["research_queries"] = [
            f"evidence for direction {key}" for key in shortlist[:3]
        ]
        return (
            True,
            "short-listed directions have no defendable structured support",
            gap_info,
        )
    scores.sort(reverse=True)
    shortlist_audit_items = [
        item
        for item in list(getattr(audit_report, "items", []) or [])
        if item.key in shortlist
    ]
    high_critical_unknowns = (
        all(
            len(list(item.decision_critical_unknowns or [])) >= 2
            for item in shortlist_audit_items
        )
        if shortlist_audit_items
        else False
    )
    # 收集關鍵未知數
    for item in shortlist_audit_items:
        for unknown in list(item.decision_critical_unknowns or []):
            if unknown not in gap_info["critical_unknowns"]:
                gap_info["critical_unknowns"].append(unknown)
    # v16.9.72 defence-in-depth: the legacy condition fired whenever
    # ``max(scores) <= 12 AND grounded_claims < 3`` — but ``grounded_claims``
    # comes from the ASCII-grounding tokenizer counter in
    # ``_stabilize_research_context``, which under-counts when the
    # synthesizer produced ``claim_attributions`` directly (the structured
    # attribution path bypasses the tokenizer counter).  Treat any of
    # ``grounded_claims``, ``grounded_summary_claims``, or
    # ``claim_attribution_count`` >= 3 as enough structured evidence to
    # respect the comparator funnel and let the judge call low-confidence
    # instead of force-killing the whole debate.
    weakly_supported = (
        max(scores) <= 12
        and grounded_claims < 3
        and grounded_summary_claims < 3
        and claim_attribution_count < 3
    )
    if weakly_supported:
        gap_info["grounded_claims_needed"] = 3 - grounded_claims
        gap_info["weak_directions"] = shortlist[:3]
        gap_info["missing_evidence_areas"] = list(research_context.unknowns or [])[:5]
        gap_info["research_queries"] = list(
            research_context.suggested_search_queries or []
        )[:3]
        return True, "all short-listed directions are weakly supported", gap_info
    if len(scores) >= 2 and abs(scores[0] - scores[1]) <= 3 and high_critical_unknowns:
        gap_info["critical_unknowns"] = gap_info["critical_unknowns"][:5]
        gap_info["research_queries"] = [
            f"resolve: {unknown}" for unknown in gap_info["critical_unknowns"][:3]
        ]
        return (
            True,
            "top short-listed directions remain non-comparable because critical unknowns are too high",
            gap_info,
        )
    return False, "", gap_info


def _apply_direction_none_gate(
    decision: Optional[DirectionDecision],
    *,
    research_context: Optional[ResearchContext],
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
    deterministic_ranking: Optional[List[Tuple[str, int]]] = None,
) -> Optional[DirectionDecision]:
    if decision is None or decision.selected_direction == "none":
        return decision
    force_none, reason, _ = _should_force_direction_none(
        decision,
        research_context=research_context,
        comparator_report=comparator_report,
        audit_report=audit_report,
        deterministic_ranking=deterministic_ranking,
    )
    if not force_none:
        return decision
    decision.selected_direction = "none"
    decision.backup_candidates = []
    decision.confidence = "low"
    decision.summary = f"{decision.summary} Deterministic abstain gate selected none because {reason}.".strip()
    return decision


def _apply_deterministic_direction_rerank(
    decision: Optional[DirectionDecision],
    *,
    research_context: Optional[ResearchContext],
    comparator_report: Optional["DirectionComparatorReport"] = None,
    audit_report: Optional["EvidenceAuditReport"] = None,
) -> Optional[DirectionDecision]:
    if decision is None or research_context is None:
        return decision
    comparator_report = _calibrate_direction_comparator_report(
        comparator_report,
        decision=decision,
        research_context=research_context,
        audit_report=audit_report,
    )
    decision = _apply_structured_direction_funnel(
        decision,
        comparator_report=comparator_report,
        audit_report=audit_report,
    )
    if decision is None:
        return None
    ranking = _build_deterministic_direction_ranking(decision, research_context)
    if not ranking:
        decision = _apply_direction_none_gate(
            decision,
            research_context=research_context,
            comparator_report=comparator_report,
            audit_report=audit_report,
            deterministic_ranking=[],
        )
        if decision is not None:
            decision.backup_candidates = _derive_backup_candidates(
                decision,
                comparator_report=comparator_report,
                audit_report=audit_report,
                deterministic_ranking=[],
            )
            decision = _align_direction_decision_summary_with_selection(decision)
        return decision

    ranked_map = {key: score for key, score in ranking}
    selected_key = str(decision.selected_direction or "").strip().upper()
    selected_score = ranked_map.get(selected_key, 0)
    top_key, top_score = ranking[0]
    top_keys = [key for key, _ in ranking[:3]]

    if top_score <= 0:
        decision.confidence = "low"
    elif selected_key != top_key and selected_score <= 0 and top_score >= 12:
        decision.selected_direction = top_key
        decision.summary = f"{decision.summary} Deterministic evidence rerank favored {top_key} because the original choice had no grounded support.".strip()
        decision.confidence = "low"
    elif (
        selected_key != top_key
        and selected_key not in top_keys
        and top_score >= max(selected_score + 12, (selected_score * 2) + 1)
    ):
        decision.selected_direction = top_key
        decision.summary = f"{decision.summary} Deterministic evidence rerank elevated {top_key} over {selected_key} due to stronger grounded support.".strip()
        decision.confidence = "low"
    elif selected_key not in top_keys:
        decision.confidence = "low"
    decision = _apply_direction_none_gate(
        decision,
        research_context=research_context,
        comparator_report=comparator_report,
        audit_report=audit_report,
        deterministic_ranking=ranking,
    )
    if decision is not None:
        decision.backup_candidates = _derive_backup_candidates(
            decision,
            comparator_report=comparator_report,
            audit_report=audit_report,
            deterministic_ranking=ranking,
        )
        decision = _align_direction_decision_summary_with_selection(decision)
    return decision


def _direction_confidence_cap_from_research_context(
    research_context: Optional[ResearchContext],
) -> str:
    if research_context is None:
        return "low"
    coverage = dict(research_context.evidence_coverage or {})
    grounded_claims = int(coverage.get("grounded_claims") or 0)
    citation_count = len(list(research_context.citations or []))
    hallucination_count = len(list(research_context.hallucination_flags or []))
    critical_unknowns = len(_extract_decision_critical_unknowns(research_context))
    if grounded_claims <= 0 or citation_count <= 0:
        return "low"
    if critical_unknowns >= 2:
        return "low"
    if grounded_claims < 3 or citation_count < 3:
        return "low"
    if hallucination_count > grounded_claims:
        return "low"
    if grounded_claims < 7 or citation_count < 5 or critical_unknowns >= 1:
        return "medium"
    return "high"


def _direction_confidence_cap_from_stage_reports(
    decision: Optional[DirectionDecision],
    *,
    comparator_report: Optional["DirectionComparatorReport"],
    audit_report: Optional["EvidenceAuditReport"],
) -> Optional[str]:
    if decision is None or decision.selected_direction == "none":
        return None
    selected_key = _canonical_report_direction_key(decision.selected_direction)
    if not selected_key:
        return "low"
    shortlist = _structured_direction_shortlist(comparator_report, audit_report)
    audit_item = _selected_audit_item(decision, audit_report)
    if shortlist and selected_key not in shortlist:
        return "low"
    if audit_item is not None:
        if int(audit_item.unsupported_count or 0) >= 3:
            return "low"
        if len(list(audit_item.decision_critical_unknowns or [])) >= 2:
            return "low"
        if (
            int(audit_item.unsupported_count or 0) >= 1
            or len(list(audit_item.summary_only_fields or [])) >= 2
        ):
            return "medium"
    return None


def _cap_direction_decision_confidence(
    decision: Optional[DirectionDecision],
    *,
    research_context: Optional[ResearchContext],
    comparator_report: Optional["DirectionComparatorReport"] = None,
    audit_report: Optional["EvidenceAuditReport"] = None,
) -> Optional[DirectionDecision]:
    if decision is None:
        return None
    base_cap = _direction_confidence_cap_from_research_context(research_context)
    stage_cap = _direction_confidence_cap_from_stage_reports(
        decision,
        comparator_report=comparator_report,
        audit_report=audit_report,
    )
    capped_rank = min(
        _direction_confidence_rank(base_cap),
        _direction_confidence_rank(stage_cap or "high"),
    )
    actual_rank = _direction_confidence_rank(decision.confidence)
    if actual_rank > capped_rank:
        decision.confidence = _direction_confidence_from_rank(capped_rank)
    if decision.selected_direction != "none":
        shortlist = _structured_direction_shortlist(comparator_report, audit_report)
        if (
            shortlist
            and decision.selected_direction not in shortlist
            and not _direction_summary_has_override_reason(decision.summary)
        ):
            decision.summary = (
                f"{decision.summary} Override reason: selected {decision.selected_direction} "
                f"outside the comparator/auditor short-list because downstream deterministic evidence support was stronger."
            ).strip()
    return decision


def _build_direction_debate_cache_payload(
    *,
    user_problem: str,
    mode: str,
    language_hint: str,
    llm_model_id: str,
    direction_judge_model_id: str,
    research_model_id: str,
    strict_json: bool,
    research_context: Optional[ResearchContext],
) -> Dict[str, Any]:
    payload = {
        "model": llm_model_id,
        "direction_judge_model": direction_judge_model_id,
        "research_model": research_model_id,
        "llm_provider": _resolve_llm_provider(),
        "debate_architecture": "v4_hard_gate_soft_ranking_backup_candidates",
        "confidence_calibration": "v5_named_stage_rule_calibrated_with_backups",
        "strict_json": bool(strict_json),
        "mode": mode,
        "language_hint": language_hint,
        "user_problem_len": len(user_problem or ""),
        "user_problem_sha256": _text_sha256(user_problem or ""),
        "research_strategy": "",
        "providers_used": [],
        "research_context_sha256": "",
    }
    if research_context is not None:
        payload["research_strategy"] = research_context.search_strategy
        payload["providers_used"] = list(research_context.providers_used or [])
        payload["research_context_sha256"] = _text_sha256(
            _model_to_stable_json(research_context)
        )
    return payload


def _legacy_build_research_swarm_specs(
    *,
    mode_config: "ModeConfig",
    language_hint: str,
) -> Tuple[Dict[str, AgentSpec], List[TaskSpec], Dict[str, str]]:
    provider_list = ", ".join(LIBRARIAN_SEARCH_PROVIDERS)
    common_contract = (
        "只能使用提供的 search evidence，不得捏造 citations 或未陳述事實。\n"
        "unknowns 必須保持未解狀態，不得升格成事實。\n"
        "若 claim 沒有證據支撐，就排除或移入 unknowns。\n" + NO_CROSS_ROLE_RULE
    )
    agent_specs: Dict[str, AgentSpec] = {
        "market_research": AgentSpec(
            name="market_research",
            role="Market Research",
            goal="從搜尋證據中抽取市場先例、ICP 痛點與採用訊號。",
            backstory=(
                f"[Market Research] 聚焦 {mode_config.name} 模式下的市場需求。\n"
                f"Search providers：{provider_list}\n" + common_contract
            ),
            output_schema_name="ResearchLaneReport",
            cost_weight=2,
        ),
        "technical_research": AgentSpec(
            name="technical_research",
            role="Technical Research",
            goal="抽取架構模式、實作限制與可靠性風險。",
            backstory=(
                f"[Technical Research] 聚焦：{mode_config.research_focus}。\n"
                f"Search providers：{provider_list}\n" + common_contract
            ),
            output_schema_name="ResearchLaneReport",
            cost_weight=2,
        ),
        "competitor_research": AgentSpec(
            name="competitor_research",
            role="Competitor Research",
            goal="整理競品、替代方案、既有工作流與市場定位缺口。",
            backstory=(
                f"[Competitor Research] 聚焦：{mode_config.biz_focus}。\n"
                f"Search providers：{provider_list}\n" + common_contract
            ),
            output_schema_name="ResearchLaneReport",
            cost_weight=2,
        ),
        "research_synthesizer": AgentSpec(
            name="research_synthesizer",
            role="Research Synthesizer",
            goal="把各 lane 輸出壓縮成單一 ResearchContext JSON，提供下游 debate 使用。",
            backstory=(
                "[Research Synthesizer] 只能合併有搜尋證據支撐的發現。"
                "不得新增超出提供證據的新 claim。\n" + common_contract
            ),
            output_schema_name="ResearchContext",
            parallel_safe=False,
            cost_weight=3,
            depends_on=["market_research", "technical_research", "competitor_research"],
        ),
    }
    task_specs = [
        TaskSpec(
            name="market_research",
            description_template=(
                "你是 [Market Research]。\n"
                "問題：\n{user_problem}\n\n"
                "語言：{language_hint}\n"
                "模式：{mode_name}\n"
                "Search providers：{search_provider_list}\n"
                "Evidence pack：\n{market_research_material}\n\n"
                "規則：\n"
                "- 只能使用 evidence pack 直接支撐的 claims。\n"
                "- 若證據不足，請把項目放進 unknowns，不可當成事實。\n"
                "- 必須附上實際使用到的 citations。\n"
                "只輸出 ResearchLaneReport JSON，且 lane/findings/market_examples/existing_tools/technical_patterns/key_risks/unknowns/citations 都必須存在。lane 固定為 'market'。"
            ),
            agent_name="market_research",
            expected_output="ResearchLaneReport JSON only.",
            output_pydantic_model="ResearchLaneReport",
        ),
        TaskSpec(
            name="technical_research",
            description_template=(
                "你是 [Technical Research]。\n"
                "問題：\n{user_problem}\n\n"
                "語言：{language_hint}\n"
                "模式：{mode_name}\n"
                "Search providers：{search_provider_list}\n"
                "Evidence pack：\n{technical_research_material}\n\n"
                "規則：\n"
                "- 只能使用 evidence pack 直接支撐的 claims。\n"
                "- 不得捏造 evidence 中不存在的 frameworks、patterns 或 risks。\n"
                "- 必須附上實際使用到的 citations。\n"
                "只輸出 ResearchLaneReport JSON，且 lane/findings/market_examples/existing_tools/technical_patterns/key_risks/unknowns/citations 都必須存在。lane 固定為 'technical'。"
            ),
            agent_name="technical_research",
            expected_output="ResearchLaneReport JSON only.",
            output_pydantic_model="ResearchLaneReport",
        ),
        TaskSpec(
            name="competitor_research",
            description_template=(
                "你是 [Competitor Research]。\n"
                "問題：\n{user_problem}\n\n"
                "語言：{language_hint}\n"
                "模式：{mode_name}\n"
                "Search providers：{search_provider_list}\n"
                "Evidence pack：\n{competitor_research_material}\n\n"
                "規則：\n"
                "- 只能使用 evidence pack 直接支撐的 claims。\n"
                "- 若某競品無法 grounding，就直接排除。\n"
                "- 必須附上實際使用到的 citations。\n"
                "只輸出 ResearchLaneReport JSON，且 lane/findings/market_examples/existing_tools/technical_patterns/key_risks/unknowns/citations 都必須存在。lane 固定為 'competitor'。"
            ),
            agent_name="competitor_research",
            expected_output="ResearchLaneReport JSON only.",
            output_pydantic_model="ResearchLaneReport",
        ),
        TaskSpec(
            name="research_synthesizer",
            description_template=(
                "請把各 lane 輸出整合成單一 ResearchContext JSON。\n"
                "問題：\n{user_problem}\n\n"
                "語言：{language_hint}\n"
                "模式：{mode_name}\n"
                "Search strategy：{search_strategy}\n"
                "Search providers：{search_provider_list}\n"
                "Suggested queries：{suggested_search_queries_json}\n"
                "Provider errors：{provider_errors_json}\n"
                "規則：\n"
                "- unsupported claims 不得留在 market_examples/existing_tools/technical_patterns/key_risks。\n"
                "- 不確定內容要移到 unknowns。\n"
                "- 不得把 unknowns 或 hallucination_flags 升格為事實。\n"
                "- 必須填好 claim_attributions，讓每個 grounded pattern/risk/tool/example 都能指回具體 citations。\n"
                "- citations/provider_errors/evidence_coverage/hallucination_flags/claim_attributions 這些欄位都必須存在。\n"
                "只輸出 JSON，且必須包含完整 ResearchContext 全部欄位。"
            ),
            agent_name="research_synthesizer",
            expected_output="ResearchContext JSON only.",
            context_task_names=[
                "market_research",
                "technical_research",
                "competitor_research",
            ],
            output_pydantic_model="ResearchContext",
        ),
    ]
    template_vars = {
        "mode_name": mode_config.name,
        "language_hint": language_hint,
        "search_provider_list": provider_list,
    }
    return agent_specs, task_specs, template_vars


def _research_task_callback(task_output: Any) -> None:
    """Emit a structured ``research_lane_done`` event after every task in
    the research swarm crew completes.

    Defined at module scope (not as a closure inside
    :func:`build_research_swarm_crew`) so pydantic can serialise the Crew
    object during checkpointing — the legacy closure form emitted
    ``UserWarning: function callbacks cannot be serialized and will
    prevent checkpointing`` on every research kickoff (regression
    introduced in v16.9.70, fixed in v16.9.72).
    """
    try:
        task_name = ""
        try:
            task_name = str(getattr(task_output, "name", "") or "").strip()
        except Exception:
            pass
        if not task_name:
            # CrewAI Task objects expose `description`; first non-empty line
            # is usually the role marker (e.g., "你是 [Market Research]。").
            try:
                desc = str(getattr(task_output, "description", "") or "").strip()
                first_line = desc.split("\n", 1)[0] if desc else ""
                task_name = first_line[:80]
            except Exception:
                pass
        if not task_name:
            return
        log_event(
            LOGGER,
            20,
            "research_lane_done",
            f"Research task '{task_name}' completed.",
            lane=task_name,
        )
    except Exception:
        # Never let the callback break the crew run.
        pass


def build_research_swarm_crew(
    user_problem: str,
    *,
    mode: str,
    language_hint: str,
    llm: Any,
    research_materials: Dict[str, Any],
) -> Crew:
    mode_config = _get_mode_config(mode)
    agent_specs, task_specs, template_vars = _build_research_swarm_specs(
        mode_config=mode_config,
        language_hint=language_hint,
    )
    agents = {
        name: _create_agent_from_spec(spec, llm) for name, spec in agent_specs.items()
    }
    lane_materials = research_materials.get("lane_materials") or {}
    problem_breakdown = research_materials.get("problem_breakdown") or {}
    lane_focus = problem_breakdown.get("lane_focus") or {}
    render_vars = {
        "user_problem": user_problem,
        "mode_name": mode_config.name,
        "language_hint": language_hint,
        "search_provider_list": template_vars["search_provider_list"],
        "search_strategy": research_materials.get("search_strategy") or "",
        "suggested_search_queries_json": json.dumps(
            research_materials.get("suggested_search_queries") or [],
            ensure_ascii=False,
        ),
        "provider_errors_json": json.dumps(
            research_materials.get("provider_errors") or {},
            ensure_ascii=False,
        ),
        "problem_breakdown_json": json.dumps(
            problem_breakdown, ensure_ascii=False, indent=2
        ),
        "market_research_brief": json.dumps(
            {
                "lane": "market",
                "core_objective": problem_breakdown.get("core_objective") or "",
                "focus": lane_focus.get("market") or [],
                "entities": problem_breakdown.get("entities") or [],
                "constraints": problem_breakdown.get("constraints") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "technical_research_brief": json.dumps(
            {
                "lane": "technical",
                "core_objective": problem_breakdown.get("core_objective") or "",
                "focus": lane_focus.get("technical") or [],
                "entities": problem_breakdown.get("entities") or [],
                "constraints": problem_breakdown.get("constraints") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "competitor_research_brief": json.dumps(
            {
                "lane": "competitor",
                "core_objective": problem_breakdown.get("core_objective") or "",
                "focus": lane_focus.get("competitor") or [],
                "entities": problem_breakdown.get("entities") or [],
                "constraints": problem_breakdown.get("constraints") or [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "market_research_material": lane_materials.get("market", "No market evidence."),
        "technical_research_material": lane_materials.get(
            "technical", "No technical evidence."
        ),
        "competitor_research_material": lane_materials.get(
            "competitor", "No competitor evidence."
        ),
    }
    task_lookup: Dict[str, Task] = {}
    ordered_tasks: List[Task] = []
    for spec in task_specs:
        task = _build_task_from_spec(
            spec,
            agents=agents,
            task_lookup=task_lookup,
            template_vars=render_vars,
        )
        task_lookup[spec.name] = task
        ordered_tasks.append(task)

    # WebUI agent-flow visibility: the research swarm runs with verbose=False
    # (so CrewAI never prints `# Agent: <role>` headers), which means the
    # WebUI's _detectAgentActivity matcher cannot light up the lanes or the
    # research_synthesizer node as each one actually runs.  Without
    # `_research_task_callback` the synthesizer only flashes for ~650ms
    # when librarian_kickoff_done fires (research_phase_done fallback),
    # which is too short for users to see.  The callback is defined at
    # module scope (NOT a closure) so that pydantic's checkpoint
    # serialiser can pickle the Crew — closures emit
    # `function callbacks cannot be serialized and will prevent
    # checkpointing` (UserWarning, see v16.9.72 fix).
    crew = Crew(
        agents=[
            agents["market_research"],
            agents["technical_research"],
            agents["competitor_research"],
            agents["research_synthesizer"],
        ],
        tasks=ordered_tasks,
        process=Process.sequential,
        verbose=False,
        task_callback=_research_task_callback,
    )
    prompt_hashes: Dict[str, str] = {}
    total_prompt_chars = 0
    for spec in task_specs:
        rendered = _render_prompt_template(spec.description_template, render_vars)
        prompt_hashes[spec.name] = _text_sha256(rendered)
        total_prompt_chars += len(rendered)
    setattr(crew, "_prompt_hashes", prompt_hashes)
    setattr(crew, "_prompt_total_chars", total_prompt_chars)
    setattr(crew, "_dag_snapshot", _build_agent_dag_snapshot(agent_specs, task_specs))
    setattr(
        crew,
        "_retry_policy",
        _aggregate_retry_policy(agent_specs, retry_policy_cls=RetryPolicy),
    )
    setattr(crew, "_crew_name", "research_swarm")
    return crew


def _create_agent_from_spec(spec: AgentSpec, llm: Any) -> Agent:
    return _create_agent_from_module(spec, llm, agent_cls=Agent)


def _build_task_from_spec(
    task_spec: TaskSpec,
    *,
    agents: Dict[str, Agent],
    task_lookup: Dict[str, Task],
    template_vars: Dict[str, str],
) -> Task:
    return _build_task_from_module(
        task_spec,
        agents=agents,
        task_lookup=task_lookup,
        template_vars=template_vars,
        render_prompt_template=_render_prompt_template,
        strict_json_enabled=STRICT_JSON_ENABLED,
        crewai_output_pydantic=CREWAI_OUTPUT_PYDANTIC,
        output_model_by_name=_output_model_by_name,
        task_cls=Task,
    )


def _build_research_context_reformat_description(
    *, raw_text: str, language_hint: str
) -> str:
    return (
        "Reformat the INPUT into a valid ResearchContext JSON object.\n"
        "Required fields:\n"
        "- user_problem: string\n"
        "- search_strategy: string\n"
        "- providers_used: list[string]\n"
        "- suggested_search_queries: list[string]\n"
        "- market_examples: list[string]\n"
        "- existing_tools: list[string]\n"
        "- technical_patterns: list[string]\n"
        "- key_risks: list[string]\n"
        "- unknowns: list[string]\n"
        "- synthesized_summary: string\n"
        "- citations: list of {provider,title,url,snippet,query,source_domain,snippet_hash,verification_status}\n"
        "- provider_errors: dict\n"
        "- evidence_coverage: dict\n"
        "- hallucination_flags: list[string]\n"
        "- claim_attributions: list of {category,claim,citation_indices,citation_urls,support_score}\n\n"
        "Rules:\n"
        "- Do not invent tools, risks, patterns, citations, URLs, providers, or verification metadata.\n"
        "- If support is unclear, move the claim to unknowns or hallucination_flags instead of upgrading it into a fact.\n"
        "- claim_attributions must be backed by citations, not by summary-only wording.\n"
        '- Preserve grounded claims. Use [], {}, or "" for missing structured values.\n'
        "- Output JSON only. No markdown. No code fence. No extra text.\n\n"
        f"Language hint: {language_hint}\n\n"
        "INPUT:\n" + limit_text(raw_text, 12000)
    )


def _build_direction_debate_prompt_bundle(
    *,
    user_problem: str,
    language_hint: str,
    research_block: str,
) -> Dict[str, str]:
    validation_guidance = _validation_first_prompt_guidance(user_problem)
    return {
        "explorer": f"""
You are [Explorer]. Generate exactly 7 product directions, keyed A through G.
Rules:
- Output JSON only. No markdown. No extra text.
- claim_attributions and citations are the PRIMARY evidence.
- The synthesized summary is SECONDARY compressed narrative only and cannot override claim_attributions or citations.
- Use grounded research context only.
- Treat the field capability matrix as binding venue/data context when it exists.
- If the research includes partial but credible evidence, produce provisional options instead of stalling.
- unknowns can constrain options but cannot be promoted into evidence.
- Do not revive unsupported claims removed by the librarian.
- Every option must include key, name, thesis, primary_metric, fastest_test, and major_risk.
- Keys must be exactly A, B, C, D, E, F, G.
- Keep options distinct and strategically meaningful.

CRITICAL: When evidence is thin or incomplete for a direction:
- Add "evidence_gaps" field listing what specific evidence would strengthen this direction.
- Add "assumptions" field listing explicit assumptions made due to missing evidence.
- Add "verification_steps" field listing what must be verified before committing.
- Provide "what_if_scenarios" with alternative outcomes if key assumptions are wrong.
- Do NOT skip or weaken a direction just because evidence is incomplete - instead, make the gaps explicit.
- Even with partial evidence, produce actionable directions with clear confidence indicators.
{validation_guidance}

Language hint: {language_hint}

User problem:
{user_problem}

Research context:
{research_block}

Return JSON:
{{
  "options": [
    {{
      "key": "A",
      "name": "...",
      "thesis": "...",
      "primary_metric": "...",
      "fastest_test": "...",
      "major_risk": "...",
      "evidence_gaps": ["what evidence is missing"],
      "assumptions": ["explicit assumptions due to missing evidence"],
      "verification_steps": ["what must be verified first"],
      "what_if_scenarios": ["alternative outcomes if assumptions are wrong"]
    }}
  ]
}}
""",
        "comparator": f"""
You are [Comparator]. Compare all seven directions A through G and funnel them down to the best three candidates.
Rules:
- Output JSON only. No markdown. No extra text.
- claim_attributions and citations are the PRIMARY evidence.
- The synthesized summary is SECONDARY compressed narrative only and cannot override claim_attributions or citations.
- Use grounded research context only.
- Apply a hard feasibility gate first, then a soft ranking pass.
- The hard feasibility gate must check whether the required data fields, history depth, and execution assumptions are actually compatible with the field capability matrix.
- The soft ranking pass should compare only after hard blockers are identified, and should reward feasibility, reversibility, speed-to-test, and evidence quality.
- Score every direction A-G across:
  feasibility_score,
  reversibility_score,
  speed_to_test_score,
  evidence_strength_score,
  downside_severity_score,
  unresolved_unknown_dependency_score.
- Use 0 to 5 integers for every score.
- hard_feasibility_pass must be true only when the direction can be executed with the available data history and explicit execution assumptions.
- hard_blockers must list concrete blockers when hard_feasibility_pass=false.
- recommended_lane must be one of "production", "exploration", or "conditional".
- downside_severity_score and unresolved_unknown_dependency_score are penalty-style dimensions: lower is better.
- composite_score should reward feasibility, reversibility, speed_to_test, and evidence_strength, while penalizing downside severity and unresolved unknown dependency.
- top_keys must contain exactly 3 direction keys, ordered best to worst.
- Do not simply mirror popularity or summary tone. Use structured comparison.
- If evidence is thin, still produce a best-effort top 3 and note the weakness in comparison_notes.
{validation_guidance}

Language hint: {language_hint}

User problem:
{user_problem}

Research context:
{research_block}

Return JSON:
{{
  "items": [
    {{
      "key": "A",
      "feasibility_score": 3,
      "reversibility_score": 4,
      "speed_to_test_score": 5,
      "evidence_strength_score": 2,
      "downside_severity_score": 2,
      "unresolved_unknown_dependency_score": 3,
      "composite_score": 9,
      "hard_feasibility_pass": true,
      "hard_blockers": [],
      "recommended_lane": "production",
      "rationale": "Fastest to validate with moderate evidence."
    }}
  ],
  "top_keys": ["A", "C", "F"],
  "comparison_notes": ["Evidence is thin for B and E, so they were excluded from the top 3."]
}}
""",
        "skeptic": f"""
You are [Skeptic]. Deeply stress-test only the comparator short-list.
Rules:
- Output JSON only. No markdown. No extra text.
- claim_attributions and citations are the PRIMARY evidence.
- The synthesized summary is SECONDARY compressed narrative only and cannot override claim_attributions or citations.
- Use grounded research context only.
- Focus only on the three directions in comparator top_keys.
- unknowns can increase caution but cannot be promoted into evidence.
- Do not revive unsupported claims removed by the librarian.
- If comparator top_keys are weak, say so explicitly instead of inventing better evidence.

Language hint: {language_hint}

User problem:
{user_problem}

Research context:
{research_block}

Return JSON:
{{
  "reviewed_keys": ["A", "C", "F"],
  "risks": [
    {{
      "key": "A",
      "irreversible_risk": "...",
      "veto_reason": "...",
      "hidden_dependency": "..."
    }}
  ],
  "global_warnings": ["..."]
}}
""",
        "auditor": f"""
You are [Evidence Auditor]. Build a per-option evidence scorecard for the debate short-list and verify whether the funnel is defensible.
Rules:
- Output JSON only. No markdown. No extra text.
- claim_attributions and citations are the PRIMARY evidence.
- The synthesized summary is SECONDARY compressed narrative only and cannot override claim_attributions or citations.
- Use grounded research context only.
- Score each option on evidence quality, not on your product preference.
- Treat comparator top_keys as the funnel input and verify whether the short-list is actually defensible.
- Mark fields as supported_fields only if they are backed by explicit claim_attributions/citations.
- Mark fields as summary_only_fields if they seem plausible only from compressed narrative.
- Mark fields as unsupported_fields if there is no clear evidence.
- decision_critical_unknowns must include only unknowns that would materially change comparison between options.
- top_keys should contain the best-supported one to three directions by evidence_score.
- If the field capability matrix shows a data-history mismatch, mention it in global_warnings instead of silently treating it as supported.

Language hint: {language_hint}

User problem:
{user_problem}

Research context:
{research_block}

Return JSON:
{{
  "items": [
    {{
      "key": "A",
      "evidence_score": 0,
      "supported_fields": ["thesis"],
      "summary_only_fields": [],
      "unsupported_fields": ["primary_metric"],
      "unsupported_count": 1,
      "decision_critical_unknowns": []
    }}
  ],
  "top_keys": ["A", "B", "C"],
  "global_warnings": ["..."]
}}
""",
        "judge": f"""  # nosec B608
You are [Judge]. Merge Explorer, Comparator, Skeptic, and Evidence Auditor outputs into one DirectionDecision.
Rules:
- Output valid DirectionDecision JSON only. No markdown. No extra text.
- claim_attributions and citations are the PRIMARY evidence.
- The synthesized summary is SECONDARY compressed narrative only and cannot override claim_attributions or citations.
- Use grounded research context only. Unsupported claims and unknowns must never be upgraded into facts.
- selected_direction must be one of "A", "B", "C", "D", "E", "F", "G", or "none".
- options must contain exactly 7 items, keyed A through G, each with key, name, thesis, primary_metric, fastest_test, and major_risk.
- backup_candidates must contain 0 to 2 direction keys from A..G, ordered best fallback first, and must never include selected_direction.
- go_conditions, kill_criteria, and verify_plan must each contain 1 to 5 concrete items.
- confidence must be "low", "medium", or "high".
- Treat Comparator as the funnel layer and Evidence Auditor as the adjudication layer.
- Respect the hard feasibility gate first. Directions with unresolved hard blockers should not become the primary production choice unless every direction is blocked and you explicitly lower confidence.
- Use soft ranking only after hard-feasibility status is clear.
- Prefer choices within comparator top_keys. Only choose outside top_keys if Comparator is clearly contradicted by the Evidence Auditor and grounded research.
- Treat the Evidence Auditor scorecard as the structured adjudication layer for evidence strength and unsupported fields.
- Prefer directions with stronger comparator composite_score, stronger evidence_score, fewer unsupported_fields, and fewer decision_critical_unknowns.
- Use the field capability matrix to separate production-feasible directions from exploration-only ideas.
- If claim_attributions are sparse, citations are weak, or evidence coverage is thin, lower confidence.
- The deterministic confidence envelope is: near-zero grounded evidence => low; thin evidence or decision-critical unknowns => at most medium; only broad grounded coverage can justify high.
- Do not select "none" merely because some unknowns remain or because the research is incomplete.
- When grounded evidence is non-zero and directions can still be compared, choose the best provisional direction and lower confidence.
- Reserve "none" for near-zero grounded evidence, direct evidence conflict, or a missing decision-critical fact that makes comparison between the short-listed directions impossible.
- If evidence is insufficient to choose responsibly, select "none" instead of forcing a direction.
- backup_candidates should usually come from comparator top_keys excluding the selected_direction.
- summary must be concise and decision-grade.
{validation_guidance}

Language hint: {language_hint}

User problem:
{user_problem}

Research context:
{research_block}
""",
    }


def build_direction_debate_crew(
    user_problem: str,
    mode: str,
    language_hint: str,
    llm: Any,
    direction_judge_llm: Any,
    research_context: Optional[ResearchContext] = None,
) -> Crew:
    mode_config = _get_mode_config(mode)
    research_block = _render_research_context_for_prompt(research_context)
    direction_prompts = _build_direction_debate_prompt_bundle(
        user_problem=user_problem,
        language_hint=language_hint,
        research_block=research_block,
    )

    explorer = Agent(
        role="Explorer",
        goal="Generate seven distinct candidate directions A-G using only grounded research evidence.",
        backstory=(
            f"[Explorer] Direction options for {mode_config.name} mode.\n"
            f"- Focus: {mode_config.research_focus}\n"
            "- Produce exactly seven strategically distinct options keyed A through G.\n"
            "- Use claim_attributions and citations as primary evidence.\n"
            "- Respect field capability matrix constraints when venue/data history limits are explicit.\n"
            "- Convert partial but grounded evidence into provisional options instead of restating uncertainty.\n"
            "- Treat summary as compressed context, not as authority.\n"
            "- Unknowns may constrain options but must not be promoted into evidence.\n"
            "- When evidence is thin, explicitly document evidence_gaps, assumptions, verification_steps, and what_if_scenarios.\n"
            "- Do NOT skip or weaken a direction due to incomplete evidence - make gaps explicit instead.\n"
            "- Provide actionable directions with clear confidence indicators even with partial evidence."
        ),
        allow_delegation=False,
        verbose=False,
        llm=direction_judge_llm,
    )

    comparator = Agent(
        role="Comparator",
        goal="Rank directions A-G with a structured scorecard and funnel them to the best three candidates.",
        backstory=(
            f"[Comparator] Structured direction comparison for {mode_config.name} mode.\n"
            f"- Focus: {mode_config.research_focus}\n"
            "- Run a hard feasibility gate before soft ranking.\n"
            "- Compare all seven directions before deep risk review.\n"
            "- Use a score vector for feasibility, reversibility, speed-to-test, evidence strength, downside severity, and unresolved unknown dependency.\n"
            "- Mark hard blockers and route data-history-mismatched ideas toward exploration rather than pretending they are production-ready.\n"
            "- Produce exactly three top_keys, ordered best to worst.\n"
            "- Use claim_attributions and citations as primary evidence.\n"
            "- Treat summary as compressed context, not as authority."
        ),
        allow_delegation=False,
        verbose=False,
        llm=direction_judge_llm,
    )

    skeptic = Agent(
        role="Skeptic",
        goal="Stress-test the comparator short-list and identify the irreversible risk and veto reason for each top candidate.",
        backstory=(
            f"[Skeptic] Direction risk review for {mode_config.name} mode.\n"
            f"- Focus: {mode_config.research_focus}\n"
            "- Review only the comparator top_keys in depth.\n"
            "- Use claim_attributions and citations as primary evidence.\n"
            "- Never revive unsupported claims.\n"
            "- Unknowns can justify caution but cannot be treated as facts.\n"
            "- Surface the hidden dependency that could invalidate each shortlisted direction."
        ),
        allow_delegation=False,
        verbose=False,
        llm=direction_judge_llm,
    )

    auditor = Agent(
        role="Evidence Auditor",
        goal="Score how well each direction A-G is actually supported by grounded evidence and flag unsupported fields.",
        backstory=(
            f"[Evidence Auditor] Evidence adjudication for {mode_config.name} mode.\n"
            "- Build a per-option evidence scorecard before final judgment.\n"
            "- Use claim_attributions and citations as primary evidence.\n"
            "- Treat summary as secondary narrative only.\n"
            "- Separate supported fields, summary-only fields, and unsupported fields.\n"
            "- Escalate decision-critical unknowns that materially affect comparison."
        ),
        allow_delegation=False,
        verbose=False,
        llm=direction_judge_llm,
    )

    judge = Agent(
        role="Judge",
        goal="Merge Explorer, Comparator, Skeptic, and Evidence Auditor into one DirectionDecision and choose A-G or none.",
        backstory=(
            f"[Judge] Final direction selection for {mode_config.name} mode.\n"
            "- Merge option quality, comparator funnel, veto logic, evidence adjudication, and grounded research into one decision.\n"
            "- Use claim_attributions and citations as primary evidence.\n"
            "- Treat Comparator as the funnel layer and the Evidence Auditor scorecard as the tie-breaker for evidence strength.\n"
            "- Apply hard-feasibility first and soft ranking second.\n"
            "- Emit backup_candidates so the system preserves second-best routes instead of collapsing too early.\n"
            "- Lower confidence when evidence coverage is thin.\n"
            "- Prefer a low-confidence provisional choice when grounded evidence exists.\n"
            "- Prefer selecting from comparator top_keys unless the evidence auditor clearly falsifies the short-list.\n"
            '- Reserve "none" for near-zero evidence or genuinely non-comparable short-listed options.'
        ),
        allow_delegation=False,
        verbose=False,
        llm=direction_judge_llm,
    )

    explorer_task = _tag_direction_stage_task(
        Task(
            description=direction_prompts["explorer"],
            agent=explorer,
            expected_output="JSON with options list only.",
        ),
        "explorer",
    )

    comparator_task_kwargs = {
        "description": direction_prompts["comparator"],
        "agent": comparator,
        "context": [explorer_task],
        "expected_output": "JSON with structured comparison matrix and top-three short-list.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        comparator_task_kwargs["output_pydantic"] = DirectionComparatorReport

    comparator_task = _tag_direction_stage_task(
        Task(**comparator_task_kwargs), "comparator"
    )

    skeptic_task = _tag_direction_stage_task(
        Task(
            description=direction_prompts["skeptic"],
            agent=skeptic,
            context=[explorer_task, comparator_task],
            expected_output="JSON with deep risk review for comparator top-three directions.",
        ),
        "skeptic",
    )

    auditor_task_kwargs = {
        "description": direction_prompts["auditor"],
        "agent": auditor,
        "context": [explorer_task, comparator_task, skeptic_task],
        "expected_output": "JSON with per-direction evidence scorecard.",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        auditor_task_kwargs["output_pydantic"] = EvidenceAuditReport

    auditor_task = _tag_direction_stage_task(Task(**auditor_task_kwargs), "auditor")

    judge_task_kwargs = {
        "description": direction_prompts["judge"],
        "agent": judge,
        "context": [explorer_task, comparator_task, skeptic_task, auditor_task],
        "expected_output": "Valid DirectionDecision JSON only (no markdown, no extra text).",
    }
    if STRICT_JSON_ENABLED and CREWAI_OUTPUT_PYDANTIC:
        judge_task_kwargs["output_pydantic"] = DirectionDecision

    judge_task = _tag_direction_stage_task(Task(**judge_task_kwargs), "judge")
    crew = Crew(
        agents=[explorer, comparator, skeptic, auditor, judge],
        tasks=[explorer_task, comparator_task, skeptic_task, auditor_task, judge_task],
        process=Process.sequential,
        verbose=False,
    )
    prompt_hashes = {
        stage_name: _text_sha256(direction_prompts[stage_name])
        for stage_name in ("explorer", "comparator", "skeptic", "auditor", "judge")
    }
    setattr(crew, "_prompt_hashes", prompt_hashes)
    setattr(
        crew,
        "_prompt_total_chars",
        sum(len(direction_prompts[stage_name]) for stage_name in prompt_hashes),
    )
    setattr(
        crew,
        "_retry_policy",
        RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=True),
    )
    setattr(crew, "_crew_name", "direction_debate")
    return crew


def _build_research_swarm_specs(
    *,
    mode_config: "ModeConfig",
    language_hint: str,
) -> Tuple[Dict[str, "AgentSpec"], List["TaskSpec"], Dict[str, str]]:
    return _build_research_swarm_specs_from_module(
        mode_config=mode_config,
        language_hint=language_hint,
        deps={
            "LIBRARIAN_SEARCH_PROVIDERS": LIBRARIAN_SEARCH_PROVIDERS,
            "AgentSpec": AgentSpec,
            "TaskSpec": TaskSpec,
        },
    )


def _normalize_rerun_agent_keys(agent_names: List[str]) -> List[str]:
    return _normalize_rerun_agent_keys_from_module(agent_names)


def _legacy_build_analysis_specs(
    *,
    mode_config: "ModeConfig",
    active_roles: Set[str],
    direction_feedback_enabled: bool = False,
) -> Tuple[Dict[str, AgentSpec], List[TaskSpec], Dict[str, str]]:
    gate_guidance = "\n".join(_mode_gate_controller_guidance(mode_config))
    analyst_focus = {
        "research": (
            "Research",
            "Surface market/user opportunities and practical hypotheses.",
            f"Focus on {mode_config.research_focus}. "
            "Produce concise, decision-oriented findings.",
        ),
        "risk": (
            "Risk",
            "Identify irreversible risks and failure conditions.",
            "Prioritize downside protection, falsifiable assumptions, and kill criteria.",
        ),
        "ops": (
            "Ops",
            "Define execution plan and operational constraints.",
            "Focus on sequencing, delivery risk, monitoring, and reliability.",
        ),
        "biz": (
            "Biz",
            "Validate monetization and distribution assumptions.",
            f"Focus on {mode_config.biz_focus}.",
        ),
        "critic": (
            "Critic",
            "Challenge weak assumptions and expose hidden coupling.",
            "Be strict and concrete; no generic criticism.",
        ),
    }
    if mode_config.name.strip().lower() == "agent":
        analyst_focus["research"] = (
            "Research",
            "Define automation scope, state boundaries, and deterministic decision assumptions.",
            f"Focus on {mode_config.research_focus}. "
            "Prioritize machine-only execution, state observability, and replayability.",
        )
        analyst_focus["risk"] = (
            "Risk",
            "Identify irreversible execution, protocol, and state-consistency risks.",
            "Prioritize deterministic failure handling, kill criteria, replay safety, and anti-corruption boundaries.",
        )
        analyst_focus["ops"] = (
            "Ops",
            "Define runtime orchestration, deployment, and reliability constraints for a headless service.",
            "Focus on retries, process supervision, structured logs, monitoring, and safe recovery.",
        )
        analyst_focus["biz"] = (
            "Biz",
            "Validate incentive alignment, reward economics, and operator sustainability.",
            f"Focus on {mode_config.biz_focus}. Avoid consumer SaaS assumptions unless explicitly stated.",
        )

    agent_specs: Dict[str, AgentSpec] = {}
    template_vars: Dict[str, str] = {
        "gate_guidance": gate_guidance,
        "direction_feedback_enabled": "true" if direction_feedback_enabled else "false",
    }
    task_specs: List[TaskSpec] = []

    for role_key in ANALYST_AGENT_ORDER:
        if role_key not in active_roles:
            continue
        role_name, goal, focus = analyst_focus[role_key]
        agent_specs[role_key] = AgentSpec(
            name=role_key,
            role=role_name,
            goal=goal,
            backstory=(
                f"[{role_name}] {focus}\n"
                "請輸出精簡、結構化、可直接用於決策的內容。\n\n"
                + NO_CROSS_ROLE_RULE
                + COMMON_OUTPUT_RULES
            ),
            output_schema_name=None,
            parallel_safe=True,
        retry_policy=RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=False),
            version="v1.0.0",
            behavior_contract=f"{role_name} specialist output must be concise and decision-useful.",
        )
        task_specs.append(
            TaskSpec(
                name=role_key,
                description_template=(
                    "你是 {mode_name} 模式下的 [{role_name}]。\n"
                    "問題：\n{user_problem}\n\n"
                    "語言：{language_hint}\n\n"
                    "聚焦重點：\n{focus}\n\n"
                    "請輸出精簡、具體、可直接被 Gate Controller 消化的 findings。"
                ),
                agent_name=role_key,
                expected_output="Structured role-specific findings only.",
            )
        )
        template_vars[f"{role_key}_role_name"] = role_name
        template_vars[f"{role_key}_focus"] = focus

    gate_context = [r for r in ANALYST_AGENT_ORDER if r in active_roles]
    compacted_gate_context = ["gate_context_compactor"]

    agent_specs["gate_context_compactor"] = AgentSpec(
        name="gate_context_compactor",
        role="Gate Context Compactor",
        goal="把 analyst 原始輸出壓成更精煉但仍保留 implementation-critical detail 的 GateContextBundle。",
        backstory=(
            "你是 gate 前置整理器。"
            "只能輸出嚴格的 GateContextBundle JSON。\n"
            + GATE_CONTEXT_COMPACTOR_RULES
        ),
        output_schema_name="GateContextBundle",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract="Must preserve implementation-critical detail while aggressively deduplicating noise.",
        depends_on=gate_context,
    )
    agent_specs["gate_controller"] = AgentSpec(
        name="gate_controller",
        role="Gate Controller",
        goal="整合各 analyst 輸出，並決定是否允許進入 CodeGen。",
        backstory=(
            "你是流程控制仲裁者。"
            "只能輸出嚴格的 GateDecision JSON。\n"
            + gate_guidance
            + "\n\n"
            + GATE_CONTROLLER_RULES
        ),
        output_schema_name="GateDecision",
        parallel_safe=False,
        retry_policy=RetryPolicy(
            max_attempts=2, retry_on_json_fail=True, retry_on_low_confidence=True
        ),
        version="v1.1.0",
        behavior_contract="Must emit strict GateDecision JSON with explicit flow-control fields.",
        depends_on=compacted_gate_context,
    )
    agent_specs["format_checker"] = AgentSpec(
        name="format_checker",
        role="Format Checker",
        goal="把 GateDecision 轉成 AnalysisReport JSON。",
        backstory=("你是嚴格 formatter。只能做結構轉換，不得新增任何事實。"),
        output_schema_name="AnalysisReport",
        parallel_safe=False,
        retry_policy=RetryPolicy(max_attempts=20, backoff_seconds=2.0, retry_on_json_fail=True),
        version="v1.0.0",
        behavior_contract="Structural conversion only; no new facts allowed.",
        depends_on=["gate_context_compactor", "gate_controller"],
    )

    task_specs.append(
        TaskSpec(
            name="gate_context_compactor",
            description_template=(
                "請把 analyst 輸出壓成 GateContextBundle JSON。\n"
                "模式：{mode_name}\n"
                "語言：{language_hint}\n"
                "問題：\n{user_problem}\n\n"
                "必填欄位：\n"
                "- executive_summary (string)\n"
                "- analyst_findings (dict[role,string])\n"
                "- implementation_requirements (list[string])\n"
                "- implementation_constraints (list[string])\n"
                "- validation_focus (list[string])\n"
                "- blocking_unknowns (list[string])\n"
                "- rerun_signals (dict[role,list[string]])\n\n"
                "規則：\n"
                "- 去重，但不可刪除 implementation-critical detail。\n"
                "- analyst_findings 必須保留各角色獨立觀點。\n"
                "- 只保留會影響 Gate Controller 決策、後續 codegen、或 rerun 判斷的內容。\n"
                "- 只輸出 JSON。"
            ),
            agent_name="gate_context_compactor",
            expected_output="GateContextBundle JSON only.",
            context_task_names=gate_context,
            output_pydantic_model="GateContextBundle",
        )
    )
    task_specs.append(
        TaskSpec(
            name="gate_controller",
            description_template=(
                "請根據 GateContextBundle 整合成 GateDecision JSON。\n"
                "模式：{mode_name}\n"
                "語言：{language_hint}\n"
                "問題：\n{user_problem}\n\n"
                "必填欄位：\n"
                "- consensus (string)\n"
                "- disagreement (string)\n"
                "- experiments (list of {{goal, criteria}})\n"
                "- ready_for_codegen (bool)\n"
                "- blocking_risks (list[string])\n"
                "- required_experiments_before_codegen (list[string])\n"
                "- advisory_experiments_after_codegen (list[string])\n"
                "- codegen_scope (production|validation)\n"
                "- validation_scope_reason (string|null)\n"
                "- validation_objectives (list[string])\n"
                "- agents_needing_rerun (list[string])\n"
                "- rerun_reasons (dict)\n"
                "- direction_feedback_needed (bool)\n"
                "- direction_feedback_reason (string|null)\n"
                "- direction_feedback_type (evidence|detail|null)\n"
                "- direction_feedback_evidence_gaps (list[string])\n"
                "- direction_feedback_questions (list[string])\n"
                "- overall_score (0-100)\n"
                "- score_breakdown (dict: feasibility/risk/roi/uncertainty)\n"
                "- confidence (low|medium|high)\n"
                "- failure_type (enum)\n"
                "- failure_details (string|null)\n"
                "- should_kill (bool)\n"
                "- kill_reason (string|null)\n"
                "Direction debate feedback loop enabled: {direction_feedback_enabled}\n"
                "模式專屬 gate guidance：\n{gate_guidance}\n"
                "只輸出 JSON。"
            ),
            agent_name="gate_controller",
            expected_output="GateDecision JSON only.",
            context_task_names=["gate_context_compactor"],
            output_pydantic_model="GateDecision",
        )
    )
    task_specs.append(
        TaskSpec(
            name="format_checker",
            description_template=(
                "請把 GateDecision 轉成 AnalysisReport JSON。\n"
                "mode_used 必須精確設為 '{mode_name}'。\n"
                "語言：{language_hint}\n"
                "必須保留來自 GateContextBundle 的 analyst_findings、implementation_requirements、"
                "implementation_constraints、validation_focus。\n"
                "gate_context_snapshot 必須保留 GateDecision 的 flow-control details。\n"
                "codegen_handoff_summary 必須是整合後、可直接交付 CodeGen 的 concise implementation brief。\n"
                "只輸出 JSON。"
            ),
            agent_name="format_checker",
            expected_output="AnalysisReport JSON only.",
            context_task_names=["gate_context_compactor", "gate_controller"],
            output_pydantic_model="AnalysisReport",
        )
    )

    return agent_specs, task_specs, template_vars


def _build_analysis_specs(
    *,
    mode_config: "ModeConfig",
    active_roles: Set[str],
    direction_feedback_enabled: bool = False,
) -> Tuple[Dict[str, AgentSpec], List[TaskSpec], Dict[str, str]]:
    return _build_analysis_specs_from_module(
        mode_config=mode_config,
        active_roles=active_roles,
        direction_feedback_enabled=direction_feedback_enabled,
        deps={
            "mode_gate_controller_guidance": _mode_gate_controller_guidance,
            "AgentSpec": AgentSpec,
            "TaskSpec": TaskSpec,
            "RetryPolicy": RetryPolicy,
            "ANALYST_AGENT_ORDER": ANALYST_AGENT_ORDER,
            "NO_CROSS_ROLE_RULE": NO_CROSS_ROLE_RULE,
            "COMMON_OUTPUT_RULES": COMMON_OUTPUT_RULES,
            "GATE_CONTROLLER_RULES": GATE_CONTROLLER_RULES,
            "GATE_CONTEXT_COMPACTOR_RULES": GATE_CONTEXT_COMPACTOR_RULES,
        },
    )
