"""
Crucible WebUI — Flask backend
──────────────────────────────────────
Routes
  GET  /                          Main SPA
  GET  /api/env                   Read .env as {key: value}
  POST /api/env                   Write {key: value} back to .env (atomic)
  GET  /api/env/schema            Grouped schema from .env.example
  POST /api/run                   Start a pipeline run → {run_id}
  GET  /api/run/<id>              Status + buffered output
  GET  /api/run/<id>/stream       SSE line-by-line output (auto-terminates)
  DELETE /api/run/<id>            Kill running process
  GET  /api/dashboard             Aggregate stats from saved_projects/
  GET  /api/runs                  List recent saved runs (supports ?q=, ?mode=, ?limit=)
  GET  /api/leaderboard           Backtest performance ranking
  GET  /api/run/<id>/backtest-chart  Equity curve + summary for a run
  GET  /api/run/<id>/detail       Full run detail (analysis, meta, code files)
  GET  /api/cost-trend            Per-run cost/score trend
  POST /webhook/trigger           Webhook-triggered pipeline run
  GET  /api/webhook/status        Webhook configuration status
"""

from __future__ import annotations

import atexit
import csv
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import logging
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ─── Logger ────────────────────────────────────────────────────────────────────
# Used to record otherwise-silent ``except Exception: pass`` swallows at DEBUG
# level so that operators investigating mysterious behaviour (e.g. "the
# dashboard didn't update") can see the swallowed traceback in
# ``CRUCIBLE_LOG_LEVEL=DEBUG`` mode without changing the swallow's
# user-visible semantics (still recovers gracefully on the happy path).
LOGGER = logging.getLogger("crucible.webui")

# ─── Schema version ────────────────────────────────────────────────────────────

SCHEMA_VERSION = "1"

# ─── Paths ─────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).parent.parent)).resolve()
ENV_FILE     = PROJECT_ROOT / ".env"
ENV_EXAMPLE  = PROJECT_ROOT / ".env.example"
SAVED_PROJECTS_DIR = PROJECT_ROOT / "saved_projects"
ENHANCED_RUNNER    = PROJECT_ROOT / "run_crucible_enhanced.py"


# Load ``.env`` into ``os.environ`` so the WebUI process honours the same env
# vars the spawned ``run_crucible.py`` subprocess
# already does (via ``crucible/runtime_logging._load_dotenv_once``).
# Without this, settings like ``CRUCIBLE_LOG_LEVEL=DEBUG`` set via the
# WebUI's "Environment Variables" panel were saved to ``.env`` but never
# took effect until the operator restarted the entire Python process from
# a shell that had already exported them — surprising behaviour that the
# user reported as "frontend terminal log still shows INFO mode" even
# after switching to DEBUG via the UI.  ``override=False`` keeps any value
# the operator already exported in the shell ahead of the WebUI launch.
def _load_dotenv_into_webui_process() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except Exception:
        return
    try:
        if ENV_FILE.is_file():
            load_dotenv(str(ENV_FILE), override=False)
    except Exception:
        # Never let .env loading crash the WebUI bootstrap.
        return


_load_dotenv_into_webui_process()

# SQLite run index
_RUN_INDEX_DIR = SAVED_PROJECTS_DIR / ".cache"
_RUN_INDEX_DB  = _RUN_INDEX_DIR / "webui_run_index.db"

# SSE stream constants
_SSE_POLL_INTERVAL = 0.15          # seconds between SSE poll ticks
_SSE_MAX_IDLE_TICKS = int(30 * 60 / _SSE_POLL_INTERVAL)  # 30-min no-progress timeout
# Keepalive interval: emit {"__keepalive__": true} every ~20 s while idle.
# Using a real data: event (not an SSE comment) ensures EventSource.onmessage
# fires on the frontend, which resets the 10-min watchdog timer — SSE comments
# (": keepalive") are invisible to onmessage and would not prevent the watchdog
# from triggering false reconnects during long LLM calls.
_SSE_KEEPALIVE_TICKS = int(20 / _SSE_POLL_INTERVAL)       # every ~20 seconds

app = Flask(__name__)


@app.errorhandler(404)
def _handle_404(exc: Any) -> Any:
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def _handle_500(exc: Any) -> Any:
    return jsonify({"error": "Internal server error"}), 500


# ─── In-memory run registry ────────────────────────────────────────────────────

_runs: dict[str, dict[str, Any]] = {}
_runs_lock = threading.Lock()

# A/B test registry
_ab_tests: dict[str, dict[str, Any]] = {}
_ab_tests_lock = threading.Lock()

# Webhook history lock (DB writes are serialised through this)
_webhook_history_lock = threading.Lock()

# Evict completed runs from _runs after this many seconds to bound memory use.
# Output lines are pruned first (largest contributor); the run shell is kept for
# a further period so status queries still return the final returncode/status.
_RUNS_OUTPUT_TTL = 300    # 5 min: trim output list after run ends
_RUNS_ENTRY_TTL  = 3600   # 1 h:  remove the run dict entry entirely


def _evict_stale_runs(skip_run_id: str = "") -> None:
    """Remove output lines and old entries from completed runs (called on each SSE poll).

    *skip_run_id* must be set to the run currently being streamed so that its
    output buffer is never cleared while the SSE generator may still need it to
    evaluate ``sent >= len(run["output"])``.  Clearing the buffer for an active
    stream would cause the termination check to fire prematurely, sending
    ``__done__`` before all output has been delivered to the client.
    """
    now = time.time()
    to_delete: list[str] = []
    with _runs_lock:
        for rid, run in _runs.items():
            if rid == skip_run_id:
                continue  # Never evict the run being actively streamed
            ended = run.get("ended_at")
            if ended is None:
                continue
            age = now - ended
            if age > _RUNS_ENTRY_TTL:
                to_delete.append(rid)
            elif age > _RUNS_OUTPUT_TTL and run.get("output"):
                run["output"] = []  # Free the (potentially large) output buffer
        for rid in to_delete:
            del _runs[rid]

# ─── SQLite run index sync state ───────────────────────────────────────────────

_SYNC_LOCK = threading.Lock()
_last_sync_time: float = 0.0
_SYNC_INTERVAL = 30.0  # max once per 30 seconds


# ─── Process cleanup on interpreter exit ───────────────────────────────────────

@atexit.register
def _cleanup_all_runs() -> None:
    """Terminate any child processes still running when Flask exits."""
    with _runs_lock:
        for run in _runs.values():
            proc: subprocess.Popen | None = run.get("process")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    LOGGER.debug("atexit terminate failed for run", exc_info=True)


# ─── .env helpers ──────────────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    target = ENV_FILE if ENV_FILE.exists() else ENV_EXAMPLE
    result: dict[str, str] = {}
    if not target.exists():
        return result
    for raw in target.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip()
            # Strip surrounding quotes so "sk-xxx" and 'sk-xxx' are stored as sk-xxx
            if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
                v = v[1:-1]
            result[k.strip()] = v
    return result


def _quote_env_value(v: str) -> str:
    """Wrap value in double quotes if it contains characters that would break
    .env parsing (``#``, leading/trailing whitespace, quotes)."""
    if "#" in v or v != v.strip() or '"' in v or "'" in v:
        # Escape existing double quotes and backslashes inside the value
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return v


def _save_env(data: dict[str, str]) -> None:
    """
    Write key/value pairs back to .env, preserving comments from .env.example.
    Uses an atomic temp-file + os.replace pattern to avoid corruption on failure.
    """
    template = ENV_EXAMPLE if ENV_EXAMPLE.exists() else None
    if template:
        out_lines: list[str] = []
        written: set[str] = set()
        for raw in template.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = raw.strip()
            if stripped.startswith("#") or not stripped:
                out_lines.append(raw)
                continue
            if "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in data:
                    out_lines.append(f"{k}={_quote_env_value(data[k])}")
                    written.add(k)
                else:
                    out_lines.append(raw)
        for k, v in data.items():
            if k not in written:
                out_lines.append(f"{k}={_quote_env_value(v)}")
        content = "\n".join(out_lines) + "\n"
    else:
        content = "\n".join(f"{k}={_quote_env_value(v)}" for k, v in data.items()) + "\n"

    # Atomic write: write to sibling temp file then rename
    env_dir = ENV_FILE.parent
    fd, tmp_path = tempfile.mkstemp(dir=env_dir, suffix=".env.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, ENV_FILE)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# Hot-reload of saved .env values into the running WebUI process.
#
# Background. The import-time ``.env`` load (see ``runtime_logging``) covers the
# initial WebUI launch.  The remaining gap was: after editing settings *via the
# WebUI* and clicking Save, the in-process ``os.environ`` still held the
# previous values until the operator killed
# the WebUI and started it again.  In particular, the next spawned
# ``run_crucible.py`` subprocess inherits ``os.environ`` from the
# WebUI parent (see ``_run_worker``'s ``_child_env = {**os.environ, ...}``)
# — if the parent never refreshed, neither did the child, and the operator
# had to restart the entire stack just for a config change to take effect.
#
# The fix is symmetric and minimal: every successful POST /api/env pushes
# the saved keys back into ``os.environ`` so subsequent reads (in this
# process, in any thread, and in any subprocess spawned afterwards) see
# the just-saved values.  No file re-read is needed since we already have
# the validated payload in memory.  We also nudge Flask's ``app.logger``
# level if the operator changed ``CRUCIBLE_LOG_LEVEL`` so any logs the
# WebUI itself emits respect the new level immediately.  The third-party-
# noise pin (``CRUCIBLE_QUIET_THIRDPARTY``) and JSON-mode toggle
# (``CRUCIBLE_JSON_LOGS``) are deliberately *not* re-applied to the
# WebUI process — those are read at next subprocess spawn, which is when
# they actually matter.  Reconfiguring the WebUI's own root logger
# mid-flight risks breaking Flask's request-log handler and is out of
# scope for the user's reported issue (the operator's complaint targets
# the spawned-process terminal log shown in the WebUI, not the WebUI's
# own internal logs).
def _apply_env_to_process(data: dict[str, str]) -> None:
    """Mirror saved key/values into the running process's ``os.environ``.

    *data* is the validated payload from POST /api/env (every value is a
    string, every key is a non-empty identifier without ``=``/newline/null
    bytes — see ``api_set_env``'s validation block).  Empty-string values
    are written as-is to mirror what python-dotenv would do with
    ``override=True``: the file says ``KEY=""`` so the runtime sees
    ``os.environ["KEY"] == ""``.  Existing consumers already pattern-match
    this with ``os.environ.get(name, "").strip()`` followed by ``if not
    raw:`` checks, so a literal empty string is treated identically to an
    absent key by every downstream reader in this codebase.

    Thread safety: ``os.environ`` mutations are atomic at the per-key
    level on CPython (each ``__setitem__`` is one bytecode op).  Concurrent
    POSTs that happen to set the *same* key with *different* values would
    have a last-writer-wins outcome — which is exactly the same outcome
    they'd produce against the on-disk ``.env`` file, so this hook does
    not change any consistency guarantee.
    """
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        try:
            os.environ[k] = v
        except (TypeError, ValueError):
            # ``os.environ`` rejects keys/values containing the platform's
            # env-var separator (NUL on POSIX, '\x00' on Windows).  The
            # validation in ``api_set_env`` already screens these out, so
            # this branch is purely defensive — never crash a save just
            # because one key tripped a platform-specific edge case.
            continue

    # If the operator changed the log level, lift Flask's logger to match
    # so request logs honour the new threshold without a restart.  We
    # touch *only* ``app.logger`` (not the root logger) because Flask
    # owns its handler and any global change risks colliding with what
    # ``crucible.runtime_logging.configure_logging`` would do at
    # subprocess startup.
    if "CRUCIBLE_LOG_LEVEL" in data:
        raw = (data.get("CRUCIBLE_LOG_LEVEL") or "").strip().upper()
        try:
            import logging as _logging
            level = getattr(_logging, raw, None)
            if isinstance(level, int):
                app.logger.setLevel(level)
        except Exception:
            # Logger reconfiguration must never bubble up — the file
            # write already succeeded and the operator's change is
            # already live for every other consumer.
            pass


def _infer_type(val: str) -> str:
    if val in ("0", "1"):
        return "boolean"
    try:
        int(val)
        return "integer"
    except ValueError:
        pass
    try:
        float(val)
        return "float"
    except ValueError:
        pass
    return "string"


# ─── Input validation helpers ──────────────────────────────────────────────────

def _safe_int(v: Any, name: str) -> int:
    try:
        return int(v)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid integer for '{name}': {v!r}") from exc


def _safe_float(v: Any, name: str) -> float:
    try:
        return float(v)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid float for '{name}': {v!r}") from exc


# ─── Command builder ───────────────────────────────────────────────────────────

def _build_command(payload: dict[str, Any]) -> tuple[list[str], str]:
    """
    Converts UI payload → (subprocess_args, stdin_text).

    stdin_text feeds the pipeline's interactive prompts:
      Line 1: analysis type  (1=Quant, 2=SaaS, 3=Agent, 4=Scientist)
      Line 2: input mode     (1=Idea, 2=Project path)
      Line 3: idea text or project path
    """
    mode = payload.get("mode", "idea")
    flags: dict[str, Any] = payload.get("flags", {})

    # Validate and normalize analysis_type
    raw_at = payload.get("analysis_type", 1)
    try:
        analysis_type = int(raw_at)
        if analysis_type not in (1, 2, 3, 4):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(f"analysis_type must be 1, 2, 3, or 4 — got {raw_at!r}")

    # The core pipeline uses read_multiline_input() which reads sys.stdin.readline()
    # in a loop until it sees the sentinel "__END_PROMPT__" on its own line, or EOF.
    # stdin is kept open by _run_worker for Feature-1 human-in-the-loop, so EOF
    # never arrives naturally — we must send the sentinel explicitly.
    # Sequence for idea mode:
    #   line 1 : analysis type  → input("Select Mode")
    #   line 2 : "1"            → input("Enter 1 or 2 [Default: 1]")  (idea path)
    #   line 3 : idea text      → first line of read_multiline_input(required=True)
    #   line 4 : __END_PROMPT__ → terminates the required idea read
    #   line 5 : ""             → empty first line → skips optional extra_notes
    # Sequence for project mode:
    #   line 1 : analysis type  → input("Select Mode")
    #   line 2 : "2"            → input("Enter 1 or 2 [Default: 1]")  (project path)
    #   line 3 : project_path   → input("Enter project folder path")
    #   line 4 : "1"            → input("Enter 1 or 2 [Default: 1]")  (scan depth, quick)
    #   line 5 : ""             → empty first line → skips optional extra_notes
    _MULTILINE_TERM = "__END_PROMPT__"
    if mode == "project":
        project_path = str(payload.get("project_path", "")).strip()
        if not project_path:
            raise ValueError("project_path is required in project mode")
        # project_path is single-line input; reject embedded newlines/CR/null
        # bytes which would inject extra answers into the subprocess's
        # interactive prompt sequence (e.g. "/tmp/x\n2\n" would silently
        # change the analysis_type or scan-depth selection).
        if any(ch in project_path for ch in ("\n", "\r", "\x00")):
            raise ValueError(
                "project_path must not contain newline or null bytes"
            )
        stdin_text = f"{analysis_type}\n2\n{project_path}\n1\n\n"
    else:
        idea = str(payload.get("idea", "")).strip()
        if not idea:
            raise ValueError("idea text is required in idea mode")
        # idea text is multiline by design (terminated by __END_PROMPT__),
        # but reject the sentinel itself appearing inside the body so a
        # malicious payload cannot truncate the read prematurely.
        if _MULTILINE_TERM in idea or "\x00" in idea:
            raise ValueError(
                f"idea must not contain the {_MULTILINE_TERM} sentinel or null bytes"
            )
        stdin_text = f"{analysis_type}\n1\n{idea}\n{_MULTILINE_TERM}\n\n"

    python = sys.executable
    cmd: list[str] = [python, str(ENHANCED_RUNNER), "run"]

    # Select / radio flags
    if flags.get("provider"):
        cmd += ["--provider", str(flags["provider"])]
    if flags.get("runtime_profile"):
        cmd += ["--runtime-profile", str(flags["runtime_profile"])]
    if flags.get("scope"):
        cmd += ["--scope", str(flags["scope"])]

    # Project directory (project mode)
    if mode == "project" and payload.get("project_path"):
        cmd += ["--project-dir", str(payload["project_path"])]

    # Boolean flags
    BOOL_FLAGS: dict[str, str] = {
        "dry_run":               "--dry-run",
        "self_check":            "--self-check",
        "direction_debate":      "--direction-debate",
        "direction_debate_only": "--direction-debate-only",
        "strict_json":           "--strict-json",
        "cost_trace":            "--cost-trace",
        "cache":                 "--cache",
        "cost_report":           "--cost-report",
        "gate_control":          "--gate-control",
        "selective_rerun":       "--selective-rerun",
        "api_version_check":     "--api-version-check",
        "codegen_auto_optimize": "--codegen-auto-optimize",
        "diff_aware":            "--diff-aware",
        "use_memory":            "--use-memory",
        "security_scan":         "--security-scan",
        "deployment_artifacts":  "--deployment-artifacts",
        "generate_tests":        "--generate-tests",
        "api_autopatch":         "--api-autopatch",
        "independent_validation":"--independent-validation",
        "ci_output":             "--ci-output",
        "auto_remediation":      "--auto-remediation",
        "dependency_audit":      "--dependency-audit",
        "html_report":           "--html-report",
        "code_quality":          "--code-quality",
        "run_registry":          "--run-registry",
        "interactive":           "--interactive",
        "dedup_check":           "--dedup-check",
        "backtest_runner":       "--backtest-runner",
        "notify":                "--notify",
        "ingest_docs":           "--ingest-docs",
        "multilang_codegen":     "--multilang-codegen",
        "post_chat":             "--post-chat",
        "agent_metrics":         "--agent-metrics",
        # Quant Analytics Suite
        "quant_analytics":       "--quant-analytics",
        "walk_forward":          "--walk-forward",
        "significance_test":     "--significance-test",
        "regime_detection":      "--regime-detection",
        "factor_analysis":       "--factor-analysis",
        "transaction_cost":      "--transaction-cost",
        "monte_carlo":           "--monte-carlo",
        "tearsheet":             "--tearsheet",
        "signal_analysis":       "--signal-analysis",
        "risk_attribution":      "--risk-attribution",
        "cointegration":         "--cointegration",
        "dynamic_correlation":   "--dynamic-correlation",
        "lockfile_gen":          "--lockfile-gen",
    }
    # Core CLI flags defined as store_true only — no --no-<flag> counterpart
    # exists in run_crucible.py.  Emitting --no-<flag> for these would
    # be passed through to the core pipeline and cause "unrecognized arguments".
    # Enhanced flags (--use-memory etc.) all use BooleanOptionalAction so they
    # DO support --no- forms and must be emitted when the user disables them.
    _STORE_TRUE_ONLY: frozenset[str] = frozenset({
        "dry_run", "self_check", "direction_debate", "direction_debate_only",
        "strict_json", "cost_trace", "cache", "cost_report", "codegen_auto_optimize",
    })
    for key, flag in BOOL_FLAGS.items():
        val = flags.get(key)
        if val is True:
            cmd.append(flag)
        elif val is False and key not in _STORE_TRUE_ONLY:
            # Explicitly disabled — emit --no-flag to override any env-var
            # defaults (e.g. use_memory / security_scan / deployment_artifacts
            # default to True and would silently stay on if we omitted the flag).
            cmd.append("--no-" + flag.lstrip("-"))

    # Numeric flags — validated individually.
    # Use `is not None` (not truthy) so that a legitimate value of 0 is still forwarded.
    if flags.get("codegen_optimize_rounds") is not None:
        cmd += ["--codegen-optimize-rounds",
                str(_safe_int(flags["codegen_optimize_rounds"], "codegen_optimize_rounds"))]
    if flags.get("codegen_optimize_threshold") is not None:
        cmd += ["--codegen-optimize-threshold",
                str(_safe_float(flags["codegen_optimize_threshold"], "codegen_optimize_threshold"))]
    if flags.get("budget_soft_cost") is not None:
        cmd += ["--budget-soft-cost",
                str(_safe_float(flags["budget_soft_cost"], "budget_soft_cost"))]
    if flags.get("budget_hard_cost") is not None:
        cmd += ["--budget-hard-cost",
                str(_safe_float(flags["budget_hard_cost"], "budget_hard_cost"))]
    if flags.get("budget_max_tokens") is not None:
        cmd += ["--budget-max-tokens",
                str(_safe_int(flags["budget_max_tokens"], "budget_max_tokens"))]

    # String flags
    for flag_key, cli_flag in [
        ("github_repo",          "--github-repo"),
        ("multilang_langs",      "--multilang-langs"),
        ("external_data",        "--external-data"),
        ("external_symbols",     "--external-symbols"),
        ("external_start",       "--external-start"),
        ("external_end",         "--external-end"),
        ("ingest_docs_dir",      "--ingest-docs-dir"),
        ("diff_base_ref",        "--diff-base-ref"),
        ("prompt_version_label", "--prompt-version-label"),
        # Feature 6: per-stage model overrides — override per-stage LLM without
        # editing .env.  The UI collects these as free-text fields and sends them
        # in the flags dict; we forward them as CLI arguments to the runner.
        ("librarian_model",       "--librarian-model"),
        ("primary_model",         "--primary-model"),
        ("direction_judge_model", "--direction-judge-model"),
        # Quant Analytics string options
        ("regime_method",         "--regime-method"),
        # Feature Bundle
        ("v169_features",         "--v169-features"),
    ]:
        if flags.get(flag_key):
            cmd += [cli_flag, str(flags[flag_key])]

    return cmd, stdin_text


# ─── JSON file reader helper ────────────────────────────────────────────────────

def _apply_schema_compat(data: dict[str, Any]) -> dict[str, Any]:
    """Feature 8: Map legacy field names to canonical names for old (schema_version=None/"0") files.

    Always returns a shallow copy so callers can safely mutate the result
    without affecting the original parsed object (whether or not migration
    is needed).
    """
    # Shallow-copy unconditionally so the returned dict is always a distinct
    # object — regardless of schema_version — preventing aliasing bugs.
    data = dict(data)
    sv = data.get("schema_version")
    if sv not in (None, "0", 0):
        return data
    # Legacy field name migrations
    _LEGACY_MAP: dict[str, str] = {
        "win_loss_rate":  "win_rate",
        "return_pct":     "total_return_pct",
    }
    for old_key, new_key in _LEGACY_MAP.items():
        if old_key in data and new_key not in data:
            data[new_key] = data[old_key]
    return data


def _read_json_file(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON file; return None on any error or non-dict top level.

    Enforces the dict contract here so every caller can safely call .get()
    without an isinstance guard — a corrupt file whose top-level value is a
    list, scalar, or null would pass a truthiness check and cause AttributeError
    on the subsequent .get() call in every one of the ~8 callsites.

    Also applies Feature 8 schema compatibility mapping for legacy field names.
    """
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(data, dict):
                return None
            return _apply_schema_compat(data)
    except Exception:
        LOGGER.debug("[webui] swallowed exception", exc_info=True)
    return None


# ─── SQLite run index ───────────────────────────────────────────────────────────
#
# v1.0.3: Thread-local connection cache.  Previously every API route opened a
# new ``sqlite3.connect`` on each request, which under Flask's multi-threaded
# dev server (and gunicorn worker pool) re-executed all the schema-bootstrap
# DDL on every call — wasted work plus per-call connection overhead.  The
# cache below scopes one connection per worker thread; idempotent
# ``CREATE TABLE IF NOT EXISTS`` / ``ALTER TABLE`` statements still run once
# per thread to bootstrap the schema, but every subsequent request reuses
# the warm connection.
#
# SQLite connections are explicitly **not** thread-safe (the underlying
# ``sqlite3`` module raises ``ProgrammingError`` on cross-thread reuse), so
# ``threading.local()`` is the correct primitive: each worker thread gets its
# own independent connection.  WAL mode lets concurrent readers and a single
# writer make progress, and ``busy_timeout=20s`` on each connection absorbs
# transient lock contention.
#
# ``conn.close()`` is no longer called by request handlers — connections
# survive for the lifetime of the worker thread and are reaped at process
# exit by the ``atexit`` hook below.  Tests that need a clean slate can call
# :func:`_reset_db_threadlocal` between cases.

_DB_TLS = threading.local()


def _bootstrap_db_schema(conn: sqlite3.Connection) -> None:
    """Run idempotent DDL on a freshly-opened connection.

    Called once per thread (from :func:`_ensure_db` on first use).  Every
    statement here must be safe to re-run if a future thread inherits an
    already-bootstrapped database file.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id      TEXT PRIMARY KEY,
            mtime       REAL,
            cost        REAL,
            tokens      INTEGER,
            quality     REAL,
            mode        TEXT,
            provider    TEXT,
            timestamp   TEXT,
            has_backtest INTEGER DEFAULT 0,
            sharpe      REAL,
            drawdown    REAL,
            total_return REAL
        )
    """)
    # Feature 8: add schema_version column to existing databases (idempotent).
    # Only suppress the "duplicate column" variant of OperationalError; any
    # other OperationalError (locked DB, disk full, etc.) is re-raised so it
    # is visible in logs instead of silently corrupting the schema.
    try:
        conn.execute("ALTER TABLE runs ADD COLUMN schema_version TEXT")
    except sqlite3.OperationalError as _alter_exc:
        if "already exists" not in str(_alter_exc).lower() and \
                "duplicate column" not in str(_alter_exc).lower():
            raise

    # Feature 9: daily budget tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget_daily (
            date       TEXT PRIMARY KEY,
            total_cost REAL NOT NULL DEFAULT 0.0,
            run_count  INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Feature 10: webhook delivery history table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webhook_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            url         TEXT NOT NULL,
            status_code INTEGER,
            success     INTEGER NOT NULL DEFAULT 0,
            attempt     INTEGER NOT NULL DEFAULT 1,
            error_msg   TEXT
        )
    """)
    conn.commit()


def _ensure_db() -> sqlite3.Connection:
    """Return the per-thread SQLite connection, opening + bootstrapping on first use.

    Subsequent calls within the same thread return the cached connection
    without re-running DDL.  Callers must **not** ``conn.close()`` the
    returned object — it is shared with future requests on the same worker
    thread and is closed at interpreter shutdown.
    """
    cached: sqlite3.Connection | None = getattr(_DB_TLS, "conn", None)
    if cached is not None:
        return cached
    _RUN_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_RUN_INDEX_DB), timeout=10)
    try:
        _bootstrap_db_schema(conn)
    except Exception:
        # If bootstrap fails do not cache the half-initialised handle — the
        # next request will retry from scratch on a fresh connection.
        try:
            conn.close()
        except Exception:
            LOGGER.debug("[webui] swallowed exception", exc_info=True)
        raise
    _DB_TLS.conn = conn
    return conn


def _reset_db_threadlocal() -> None:
    """Close + drop the cached thread-local connection (test fixture helper).

    Production code should never call this — connections live for the
    thread's full lifetime.  Tests that swap out ``_RUN_INDEX_DB`` between
    cases need a clean handle, hence the public helper.
    """
    cached: sqlite3.Connection | None = getattr(_DB_TLS, "conn", None)
    if cached is None:
        return
    try:
        cached.close()
    except Exception:
        LOGGER.debug("[webui] swallowed exception", exc_info=True)
    if hasattr(_DB_TLS, "conn"):
        del _DB_TLS.conn


@atexit.register
def _close_db_at_exit() -> None:
    """Close the bootstrap thread's connection cleanly at interpreter exit.

    Worker-thread connections held in their own ``threading.local`` slots
    are closed by the OS when the threads terminate; we only get the
    main / current-thread slot here.  Best-effort — failures must never
    block process shutdown.
    """
    _reset_db_threadlocal()


def _extract_run_row(d: Path) -> dict[str, Any]:
    """Read all relevant JSON files for a run directory and return a DB row dict."""
    try:
        mtime = d.stat().st_mtime
    except OSError:
        mtime = 0.0
    row: dict[str, Any] = {
        "run_id": d.name,
        "mtime": mtime,
        "cost": None,
        "tokens": None,
        "quality": None,
        "mode": None,
        "provider": None,
        "timestamp": None,
        "has_backtest": 0,
        "sharpe": None,
        "drawdown": None,
        "total_return": None,
        "schema_version": None,
    }

    # analysis_result.json
    analysis = _read_json_file(d / "analysis_result.json")
    if analysis:
        # Feature 8: extract schema_version from analysis result
        sv = analysis.get("schema_version")
        if sv is not None:
            row["schema_version"] = str(sv)
        if row["cost"] is None:
            try:
                _c = float(analysis.get("total_cost"))  # type: ignore[arg-type]
                row["cost"] = _c if math.isfinite(_c) else None
            except (TypeError, ValueError):
                pass
        if row["tokens"] is None:
            try:
                _t = int(float(analysis.get("total_tokens")))  # type: ignore[arg-type]
                row["tokens"] = _t
            except (TypeError, ValueError):
                pass
        if row["quality"] is None:
            v = analysis.get("quality_score") if analysis.get("quality_score") is not None else analysis.get("score")
            try:
                fq = float(v) if v is not None else None
                row["quality"] = fq if (fq is None or math.isfinite(fq)) else None
            except (TypeError, ValueError):
                pass

    # run_meta.json
    meta = _read_json_file(d / "run_meta.json")
    if meta:
        row["mode"] = str(meta.get("mode") or "").lower() or None
        row["provider"] = meta.get("llm_provider") or None
        row["timestamp"] = meta.get("timestamp") or None
        if row["cost"] is None:
            try:
                _c = float(meta.get("total_cost"))  # type: ignore[arg-type]
                row["cost"] = _c if math.isfinite(_c) else None
            except (TypeError, ValueError):
                pass
        if row["tokens"] is None:
            try:
                _t = int(float(meta.get("total_tokens")))  # type: ignore[arg-type]
                row["tokens"] = _t
            except (TypeError, ValueError):
                pass

    # backtest_report.json / summary.json — backtest metrics.
    # summary.json may live in sample_out/ subdirectory for older runs.
    for bt_file in (
        "backtest_report.json",
        "summary.json",
        "sample_out/summary.json",
    ):
        bt = _read_json_file(d / bt_file)
        if bt:
            sharpe = bt.get("sharpe_ratio")
            # Accept both the canonical pct-field name and the legacy short alias
            # so that scripts using either "max_drawdown_pct" or "max_drawdown" work.
            dd = bt.get("max_drawdown_pct") if "max_drawdown_pct" in bt else bt.get("max_drawdown")
            ret = bt.get("total_return_pct") if "total_return_pct" in bt else bt.get("total_return")
            # Accept the file if any of the three key metrics exist
            if any(v is not None for v in (sharpe, dd, ret)):
                row["has_backtest"] = 1
                if row["sharpe"] is None and sharpe is not None:
                    try:
                        fv = float(sharpe)
                        row["sharpe"] = fv if math.isfinite(fv) else None
                    except (TypeError, ValueError):
                        pass
                if row["drawdown"] is None and dd is not None:
                    try:
                        fv = float(dd)
                        row["drawdown"] = fv if math.isfinite(fv) else None
                    except (TypeError, ValueError):
                        pass
                if row["total_return"] is None and ret is not None:
                    try:
                        fv = float(ret)
                        row["total_return"] = fv if math.isfinite(fv) else None
                    except (TypeError, ValueError):
                        pass
                break

    return row


def _sync_run_index() -> None:
    """
    Synchronise the SQLite run index with the filesystem.
    Upserts changed/new run directories and removes rows for deleted directories.
    Rate-limited to at most once every _SYNC_INTERVAL seconds via _SYNC_LOCK.
    """
    global _last_sync_time
    # Acquire non-blocking: if another thread is already syncing, skip entirely.
    # The interval check is done INSIDE the lock so that _last_sync_time is
    # always read and written while the lock is held — eliminating the TOCTOU
    # race of the previous double-checked pattern.
    if not _SYNC_LOCK.acquire(blocking=False):
        return  # Another thread is syncing
    try:
        if time.time() - _last_sync_time < _SYNC_INTERVAL:
            return  # Within rate-limit window; skip sync
        if not SAVED_PROJECTS_DIR.exists():
            return
        conn = _ensure_db()
        try:
            # Current dirs on disk
            dirs: dict[str, float] = {}
            for d in SAVED_PROJECTS_DIR.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    try:
                        dirs[d.name] = d.stat().st_mtime
                    except OSError:
                        pass

            # Current DB state
            existing: dict[str, float] = {
                row[0]: row[1]
                for row in conn.execute("SELECT run_id, mtime FROM runs").fetchall()
            }

            # Upsert changed or new entries — isolate exceptions per run so one
            # bad directory never aborts the entire sync.
            for run_id, mtime in dirs.items():
                db_mtime = existing.get(run_id)
                if db_mtime is not None and abs(db_mtime - mtime) < 0.01:
                    continue  # No change
                try:
                    d = SAVED_PROJECTS_DIR / run_id
                    row = _extract_run_row(d)
                    conn.execute("""
                        INSERT OR REPLACE INTO runs
                            (run_id, mtime, cost, tokens, quality, mode, provider, timestamp,
                             has_backtest, sharpe, drawdown, total_return, schema_version)
                        VALUES
                            (:run_id, :mtime, :cost, :tokens, :quality, :mode, :provider, :timestamp,
                             :has_backtest, :sharpe, :drawdown, :total_return, :schema_version)
                    """, row)
                    # Feature 9: record cost in budget_daily whenever a new run is upserted
                    if row.get("cost") is not None:
                        try:
                            _record_run_cost(float(row["cost"]), conn=conn)
                        except Exception:
                            LOGGER.debug("[webui] swallowed exception", exc_info=True)
                except Exception:
                    LOGGER.debug("[webui] swallowed exception", exc_info=True)  # Skip this run; continue syncing others

            # Delete rows for directories that no longer exist
            stale = set(existing.keys()) - set(dirs.keys())
            if stale:
                conn.executemany(
                    "DELETE FROM runs WHERE run_id = ?",
                    [(r,) for r in stale]
                )

            conn.commit()
        except Exception:
            # Roll back any partial writes so the DB stays consistent before
            # the connection is closed.  The outer except re-catches this as a
            # non-fatal sync failure.
            try:
                conn.rollback()
            except Exception:
                LOGGER.debug("[webui] swallowed exception", exc_info=True)
            raise
        _last_sync_time = time.time()
    except Exception:
        # Non-fatal: fall back to filesystem scan.
        # Update _last_sync_time even on failure so the rate-limit guard
        # (_SYNC_INTERVAL) prevents hammering a broken DB (locked, disk-full,
        # etc.) on every subsequent API call that triggers _scan_saved_runs.
        _last_sync_time = time.time()
    finally:
        _SYNC_LOCK.release()


# ─── Budget helpers (Feature 9) ───────────────────────────────────────────────

def _record_run_cost(cost: float, conn: sqlite3.Connection | None = None) -> None:
    """Upsert today's cost into budget_daily.  If *conn* is supplied (already open),
    the caller is responsible for committing; otherwise a new connection is opened and
    committed here.

    Silently ignores non-finite or negative cost values to prevent corrupting the
    budget ledger with nonsensical data (e.g. NaN, Inf, or a pipeline bug returning
    a negative cost).
    """
    if not math.isfinite(cost) or cost < 0:
        return
    today = time.strftime("%Y-%m-%d")
    own_conn = conn is None
    if own_conn:
        conn = _ensure_db()
    conn.execute(
        """
        INSERT INTO budget_daily (date, total_cost, run_count)
        VALUES (?, ?, 1)
        ON CONFLICT(date) DO UPDATE SET
            total_cost = total_cost + excluded.total_cost,
            run_count  = run_count  + 1
        """,
        (today, float(cost)),
    )
    if own_conn:
        # v1.0.3: connection is now thread-local-cached; we keep the commit
        # here for the *own_conn* branch since callers in the supplied-conn
        # branch are still expected to commit at their own checkpoint.
        conn.commit()


# ─── Filesystem helpers ───────────────────────────────────────────────────────

def _safe_mtime(p: Path) -> float:
    """Return mtime or 0.0 — guards against TOCTOU deletion between iterdir() and stat()."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ─── Saved-projects scanner ────────────────────────────────────────────────────

def _scan_saved_runs(
    limit: int = 50,
    query: str | None = None,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return list of run summary dicts, sorted newest-first.
    Tries SQLite index first; falls back to raw filesystem scan on failure.
    Supports optional substring search on run_id (query) and mode filter.
    """
    # Attempt SQLite-backed fast path
    try:
        _sync_run_index()
        conn = _ensure_db()
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            clauses.append("run_id LIKE ?")
            params.append(f"%{query}%")
        if mode:
            clauses.append("LOWER(mode) = ?")
            params.append(mode.lower())
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"""
            SELECT run_id, mtime, cost, tokens, quality, mode, provider, timestamp,
                   has_backtest, sharpe, drawdown, total_return
            FROM runs
            {where}
            ORDER BY mtime DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            result.append({
                "id":          row[0],
                "name":        row[0],
                "mtime":       row[1],
                "cost":        row[2],
                "tokens":      row[3],
                "quality":     row[4],
                "mode":        row[5],
                "provider":    row[6],
                "timestamp":   row[7],
                "has_backtest":row[8],
                "sharpe":      row[9],
                "drawdown":    row[10],
                "total_return":row[11],
            })
        return result
    except Exception:
        # Fall back to filesystem scan (no SQLite filtering support in fallback)
        pass

    # Filesystem fallback (no query/mode filtering in fallback path)
    runs: list[dict[str, Any]] = []
    if not SAVED_PROJECTS_DIR.exists():
        return runs

    entries = sorted(
        SAVED_PROJECTS_DIR.iterdir(),
        key=_safe_mtime,
        reverse=True,
    )
    for d in entries:
        if not d.is_dir() or d.name.startswith("."):
            continue
        if query and query.lower() not in d.name.lower():
            continue
        row = _extract_run_row(d)
        if mode and (row.get("mode") or "").lower() != mode.lower():
            continue
        runs.append({
            "id":          row["run_id"],
            "name":        row["run_id"],
            "mtime":       row["mtime"],
            "cost":        row["cost"],
            "tokens":      row["tokens"],
            "quality":     row["quality"],
            "mode":        row["mode"],
            "provider":    row["provider"],
            "timestamp":   row["timestamp"],
            "has_backtest":row["has_backtest"],
            "sharpe":      row["sharpe"],
            "drawdown":    row["drawdown"],
            "total_return":row["total_return"],
        })
        if len(runs) >= limit:
            break
    return runs


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index() -> str:
    return render_template("index.html", webui_url=os.environ.get("WEBUI_URL", ""))


# ── Env ───────────────────────────────────────────────────────────────────────

@app.route("/api/env", methods=["GET"])
def api_get_env():
    return jsonify(_load_env())


@app.route("/api/env", methods=["POST"])
def api_set_env():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    for k, v in data.items():
        if not isinstance(k, str) or not k or "=" in k or "\n" in k or "\r" in k or "\x00" in k:
            return jsonify({"error": f"Invalid key name: {k!r}"}), 400
        if not isinstance(v, str):
            return jsonify({"error": f"Value for '{k}' must be a string, got {type(v).__name__}"}), 400
        if "\n" in v or "\r" in v or "\x00" in v:
            return jsonify({"error": f"Value for '{k}' must not contain newlines or null bytes"}), 400
    try:
        _save_env(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    # Hot-reload the saved values into this process's os.environ so the
    # change takes effect without a WebUI restart.  Subsequent
    # subprocess spawns inherit os.environ via ``_child_env``, so the
    # next pipeline run picks up the new settings immediately.  We do
    # this *after* ``_save_env`` succeeds so a failed file write never
    # leaves the process state ahead of disk.
    _apply_env_to_process(data)
    return jsonify({"success": True})


@app.route("/api/env/schema")
def api_env_schema():
    if not ENV_EXAMPLE.exists():
        return jsonify({})
    groups: dict[str, list[dict]] = {}
    current_group = "General"
    try:
        _env_text = ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return jsonify({})
    for raw in _env_text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if text and 1 <= len(text.split()) <= 6 and not text.startswith("="):
                current_group = text
        elif "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            groups.setdefault(current_group, []).append(
                {"key": k, "default": v.strip(), "type": _infer_type(v.strip())}
            )
    return jsonify(groups)


# ── Shared run worker (eliminates duplication between api_start_run and webhook_trigger) ──

# Regex for detecting AWAIT_INPUT protocol markers emitted by the pipeline
_AWAIT_INPUT_RE = re.compile(r'\[AWAIT_INPUT(?::([^\]]*))?\]')
# Regex for detecting stage markers
_STAGE_MARKER_RE = re.compile(r'\[Stage\s+(\d+)\b|stage=(\w+)|^=+\s*Stage\s+(\d+)', re.IGNORECASE)
# Regex for token count lines
_TOKEN_COUNT_RE = re.compile(r'tokens?[:\s]+(\d+)', re.IGNORECASE)


def _run_worker(run_id: str, cmd: list[str], stdin_text: str) -> None:
    """Shared worker function for starting a subprocess, streaming its output into
    ``_runs[run_id]``, and handling all lifecycle state transitions.

    This function is the single canonical implementation; both ``api_start_run``
    and ``webhook_trigger`` launch it via ``threading.Thread``.

    Features implemented here:
      - Feature 1: AWAIT_INPUT protocol detection, stdin_pipe storage
      - Feature 2: stage timing + token count tracking
    """
    proc: "subprocess.Popen[str] | None" = None
    try:
        # Force UTF-8 I/O on the child Python process so that its stdout
        # is always UTF-8-encoded, matching the encoding="utf-8" we use
        # when reading back.  Without this, Windows uses the console
        # code-page (cp950 / cp936) which produces mojibake in the WebUI.
        _child_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_ROOT),
            bufsize=1,
            env=_child_env,
        )
        with _runs_lock:
            run_rec = _runs.get(run_id)
            if run_rec is not None:
                run_rec["process"] = proc
                run_rec["status"] = "running"

        # Feed interactive prompts — we keep stdin open (store the pipe reference
        # for Feature 1 human-in-the-loop), writing the initial prompt text.
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
        except Exception as write_exc:
            with _runs_lock:
                run_rec = _runs.get(run_id)
                if run_rec is not None:
                    run_rec["output"].append(
                        f"[WARN] Could not write to process stdin: {write_exc}"
                    )

        # Feature 1: store the open stdin pipe so that the /signal endpoint can
        # write to it later.  We do NOT close stdin here — the process may need
        # to receive further input.  The pipe is closed when we detect that the
        # process no longer needs input (process exits or stdin breaks).
        with _runs_lock:
            run_rec = _runs.get(run_id)
            if run_rec is not None:
                run_rec["stdin_pipe"] = proc.stdin

        # Stream stdout line by line — no hard cap; output grows as needed.
        # Noisy SDK debug lines (httpx wire logs, LiteLLM internals, etc.)
        # are suppressed at the frontend display layer, not here, so the
        # full output is always available for post-run inspection.
        # Guard against KeyError: _evict_stale_runs() (fired from the SSE
        # generator's `finally` block after client disconnect) can remove
        # the run_id entry while the process is still producing output.
        line_index = 0
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            with _runs_lock:
                run_rec = _runs.get(run_id)
                if run_rec is None:
                    line_index += 1
                    continue
                run_rec["output"].append(stripped)

                # Feature 1: detect AWAIT_INPUT marker
                m_await = _AWAIT_INPUT_RE.search(stripped)
                if m_await:
                    run_rec["awaiting_input"] = True
                    run_rec["input_prompt"] = (m_await.group(1) or "").strip()

                # Feature 2: detect stage markers
                m_stage = _STAGE_MARKER_RE.search(stripped)
                if m_stage:
                    stage_id: str = (
                        m_stage.group(1) or m_stage.group(2) or m_stage.group(3) or ""
                    )
                    stages: list[dict[str, Any]] = run_rec.setdefault("stages", [])
                    now_ts = time.time()
                    # Close the previous open stage
                    if stages and stages[-1].get("ended_at") is None:
                        stages[-1]["ended_at"] = now_ts
                    stages.append({
                        "stage_id":   stage_id,
                        "label":      stripped[:120],
                        "start_line": line_index,
                        "started_at": now_ts,
                        "ended_at":   None,
                    })

                # Feature 2: detect token counts
                m_tok = _TOKEN_COUNT_RE.search(stripped)
                if m_tok:
                    try:
                        tok_count = int(m_tok.group(1))
                        tok_dict: dict[str, int] = run_rec.setdefault("token_counts", {})
                        agent_key = f"line_{line_index}"
                        tok_dict[agent_key] = tok_count
                    except ValueError:
                        pass

            line_index += 1

        proc.wait()
        # stdin/stdout pipes are closed in the finally block below, which
        # covers both the normal exit path and all exception paths.

        with _runs_lock:
            run_rec = _runs.get(run_id)
            if run_rec is not None:
                # Feature 2: close the final open stage
                stages = run_rec.get("stages", [])
                if stages and stages[-1].get("ended_at") is None:
                    stages[-1]["ended_at"] = time.time()
                # Preserve "cancelled" status that api_kill_run may have set
                # while the process was still winding down.
                if run_rec["status"] != "cancelled":
                    run_rec["status"] = "done" if proc.returncode == 0 else "error"
                # Only overwrite ended_at/returncode if api_kill_run has not
                # already set them — avoids restarting the eviction TTL clock.
                if run_rec.get("ended_at") is None:
                    run_rec["ended_at"] = time.time()
                if run_rec.get("returncode") is None:
                    run_rec["returncode"] = proc.returncode
                # Feature 1: clear awaiting_input state on process exit
                run_rec["awaiting_input"] = False

    except Exception as exc:
        with _runs_lock:
            run_rec = _runs.get(run_id)
            if run_rec is not None:
                if run_rec["status"] != "cancelled":
                    run_rec["status"] = "error"
                run_rec["output"].append(f"[WEBUI ERROR] {exc}")
                if run_rec.get("ended_at") is None:
                    run_rec["ended_at"] = time.time()
                if run_rec.get("returncode") is None:
                    run_rec["returncode"] = -1
                run_rec["awaiting_input"] = False
    finally:
        # Ensure both stdin and stdout pipes are always closed, even when an
        # exception short-circuits the normal proc.wait() path above.
        # Closing stdout is important to prevent file-descriptor exhaustion
        # over long-lived server processes with many sequential runs.
        if proc is not None:
            try:
                proc.stdin.close()
            except Exception:
                LOGGER.debug("[webui] swallowed exception", exc_info=True)
            try:
                proc.stdout.close()
            except Exception:
                LOGGER.debug("[webui] swallowed exception", exc_info=True)


# ── Run management ────────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def api_start_run():
    payload = request.get_json(silent=True) or {}
    run_id = uuid.uuid4().hex[:8]

    try:
        cmd, stdin_text = _build_command(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with _runs_lock:
        _runs[run_id] = {
            "id": run_id,
            "cmd": cmd,
            "stdin": stdin_text,
            "status": "starting",
            "output": [],
            "started_at": time.time(),
            "ended_at": None,
            "returncode": None,
            "process": None,
            # Feature 1 fields
            "stdin_pipe":     None,
            "awaiting_input": False,
            "input_prompt":   "",
            # Feature 2 fields
            "stages":       [],
            "token_counts": {},
            # A/B test back-reference (None for standalone runs)
            "ab_id":        None,
        }

    threading.Thread(target=_run_worker, args=(run_id, cmd, stdin_text), daemon=True).start()
    return jsonify({"run_id": run_id, "cmd": " ".join(cmd)})


@app.route("/api/run/<run_id>")
def api_get_run(run_id: str):
    with _runs_lock:
        run = _runs.get(run_id)
        if not run:
            return jsonify({"error": "Not found"}), 404
        # Snapshot all mutable fields under the lock to avoid races with _run_worker
        _snap = {
            "id":             run["id"],
            "status":         run["status"],
            "output":         list(run["output"]),
            "started_at":     run["started_at"],
            "ended_at":       run["ended_at"],
            "returncode":     run["returncode"],
            "awaiting_input": run.get("awaiting_input", False),
            "input_prompt":   run.get("input_prompt", ""),
        }
    _evict_stale_runs(skip_run_id=run_id)
    return jsonify(_snap)


@app.route("/api/run/<run_id>/stream")
def api_stream_run(run_id: str):
    """
    Server-Sent Events stream of run output.
    - Supports ?from=N to resume from line N (avoids replaying already-sent lines
      after a watchdog-triggered reconnect).
    - Terminates cleanly when the run finishes (done/error/cancelled).
    - Terminates with a timeout event after 30 min of no new output.
    - Handles client disconnect via GeneratorExit.
    """
    try:
        resume_from = max(0, int(request.args.get("from", 0)))
    except (ValueError, TypeError):
        resume_from = 0

    def _generate():
        sent = resume_from
        idle_ticks = 0
        try:
            while True:
                # ── Snapshot run state under lock ─────────────────────────
                # The worker thread writes run["output"] and run["status"]
                # concurrently.  Taking a snapshot inside the lock prevents
                # data races (premature __done__, lost output lines).
                with _runs_lock:
                    run = _runs.get(run_id)
                    if run is not None:
                        new_lines = run["output"][sent:]
                        run_status = run["status"]
                        run_rc = run.get("returncode", -1)
                    else:
                        new_lines = []
                        run_status = None
                        run_rc = -1

                if run is None:
                    yield f"data: {json.dumps({'error': 'Run not found'})}\n\n"
                    return

                if new_lines:
                    for line in new_lines:
                        yield f"data: {json.dumps(line)}\n\n"
                    sent += len(new_lines)
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    # Keepalive ping: sent as a real data event so EventSource.onmessage
                    # fires on the frontend and resets the watchdog timer.  An SSE comment
                    # (": keepalive") would NOT trigger onmessage and would therefore fail
                    # to prevent the 10-min watchdog from firing during long LLM calls.
                    if idle_ticks % _SSE_KEEPALIVE_TICKS == 0:
                        yield f"data: {json.dumps({'__keepalive__': True})}\n\n"

                # Idle timeout: 30 min with no new output (run is hung)
                if idle_ticks >= _SSE_MAX_IDLE_TICKS:
                    yield (
                        f"data: {json.dumps({'__done__': True, 'returncode': -999, 'timeout': True})}\n\n"
                    )
                    return

                # Normal termination: run finished and all output sent
                if run_status in ("done", "error", "cancelled"):
                    if not new_lines:
                        yield f"data: {json.dumps({'__done__': True, 'returncode': run_rc})}\n\n"
                        return

                time.sleep(_SSE_POLL_INTERVAL)
                _evict_stale_runs(skip_run_id=run_id)

        except GeneratorExit:
            # Client disconnected.  DO NOT terminate the subprocess here —
            # the frontend's SSE reconnect logic ([Reconnecting in 2s…]) would
            # otherwise race against `proc.terminate()` and a 2-second network
            # hiccup mid-pipeline (e.g. Wi-Fi handoff, browser tab visibility
            # change, proxy keep-alive drop) would kill an hour-long run and
            # surface as "❌ Run exited with code 1" on reconnect.
            #
            # The subprocess is safely reclaimed by three other paths:
            #   1. `DELETE /api/run/<id>` — explicit user-initiated cancel.
            #   2. The `@atexit` handler (_cleanup_all_runs) — terminates any
            #      still-alive processes when Flask itself exits.
            #   3. The subprocess's own natural exit when the pipeline finishes.
            #
            # If a browser tab is genuinely closed, the run will continue to
            # completion and its output is preserved in `_runs[run_id]["output"]`
            # until _evict_stale_runs() prunes it by TTL.  This is the right
            # trade-off: orphan one successful run versus aborting a
            # legitimate 45-min direction-debate over a 2-second reconnect.
            return
        finally:
            # One final eviction pass after the stream ends (normal completion
            # or client disconnect).  No skip_run_id: this run_id is no longer
            # being actively streamed, so its output buffer is now eligible for
            # the TTL-based pruning on the next cycle that finds it old enough.
            _evict_stale_runs()

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/run/<run_id>/status")
def api_run_status(run_id: str):
    """Lightweight status-only endpoint used by the SSE reconnect/watchdog logic."""
    with _runs_lock:
        run = _runs.get(run_id)
        if not run:
            return jsonify({"error": "Not found"}), 404
        _snap = {
            "id":         run["id"],
            "status":     run["status"],
            "returncode": run["returncode"],
            "ended_at":   run["ended_at"],
        }
    return jsonify(_snap)


@app.route("/api/run/<run_id>", methods=["DELETE"])
def api_kill_run(run_id: str):
    # Atomically check status and mark cancelled *inside the lock* to avoid a
    # race where the worker thread sets status="done" between our read and our
    # write, which would incorrectly overwrite "done" with "cancelled".
    proc_to_kill: subprocess.Popen | None = None
    with _runs_lock:
        run = _runs.get(run_id)
        if not run:
            return jsonify({"error": "Not found"}), 404
        if run.get("process") and run["status"] in ("running", "starting"):
            proc_to_kill = run["process"]
            run["status"] = "cancelled"
            # Set ended_at and returncode eagerly so that _evict_stale_runs() can
            # start the TTL clock even if the worker thread is slow to terminate.
            # The worker will overwrite both with the actual process values once
            # it observes the process exit, which is safe and expected.
            if run.get("ended_at") is None:
                run["ended_at"] = time.time()
            if run.get("returncode") is None:
                run["returncode"] = -1
    # Terminate outside the lock so we don't hold it during a blocking syscall
    if proc_to_kill:
        try:
            proc_to_kill.terminate()
        except Exception:
            LOGGER.debug("[webui] swallowed exception", exc_info=True)
    return jsonify({"success": True})


# ── Run detail endpoint ───────────────────────────────────────────────────────

@app.route("/api/run/<run_id>/detail")
def api_run_detail(run_id: str):
    """
    Returns full content of JSON report files and a list of code files for a run.
    Response: {
        "files": {
            "analysis": {...} | null,
            "meta": {...} | null,
            "review": {...} | null,
            "security": {...} | null,
            "validation": {...} | null,
            "backtest": {...} | null,
        },
        "code_files": ["main.py", ...]
    }
    """
    run_dir = SAVED_PROJECTS_DIR / run_id
    # Reject path-traversal attempts (e.g. run_id="..") before any I/O.
    # Flask's <string> converter blocks slashes but allows dots, so ".."
    # would resolve to the parent of SAVED_PROJECTS_DIR.
    try:
        run_dir.resolve().relative_to(SAVED_PROJECTS_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid run_id"}), 400
    if not run_dir.exists() or not run_dir.is_dir():
        return jsonify({"error": "Run not found"}), 404

    file_map = {
        "analysis":   "analysis_result.json",
        "meta":       "run_meta.json",
        "review":     "review_report.json",
        "security":   "security_report.json",
        "validation": "independent_validation_report.json",
        "backtest":   "backtest_report.json",
    }

    files: dict[str, Any] = {}
    for key, filename in file_map.items():
        files[key] = _read_json_file(run_dir / filename)

    # List code files
    code_dir = run_dir / "code"
    code_files: list[str] = []
    if code_dir.exists() and code_dir.is_dir():
        try:
            code_files = sorted(
                f.name for f in code_dir.iterdir()
                if f.is_file() and not f.is_symlink()
            )
        except OSError:
            pass

    return jsonify({"files": files, "code_files": code_files})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/api/dashboard")
def api_dashboard():
    saved = _scan_saved_runs(50)
    # Exclude NaN/Inf: float('nan') is not None passes the None check but
    # poisons sum() → NaN, which jsonify() serialises as the invalid JSON
    # token NaN (not null), breaking JSON.parse() in the browser.
    total_cost = sum(
        r["cost"] for r in saved
        if r["cost"] is not None and math.isfinite(r["cost"])
    )
    quality_vals = [
        r["quality"] for r in saved
        if r["quality"] is not None and math.isfinite(r["quality"])
    ]
    avg_quality = sum(quality_vals) / len(quality_vals) if quality_vals else 0.0

    with _runs_lock:
        session_runs = [
            {"id": r["id"], "status": r["status"], "started_at": r["started_at"]}
            for r in _runs.values()
        ]

    return jsonify({
        "total_saved_runs": len(saved),
        "total_cost": round(total_cost, 5),
        "avg_quality": round(avg_quality, 3),
        "saved_runs": saved[:20],
        "session_runs": session_runs,
    })


@app.route("/api/runs")
def api_runs():
    try:
        raw_limit = request.args.get("limit", 30)
        limit = max(1, min(int(raw_limit), 200))
    except (ValueError, TypeError):
        limit = 30

    query = (request.args.get("q") or "").strip() or None
    mode  = (request.args.get("mode") or "").strip().lower() or None

    return jsonify(_scan_saved_runs(limit=limit, query=query, mode=mode))


# ── Leaderboard ───────────────────────────────────────────────────────────────

# Metrics where lower is better (ascending sort)
_ASCENDING_METRICS = {"max_drawdown"}

# Map URL param names to DB column / JSON key names
_LEADERBOARD_METRIC_COLUMNS = {
    "sharpe_ratio":   "sharpe",
    "max_drawdown":   "drawdown",
    "total_return":   "total_return",
    "win_rate":       None,   # not stored in DB, read from JSON
    "trade_count":    None,   # not stored in DB, read from JSON
    "profit_factor":  None,   # not stored in DB, read from JSON
}


@app.route("/api/leaderboard")
def api_leaderboard():
    """
    Return a ranked list of runs sorted by backtest performance metric.
    Query params:
      limit   — max results (default 50, max 200)
      sort_by — metric to rank by: sharpe_ratio, max_drawdown, total_return,
                win_rate, trade_count, profit_factor  (default: sharpe_ratio)
      mode    — filter by pipeline mode
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except (ValueError, TypeError):
        limit = 50

    sort_by = (request.args.get("sort_by") or "sharpe_ratio").strip().lower()
    if sort_by not in _LEADERBOARD_METRIC_COLUMNS:
        return jsonify({"error": f"Invalid sort_by metric: {sort_by!r}"}), 400

    mode_filter = (request.args.get("mode") or "").strip().lower() or None

    if not SAVED_PROJECTS_DIR.exists():
        return jsonify({"runs": [], "total": 0})

    # Collect all runs with backtest data
    entries: list[dict[str, Any]] = []

    try:
        dirs = sorted(
            SAVED_PROJECTS_DIR.iterdir(),
            key=_safe_mtime,
            reverse=True,
        )
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500

    for d in dirs:
        if not d.is_dir() or d.name.startswith("."):
            continue

        # Read backtest metrics from backtest_report.json and summary.json.
        # summary.json may live in sample_out/ subdirectory for older runs.
        bt_data: dict[str, Any] = {}
        for bt_file in (
            "backtest_report.json",
            "summary.json",
            "sample_out/summary.json",
        ):
            bt = _read_json_file(d / bt_file)
            # Guard against corrupt JSON whose top-level value is not a dict
            # (e.g. a list or scalar) — calling .get() on a non-dict raises
            # AttributeError and crashes the leaderboard endpoint.
            if bt and isinstance(bt, dict):
                bt_data = bt
                break

        if not bt_data:
            continue

        # Must have at least one backtest metric — check both canonical pct-field
        # names and legacy short aliases so either file format passes the gate.
        metric_keys = ("sharpe_ratio", "max_drawdown", "max_drawdown_pct",
                       "total_return", "total_return_pct",
                       "win_rate", "trade_count", "profit_factor")
        if not any(bt_data.get(k) is not None for k in metric_keys):
            continue

        # Read analysis score and mode/provider from other files
        analysis = _read_json_file(d / "analysis_result.json") or {}
        meta = _read_json_file(d / "run_meta.json") or {}

        run_mode = str(meta.get("mode") or analysis.get("mode_used") or "").lower()
        if mode_filter and run_mode != mode_filter:
            continue

        score_raw = analysis.get("score")
        try:
            _s = float(score_raw) if score_raw is not None else None
            score = _s if (_s is None or math.isfinite(_s)) else None
        except (TypeError, ValueError):
            score = None

        def _flt(val: Any) -> float | None:
            try:
                fv = float(val) if val is not None else None
                # NaN/Inf are not valid JSON; filter them out here so the
                # leaderboard response never contains bare NaN/Infinity tokens.
                return fv if (fv is None or math.isfinite(fv)) else None
            except (TypeError, ValueError):
                return None

        entry: dict[str, Any] = {
            "run_id":       d.name,
            "mode":         run_mode or "unknown",
            "provider":     meta.get("llm_provider") or "unknown",
            "score":        score,
            "sharpe_ratio": _flt(bt_data.get("sharpe_ratio")),
            "max_drawdown": _flt(bt_data.get("max_drawdown_pct") if "max_drawdown_pct" in bt_data else bt_data.get("max_drawdown")),
            "total_return": _flt(bt_data.get("total_return_pct") if "total_return_pct" in bt_data else bt_data.get("total_return")),
            "win_rate":     _flt(bt_data.get("win_rate")),
            "trade_count":  _flt(bt_data.get("trade_count")),
            "profit_factor":_flt(bt_data.get("profit_factor")),
        }
        entries.append(entry)

    # Sort by requested metric
    ascending = sort_by in _ASCENDING_METRICS

    def _sort_key(e: dict[str, Any]) -> float:
        v = e.get(sort_by)
        if v is None:
            # Push None to the end regardless of sort direction.
            # ascending=True  → reverse=False → last = largest  → float("inf")
            # ascending=False → reverse=True  → last = smallest → float("-inf")
            return float("inf") if ascending else float("-inf")
        return v

    entries.sort(key=_sort_key, reverse=(not ascending))
    entries = entries[:limit]

    # Add rank
    for i, e in enumerate(entries, 1):
        e["rank"] = i

    return jsonify({"runs": entries, "total": len(entries), "sort_by": sort_by})


# ── Cost trend ────────────────────────────────────────────────────────────────

@app.route("/api/cost-trend")
def cost_trend() -> Response:
    """
    Return per-run cost/score trend data from saved_projects/.

    Query params:
      limit  — max runs to return (default 30, max 100)
      mode   — filter by pipeline mode (optional)

    Response: { "runs": [ { "run_id", "timestamp", "score", "mode", "provider" }, ... ] }
    Sorted oldest → newest for charting.
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 30)), 100))
    except (ValueError, TypeError):
        limit = 30
    mode_filter = (request.args.get("mode") or "").strip().lower()

    saved_dir = SAVED_PROJECTS_DIR
    if not saved_dir.exists():
        return jsonify({"runs": [], "error": "saved_projects directory not found"})

    runs = []
    try:
        entries = sorted(saved_dir.iterdir(), key=_safe_mtime)
    except OSError as exc:
        return jsonify({"runs": [], "error": str(exc)})

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        analysis_file = entry / "analysis_result.json"
        meta_file = entry / "run_meta.json"
        if not analysis_file.exists():
            continue
        try:
            analysis = _read_json_file(analysis_file)
            if not analysis:
                continue
            meta: dict = _read_json_file(meta_file) or {}

            run_mode = str(meta.get("mode") or analysis.get("mode_used") or "").lower()
            if mode_filter and run_mode and run_mode != mode_filter:
                continue

            score_raw = analysis.get("score")
            try:
                _s = float(score_raw) if score_raw is not None else None
                score = _s if (_s is None or math.isfinite(_s)) else None
            except (TypeError, ValueError):
                score = None

            runs.append({
                "run_id": entry.name,
                "timestamp": meta.get("timestamp") or analysis.get("timestamp") or "",
                "score": score,
                "mode": meta.get("mode") or analysis.get("mode_used") or "unknown",
                "provider": meta.get("llm_provider") or "unknown",
                "risk_level": analysis.get("risk_level") or "unknown",
                "gate_decision": analysis.get("gate_decision") or "unknown",
            })
        except (OSError, TypeError, ValueError):
            continue

    # Sort oldest→newest, then apply limit (keep the most recent `limit` entries)
    runs.sort(key=lambda r: r["timestamp"])
    runs = runs[-limit:]

    return jsonify({"runs": runs, "total_found": len(runs)})


# ── Webhook ───────────────────────────────────────────────────────────────────

def _verify_webhook_signature(secret: str, payload_bytes: bytes, header_sig: str) -> bool:
    """
    Validate HMAC-SHA256 signature from X-Webhook-Signature header.
    Expected format: "sha256=<hex_digest>"
    """
    if not header_sig or not header_sig.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    # Constant-time comparison to prevent timing attacks
    return hmac.compare_digest(expected, header_sig)


@app.route("/webhook/trigger", methods=["POST"])
def webhook_trigger():
    """
    Webhook endpoint to trigger a pipeline run programmatically.

    Required header: X-Webhook-Signature: sha256=<hmac_hex>
    Body (JSON): {
        "idea": str,
        "analysis_type": 1-4,
        "mode": "idea" | "project",
        "secret": str,   # ignored — auth is via HMAC header
        "flags": {}
    }
    Returns: {"run_id": str, "queued": true}
    """
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        return jsonify({"error": "Webhook not configured — set WEBHOOK_SECRET env var"}), 503

    payload_bytes = request.get_data()
    header_sig = request.headers.get("X-Webhook-Signature", "")

    if not _verify_webhook_signature(webhook_secret, payload_bytes, header_sig):
        return jsonify({"error": "Signature verification failed"}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object body"}), 400

    run_id = uuid.uuid4().hex[:8]

    # Build command using the same logic as api_start_run
    try:
        cmd, stdin_text = _build_command(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with _runs_lock:
        _runs[run_id] = {
            "id": run_id,
            "cmd": cmd,
            "stdin": stdin_text,
            "status": "starting",
            "output": [],
            "started_at": time.time(),
            "ended_at": None,
            "returncode": None,
            "process": None,
            # Feature 1 fields
            "stdin_pipe":     None,
            "awaiting_input": False,
            "input_prompt":   "",
            # Feature 2 fields
            "stages":       [],
            "token_counts": {},
            # A/B test back-reference (None for standalone runs)
            "ab_id":        None,
        }

    threading.Thread(target=_run_worker, args=(run_id, cmd, stdin_text), daemon=True).start()
    return jsonify({"run_id": run_id, "queued": True})


@app.route("/api/webhook/status")
def api_webhook_status():
    """Returns whether the webhook endpoint is configured."""
    configured = bool(os.environ.get("WEBHOOK_SECRET", "").strip())
    return jsonify({"configured": configured, "endpoint": "/webhook/trigger"})


# ─── Feature 1: Human-in-the-loop stdin signal ────────────────────────────────

@app.route("/api/run/<run_id>/signal", methods=["POST"])
def api_run_signal(run_id: str):
    """Send a text message to the running process's stdin (human-in-the-loop).

    Body (JSON):
        {"text": "<input text>", "force": false}

    Guards:
      - Run must be in "running" status.
      - Run must have ``awaiting_input=True`` unless ``force=True`` is set.
    """
    with _runs_lock:
        run = _runs.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404

    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "'text' must be a string"}), 400
    # Reject embedded newlines and null bytes — without this guard the caller
    # could inject multiple stdin answers in a single signal call (one per
    # embedded \n), bypassing the per-prompt awaiting_input gate and confusing
    # pipeline state. This mirrors the same guard applied in _build_command
    # for the project_path / idea inputs.  Length capped at 4096
    # to prevent a single signal from monopolising the pipe buffer.
    if any(ch in text for ch in ("\n", "\r", "\x00")):
        return jsonify({"error": "text must not contain newline or null bytes"}), 400
    if len(text) > 4096:
        return jsonify({"error": "text exceeds 4096 character limit"}), 400

    force = bool(body.get("force", False))

    with _runs_lock:
        run = _runs.get(run_id)
        if not run:
            return jsonify({"error": "Run not found"}), 404
        if run.get("status") != "running":
            return jsonify({"error": "Run is not in 'running' state"}), 409
        if not force and not run.get("awaiting_input"):
            return jsonify({"error": "Run is not awaiting input; use force=true to override"}), 409
        stdin_pipe = run.get("stdin_pipe")

    if stdin_pipe is None:
        return jsonify({"error": "No stdin pipe available for this run"}), 500

    try:
        stdin_pipe.write(text + "\n")
        stdin_pipe.flush()
    except (OSError, ValueError) as exc:
        # OSError: broken pipe / write failure.
        # ValueError: "I/O operation on closed file" — raised when _run_worker's
        # finally block closes proc.stdin concurrently (race between process exit
        # and this signal handler).  Both are non-fatal from the caller's view.
        return jsonify({"error": f"Failed to write to stdin: {exc}"}), 500

    with _runs_lock:
        run = _runs.get(run_id)
        if run is not None:
            run["awaiting_input"] = False

    return jsonify({"success": True, "sent": text})


# ─── Feature 2: Stage timing endpoint ────────────────────────────────────────

@app.route("/api/run/<run_id>/stages")
def api_run_stages(run_id: str):
    """Return stage timing events and token counts parsed from run output.

    Response:
        {"run_id": str, "stages": [{stage_id, label, start_line, started_at, ended_at}],
         "token_counts": {"line_N": int, ...}}
    """
    with _runs_lock:
        run = _runs.get(run_id)
        if not run:
            return jsonify({"error": "Run not found"}), 404
        _snap = {
            "run_id":       run_id,
            "stages":       list(run.get("stages", [])),
            "token_counts": dict(run.get("token_counts", {})),
        }
    return jsonify(_snap)


# ─── Feature 3: Run comparison endpoint ──────────────────────────────────────

def _run_detail_summary(run_id: str) -> dict[str, Any]:
    """Build a detail summary dict for a saved run directory (used by compare endpoint)."""
    run_dir = SAVED_PROJECTS_DIR / run_id
    try:
        run_dir.resolve().relative_to(SAVED_PROJECTS_DIR.resolve())
    except ValueError:
        return {"error": "Invalid run_id"}

    if not run_dir.exists() or not run_dir.is_dir():
        return {"error": "Run not found"}

    analysis  = _read_json_file(run_dir / "analysis_result.json")
    meta      = _read_json_file(run_dir / "run_meta.json")
    backtest  = _read_json_file(run_dir / "backtest_report.json")

    # List code files
    code_dir = run_dir / "code"
    code_files: list[str] = []
    if code_dir.exists() and code_dir.is_dir():
        try:
            code_files = sorted(
                f.name for f in code_dir.iterdir()
                if f.is_file() and not f.is_symlink()
            )
        except OSError:
            pass

    # Extract a compact backtest summary (finite floats only)
    bt_summary: dict[str, Any] = {}
    if backtest:
        for key in ("sharpe_ratio", "max_drawdown", "max_drawdown_pct",
                    "total_return", "total_return_pct", "win_rate",
                    "trade_count", "profit_factor"):
            v = backtest.get(key)
            if v is not None:
                try:
                    fv = float(v)
                    if math.isfinite(fv):
                        bt_summary[key] = fv
                except (TypeError, ValueError):
                    pass

    return {
        "run_id":       run_id,
        "analysis":     analysis,
        "meta":         meta,
        "code_files":   code_files,
        "bt_summary":   bt_summary,
    }


@app.route("/api/run/compare")
def api_run_compare():
    """Compare two saved runs side by side.

    Query params: a=<run_id>  b=<run_id>

    Response: {"a": {detail...}, "b": {detail...}}
    """
    run_id_a = (request.args.get("a") or "").strip()
    run_id_b = (request.args.get("b") or "").strip()
    if not run_id_a or not run_id_b:
        return jsonify({"error": "Query params 'a' and 'b' are required"}), 400

    # Path-traversal guard for both IDs
    for rid in (run_id_a, run_id_b):
        try:
            (SAVED_PROJECTS_DIR / rid).resolve().relative_to(SAVED_PROJECTS_DIR.resolve())
        except ValueError:
            return jsonify({"error": f"Invalid run_id: {rid!r}"}), 400

    summary_a = _run_detail_summary(run_id_a)
    summary_b = _run_detail_summary(run_id_b)

    # _run_detail_summary returns {"error": ...} for missing/invalid runs.
    # Surface these as proper 4xx responses instead of burying them in a 200 body.
    if "error" in summary_a:
        return jsonify({"error": f"Run A ({run_id_a!r}): {summary_a['error']}"}), 404
    if "error" in summary_b:
        return jsonify({"error": f"Run B ({run_id_b!r}): {summary_b['error']}"}), 404

    return jsonify({"a": summary_a, "b": summary_b})


# ─── Feature 4: API key validation ───────────────────────────────────────────

def _mask_api_key(key: str) -> str:
    """Return a masked representation of an API key to prevent leaking it in error messages."""
    if not key:
        return "(empty)"
    return key[:4] + "..." + key[-4:] if len(key) > 8 else "sk-..."


def _is_safe_url(url: str) -> bool:
    """Return True if *url* points to a public (non-private) HTTPS/HTTP host.

    Rejects private/loopback/link-local IPs and non-HTTP schemes to prevent
    SSRF when the server makes outbound requests on behalf of a user.
    Ollama (localhost) is intentionally allowed via the ``allow_localhost``
    flag on the caller side — this function is strict by default.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_global
    except ValueError:
        pass
    # Hostname is a DNS name — resolve and check all addresses
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for _fam, _type, _proto, _canon, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
            if not addr.is_global:
                return False
        except ValueError:
            return False
    return True


@app.route("/api/env/validate", methods=["POST"])
def api_env_validate():
    """Validate an API key by making a live request to the provider.

    Body (JSON):
        {"provider": "openrouter"|"alibaba_coding_plan"|"ollama",
         "api_key": "...",
         "base_url": "..."}

    Response:
        {"valid": bool, "error": str|null, "latency_ms": int}
    """
    body = request.get_json(silent=True) or {}
    provider = str(body.get("provider", "")).strip().lower()
    api_key  = str(body.get("api_key",  "")).strip()
    base_url = str(body.get("base_url",  "")).strip().rstrip("/")

    VALID_PROVIDERS = ("openrouter", "alibaba_coding_plan", "ollama")
    if provider not in VALID_PROVIDERS:
        return jsonify({"error": f"Unknown provider: {provider!r}"}), 400

    t_start = time.monotonic()

    def _do_request(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str | None]:
        """Returns (status_code, error_str_or_None)."""
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, None
        except urllib.error.HTTPError as exc:
            return exc.code, None  # HTTP error but reachable
        except urllib.error.URLError as exc:
            return -1, str(exc.reason)
        except Exception as exc:
            return -1, str(exc)

    error_msg: str | None = None
    status_code = -1

    try:
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            status_code, error_msg = _do_request(url, headers, timeout=5.0)

        elif provider == "alibaba_coding_plan":
            if not base_url:
                # Fall back to the value stored in .env
                base_url = _load_env().get("ALIBABA_CODING_PLAN_BASE_URL", "").strip().rstrip("/")
            if not base_url:
                return jsonify({"error": "base_url is required for this provider — set it in Settings first"}), 400
            if not _is_safe_url(base_url):
                return jsonify({"error": "base_url must point to a public host (private/internal addresses are blocked)"}), 400
            url = base_url + "/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            status_code, error_msg = _do_request(url, headers, timeout=5.0)

        elif provider == "ollama":
            if not base_url:
                return jsonify({"error": "base_url is required for Ollama"}), 400
            # Ollama intentionally runs on localhost — only allow loopback
            _parsed = urllib.parse.urlparse(base_url)
            _host = (_parsed.hostname or "").lower()
            if _host not in ("localhost", "127.0.0.1", "::1"):
                if not _is_safe_url(base_url):
                    return jsonify({"error": "Ollama base_url must be localhost or a public host"}), 400
            url = base_url + "/api/tags"
            status_code, error_msg = _do_request(url, {}, timeout=3.0)

    except Exception as exc:
        # Prevent any accidental key leakage in generic exception messages
        error_msg = str(exc).replace(api_key, _mask_api_key(api_key)) if api_key else str(exc)
        status_code = -1

    latency_ms = int((time.monotonic() - t_start) * 1000)
    valid = status_code in (200, 201, 206)

    # Never expose the raw API key in the error message
    if error_msg and api_key and api_key in error_msg:
        error_msg = error_msg.replace(api_key, _mask_api_key(api_key))

    return jsonify({
        "valid":       valid,
        "error":       error_msg,
        "latency_ms":  latency_ms,
        "status_code": status_code,
    })


# ─── Feature 5: Enhanced backtest chart (drawdown + monthly returns) ─────────

@app.route("/api/run/<run_id>/backtest-chart")
def api_backtest_chart(run_id: str):
    """
    Returns equity curve, drawdown curve, monthly returns, and summary metrics for a run.

    Response:
    {
        "equity_curve":    [{"ts": str, "equity": float}, ...],
        "drawdown_curve":  [{"ts": str, "dd": float}, ...],
        "monthly_returns": {"YYYY-MM": float, ...},
        "summary": {sharpe_ratio, max_drawdown, total_return, win_rate, trade_count, profit_factor},
        "has_data": bool
    }
    """
    run_dir = SAVED_PROJECTS_DIR / run_id
    try:
        run_dir.resolve().relative_to(SAVED_PROJECTS_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid run_id"}), 400
    if not run_dir.exists() or not run_dir.is_dir():
        return jsonify({"error": "Run not found"}), 404

    # Read summary from backtest_report.json
    bt = _read_json_file(run_dir / "backtest_report.json")
    summary: dict[str, Any] = {}
    if bt:
        _canonical_src: dict[str, str] = {
            "max_drawdown": "max_drawdown_pct",
            "total_return": "total_return_pct",
        }
        for key in ("sharpe_ratio", "max_drawdown", "total_return", "win_rate",
                    "trade_count", "profit_factor"):
            if key in _canonical_src:
                canonical = _canonical_src[key]
                v = bt.get(canonical) if canonical in bt else bt.get(key)
            else:
                v = bt.get(key)
            if v is not None:
                try:
                    fv = float(v)
                    if math.isfinite(fv):
                        summary[key] = fv
                except (TypeError, ValueError):
                    pass

    # Read equity curve
    equity_curve: list[dict[str, Any]] = []
    ledger_candidates = [
        run_dir / "sample_out" / "ledger.csv",
        run_dir / "ledger.csv",
        run_dir / "data" / "ledger.csv",
    ]
    for ledger_path in ledger_candidates:
        if not ledger_path.exists():
            continue
        try:
            with ledger_path.open(newline="", encoding="utf-8", errors="replace") as fh:
                reader = csv.DictReader(fh)
                fieldnames = reader.fieldnames or []
                equity_col: str | None = None
                for candidate in (
                    "equity", "Equity",
                    "cumulative_pnl", "CumulativePnl",
                    "portfolio_value", "PortfolioValue",
                    "value", "Value",
                    "close", "Close",
                ):
                    if candidate in fieldnames:
                        equity_col = candidate
                        break
                ts_col: str | None = None
                for candidate in (
                    "timestamp", "Timestamp",
                    "ts", "date", "Date",
                    "datetime", "DateTime",
                ):
                    if candidate in fieldnames:
                        ts_col = candidate
                        break
                if equity_col and ts_col:
                    for row in reader:
                        ts_val = row.get(ts_col, "")
                        eq_val = row.get(equity_col)
                        if eq_val is not None:
                            try:
                                fv = float(eq_val)
                                if math.isfinite(fv):
                                    equity_curve.append({"ts": str(ts_val), "equity": fv})
                            except (TypeError, ValueError):
                                pass
        except Exception:
            LOGGER.debug("[webui] swallowed exception", exc_info=True)
        if equity_curve:
            break

    # Feature 5: compute drawdown_curve from equity_curve
    drawdown_curve: list[dict[str, Any]] = []
    if len(equity_curve) >= 2:
        peak = equity_curve[0]["equity"]
        for pt in equity_curve:
            eq = pt["equity"]
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak != 0.0 else 0.0
            drawdown_curve.append({"ts": pt["ts"], "dd": round(dd, 6)})

    # Feature 5: compute monthly_returns from equity_curve
    monthly_returns: dict[str, float] = {}
    if len(equity_curve) >= 2:
        # Group points by YYYY-MM prefix of their ts string
        month_buckets: dict[str, list[float]] = {}
        for pt in equity_curve:
            ts_str = str(pt["ts"])
            # Accept ISO-format timestamps; take first 7 chars for YYYY-MM
            month_key = ts_str[:7] if len(ts_str) >= 7 else ts_str
            month_buckets.setdefault(month_key, []).append(pt["equity"])
        for month_key in sorted(month_buckets.keys()):
            vals = month_buckets[month_key]
            start_eq = vals[0]
            end_eq   = vals[-1]
            if start_eq != 0.0:
                ret = (end_eq - start_eq) / start_eq
                if math.isfinite(ret):
                    monthly_returns[month_key] = round(ret, 6)

    has_data = bool(summary) or bool(equity_curve)
    return jsonify({
        "equity_curve":    equity_curve,
        "drawdown_curve":  drawdown_curve,
        "monthly_returns": monthly_returns,
        "summary":         summary,
        "has_data":        has_data,
    })


# ─── Feature 6: Per-stage model env var support ───────────────────────────────

# Canonical per-stage model env var names across all supported providers
_STAGE_MODEL_VARS: list[str] = [
    "OPENROUTER_PRIMARY_MODEL",
    "OPENROUTER_DIRECTION_JUDGE_MODEL",
    "OPENROUTER_LIBRARIAN_MODEL",
    "ALIBABA_PRIMARY_MODEL",
    "ALIBABA_DIRECTION_JUDGE_MODEL",
    "ALIBABA_LIBRARIAN_MODEL",
    "OLLAMA_PRIMARY_MODEL",
    "OLLAMA_DIRECTION_JUDGE_MODEL",
    "OLLAMA_LIBRARIAN_MODEL",
]


@app.route("/api/run/stage-models")
def api_stage_models():
    """Return per-stage model env var names and their current .env values.

    Response: {"OPENROUTER_PRIMARY_MODEL": "...", ...}
    """
    env_data = _load_env()
    result: dict[str, str | None] = {}
    for var in _STAGE_MODEL_VARS:
        result[var] = env_data.get(var) or None
    return jsonify(result)


# ─── Feature 7: A/B test run ─────────────────────────────────────────────────

@app.route("/api/ab-test/run", methods=["POST"])
def api_ab_test_run():
    """Start two parallel runs for A/B comparison.

    Body (JSON):
        {"variant_a": <run_payload>, "variant_b": <run_payload>}

    Response:
        {"ab_id": str, "run_id_a": str, "run_id_b": str}
    """
    body = request.get_json(silent=True) or {}
    variant_a = body.get("variant_a")
    variant_b = body.get("variant_b")

    if not isinstance(variant_a, dict) or not isinstance(variant_b, dict):
        return jsonify({"error": "'variant_a' and 'variant_b' must be JSON objects"}), 400

    ab_id = uuid.uuid4().hex[:12]

    # Create run A
    try:
        cmd_a, stdin_a = _build_command(variant_a)
    except ValueError as exc:
        return jsonify({"error": f"variant_a error: {exc}"}), 400

    # Create run B
    try:
        cmd_b, stdin_b = _build_command(variant_b)
    except ValueError as exc:
        return jsonify({"error": f"variant_b error: {exc}"}), 400

    run_id_a = uuid.uuid4().hex[:8]
    run_id_b = uuid.uuid4().hex[:8]

    now = time.time()
    with _runs_lock:
        for rid, cmd, stdin_text in ((run_id_a, cmd_a, stdin_a), (run_id_b, cmd_b, stdin_b)):
            _runs[rid] = {
                "id": rid,
                "cmd": cmd,
                "stdin": stdin_text,
                "status": "starting",
                "output": [],
                "started_at": now,
                "ended_at": None,
                "returncode": None,
                "process": None,
                "stdin_pipe":     None,
                "awaiting_input": False,
                "input_prompt":   "",
                "stages":       [],
                "token_counts": {},
                "ab_id": ab_id,
            }

    with _ab_tests_lock:
        _ab_tests[ab_id] = {
            "ab_id":     ab_id,
            "run_id_a":  run_id_a,
            "run_id_b":  run_id_b,
            "created_at": now,
        }

    threading.Thread(target=_run_worker, args=(run_id_a, cmd_a, stdin_a), daemon=True).start()
    threading.Thread(target=_run_worker, args=(run_id_b, cmd_b, stdin_b), daemon=True).start()

    return jsonify({"ab_id": ab_id, "run_id_a": run_id_a, "run_id_b": run_id_b})


@app.route("/api/ab-test/<ab_id>")
def api_ab_test_status(ab_id: str):
    """Return status and basic comparison for an A/B test pair.

    Response: {
        "ab_id": str,
        "run_id_a": str,
        "run_id_b": str,
        "a": {status, cost, quality, returncode},
        "b": {status, cost, quality, returncode}
    }
    """
    with _ab_tests_lock:
        ab = _ab_tests.get(ab_id)
    if not ab:
        return jsonify({"error": "A/B test not found"}), 404

    def _run_summary(rid: str) -> dict[str, Any]:
        # Capture all mutable fields under the lock so that concurrent
        # modifications to the run dict (status transitions, eviction) cannot
        # produce a torn read after we release the lock.
        with _runs_lock:
            run = _runs.get(rid)
            if not run:
                return {"status": "unknown", "cost": None, "quality": None, "returncode": None}
            run_status = run.get("status")
            run_returncode = run.get("returncode")
        # Filesystem I/O happens outside the lock — holding it during disk reads
        # would block all other routes for the duration of the file access.
        cost: float | None = None
        quality: float | None = None
        if run_status in ("done", "error"):
            run_dir = SAVED_PROJECTS_DIR / rid
            analysis = _read_json_file(run_dir / "analysis_result.json") if run_dir.is_dir() else None
            if analysis:
                cost_raw = analysis.get("total_cost")
                if cost_raw is not None:
                    try:
                        cost = float(cost_raw)
                    except (TypeError, ValueError):
                        pass
                try:
                    q = analysis.get("quality_score") if analysis.get("quality_score") is not None else analysis.get("score")
                    quality = float(q) if q is not None else None
                except (TypeError, ValueError):
                    pass
        return {
            "status":     run_status,
            "cost":       cost,
            "quality":    quality,
            "returncode": run_returncode,
        }

    return jsonify({
        "ab_id":    ab_id,
        "run_id_a": ab["run_id_a"],
        "run_id_b": ab["run_id_b"],
        "created_at": ab.get("created_at"),
        "a": _run_summary(ab["run_id_a"]),
        "b": _run_summary(ab["run_id_b"]),
    })


# ─── Feature 9: Budget status endpoint ───────────────────────────────────────

@app.route("/api/budget/status")
def api_budget_status():
    """Return cost and run_count aggregates plus the operator-configured caps.

    Response:
    {
        "today":      {"cost": float, "run_count": int},
        "month":      {"cost": float, "run_count": int},
        "all_time":   {"cost": float, "run_count": int},
        "daily_limit":   float | null,   # BUDGET_HARD_COST_LIMIT (USD)
        "soft_limit":    float | null,   # BUDGET_SOFT_COST_LIMIT (USD)
        "max_total_tokens": int | null   # BUDGET_MAX_TOTAL_TOKENS
    }

    The three ``*_limit`` fields are surfaced so the front-end budget bar can
    render the configured cap (UI<->backend alignment: previously the UI read
    ``data.daily_limit`` but the backend only returned the aggregates, leaving
    the cap badge / progress fill as dead UI).  ``null`` means "no cap set".
    """
    today_str = time.strftime("%Y-%m-%d")
    month_str = time.strftime("%Y-%m")  # YYYY-MM prefix

    try:
        conn = _ensure_db()

        def _agg(where: str, param: str) -> dict[str, Any]:
            row = conn.execute(
                f"SELECT COALESCE(SUM(total_cost),0), COALESCE(SUM(run_count),0) "
                f"FROM budget_daily WHERE {where}",
                (param,),
            ).fetchone()
            return {"cost": round(float(row[0]), 6), "run_count": int(row[1])}

        today_data    = _agg("date = ?", today_str)
        month_data    = _agg("date LIKE ?", month_str + "%")
        all_time_row  = conn.execute(
            "SELECT COALESCE(SUM(total_cost),0), COALESCE(SUM(run_count),0) FROM budget_daily"
        ).fetchone()
        all_time_data = {"cost": round(float(all_time_row[0]), 6), "run_count": int(all_time_row[1])}
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    def _env_float(name: str) -> float | None:
        raw = (os.environ.get(name, "") or "").strip()
        if not raw:
            return None
        try:
            v = float(raw)
        except ValueError:
            return None
        # Reject sentinel zero / negative — these effectively disable the cap
        # and should be reported as null so the front-end hides the badge.
        return v if v > 0.0 else None

    def _env_int(name: str) -> int | None:
        raw = (os.environ.get(name, "") or "").strip()
        if not raw:
            return None
        try:
            v = int(raw)
        except ValueError:
            return None
        return v if v > 0 else None

    return jsonify({
        "today":            today_data,
        "month":            month_data,
        "all_time":         all_time_data,
        "daily_limit":      _env_float("BUDGET_HARD_COST_LIMIT"),
        "soft_limit":       _env_float("BUDGET_SOFT_COST_LIMIT"),
        "max_total_tokens": _env_int("BUDGET_MAX_TOTAL_TOKENS"),
    })


# ─── Feature 10: Webhook retry + history ─────────────────────────────────────

def _send_notification_with_retry(
    url: str,
    payload: dict[str, Any],
    max_attempts: int = 3,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Send a JSON POST to *url* with exponential backoff retry.

    Stores each attempt in the ``webhook_history`` DB table.
    Returns a summary dict: {success, attempts, last_status_code, last_error}.
    Exponential backoff delays: 1s, 2s, 4s (doubling, up to max_attempts).
    Never logs or exposes raw secrets present in ``payload``.
    """
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_status = -1
    last_error: str | None = None
    success = False
    attempt = 0  # ensures defined in return even if max_attempts <= 0

    for attempt in range(1, max_attempts + 1):
        ts = time.time()
        status_code = -1
        error_msg: str | None = None

        try:
            req = urllib.request.Request(
                url,
                data=body_bytes,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status_code = resp.status
        except urllib.error.HTTPError as exc:
            status_code = exc.code
        except urllib.error.URLError as exc:
            error_msg = str(exc.reason)
        except Exception as exc:
            error_msg = str(exc)

        attempt_success = status_code in (200, 201, 202, 204)
        last_status  = status_code
        last_error   = error_msg

        # Persist attempt to DB
        try:
            with _webhook_history_lock:
                conn = _ensure_db()
                conn.execute(
                    """
                    INSERT INTO webhook_history (ts, url, status_code, success, attempt, error_msg)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (ts, url, status_code if status_code != -1 else None,
                     1 if attempt_success else 0, attempt, error_msg),
                )
                conn.commit()
        except Exception:
            LOGGER.debug("[webui] swallowed exception", exc_info=True)  # DB write failure must not abort the retry loop

        if attempt_success:
            success = True
            break

        # Backoff: 1s, 2s, 4s — only sleep if there are more attempts remaining
        if attempt < max_attempts:
            time.sleep(2 ** (attempt - 1))

    return {
        "success":          success,
        "attempts":         attempt,
        "last_status_code": last_status,
        "last_error":       last_error,
    }


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Send a test notification to the configured NOTIFY_WEBHOOK_URL.

    Uses the retry logic from _send_notification_with_retry.

    Response: {"success": bool, "attempts": int, "last_status_code": int, "last_error": str|null}
    """
    notify_url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not notify_url:
        return jsonify({"error": "NOTIFY_WEBHOOK_URL is not configured"}), 503

    result = _send_notification_with_retry(
        url=notify_url,
        payload={"event": "test", "source": "quantsaas-webui", "ts": time.time()},
        max_attempts=3,
        timeout=10.0,
    )
    status_code = 200 if result["success"] else 500
    return jsonify(result), status_code


@app.route("/api/webhook/history")
def api_webhook_history():
    """Return the last 50 webhook delivery attempts, newest-first.

    Response: {"history": [{id, ts, url, status_code, success, attempt, error_msg}, ...]}
    """
    try:
        conn = _ensure_db()
        rows = conn.execute(
            """
            SELECT id, ts, url, status_code, success, attempt, error_msg
            FROM webhook_history
            ORDER BY ts DESC
            LIMIT 50
            """
        ).fetchall()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    history = [
        {
            "id":          row[0],
            "ts":          row[1],
            "url":         row[2],
            "status_code": row[3],
            "success":     bool(row[4]),
            "attempt":     row[5],
            "error_msg":   row[6],
        }
        for row in rows
    ]
    return jsonify({"history": history})


# ─── Feature: Multi-project comparison ────────────────────────────────────────

@app.route("/api/v169/compare")
def api_v169_compare():
    """Return a side-by-side comparison of the N most-recent saved runs.

    Query params:
        limit (int, default 10) — number of recent runs to compare
        sort  (str, default "score") — field to sort by: score | date | risk

    Response:
        {"runs": [...], "summary": {...}}
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 10)), 50))
    except (ValueError, TypeError):
        limit = 10
    sort_by = request.args.get("sort", "score")

    runs_data: list[dict[str, Any]] = []
    if SAVED_PROJECTS_DIR.is_dir():
        dirs = sorted(
            (d for d in SAVED_PROJECTS_DIR.iterdir() if d.is_dir() and d.name != ".cache"),
            key=_safe_mtime,
            reverse=True,
        )[:limit]
        for d in dirs:
            analysis_path = d / "analysis_result.json"
            meta_path = d / "run_meta.json"
            a_data: dict[str, Any] = _read_json_file(analysis_path) or {}
            m_data: dict[str, Any] = _read_json_file(meta_path) or {}
            if not a_data and not m_data:
                continue
            runs_data.append({
                "run_id": d.name,
                "project_name": a_data.get("project_name") or d.name,
                "date": m_data.get("timestamp") or "",
                "score": a_data.get("score"),
                "risk_level": str(a_data.get("risk_level") or "unknown"),
                "gate_decision": a_data.get("gate_decision") or "",
                "experiments_count": len(a_data.get("experiments") or []),
                "blocking_risks_count": len(a_data.get("blocking_risks") or []),
                "cost_usd": m_data.get("total_cost_usd"),
                "duration_s": m_data.get("duration_seconds"),
                "mode": str(a_data.get("mode_used") or m_data.get("mode") or ""),
            })

    # Sort — guard against NaN/Inf scores: Python's sort is undefined for NaN
    # comparisons and json.dumps would produce invalid JSON for Inf/NaN values.
    def _sort_key(r: dict[str, Any]) -> Any:
        if sort_by == "score":
            v = r.get("score")
            if v is not None:
                try:
                    fv = float(v)
                    if math.isfinite(fv):
                        return -fv
                except (TypeError, ValueError):
                    pass
            return float("inf")  # sort runs with missing/invalid score last
        if sort_by == "risk":
            risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            return risk_order.get(r.get("risk_level", ""), 99)
        return r.get("date") or ""

    runs_data.sort(key=_sort_key)

    # Collect only finite numeric scores so sum/max/min are always well-defined.
    scores: list[float] = []
    for _r in runs_data:
        _v = _r.get("score")
        if _v is not None:
            try:
                _fv = float(_v)
                if math.isfinite(_fv):
                    scores.append(_fv)
            except (TypeError, ValueError):
                pass
    summary = {
        "total_runs": len(runs_data),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "max_score": max(scores) if scores else None,
        "min_score": min(scores) if scores else None,
        "risk_distribution": {},
        "gate_distribution": {},
    }
    for r in runs_data:
        rk = r.get("risk_level", "unknown")
        summary["risk_distribution"][rk] = summary["risk_distribution"].get(rk, 0) + 1
        gk = r.get("gate_decision", "unknown") or "unknown"
        summary["gate_distribution"][gk] = summary["gate_distribution"].get(gk, 0) + 1

    return jsonify({"runs": runs_data, "summary": summary})


# ─── Feature: Prometheus metrics endpoint ─────────────────────────────────────

@app.route("/api/v169/metrics")
def api_v169_metrics():
    """Return Prometheus text format metrics for the most recent completed run.

    Response: text/plain; Prometheus exposition format
    """
    # Find the most recent completed run dir
    run_dir_path: Path | None = None
    if SAVED_PROJECTS_DIR.is_dir():
        dirs = sorted(
            (d for d in SAVED_PROJECTS_DIR.iterdir() if d.is_dir() and d.name != ".cache"),
            key=_safe_mtime,
            reverse=True,
        )
        for d in dirs:
            if (d / "analysis_result.json").is_file():
                run_dir_path = d
                break

    if run_dir_path is None:
        return Response("# No completed runs found\n", mimetype="text/plain")

    prom_file = run_dir_path / "metrics.prom"
    if prom_file.is_file():
        try:
            with open(prom_file, encoding="utf-8") as fh:
                content = fh.read()
            return Response(content, mimetype="text/plain; version=0.0.4; charset=utf-8")
        except OSError as exc:
            return Response(f"# Error reading metrics.prom: {exc}\n", mimetype="text/plain")
    # File not yet generated — attempt on-demand generation
    try:
        import importlib as _imp
        _mod = _imp.import_module("crucible.features.prometheus_exporter")
        _mod.generate_metrics(str(run_dir_path))
        if prom_file.is_file():
            with open(prom_file, encoding="utf-8") as fh:
                content = fh.read()
            return Response(content, mimetype="text/plain; version=0.0.4; charset=utf-8")
        return Response("# metrics.prom not written by generate_metrics\n", mimetype="text/plain")
    except Exception as exc:
        return Response(f"# Error generating metrics: {exc}\n", mimetype="text/plain")


# ─── Feature: Grafana dashboard download ──────────────────────────────────────

@app.route("/api/v169/grafana-dashboard")
def api_v169_grafana_dashboard():
    """Return the generated Grafana dashboard JSON for import.

    Generates from the most recent run's grafana_dashboard.json if available,
    or generates on-the-fly via the grafana_dashboard feature module.
    """
    dashboard_path: Path | None = None
    if SAVED_PROJECTS_DIR.is_dir():
        dirs = sorted(
            (d for d in SAVED_PROJECTS_DIR.iterdir() if d.is_dir() and d.name != ".cache"),
            key=_safe_mtime,
            reverse=True,
        )
        for d in dirs:
            candidate = d / "grafana_dashboard.json"
            if candidate.is_file():
                dashboard_path = candidate
                break

    if dashboard_path is None:
        return jsonify({"error": "No Grafana dashboard found. Run with --v169-features grafana_dashboard first."}), 404

    try:
        with open(dashboard_path, encoding="utf-8") as fh:
            dashboard = json.load(fh)
        return jsonify(dashboard)
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"error": str(exc)}), 500
