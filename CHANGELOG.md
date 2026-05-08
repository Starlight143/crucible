# Changelog

All notable changes to this project are documented in this file.
Versioning follows [Semantic Versioning](https://semver.org/). The first public release was **v1.0.0**.

---

## [v1.0.4] — 2026-05-08

### Fixed
- **Reasoning-model `<think>` blocks corrupted every structured-output
  extractor**: DeepSeek-V3/V4, GLM-5.1, Qwen-3.5 and o1-class models emit
  chain-of-thought inside `<think>…</think>` (and `<thinking>` /
  `<reasoning>` / `<reflection>` / `<scratchpad>` aliases) ahead of the
  answer. Any brace- or fence-shape token in the reasoning text was
  captured as the first outermost JSON object / longest fenced block, so
  the real answer was discarded. The most visible symptom was Stage 0
  Direction Debate falling back to `[Warn] Direction debate could not
  produce a valid decision`; the same defect silently truncated every
  adversarial-review run, every auto-remediation patch, every generated
  test file, and every multi-language translation. New shared helper
  `crucible.output_validation.strip_reasoning_blocks` now runs ahead of
  every structured-output scan: `_extract_first_json_object` /
  `_extract_first_json_array`, `extract_json`, the section_06 API-version
  regex, `independent_validator._extract_json_from_response`,
  `backtest_runner._extract_code_block`, and the four feature-module
  `_strip_code_fences` helpers. 15 new regression tests.
- **Direction Debate force-none gate had no diagnostic surface**
  (`crucible/modules/section_02_research_and_llm.py`):
  `_should_force_direction_none` returned silently with no print and no
  dump, so users had no way to tell which gate fired. The path now prints
  `[Warn] … gate fired (reason=… citations=… grounded_claims=…
  weak_directions=…)` and writes a JSON debug dump under
  `saved_projects/direction_debug/` tagged `note=force_none:<reason>`.
- **`run_direction_debate` final exit summary**
  (`crucible/modules/section_02_research_and_llm.py`): after all
  refinement iterations exhaust, the loop now prints one summary line
  with the last `gap_info` (or points at the latest dump file for
  JSON-parse failures) before returning None.
- **Runner [Warn] message points at diagnostics**
  (`crucible/modules/section_07_selfcheck_output_main.py`): the generic
  fallback message now references the preceding `[Warn]` line(s) and
  `saved_projects/direction_debug/`.

### Validation
- pytest: 1 941 passed, 1 skipped under `-m "not slow and not network"`.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

---

## [v1.0.3] — 2026-05-08

### Changed
- **Centralised env-var parsing** (new `crucible/_env.py`): the
  `_env_int` / `_env_float` / `_env_bool` / `_env_str` / `_env_optional_*`
  helpers duplicated across 26 production files now delegate to one
  canonical implementation. Per-file shims keep call sites unchanged;
  central helpers expose `clamp_min` / `clamp_max` / `finite_only` and
  sentinel-aware (`unlimited`, `inf`, `none`) behaviour as flags. Backed
  by 73 new tests in `tests/test_env_helpers.py`.
- **CI workflow split into `lint` / `test` / `security` jobs**
  (`.github/workflows/ci.yml`): lint and security run as independent
  ubuntu-only jobs installing only `ruff`/`mypy` and `bandit`/`pip-audit`
  respectively. The test job keeps the 2×3 OS-by-Python matrix but
  invokes pytest with `-n auto --durations=20 -m "not slow and not
  network"` (suite drops from ~200 s → ~25 s). Lint/mypy scope is read
  from `pyproject.toml`.
- **mypy coverage expanded from 25 → 74 source files**
  (`pyproject.toml [tool.mypy]`): fixed previously-masked type errors
  in `runtime_logging.py`, `hooks.py`, `features/checkpoint.py`,
  `features/portfolio_backtest.py`, `convergence_guard.py`, and
  `web_research/http_clients.py`.
- **Silent `except Exception: pass` swallows now logged**
  (`webui/app.py` × 9, `run_crucible_enhanced.py` × 3): each bare-pass
  swallow records its traceback at `LOGGER.debug` level under the new
  `crucible.webui` / `crucible.runner` loggers, surfaced when
  `CRUCIBLE_LOG_LEVEL=DEBUG`.
- **`output_validation.extract_json` refactored**
  (`crucible/output_validation.py`): the 100-line three-strategy chain
  becomes four named helpers (`_coerce_to_str`, `_try_direct_json`,
  `_try_markdown_block`, `_scan_outermost_json`) plus a 25-line
  orchestrator.
- **Test marker discipline + parallel runner**
  (`tests/conftest.py`, `pyproject.toml`): registered `slow`,
  `network`, `integration` markers; tagged the 7 tests >5 s wall-clock
  as `slow`. Added `pytest-xdist` + `pytest-cov` to
  `requirements-dev.txt` with a `[tool.coverage.run]` config.
- **WebUI SQLite connection cached per worker thread**
  (`webui/app.py`): one `sqlite3.connect` per thread via
  `threading.local()` instead of re-opening on every request and
  re-running schema DDL. Bootstrap connection reaped at interpreter
  exit via `atexit`; `_reset_db_threadlocal()` test helper exposed.
- **WebUI frontend assets split out of the template**
  (`webui/templates/index.html` → `webui/static/css/app.css` +
  `webui/static/js/app.js`): the inline `<style>` (1 092 lines) and
  `<script>` (3 439 lines) blocks become sidecar files served by
  Flask's default `/static/` route. The HTML shell drops to 555 lines,
  browsers cache CSS/JS independently of the template, and
  `Content-Security-Policy: script-src 'self'` becomes achievable. The
  lone Jinja expression in the original inline script
  (`{{ webui_url | tojson }}`) is bridged through `window.WEBUI_URL`:
  a 1-line inline `<script>` in `index.html` sets the global before
  `app.js` loads, and `app.js` reads `window.WEBUI_URL ||
  window.location.host`. New `tests/test_webui_frontend_integration.py`
  (28 tests) pins the bridge ordering, rejects Jinja artifacts in
  served assets, asserts every canonical agent-flow SSE event matches
  an `evMap` regex, checks every state-handler key has a matching
  branch, proves every inline `onclick` symbol resolves to a top-level
  function, and verifies the SSE `EventSource` wiring — all against
  the served assets via Flask's `test_client()`.
- **Internal version-tag scrubbing** (~110 occurrences across 25 files):
  removed pre-1.0 audit-batch references (`v16.x.y` / `v16.0.x`
  prefixes in inline comments, `Regression (v16.X.Y):` test docstring
  prefixes, `# ── v16.X feature_name ──` section dividers, the
  `Generated by Crucible v16.9` HTML report footer, and the
  `OLD_version/crucible_v14.py` provenance line in
  `crucible/SECTION_MANIFEST.md` plus the auto-generated section-module
  headers).  CLI flag `--v169-features` and env var `V169_FEATURES`
  remain as public-API names (renaming would break downstream user
  scripts).  Comments and docstrings only — no behaviour change.

### Validation
- pytest: 1 926 passed, 1 skipped under `-m "not slow and not network"`.
- pytest `-m slow`: 7 passed.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.
- `python -m mypy`: 0 errors across 74 files.
- `python -m ruff check`: clean.

---

## [v1.0.2] — 2026-05-06

### Fixed
- **WebUI long-session freeze** (`webui/templates/index.html`): four independent
  leaks that progressively froze the WebUI on multi-hour runs.
  - Terminal DOM trimmed symmetrically with `sess.lines` (cap 5 000); previously
    DOM nodes accumulated forever while the array was capped.
  - `visibilitychange` handler reopens any `EventSource` killed by background-tab
    throttling (Chrome Memory Saver, OS sleep) on tab refocus.
  - `pagehide` handler closes all `EventSource` connections on tab close so
    server-side SSE generators release immediately instead of waiting 30 min.
  - `showPage()` clears `_ABState.pollTimer` when navigating away from the
    A/B-test page; `_initABTest()` re-arms it on return.

### Validation
- pytest: 1825 passed, 1 skipped.
- `crucible/smoke_test.py`: 5/5 OK.
- `run_crucible.py --self-check`: OK.

---

## [v1.0.1] — 2026-05-04

### Changed
- **Test-suite naming hygiene**: Removed pre-1.0 internal version tags from test
  filenames and docstrings so the test layout reflects what each module covers,
  not which audit batch produced it. Test discovery, collection rules, and
  assertion logic are unchanged — only filenames and human-readable comments
  were touched. Renames (tracked via `git mv` to preserve history):
  - `tests/test_v16_9_68_audit_fixes.py` → `tests/test_security_and_safety_guards.py`
  - `tests/test_v16_9_69_audit_fixes.py` → `tests/test_subprocess_and_runtime_guards.py`
  - `tests/test_v16_9_71_cjk_grounding.py` → `tests/test_cjk_grounding.py`
  - `tests/test_v16_9_72_audit_fixes.py` → `tests/test_research_synthesizer_guards.py`
  - `tests/test_v16_9_73_audit_fixes.py` → `tests/test_codegen_fix_loop_and_dotenv.py`
  - `tests/test_v16_9_74_env_hot_reload.py` → `tests/test_env_hot_reload.py`
- **Pre-1.0 version tags scrubbed** from incidental docstrings/comments in
  `tests/test_http_clients.py`, `tests/test_crucible_runtime.py`, and
  `tests/test_resilience.py`. Production source files retain their inline
  historical annotations untouched (no behaviour change).

### Validation
- Full pytest suite: 1821 passed, 5 skipped.
- `crucible/smoke_test.py`: all 5 checks pass.
- `run_crucible.py --self-check`: OK.

---

## [v1.0.0] — 2026-05-04

### Added
- First public release. See README and feature documentation for the full
  capability surface.

---

## [v0.9.0] — 2026-05-01

### Fixed
- **WebUI hot-reload**: `POST /api/env` now mutates `os.environ` in-place so saved settings take effect immediately without a process restart.

---

## [v0.8.6] — 2026-04

### Fixed
- **Direction Debate (CJK)**: Direction Debate now reliably produces a valid decision on Traditional Chinese runs; resolved `[Warn] Direction debate could not produce a valid decision` regression.
- **WebUI agent-flow**: `research_synthesizer` node now lights up correctly; fixed `format_checker` false `self_check` activation.
- **Quant backtest LLM-fix loop**: Added syntax gate to prevent malformed patches from entering the auto-fix retry cycle.
- **Pearson r**: NaN clamp applied after correlation computation to prevent invalid values propagating downstream.
- **`.env` DEBUG propagation**: `DEBUG` flag now correctly forwarded to sub-processes spawned by the enhanced runner.
- **Parallel log audit**: Fixed pydantic checkpoint warning, DEBUG-logger stdout flood and interleaving, force-none second branch, `provider_errors` → `key_risks` field contamination, GitHub repo-search token guard.

---

## [v0.8.5] — 2026-03 / 2026-04

### Fixed
- **CodeGen token exhaustion**: Raised token cap to 65 536; added supplement retry when primary generation is truncated. Reduced `CODEGEN_BATCH_SIZE` 3 → 2 to prevent truncation mid-batch.
- **CodeBundle formatter starvation**: Formatter LLM cap raised to `CODEGEN_MAX_TOKENS`; formatter also capped at 8 192 tokens maximum.
- **Never-terminate codegen**: Convergence guard now fires correctly when the codegen loop stalls; project-wide subnormal-divisor sweep applied.
- **CJK / fullwidth-punctuation sanitisation**: `_sanitize_code_bundle` now deterministically repairs fullwidth punctuation in generated Python source.
- **Double/triple-escaped LLM output**: `_unescape_llm_code_content` uses iterative unescaping to handle layered escape sequences from some providers.
- **Web search hardening**: Stripped `site:` qualifiers and truncated oversized queries; `grep.app` CJK skip; DDG CAPTCHA filter; paperswithcode truncation guard.
- **ResearchContext extraction**: Tolerates GLM-5.1 and other non-standard format mismatches without crashing.
- **WebUI SSE disconnect**: Client disconnect no longer kills the subprocess; run continues and results remain retrievable.
- **Reasoning-model empty response**: Empty LLM responses from reasoning models now classified as transient (retried) rather than fatal.
- **HITL SSE reconnect**: Human-in-the-loop SSE stream restores correctly after client reconnect.
- **Subnormal-divisor sweep**: All division paths across infrastructure modules guarded with `not (x > 1e-14)` pattern.
- **Fail-closed defaults**: Paper-mode and auth-manager now default to the safe/closed state on configuration parse failure.
- **env-bool whitelist**: All `os.environ` boolean reads switched to explicit `{"1","true","yes","on"}` whitelist; ambiguous values return the default rather than truthy.
- **`BaseException` narrowing**: Overly broad `except BaseException` replaced with specific exception types across worker threads.
- **Cost tracker O(n²) → O(1)**: Stage-cost accumulation loop refactored to constant-time append.

---

## [v0.8.4] — 2026-03

### Fixed
- **Agent-flow visibility**: `research_synthesizer` and all crew-agent nodes now light up in real time on the WebUI flow panel.
- **Research phase hardening**: Belt-and-braces retry on research swarm lane failures; arxiv XML parse guard added.
- **Librarian hardening**: Per-host circuit breakers, retry classification improvements, `grep.app` CJK skip.
- **HTTP 202 bot-detection**: `safe_http_text` now treats HTTP 202 responses from known bot-detection gateways as errors.
- **GitHub search auth guard**: `search/code` endpoint skipped when no `GITHUB_TOKEN` is configured (avoids guaranteed 401).
- **Analyst terminal log visibility**: Analyst agent output now visible in terminal during live runs.
- **`format_checker` field mapping**: Fixed field mapping regression that caused `format_checker` to silently drop analyst findings.
- **Reformat fallback**: `_run_schema_reformatter` and all cascading callers now propagate `OperationCancelledError` correctly.

---

## [v0.8.3] — 2026-02 / 2026-03

### Fixed
- **Quant analytics correctness**: Fixes across walk-forward validation, signal half-life, OLS t-stat, Sharpe subnormal guard, CAGR annualisation, Calmar formula, spread P&L denominator.
- **Telemetry deadlock**: Background telemetry thread no longer deadlocks when the main process is under high load.
- **Stale-warn flood**: `StaleLoopWarning` flood suppressed; only fires once per convergence guard context.
- **Thread-safety sweep**: DCL (double-checked locking) races fixed in `run_registry`, `bootstrap`, `auth_manager`; ABBA deadlock in hook registry resolved.
- **Atomic writes**: All file-write paths switched to `tmp → rename` atomic pattern to prevent partial writes on crash.
- **Path traversal**: `_resolve_entrypoint_path` input sanitised; SSRF redirect bypass blocked.
- **`OperationCancelledError` propagation**: Cancellation correctly propagates through all stage boundaries; cancelled runs no longer recorded as pipeline failures.
- **Checkpoint state precedence**: COMPLETED state from prior checkpoints correctly overrides in-progress state on resume.

---

## [v0.8.2] — 2026-01 / 2026-02

### Fixed
- **Infrastructure hardening**: Comprehensive multi-pass audit of all infrastructure modules covering thread-safety, near-zero / NaN / Inf guards, atomic writes, env-var safety, div-zero guards, circuit breaker races, and iterable safety.
- **HTTP Retry**: `@with_http_retry` falsy-zero edge case fixed; HTTP 408 (Request Timeout) added to retryable set.
- **Checkpoint atomicity**: Checkpoint state transitions are now atomic; partial state files can no longer be observed by concurrent readers.
- **Signal half-life**: Signal decay computation corrected for edge cases at boundary days.
- **XSS / HTML injection**: All user-supplied strings rendered in HTML reports are now properly escaped.
- **Bool-int coercion**: `bool` subtype of `int` no longer passes numeric-type guards accidentally.
- **DSR (Dynamic Sharpe Ratio)**: Reverted unstable DSR estimator to a validated reference implementation.

---

## [v0.8.1] — 2025-12 / 2026-01

### Fixed
- Comprehensive initial audit of v0.8.0 feature suite: formula errors, double-counted metrics, thread-safety, CSV column search, mutable ContextVar defaults, sanitization bypass, class name collisions, data loss in token tracking.

---

## [v0.8.0] — 2025-12

### Added
- **Quant Analytics Suite** — 25 new post-processing modules:
  - `walk_forward_validator.py` — Walk-forward cross-validation for quant strategies
  - `signal_analyzer.py` — Signal decay and IC analysis
  - `regime_detector.py` — Market regime detection (volatility / trend / HMM)
  - `monte_carlo.py` — Monte Carlo simulation and stress testing
  - `factor_analyzer.py` — CAPM / Fama-French factor exposure regression
  - `transaction_cost_model.py` — Transaction cost sensitivity analysis
  - `tearsheet_generator.py` — Strategy tearsheet (Markdown + HTML)
  - `cointegration_analyzer.py` — Cointegrated pair-trading analysis
  - `dynamic_correlation.py` — Rolling correlation matrix + PCA decomposition
  - `code_lockfile_generator.py` — `pyproject.toml` + pinned `requirements.txt` generation
  - `citation_verifier.py` — Evidence citation grounding and URL verification
  - `chat_bot.py` — Interactive in-run Q&A chatbot
  - `config_wizard.py` — Guided configuration setup
  - `grafana_dashboard.py` — Grafana dashboard JSON export
  - `global_knowledge_base.py` — Cross-run knowledge accumulation
  - `few_shot_injector.py` — Few-shot example injection into prompts
  - `alt_data_connectors.py` — Alternative data source connectors
  - `celery_worker.py` — Celery async task worker integration
  - `auth_manager.py` — Pluggable authentication manager
  - `report_annotations.py` — Structured annotation overlay for HTML reports
  - `notion_export.py` — Notion page export integration
  - `mlflow_sink.py` — MLflow experiment tracking sink
  - `prometheus_exporter.py` — Prometheus metrics endpoint
  - `julia_codegen.py` — Julia language code generation target
  - `lockfile_gen_runner.py` — Lockfile generation runner
- **WebUI**: Feature Bundle checkbox grid replaced free-text input for analytics flags.
- **Interactive HTML reports**: Quant analytics results exported as self-contained interactive HTML.

---

## [v0.7.0] — 2025-11

### Added
- **Enhanced runner** (`run_crucible_enhanced.py`) with full post-processing pipeline:
  - Security scan (bandit + built-in regex rules)
  - Deployment artifact generation (Dockerfile, docker-compose, K8s manifests, Helm chart)
  - Automated backtest runner with real market data (yfinance / Binance / CCXT)
  - Independent validation agent (adversarial LLM review + subprocess test execution)
  - Auto-remediation loop (LLM patch → syntax check → re-scan, up to N rounds)
  - Dependency audit (pip-audit)
  - HTML report aggregator
  - Run registry (SQLite-backed cross-run query)
  - Notification webhooks (Slack / Discord / generic HTTP)
  - A/B test runner for comparing pipeline variants
  - Watch mode (file-change trigger with debounce)
  - Batch mode (multi-project parallel execution)
  - Post-analysis chat (interactive Q&A grounded on run artifacts)

---

## [v0.6.0] — 2025-10

### Added
- **WebUI** — Flask-based single-page application:
  - Idea Mode and Project Path pages with full flag selector panels
  - Real-time SSE streaming of pipeline output
  - Dashboard with cost trends, quality distribution, and stage radar chart
  - Leaderboard for backtest strategy rankings
  - Compare Runs and A/B Test pages
  - Settings page with live API key validation and `.env` editing
  - Human-in-the-loop (HITL) approval flow

---

## [v0.5.0] — 2025-09

### Added
- **Direction Debate stage** (Stage 2):
  - Direction Proposer generates 7 mutually exclusive strategic directions
  - Evidence Auditor scores evidence quality per direction
  - Multi-axis Comparator ranks directions across 6 dimensions
  - Direction Judge selects the winner with go-conditions and kill-criteria

---

## [v0.4.0] — 2025-08

### Added
- **Research Swarm** (Stage 1): three parallel research lanes (Market, Technical, Competitor) with independent evidence extraction rules.
- **Research Synthesizer**: cross-validates findings across lanes; unsupported claims moved to `unknowns` or flagged as `hallucination_flags`.
- **Librarian** (Stage 0): web search agent with two-layer caching (per-query + per-ResearchContext).

---

## [v0.3.0] — 2025-07

### Added
- **Analysis Crew** (Stage 3): five specialist analysts (Research, Risk, Ops, Biz, Critic) running in parallel.
- **Gate Controller**: decides `proceed`, `targeted analyst rerun`, or `kill` based on evidence quality.
- **Format Checker**: assembles the final `AnalysisReport` without adding new information.

---

## [v0.2.0] — 2025-06

### Added
- **CodeGen + Quality Loop** (Stage 4): multi-file code generation with dependency graph, `py_compile` + smoke validation, LLM-backed quality loop, and Review & Fix pass.
- **Auto-optimize**: `codegen_critic` generate → critique → refine loop with plateau detection and budget guard.
- **Output scopes**: `mvp` / `full` / `production` controlling the completeness of generated code.

---

## [v0.1.0] — 2025-05

### Added
- Initial pipeline prototype: single-agent research → single-agent codegen flow.
- Basic Pydantic output models (`AnalysisReport`, `CodeBundle`).
- CLI entry point (`run_crucible.py`) with `--dry-run` and `--self-check`.
- Support for OpenRouter as the first LLM provider.
