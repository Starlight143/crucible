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
from typing import Any, Iterator, Optional

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

# v1.1.0 fourth-pass (F-3): honour ``X-Forwarded-*`` headers when
# ``CRUCIBLE_TRUST_FORWARDED=1`` (operator opt-in).  Without this,
# ``request.host`` returns the internal host (e.g. ``127.0.0.1:5000``)
# but browsers send ``Referer`` carrying the public host (e.g.
# ``crucible.example.com``) → the new same-origin check in
# ``_enforce_xhr_header_on_state_changes`` flags every legitimate
# same-origin POST as cross-origin → 403.  Opt-in because trusting
# forwarded headers without a properly-configured proxy is itself an
# IP-spoofing vector.  Operators behind nginx / Caddy / Cloudflare
# set the env var; standalone deployments leave it off.
try:
    from werkzeug.middleware.proxy_fix import ProxyFix as _ProxyFix
    _trust_forwarded_raw = (os.environ.get("CRUCIBLE_TRUST_FORWARDED") or "").strip().lower()
    if _trust_forwarded_raw in {"1", "true", "yes", "on"}:
        # x_for=1 / x_proto=1 / x_host=1: trust ONE hop of the forwarded
        # chain (the proxy directly in front of us); refuse to trust
        # arbitrary client-supplied chains.
        app.wsgi_app = _ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
except ImportError:
    # Werkzeug always ships with Flask; the import is defensive in case
    # someone strips middleware in a fork.
    pass

# v1.1.0: hard cap the request body at 1 MB.  None of the default
# endpoints (POST /api/run with ``idea`` + ``project_path``, POST /api/env
# with the full settings dict, POST /api/run/<id>/signal with a 4 KB cap
# already applied inline) need anywhere near 1 MB; the cap kills a class
# of DoS attempts where a single oversized JSON body buffers in Flask's
# pre-route parser and OOMs the worker before the route handler can apply
# its own length guard.
#
# v1.1.0 fourth-pass (F-8): operators who paste large idea briefs / CSV
# uploads can raise the cap via ``CRUCIBLE_MAX_CONTENT_LENGTH_MB``
# (integer megabytes).  We clamp to [1, 64] MB so an extreme value can't
# defeat the DoS guard.  Default stays at 1 MB.
_max_mb_raw = (os.environ.get("CRUCIBLE_MAX_CONTENT_LENGTH_MB") or "").strip()
try:
    _max_mb = int(_max_mb_raw) if _max_mb_raw else 1
except ValueError:
    _max_mb = 1
_max_mb = max(1, min(64, _max_mb))
app.config["MAX_CONTENT_LENGTH"] = _max_mb * 1024 * 1024


# v1.1.0: CSRF / drive-by hardening for state-mutating endpoints.
# We require the ``X-Requested-With: XMLHttpRequest`` header on every
# unsafe-method request to the API surface.  The frontend already attaches
# this header to every ``fetch()`` call; a malicious cross-origin page
# loaded in the operator's browser CANNOT add this header without first
# triggering a CORS preflight (which Flask answers without state mutation),
# so the request is blocked at the routing layer before reaching the
# handler.  Webhook endpoints are exempt because they use HMAC signature
# verification and are intentionally callable from third-party services.
_XHR_EXEMPT_PREFIXES = ("/webhook/",)
_XHR_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@app.before_request
def _enforce_xhr_header_on_state_changes() -> Any:
    """Block cross-origin state-mutating requests that lack ``X-Requested-With``.

    The check is conservative: only POST/PUT/PATCH/DELETE to ``/api/*``
    are gated.  Read-only GET requests, OPTIONS preflights, the SPA at
    ``/``, static files, and webhook endpoints (HMAC-verified) are all
    allowed through unconditionally.

    v1.1.0 refinement: the header requirement only applies when the
    request carries an ``Origin`` header (i.e. came from a browser).
    Server-to-server callers (curl, the Flask test client, internal
    schedulers) do not set ``Origin`` and pass through.  A genuine
    drive-by attack from a malicious cross-origin page CANNOT suppress
    the ``Origin`` header (the browser sets it automatically on every
    cross-origin fetch / form post) so the cross-origin attack surface
    is still fully covered.  This matches the threat model: same-origin
    XHR (frontend → backend) is trusted; cross-origin XHR is blocked
    unless the frontend explicitly adds the header.
    """
    method = (request.method or "").upper()
    if method in _XHR_SAFE_METHODS:
        return None
    path = request.path or ""
    if not path.startswith("/api/"):
        return None
    for prefix in _XHR_EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return None
    # v1.1.0 third-pass: decide whether this looks like a browser
    # cross-origin call.
    #
    # * ``Origin`` present and a real host → browser issued; require
    #   X-Requested-With.
    # * ``Origin: null`` (sandboxed iframe, data:/file: redirect,
    #   certain Safari versions, server-pushed pages with opaque
    #   origin) → untrusted; require X-Requested-With.
    # * No ``Origin`` but ``Referer`` from a different host →
    #   privacy-preserving browser flow with origin suppression;
    #   treat as cross-origin and require X-Requested-With.
    # * Neither header present (curl, Flask test client, internal
    #   scheduler) → server-to-server; pass.
    origin = (request.headers.get("Origin") or "").strip()
    referer = (request.headers.get("Referer") or "").strip()
    host = (request.host or "").lower()

    # v1.1.0 fourth-pass (F-3): also consult ``X-Forwarded-Host`` so
    # comparing Referer against the request host works correctly
    # behind a reverse proxy.  If ProxyFix is wired up,
    # ``request.host`` already reflects the forwarded value and this
    # is a no-op; if ProxyFix is OFF we still try to be lenient on
    # same-origin requests when an explicit forwarded host matches.
    #
    # v1.1.2 (audit fix G5-C-MED-4): gate the X-Forwarded-Host read behind
    # the same ``CRUCIBLE_TRUST_FORWARDED`` opt-in that controls ProxyFix.
    # Previously the header was consulted unconditionally — any HTTP client
    # that controls headers (curl, malicious internal pod) could set
    # ``Referer: http://attacker.com/`` together with
    # ``X-Forwarded-Host: attacker.com`` and convince this gate that a
    # cross-host POST was same-origin.  When the operator has not opted into
    # trusting forwarded headers, ignore X-Forwarded-Host entirely.
    #
    # v1.1.2 (audit fix G5-C-MED-5): split on the first comma.  Multi-hop
    # proxy chains legitimately emit ``X-Forwarded-Host: real.com, edge.net``
    # — the un-split string never matches a Referer's host, so legitimate
    # same-origin POSTs behind multi-hop proxies were 403'd as "missing
    # X-Requested-With header".  Standard practice (matches Werkzeug
    # ProxyFix's behaviour) is to take the first hop and discard the rest.
    _forwarded_trusted = os.environ.get("CRUCIBLE_TRUST_FORWARDED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if _forwarded_trusted:
        _fwd_raw = (request.headers.get("X-Forwarded-Host") or "").strip().lower()
        fwd_host = _fwd_raw.split(",", 1)[0].strip() if _fwd_raw else ""
    else:
        fwd_host = ""

    requires_xhr_header = False
    if origin:
        # Any Origin header — including a literal "null" opaque origin —
        # signals a browser-initiated request that must carry the XHR
        # header.  Same-origin POSTs from the SPA carry it automatically
        # via the fetch shim; cross-origin attackers cannot add it
        # without triggering a CORS preflight we never approve.
        requires_xhr_header = True
    elif referer:
        # No Origin (privacy mode / older browsers) but Referer present
        # → still a browser flow.  Compare Referer's netloc against the
        # request host AND any forwarded host so reverse-proxy
        # deployments are not falsely cross-origin.
        try:
            ref_host = (urllib.parse.urlparse(referer).netloc or "").lower()
        except Exception:
            # Malformed Referer (extension-rewritten, ``data:`` scheme
            # raising in urlparse) — fail closed: require the header.
            ref_host = None
        # v1.1.0 fourth-pass: also fail closed when urlparse succeeds
        # but produces an empty netloc.  ``javascript:alert(1)``,
        # ``data:,xxx``, ``/relative/path`` all parse cleanly to
        # ``netloc == ""``.  An empty netloc never legitimately
        # represents same-origin → require the XHR header.
        if not ref_host:  # covers None AND ""
            requires_xhr_header = True
        elif ref_host not in (host, fwd_host):
            requires_xhr_header = True

    if not requires_xhr_header:
        return None

    xhr = (request.headers.get("X-Requested-With") or "").strip().lower()
    if xhr != "xmlhttprequest":
        return jsonify({
            "error": "missing X-Requested-With header",
            "detail": (
                "cross-origin state-mutating API calls must include "
                "X-Requested-With: XMLHttpRequest; same-origin XHR adds "
                "this header automatically"
            ),
        }), 403
    return None


@app.errorhandler(404)
def _handle_404(exc: Any) -> Any:
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(413)
def _handle_413(exc: Any) -> Any:
    # Triggered when MAX_CONTENT_LENGTH is exceeded; surface a clear error
    # instead of Flask's default HTML response.
    return jsonify({
        "error": "request body too large",
        "limit_bytes": app.config.get("MAX_CONTENT_LENGTH", 0),
    }), 413


@app.errorhandler(500)
def _handle_500(exc: Any) -> Any:
    return jsonify({"error": "Internal server error"}), 500


def _safe_500(exc: BaseException, context: str) -> Any:
    """Return a generic JSON 500 with a short log-correlation id and log
    the raw exception via ``LOGGER.exception`` for operator triage.

    v1.1.2 (audit fix G7-C-HIGH-1): five endpoints used to ``jsonify({
    "error": str(exc) })`` directly, leaking on-disk DB paths (sqlite3
    errors), absolute filesystem paths (``OSError`` / WindowsError),
    and internal hostnames (``urllib.error.URLError`` wrapped around
    ``getaddrinfo``).  We now log the raw exception server-side and
    return only a generic message + a short ``log_id`` the operator can
    grep in the WebUI log for the underlying detail.  Endpoint-specific
    user-input validation errors (400-class) are unaffected — those
    legitimately reflect malformed requests.
    """
    log_id = uuid.uuid4().hex[:8]
    try:
        LOGGER.exception("[webui] %s failed (log_id=%s)", context, log_id)
    except Exception:
        # Logger itself is broken — at least don't propagate further.
        pass
    return jsonify({
        "error": "internal error",
        "log_id": log_id,
        "detail": (
            "Server-side error; the full traceback is in the WebUI log "
            f"under log_id={log_id}.  Send the log_id to your operator."
        ),
    }), 500


# v1.1.2 (sixth-pass H-4): regex patterns used by ``_redact_for_client`` to
# scrub absolute filesystem paths.  Compiled at module load so the hot path
# (every webhook history row, every notify_test response) avoids per-call
# ``re.compile``.  Patterns intentionally only match recognised absolute-
# path prefixes so URL paths in error messages stay readable.
_PATH_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Windows ``C:\Users\...\file.ext`` form (and similar drive letters).
    re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\){1,}([^\\/:*?\"<>|\r\n]+)"),
    # POSIX absolute path under common home / system roots.
    re.compile(r"/(?:home|Users|root|tmp|var|opt|etc|usr|mnt|srv)/[^\s\"']+/([^/\s\"']+)"),
)


def _redact_for_client(text: Any, *, max_len: int = 300) -> str:
    """Scrub a free-form error string before exposing it to the client.

    v1.1.2 (sixth-pass H-4): four endpoints
    (``api_notify_test``, ``api_webhook_history``, the
    ``api_v169_metrics`` text/plain branches, ``api_signal``,
    ``api_list_projects``) used to embed ``str(exc)`` in their response
    bodies.  Operator-only or not, these paths could carry sk-* tokens
    (when the URL was misconfigured to include credentials), absolute
    filesystem paths (``OSError``'s ``strerror`` + ``filename``), and
    internal hostnames.  This helper:

    1. Routes the string through Run Insights' ``_VALUE_SECRET_PATTERNS``
       so any of the 14+ vendor-specific secret prefixes are masked.
    2. Replaces absolute Windows / POSIX paths with ``<path>/<basename>``
       so the directory tree stays out of the response.
    3. Caps length at *max_len* to bound clients that aren't expecting
       multi-KB error bodies.

    Returns the empty string for ``None`` / empty input.
    """
    if text is None or text == "":
        return ""
    out = str(text)
    try:
        try:
            from crucible.features.run_insights.redact import _redact_string_value as _rs
        except ImportError:
            from features.run_insights.redact import _redact_string_value as _rs  # type: ignore[no-redef]
        out = _rs(out)
    except Exception:
        # Defensive: redact module may be unavailable in degraded envs.
        pass
    try:
        for _pat in _PATH_REDACT_PATTERNS:
            out = _pat.sub(r"<path>/\1", out)
    except Exception:
        pass
    if len(out) > max_len:
        out = out[: max_len - 1] + "…"
    return out


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

# v1.1.2 (audit fix G5-C-MED-6): cap concurrent worker threads so a scripted
# attacker or a runaway autorun loop cannot exhaust process / memory budgets
# by triggering N parallel subprocess.Popen chains through /api/run.
# Override via ``CRUCIBLE_WEBUI_MAX_CONCURRENT_RUNS`` (whitelist parser
# semantics — typo falls back to default 4).
def _parse_max_concurrent_runs() -> int:
    raw = (os.environ.get("CRUCIBLE_WEBUI_MAX_CONCURRENT_RUNS") or "").strip()
    if not raw:
        return 4
    try:
        n = int(raw)
        if n <= 0:
            return 4
        return min(n, 64)  # hard ceiling — refuse to allow truly silly values
    except (ValueError, TypeError):
        return 4


_RUNS_MAX_CONCURRENT = _parse_max_concurrent_runs()
_runs_semaphore = threading.BoundedSemaphore(value=_RUNS_MAX_CONCURRENT)

# v1.1.2 (audit fix G5-C-MED-9): cap the in-memory output ring per run so a
# long-running chatty pipeline cannot grow without bound.  Lines beyond the
# cap are dropped from the head (FIFO) — operators investigating cost / token
# usage / final verdict need the TAIL; the head is reproducible from
# saved_projects/ on disk.  Override via
# ``CRUCIBLE_WEBUI_MAX_OUTPUT_LINES_PER_RUN``.
def _parse_max_output_lines() -> int:
    raw = (os.environ.get("CRUCIBLE_WEBUI_MAX_OUTPUT_LINES_PER_RUN") or "").strip()
    if not raw:
        return 50_000
    try:
        n = int(raw)
        if n <= 0:
            return 50_000
        return min(n, 2_000_000)
    except (ValueError, TypeError):
        return 50_000


_RUNS_MAX_OUTPUT_LINES = _parse_max_output_lines()


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


# v1.1.2 (audit fix G5-C-LOW-14): proactive eviction timer.  Previously
# ``_evict_stale_runs`` was only called from inside the SSE generator;
# headless / webhook-only deployments (no dashboard tab open) accumulated
# completed run records indefinitely until a page load triggered the
# sweep.  We now run a tiny daemon timer that re-arms itself every
# ``_EVICTION_TIMER_SECS`` seconds for the lifetime of the process.
# Daemon=True means the timer shuts down cleanly when Flask exits and
# never blocks interpreter shutdown.
_EVICTION_TIMER_SECS = 60.0
_eviction_timer: "threading.Timer | None" = None
_eviction_timer_lock = threading.Lock()


def _periodic_evict_runs() -> None:
    """Run the staleness sweep, then re-arm the timer."""
    try:
        _evict_stale_runs("")
    except Exception:
        LOGGER.debug("[webui] periodic eviction failed", exc_info=True)
    finally:
        _schedule_eviction_timer()


def _schedule_eviction_timer() -> None:
    """Arm a one-shot daemon Timer that calls ``_periodic_evict_runs`` once
    after ``_EVICTION_TIMER_SECS`` seconds.  Idempotent: if a timer is
    already armed, this is a no-op.
    """
    global _eviction_timer
    with _eviction_timer_lock:
        if _eviction_timer is not None and _eviction_timer.is_alive():
            return
        t = threading.Timer(_EVICTION_TIMER_SECS, _periodic_evict_runs)
        t.daemon = True
        t.name = "crucible-webui-eviction"
        _eviction_timer = t
        t.start()


# Arm at import time so the sweep runs even before the first SSE poll.
_schedule_eviction_timer()


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

    v1.1.6 fix: the front-end ``saveSettings`` flow (since v1.1.0) only POSTs
    keys whose input value differs from the page-render baseline — so ``data``
    is the *dirty* subset, not the full env state.  The previous loop body
    treated "key not in data" as "use the .env.example raw line", which
    silently reset every untouched key (including real API keys) to its
    template default whenever the operator saved an unrelated setting.

    Fix: load the current ``.env`` first and merge ``data`` over it; then
    iterate the template against the *merged* dict.  Unchanged keys now
    resolve to their on-disk value, dirty keys resolve to the POSTed value,
    and keys present only in ``.env`` (not in the template — operator-only
    overrides) are appended after the template body instead of being dropped.
    All four cases are pinned by ``tests/test_webui_env_save_preserves_unchanged.py``.
    """
    current = _load_env() if ENV_FILE.exists() else {}
    merged: dict[str, str] = {**current, **data}

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
                if k in merged:
                    out_lines.append(f"{k}={_quote_env_value(merged[k])}")
                    written.add(k)
                else:
                    # Key is commented-out or otherwise absent from both
                    # current .env and the POST payload — leave the raw
                    # template line untouched.
                    out_lines.append(raw)
        for k, v in merged.items():
            if k not in written:
                out_lines.append(f"{k}={_quote_env_value(v)}")
        content = "\n".join(out_lines) + "\n"
    else:
        content = "\n".join(f"{k}={_quote_env_value(v)}" for k, v in merged.items()) + "\n"

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

    # v1.0.5: surface quality-loop outcome fields on the dashboard / runs API.
    # ``quality_passed`` mirrors ``review_report.passes`` (bool → 0/1/null) and
    # ``quality_loop_failure_type`` mirrors the structured review failure_type
    # (e.g. ``QUALITY_LOOP_GAVE_UP``).  Both are written to the top level of
    # ``run_meta.json`` by section_07 so the dashboard list and run-detail
    # modal can render a quality-status badge without re-parsing
    # ``review_report.json`` on every request.  Older runs that predate the
    # field stay NULL and the frontend renders no badge for them.
    for _col_name, _col_type in (
        ("quality_passed", "INTEGER"),
        ("quality_loop_failure_type", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {_col_name} {_col_type}")
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
        # v1.0.5: quality-loop outcome surfaced from run_meta.json.  ``None``
        # on older runs that predate the structured field; the frontend uses
        # this tri-state (true / false / None) to render the badge.
        "quality_passed": None,
        "quality_loop_failure_type": None,
    }

    # ── Cost / token extraction with USD priority ────────────────────────────
    # v1.0.5 round 4 (cost surfacing): the legacy ``total_cost`` field on
    # ``analysis_result.json`` was a token-derived "cost_units" value with no
    # USD semantics; the actual USD spend lives in ``run_meta.total_cost_usd``
    # (promoted from ``run_snapshot.cost_summary`` by section_07).  We prefer
    # USD whenever available so the dashboard renders real billing dollars
    # instead of arbitrary token-units.  Older saved_projects/ that predate
    # the meta-promotion fall through to ``run_snapshot.json`` directly.
    #
    # The full float value is preserved end-to-end (no rounding at this
    # layer): SQLite stores REAL = double-precision IEEE 754, which keeps
    # all 6 decimal places of the per-call OpenRouter cost.  Any precision
    # loss in the user-visible UI happens only in the display formatter,
    # never on the wire.
    def _coerce_finite_float(value: Any) -> Optional[float]:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None

    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    # analysis_result.json
    analysis = _read_json_file(d / "analysis_result.json")
    if analysis:
        # Feature 8: extract schema_version from analysis result
        sv = analysis.get("schema_version")
        if sv is not None:
            row["schema_version"] = str(sv)
        if row["cost"] is None:
            # USD has priority over the legacy units field.
            row["cost"] = _coerce_finite_float(analysis.get("total_cost_usd"))
            if row["cost"] is None:
                row["cost"] = _coerce_finite_float(analysis.get("total_cost"))
        if row["tokens"] is None:
            row["tokens"] = _coerce_int(analysis.get("total_tokens"))
        if row["quality"] is None:
            v = analysis.get("quality_score") if analysis.get("quality_score") is not None else analysis.get("score")
            row["quality"] = _coerce_finite_float(v)

    # run_meta.json
    meta = _read_json_file(d / "run_meta.json")
    if meta:
        row["mode"] = str(meta.get("mode") or "").lower() or None
        row["provider"] = meta.get("llm_provider") or None
        row["timestamp"] = meta.get("timestamp") or None
        if row["cost"] is None:
            # USD priority again — meta is the canonical source after v1.0.5.
            row["cost"] = _coerce_finite_float(meta.get("total_cost_usd"))
            if row["cost"] is None:
                row["cost"] = _coerce_finite_float(meta.get("total_cost"))
        if row["tokens"] is None:
            row["tokens"] = _coerce_int(meta.get("total_tokens"))
        # v1.0.5: structured quality outcome.  ``quality_passed`` is written
        # by section_07 as a Python bool; SQLite stores 0/1/None.  We accept
        # the raw bool, ints {0,1}, and the strings ``"true"``/``"false"`` so
        # operator-edited meta files do not silently drop the field.
        if "quality_passed" in meta:
            qp_raw = meta.get("quality_passed")
            if isinstance(qp_raw, bool):
                row["quality_passed"] = 1 if qp_raw else 0
            elif isinstance(qp_raw, int) and qp_raw in (0, 1):
                row["quality_passed"] = qp_raw
            elif isinstance(qp_raw, str):
                _qs = qp_raw.strip().lower()
                if _qs in {"true", "1", "yes", "passed"}:
                    row["quality_passed"] = 1
                elif _qs in {"false", "0", "no", "failed"}:
                    row["quality_passed"] = 0
        qft = meta.get("quality_loop_failure_type")
        if isinstance(qft, str) and qft.strip():
            # Mirror the backend's strict validation: only persist values
            # whose normalised form matches the allowed enum.  Anything else
            # is dropped so the frontend never renders a phantom failure.
            qft_norm = qft.strip().upper()
            if qft_norm in {"QUALITY_LOOP_GAVE_UP"}:
                row["quality_loop_failure_type"] = qft_norm

    # run_snapshot.json fallback for legacy saved_projects/ that predate the
    # cost-promotion-into-run_meta fix (any run created before v1.0.5 round 4).
    # The snapshot's ``cost_summary`` block is the authoritative end-of-run
    # cost ledger frozen by section_07 just before save_project_output is
    # called.  Reading it here lets the dashboard show real $ figures for
    # historical runs without requiring an explicit migration step.
    if row["cost"] is None or row["tokens"] is None:
        snapshot = _read_json_file(d / "run_snapshot.json")
        if snapshot:
            cs = snapshot.get("cost_summary")
            if isinstance(cs, dict):
                if row["cost"] is None:
                    row["cost"] = _coerce_finite_float(cs.get("total_cost_usd"))
                    if row["cost"] is None:
                        row["cost"] = _coerce_finite_float(cs.get("total_cost"))
                if row["tokens"] is None:
                    row["tokens"] = _coerce_int(cs.get("total_tokens"))

    # Fallback path for older runs whose run_meta.json predates the v1.0.5
    # round 2 promotion of quality_passed / quality_loop_failure_type to the
    # top level: read review_report.json directly so the dashboard badge
    # still works on saved_projects/ created by earlier crucible versions.
    if row["quality_passed"] is None or row["quality_loop_failure_type"] is None:
        review = _read_json_file(d / "review_report.json")
        if review:
            if row["quality_passed"] is None:
                rp = review.get("passes")
                if isinstance(rp, bool):
                    row["quality_passed"] = 1 if rp else 0
            if row["quality_loop_failure_type"] is None:
                rft = review.get("failure_type")
                if isinstance(rft, str) and rft.strip():
                    rft_norm = rft.strip().upper()
                    if rft_norm in {"QUALITY_LOOP_GAVE_UP"}:
                        row["quality_loop_failure_type"] = rft_norm

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
                             has_backtest, sharpe, drawdown, total_return, schema_version,
                             quality_passed, quality_loop_failure_type)
                        VALUES
                            (:run_id, :mtime, :cost, :tokens, :quality, :mode, :provider, :timestamp,
                             :has_backtest, :sharpe, :drawdown, :total_return, :schema_version,
                             :quality_passed, :quality_loop_failure_type)
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
                   has_backtest, sharpe, drawdown, total_return,
                   quality_passed, quality_loop_failure_type
            FROM runs
            {where}
            ORDER BY mtime DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            qp_raw = row[12]
            # Re-emit as JSON booleans (or null) so the frontend never has to
            # special-case the SQLite int(0/1) representation.
            if qp_raw is None:
                qp = None
            else:
                try:
                    qp = bool(int(qp_raw))
                except (TypeError, ValueError):
                    qp = None
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
                "quality_passed":            qp,
                "quality_loop_failure_type": row[13],
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
        # Mirror SQLite path's tri-state quality_passed encoding (true/false/null
        # — never int) so the frontend sees the same shape regardless of which
        # branch produced the row.
        qp_raw = row.get("quality_passed")
        if qp_raw is None:
            qp_emit: bool | None = None
        else:
            try:
                qp_emit = bool(int(qp_raw))
            except (TypeError, ValueError):
                qp_emit = None
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
            "quality_passed":            qp_emit,
            "quality_loop_failure_type": row.get("quality_loop_failure_type"),
        })
        if len(runs) >= limit:
            break
    return runs


# ─── Routes ────────────────────────────────────────────────────────────────────

# ── Static asset cache-busting ────────────────────────────────────────────────
# Without a versioned query string Flask serves ``app.js`` / ``app.css`` with
# strong ETags; browsers then send ``If-None-Match`` on subsequent loads and
# receive ``304 Not Modified`` — meaning even Ctrl+F5 can keep an old bundle in
# disk cache on some Chromium builds.  We compute a short content hash for each
# static asset and pass it to ``index.html`` so the script / link tags become
# ``app.js?v=<hash>`` — when the file content changes the URL changes, which
# guarantees a fresh fetch on the very next page load.
_STATIC_ASSET_HASH_CACHE: dict[str, str] = {}


_EMPTY_FILE_SHA1_HEAD = "da39a3ee5e"  # sha1(b"")[:10] — never cache this


def _static_asset_hash(rel_path: str) -> str:
    """Returns the first 10 hex chars of sha1(file_bytes) for a static asset.

    Result is memoised per-process — restart Flask to pick up new hashes
    (which is the operator's existing workflow anyway).  Missing files
    return ``"x"`` so the URL is still well-formed and the browser will
    fetch it normally.

    v1.1.0 fifth-pass (G-22): NEVER cache the empty-file sha1 prefix.
    Editor truncate-then-write semantics (VS Code / vim) momentarily
    leave ``app.js`` at zero bytes during save.  A Flask request hitting
    ``index()`` in that window would permanently cache the empty-file
    hash, after which subsequent edits would NOT bust the cache until
    Flask restart.  The empty-file sentinel is treated as ephemeral
    and re-read on the next request.
    """
    cached = _STATIC_ASSET_HASH_CACHE.get(rel_path)
    if cached and cached not in (_EMPTY_FILE_SHA1_HEAD, "x"):
        return cached
    try:
        import hashlib
        full = (PROJECT_ROOT / "webui" / "static" / rel_path)
        data = full.read_bytes()
        if not data:
            # Truncated-during-save snapshot.  Return an ephemeral
            # sentinel that survives until the file is re-readable;
            # DO NOT cache so the next request re-reads.
            return "x"
        h = hashlib.sha1(data).hexdigest()[:10]
    except Exception:  # noqa: BLE001 — never break page rendering
        h = "x"
    # Only cache once we have a real, non-empty-file hash.
    if h != "x":
        _STATIC_ASSET_HASH_CACHE[rel_path] = h
    return h


@app.route("/")
def index() -> str:
    return render_template(
        "index.html",
        webui_url=os.environ.get("WEBUI_URL", ""),
        asset_js_v=_static_asset_hash("js/app.js"),
        asset_css_v=_static_asset_hash("css/app.css"),
    )


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
        return _safe_500(exc, "api_save_env: _save_env")
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
    """Parse .env.example into a schema of {group: [{key, default, type}]}.

    v1.1.0 fifth-pass (G-21): tightened group-header heuristic.  The
    prior rule (any 1-6 token comment is a header) caused 6-token
    description sentences (e.g. ``# Alibaba Coding Plan Stage 0
    direction judge model.``) to hijack adjacent env keys into a
    description-shaped "group name".  New rules — a comment line is
    treated as a group header iff ANY of:

      * it is an explicit divider (begins/ends with ``=``, ``-``, ``─``,
        ``━``, ``#``, ``*``, or has 3+ such characters);
      * it is an UPPER-CASE-ONLY token sequence of 1-6 tokens (e.g.
        ``# OPENROUTER`` or ``# RUN INSIGHTS LEDGER``);
      * it is 1-3 tokens AND none of those tokens contain ``=`` or
        a colon (single-word section titles like ``# Cache`` still work).

    Everything else is treated as a description — preserving the
    previous header.  Genuine multi-word section headings should use
    the explicit divider form ``# === Section name ===``.  This makes
    the parser stable against a class of description-sentence hijacks
    that the v1.1.0 fifth-pass audit found.
    """
    if not ENV_EXAMPLE.exists():
        return jsonify({})
    groups: dict[str, list[dict]] = {}
    current_group = "General"
    try:
        _env_text = ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return jsonify({})

    def _is_section_header(text: str) -> bool:
        """Apply the tightened heuristic — see docstring above."""
        if not text:
            return False
        stripped = text.strip()
        # Strip surrounding divider characters before counting tokens so
        # ``# === Section ===`` is recognised as the section name "Section".
        for ch in ("=", "-", "─", "━", "*", "#"):
            stripped = stripped.strip(ch).strip()
        if not stripped:
            return False
        tokens = stripped.split()
        n = len(tokens)
        if n > 6:
            return False
        # Reject any token containing ``=`` or a colon (description sentences
        # frequently mention ``ENV_NAME=value`` or ``Note:`` and would
        # otherwise hijack the group when terse).
        if any(("=" in tok or ":" in tok) for tok in tokens):
            return False
        # 1-3 tokens of plain identifier-shape: accept (e.g. "Cache",
        # "Run Insights", "Backtest & Optimisation").
        if n <= 3:
            return True
        # 4-6 tokens: require explicit divider syntax OR all-uppercase.
        if "=" in text or "─" in text or "━" in text or "*" in text:
            return True
        # Allow ALL-CAPS phrasing as a divider convention.
        if stripped.upper() == stripped and any(ch.isalpha() for ch in stripped):
            return True
        return False

    for raw in _env_text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if _is_section_header(text):
                # Normalise: drop surrounding divider chars for display.
                for ch in ("=", "-", "─", "━", "*", "#"):
                    text = text.strip(ch).strip()
                if text:
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


# ─── Run Insights env overrides (per-run flag panel) ─────────────────────────
# The Idea / Path mode flag panels expose five Run Insights boolean toggles
# (mirrored from the Settings page CRUCIBLE_RUN_INSIGHTS_* keys).  When the
# operator flips one of those toggles for a single run, we don't want to
# rewrite ``.env`` — we just override the env var on that one subprocess so
# the recorder configured at module import time sees the new value.  This
# helper resolves the override dict; ``_run_worker`` merges it into
# ``_child_env``.  Boolean None values are intentionally left out so that
# unset toggles inherit the parent process's env (which itself was loaded
# from ``.env`` at Flask startup).
_RUN_INSIGHTS_FLAG_TO_ENV: dict[str, str] = {
    "run_insights_enabled":       "CRUCIBLE_RUN_INSIGHTS_ENABLED",
    "run_insights_record_output": "CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT",
    "run_insights_record_errors": "CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS",
    "run_insights_record_debate": "CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE",
    "run_insights_redact":        "CRUCIBLE_RUN_INSIGHTS_REDACT",
    # v1.1.8 — Direction Debate Audit Mode per-run toggles.  Frontend
    # mirror lives in webui/static/js/app.js:ENV_BACKED_FLAGS — both
    # must stay in lockstep.  RHS env names match the actual reads in
    # section_02_research_and_llm.py and the recorder helper
    # ``_resolve_record_debate_finding`` (producer→consumer wiring
    # verified by test_wiring.py per CLAUDE.md § 9.6).
    "debate_audit_mode":          "CRUCIBLE_DEBATE_AUDIT_MODE",
    "debate_external_critic":     "CRUCIBLE_DEBATE_EXTERNAL_CRITIC",
    # v1.1.8 extended — Direction Gate Tuning per-run toggle.  RHS env
    # name matches the read site in
    # ``crucible/features/direction_debate/degraded.py`` (Phase 7).
    # Same producer→consumer wiring rule applies.
    "debate_tolerate_unverifiable_evidence": "CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE",
}


# v1.1.0 fourth-pass (F-9): the three ``_STORE_TRUE_ONLY`` flags
# (``cache``, ``strict_json``, ``cost_trace``) don't have ``--no-``
# CLI variants because their argparse definitions use the legacy
# ``store_true`` action.  Unchecking them in the idea/path panel
# previously had no effect — the subprocess inherited the ``.env``
# default which is typically ``1``, so the feature ran even though
# the UI said "off".  This mapping lets us override via env var
# instead of CLI: unchecking the box sets the corresponding
# env=``0`` for the subprocess, which the core pipeline reads via
# ``_env.env_bool()`` and respects.
#
# v1.1.0 fifth-pass (G-1): the fourth-pass shipped the WRONG env
# names — ``CRUCIBLE_CACHE`` / ``CRUCIBLE_STRICT_JSON`` /
# ``CRUCIBLE_COST_TRACE`` are NOT what the core pipeline reads.
# The actual read sites use the un-prefixed legacy names
# (``section_07_selfcheck_output_main.py:323-325`` reads
# ``env_bool("STRICT_JSON", ...)``, ``env_bool("LOCAL_CACHE", ...)``
# and ``env_bool("COST_TRACE", ...)``; ``section_02`` /
# ``section_05`` / ``section_06`` mirror this).  So the F-9 fix
# silently regressed — per-run uncheck wrote ``CRUCIBLE_STRICT_JSON=0``
# while the pipeline kept reading ``STRICT_JSON`` from the inherited
# parent env (still ``1``).  Tests passed because they only
# verified the mapping was internally self-consistent, never that
# the RHS keys match what the pipeline actually reads — a textbook
# "producer is tested, consumer wiring is not" trap (see CLAUDE.md
# § 9.5 for the test pattern that now pins this).
_STORE_TRUE_FLAG_TO_ENV: dict[str, str] = {
    "cache":       "LOCAL_CACHE",
    "strict_json": "STRICT_JSON",
    "cost_trace":  "COST_TRACE",
}


def _resolve_run_insights_env_overrides(flags: dict[str, Any]) -> dict[str, str]:
    """Maps per-run flag toggles → subprocess env vars.

    Returned dict is merged into ``_child_env`` by ``_run_worker``.  Only
    explicitly True / False values produce an entry; missing / None values
    leave the parent env (i.e. ``.env`` defaults) in place so the panel's
    "untouched" state behaves identically to "the user opened Idea mode
    without changing anything".

    v1.1.0 fourth-pass: also resolves ``_STORE_TRUE_FLAG_TO_ENV`` so the
    three legacy ``store_true``-only flags (``cache`` / ``strict_json`` /
    ``cost_trace``) can finally be per-run-disabled.  The function name
    is kept for backward compatibility with the four call sites; despite
    the "run_insights" in the name, this is now the canonical
    flags→env resolver for ALL env-backed bool flags.
    """
    out: dict[str, str] = {}
    if not isinstance(flags, dict):
        return out
    # Run-insights toggles (cleanly bidirectional True/False ↔ "1"/"0").
    for flag_key, env_key in _RUN_INSIGHTS_FLAG_TO_ENV.items():
        val = flags.get(flag_key)
        if val is True:
            out[env_key] = "1"
        elif val is False:
            out[env_key] = "0"
    # Store-true legacy flags that lack ``--no-`` CLI form: same
    # bidirectional resolution.  Without this branch, the only effect
    # of unchecking the box was visual — the run_insights ledger and
    # cache code paths still ran because the env var stayed at its
    # ``.env`` default.
    for flag_key, env_key in _STORE_TRUE_FLAG_TO_ENV.items():
        val = flags.get(flag_key)
        if val is True:
            out[env_key] = "1"
        elif val is False:
            out[env_key] = "0"
    return out


def _run_worker(
    run_id: str,
    cmd: list[str],
    stdin_text: str,
    env_overrides: dict[str, str] | None = None,
) -> None:
    """Shared worker function for starting a subprocess, streaming its output into
    ``_runs[run_id]``, and handling all lifecycle state transitions.

    This function is the single canonical implementation; both ``api_start_run``
    and ``webhook_trigger`` launch it via ``threading.Thread``.

    Features implemented here:
      - Feature 1: AWAIT_INPUT protocol detection, stdin_pipe storage
      - Feature 2: stage timing + token count tracking
      - Run Insights per-run override: ``env_overrides`` (computed upstream by
        ``_resolve_run_insights_env_overrides``) is merged into ``_child_env``
        AFTER the standard PYTHONUTF8 / CRUCIBLE_RUN_ID seed so the operator's
        single-run toggle wins over ``.env`` defaults but never overwrites the
        correlation id.

    v1.1.2 (audit fix G5-C-MED-6)
    -----------------------------
    Wraps the entire worker body in a ``threading.BoundedSemaphore`` acquire
    so concurrent active runs are capped at ``_RUNS_MAX_CONCURRENT`` (default
    4, env-override ``CRUCIBLE_WEBUI_MAX_CONCURRENT_RUNS``).  When the cap
    is reached, additional ``/api/run`` / ``/webhook/trigger`` / ``/api/ab-test/run``
    invocations block here in their respective threads — the operator/HTTP
    caller already received their ``run_id`` and ``status=starting``, so the
    SSE stream simply doesn't see line activity until a worker slot opens.
    Subsequent runs still appear in the dashboard run list.  The semaphore
    is bounded so a programming error that releases without acquiring
    raises immediately rather than silently inflating the cap.
    """
    proc: "subprocess.Popen[str] | None" = None
    # v1.1.2 (sixth-pass H-5): bound the semaphore acquire and re-check the
    # cancellation flag immediately after.  Previously a saturated worker
    # pool would pin this thread forever on ``acquire()`` and — once a slot
    # opened — happily spawn the subprocess even though the operator had
    # issued ``DELETE /api/run/<id>`` while we waited.  The blocked record
    # also lacked an ``ended_at`` field, so ``_evict_stale_runs`` never
    # reclaimed it (memory leak).  60 seconds is a generous burst-queue
    # tolerance; longer-than-that means the operator's intent has almost
    # certainly drifted.
    _SEM_ACQUIRE_TIMEOUT_SEC = 60.0
    acquired = False
    try:
        try:
            acquired = _runs_semaphore.acquire(timeout=_SEM_ACQUIRE_TIMEOUT_SEC)
        except Exception:
            acquired = False
        if not acquired:
            with _runs_lock:
                _rec_to = _runs.get(run_id)
                if _rec_to is not None:
                    if _rec_to.get("status") != "cancelled":
                        _rec_to["status"] = "error"
                        _rec_to["output"].append(
                            "[WEBUI ERROR] Worker slot acquire timed out after "
                            f"{_SEM_ACQUIRE_TIMEOUT_SEC:.0f}s (cap={_RUNS_MAX_CONCURRENT})."
                        )
                    if _rec_to.get("ended_at") is None:
                        _rec_to["ended_at"] = time.time()
                    if _rec_to.get("returncode") is None:
                        _rec_to["returncode"] = -1
                    _rec_to["awaiting_input"] = False
            return
        # Re-check the cancellation flag now that we hold a worker slot.
        # A DELETE that landed while we were queued must NOT result in a
        # subprocess being spawned: the operator's intent was to abandon.
        _cancelled_pre_spawn = False
        with _runs_lock:
            _rec_pre = _runs.get(run_id)
            if _rec_pre is None or _rec_pre.get("status") == "cancelled":
                _cancelled_pre_spawn = True
                if _rec_pre is not None:
                    if _rec_pre.get("ended_at") is None:
                        _rec_pre["ended_at"] = time.time()
                    if _rec_pre.get("returncode") is None:
                        _rec_pre["returncode"] = -1
                    _rec_pre["awaiting_input"] = False
        if _cancelled_pre_spawn:
            return
        # Force UTF-8 I/O on the child Python process so that its stdout
        # is always UTF-8-encoded, matching the encoding="utf-8" we use
        # when reading back.  Without this, Windows uses the console
        # code-page (cp950 / cp936) which produces mojibake in the WebUI.
        _child_env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            # v1.1.0: propagate the WebUI's run_id into the spawned pipeline so
            # all telemetry, log lines, and run_insights ledger entries share
            # the same correlation id.  Without this, the pipeline generates
            # an unrelated UUID and the per-run Insights tab cannot find any
            # events because the ledger run_id never matches `sess.run_id`.
            "CRUCIBLE_RUN_ID": run_id,
        }
        # Per-run Run Insights toggle overrides — never permitted to touch
        # CRUCIBLE_RUN_ID (defensive: callers should not pass it, but we
        # strip it just in case to keep correlation intact).
        if env_overrides:
            for k, v in env_overrides.items():
                if k == "CRUCIBLE_RUN_ID":
                    continue
                _child_env[k] = v
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

        # Stream stdout line by line.
        # v1.1.2 (audit fix G5-C-MED-9): hard-cap the in-memory output ring
        # at ``_RUNS_MAX_OUTPUT_LINES`` so a long-running chatty pipeline
        # (8-hour runs are within the supported envelope) cannot consume
        # GBs of Flask memory.  Older lines are evicted FIFO from the head;
        # the ``output_evicted`` counter tracks how many lines were dropped
        # so the SSE generator can serve a graceful "[NOTE] earlier output
        # truncated" notice to slow resumers without breaking the
        # cumulative ``sent`` index used for resume.  Tail is preserved
        # because operators investigating cost / token usage / final
        # verdict need the LAST lines, not the first; the truncated head
        # is recoverable from saved_projects/ on disk.
        # Noisy SDK debug lines (httpx wire logs, LiteLLM internals, etc.)
        # are suppressed at the frontend display layer, not here, so the
        # full output is always available for post-run inspection.
        # Guard against KeyError: _evict_stale_runs() (fired from the SSE
        # generator's `finally` block after client disconnect) can remove
        # the run_id entry while the process is still producing output.
        line_index = 0
        for line in proc.stdout:
            stripped = line.rstrip("\n")
            # v1.1.2 (sixth-pass M-4): redact secrets and absolute paths
            # from every captured stdout line before it lands in the run's
            # output buffer.  15+ ``print(f"[Error] ...: {e}")`` call sites
            # across section_01 / 02 / 04 / 05 / 06 historically embedded
            # raw exception text that can carry ``sk-or-v1-…`` /
            # ``sk-ant-…`` API keys, internal hostnames, and absolute
            # filesystem paths.  Patching the source per call site is
            # 15+ edits with the same one-letter regression every release;
            # the capture boundary here is the single chokepoint every
            # subprocess stdout line MUST pass through, so redacting here
            # is structurally one fix covers all.  ``max_len`` is raised
            # to 8000 so full pipeline output is preserved (the helper's
            # default 300 is sized for short API error messages).
            stripped = _redact_for_client(stripped, max_len=8000)
            with _runs_lock:
                run_rec = _runs.get(run_id)
                if run_rec is None:
                    line_index += 1
                    continue
                _out_buf = run_rec["output"]
                _out_buf.append(stripped)
                # FIFO eviction: drop oldest line(s) once buffer exceeds cap.
                if len(_out_buf) > _RUNS_MAX_OUTPUT_LINES:
                    _excess = len(_out_buf) - _RUNS_MAX_OUTPUT_LINES
                    del _out_buf[:_excess]
                    run_rec["output_evicted"] = (
                        int(run_rec.get("output_evicted") or 0) + _excess
                    )

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
        # v1.1.2 (audit fix G5-C-MED-6): release the worker-slot reservation
        # acquired at the top of this function.  Release in the outermost
        # finally so an exception during subprocess.Popen or any later step
        # still returns the slot to the pool — without this a single
        # crashing run would permanently shrink the worker pool.
        #
        # v1.1.2 (sixth-pass H-5): only release if the acquire actually
        # succeeded.  The acquire is now bounded by
        # ``_SEM_ACQUIRE_TIMEOUT_SEC`` and may legitimately return False,
        # in which case releasing would inflate the cap (BoundedSemaphore
        # would raise ValueError on the next over-release).
        if acquired:
            try:
                _runs_semaphore.release()
            except ValueError:
                # BoundedSemaphore raises if released too many times — defensive,
                # should not happen but never propagate this failure.
                LOGGER.warning(
                    "[webui] _runs_semaphore.release raised ValueError; "
                    "worker-slot accounting may have drifted"
                )


# ── Run management ────────────────────────────────────────────────────────────

@app.route("/api/run", methods=["POST"])
def api_start_run():
    payload = request.get_json(silent=True) or {}
    run_id = uuid.uuid4().hex[:8]

    try:
        cmd, stdin_text = _build_command(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    env_overrides = _resolve_run_insights_env_overrides(payload.get("flags", {}) or {})

    with _runs_lock:
        _runs[run_id] = {
            "id": run_id,
            "cmd": cmd,
            "stdin": stdin_text,
            "status": "starting",
            "output": [],
            # v1.1.2 (audit fix G5-C-MED-9): FIFO eviction counter for the
            # capped output ring.  See _RUNS_MAX_OUTPUT_LINES.
            "output_evicted": 0,
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

    threading.Thread(
        target=_run_worker,
        args=(run_id, cmd, stdin_text),
        kwargs={"env_overrides": env_overrides},
        daemon=True,
    ).start()
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
        # v1.1.2 (audit fix G5-C-MED-9): one-shot notice when the resumer
        # has lost data due to head eviction (output ring cap).  Set to
        # True after the notice is emitted so the SSE stream doesn't spam
        # it on every poll.
        truncation_notified = False
        try:
            while True:
                # ── Snapshot run state under lock ─────────────────────────
                # The worker thread writes run["output"] and run["status"]
                # concurrently.  Taking a snapshot inside the lock prevents
                # data races (premature __done__, lost output lines).
                with _runs_lock:
                    run = _runs.get(run_id)
                    if run is not None:
                        # v1.1.2 (audit fix G5-C-MED-9): map cumulative
                        # ``sent`` to a buffer-local slice via the FIFO
                        # eviction counter.  Three cases:
                        #
                        # 1. sent >= evicted: sliceable; serve [sent - evicted:].
                        # 2. sent <  evicted: resumer lost data — slice from
                        #    buffer start and (one-time) emit a truncation
                        #    notice.  Sent is advanced to evicted so the
                        #    consumer's cumulative index converges to
                        #    where the buffer actually starts.
                        evicted = int(run.get("output_evicted") or 0)
                        if sent < evicted:
                            new_lines = list(run["output"])
                            if not truncation_notified:
                                new_lines.insert(
                                    0,
                                    "[NOTE] Output buffer truncated: "
                                    f"{evicted - sent} earlier line(s) evicted "
                                    "due to memory cap. Tail preserved; head "
                                    "is recoverable from saved_projects/.",
                                )
                                truncation_notified = True
                            sent = evicted
                        else:
                            new_lines = run["output"][sent - evicted:]
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
                    #
                    # v1.1.2 (audit fix G5-C-MED-10): IMPORTANT — keepalive
                    # payloads do NOT increment ``sent``.  This is by design:
                    # ``sent`` is the cumulative count of REAL output lines so
                    # the resume-from-N replay logic stays correct.  If a
                    # future maintainer "fixes" the keepalive to use an SSE
                    # comment (`: keepalive`) the EventSource watchdog will
                    # fire silently during long LLM stages — that was the
                    # original bug this design pattern closes.  A regression
                    # test in tests/test_v1_1_2_audit_fixes.py pins this
                    # contract by reading the function source via
                    # ``inspect.getsource``.
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
                        # v1.1.2 (audit fix G5-C-MED-11): pad the final
                        # ``__done__`` event with a 2 KB SSE comment so
                        # proxies that buffer ≤ 4 KB (nginx with
                        # ``proxy_buffering on``, Cloudflare with default
                        # tier) flush immediately rather than holding the
                        # tiny terminal event in a partial-fill buffer.
                        # ``X-Accel-Buffering: no`` on the response
                        # disables this at the nginx layer when present
                        # but third-party CDNs / reverse proxies may not
                        # honour the header — the padding guarantees the
                        # flush.  Comment lines (``:`` prefix) are stripped
                        # by EventSource before reaching ``onmessage`` so
                        # the frontend sees nothing extra.
                        yield ":" + (" " * 2048) + "\n\n"
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

    # v1.0.5 round 4: round to 6 decimals to match cost_tracker's persistence
    # precision.  OpenRouter per-call costs reach the 6th decimal (e.g.
    # $0.000003 for cheap-model cached tokens) — rounding to 5 silently
    # drops 90 % of the lowest-cost calls to $0.00 when summed across many
    # runs.  Expose ``total_cost_usd`` as an explicit alias so newer
    # clients can disambiguate USD billing from any legacy cost-units field.
    return jsonify({
        "total_saved_runs": len(saved),
        "total_cost":     round(total_cost, 6),
        "total_cost_usd": round(total_cost, 6),
        "avg_quality":    round(avg_quality, 3),
        "saved_runs":     saved[:20],
        "session_runs":   session_runs,
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


# ── Run Insights ledger (v1.1.0) ──────────────────────────────────────────────
# Browse the .crucible_insights/ ledger written by features/run_insights.
# Three endpoints:
#   GET /api/insights/summary          — total event counts per stream + recent
#   GET /api/insights/events           — paginated event feed (?stream=, ?run_id=,
#                                        ?project_name=, ?since=, ?cursor=, ?limit=)
#   GET /api/run/<run_id>/insights     — events filtered to a single run_id
#
# These are *read-only* — there is no write endpoint by design.  Recording
# happens inside the pipeline; the WebUI is purely an observer.

_INSIGHTS_STREAMS = ("output", "error", "debate", "params")


def _insights_root() -> Path:
    """Resolve the ledger root from env, anchored to PROJECT_ROOT."""
    raw = (os.environ.get("CRUCIBLE_RUN_INSIGHTS_DIR") or ".crucible_insights").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p


def _read_jsonl_stream(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        out.append(obj)
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        LOGGER.debug("insights: read %s failed: %s", path, exc)
    return out


def _iter_jsonl_stream(path: Path) -> Iterator[dict[str, Any]]:
    """Lazily yield decoded JSONL records from *path*.

    v1.1.2 (audit fix G2-C-HIGH-2): the original ``_read_jsonl_stream``
    materialises every line into a Python list, which made
    ``/api/insights/summary``, ``/api/insights/events`` and
    ``/api/run/<id>/insights`` O(N) in ledger size for every request.  The
    user plan is to accumulate 2-4 weeks of real-world ledger data before
    v1.2.0 ships; that is hundreds of thousands of lines, and a dashboard
    poll would OOM the Flask process.  This generator yields one decoded
    record at a time so callers can apply filters lazily and break early
    when ``len(collected) >= limit`` is reached.

    Defensive: malformed JSON lines are silently skipped (matching the
    legacy list helper's behaviour); OSError on open is logged and the
    generator yields nothing.
    """
    if not path.exists():
        return
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        LOGGER.debug("insights: read %s failed: %s", path, exc)
        return
    try:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _tail_jsonl_stream(path: Path, n: int = 5) -> list[dict[str, Any]]:
    """Return the last *n* well-formed JSON records of *path* without
    materialising the whole file.

    v1.1.2 (audit fix G2-C-HIGH-2): used by ``api_insights_summary`` so
    the dashboard's recent-events feed reads only the trailing slice of
    each stream rather than the entire JSONL.  The implementation reads
    blocks from the end of the file via ``os.SEEK_END``-relative seeks,
    keeps the last *n* parseable records, and stops once the buffer
    contains enough.  Falls back to the full-file streaming reader on
    any OSError (small ledger / unusual filesystem) so the dashboard
    still works in degraded conditions.
    """
    if n <= 0 or not path.exists():
        return []
    block_size = 64 * 1024  # 64 KiB
    keep: list[dict[str, Any]] = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            pos = file_size
            buf = b""
            while pos > 0 and len(keep) < n:
                read_size = min(block_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size) + buf
                # Save any incomplete leading bytes for the next iteration.
                first_nl = chunk.find(b"\n") if pos > 0 else -1
                if first_nl >= 0:
                    buf = chunk[:first_nl]
                    chunk = chunk[first_nl + 1:]
                else:
                    buf = b""
                # Parse the chunk in reverse so we keep the newest records.
                lines = chunk.split(b"\n")
                for raw in reversed(lines):
                    stripped = raw.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(obj, dict):
                        keep.append(obj)
                        if len(keep) >= n:
                            break
    except OSError as exc:
        LOGGER.debug("insights: tail %s failed: %s", path, exc)
        # Degraded fallback: stream the whole file and slice.
        all_records = list(_iter_jsonl_stream(path))
        return all_records[-n:]
    # ``keep`` was built newest-first; preserve that order.
    return keep[:n]


def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with open(path, "rb") as fh:
            return sum(1 for ln in fh if ln.strip())
    except OSError:
        return 0


@app.route("/api/insights/summary")
def api_insights_summary():
    """Return aggregate stats for the insights ledger.

    Output::

        {
          "enabled": bool,
          "root": "<absolute path>",
          "schema_version": 1,
          "streams": {
            "output": {"lines": int, "exists": bool},
            "error":  {"lines": int, "exists": bool},
            "debate": {"lines": int, "exists": bool},
            "params": {"lines": int, "exists": bool}
          },
          "recent": [<last 5 events across all streams, newest first>]
        }
    """
    enabled = (
        os.environ.get("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    root = _insights_root()
    streams: dict[str, dict[str, Any]] = {}
    recent_pool: list[dict[str, Any]] = []
    for s in _INSIGHTS_STREAMS:
        path = root / f"{s}.jsonl"
        lines = _count_jsonl_lines(path)
        streams[s] = {"lines": lines, "exists": path.exists()}
        if lines > 0:
            # v1.1.2 (audit fix G2-C-HIGH-2): tail-only read (seek to end +
            # read backward) keeps the dashboard's recent-events feed O(n)
            # in *tail size* rather than O(N) in *ledger size*.  Previously
            # the entire JSONL was materialised, then sliced to 5.
            recent_pool.extend(_tail_jsonl_stream(path, n=5))
    recent_pool.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return jsonify({
        "enabled": enabled,
        "root": str(root),
        "schema_version": 1,
        "streams": streams,
        "recent": recent_pool[:10],
    })


@app.route("/api/insights/events")
def api_insights_events():
    """Paginated event feed.

    Query params:
        stream       — output|error|debate|params (default: all)
        run_id       — filter by run_id
        project_name — filter by project_name (case-insensitive substring)
        since        — ISO-8601 ts lower bound
        kind         — filter by event kind (e.g. error_record)
        limit        — max events to return (1..500, default 100)
    """
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except (ValueError, TypeError):
        limit = 100

    target_streams: tuple[str, ...]
    requested_stream = (request.args.get("stream") or "").strip().lower()
    if requested_stream and requested_stream in _INSIGHTS_STREAMS:
        target_streams = (requested_stream,)
    else:
        target_streams = _INSIGHTS_STREAMS

    run_id_filter = (request.args.get("run_id") or "").strip()
    project_filter = (request.args.get("project_name") or "").strip().lower()
    since_filter = (request.args.get("since") or "").strip()
    kind_filter = (request.args.get("kind") or "").strip()

    root = _insights_root()
    collected: list[dict[str, Any]] = []
    # v1.1.2 (audit fix G2-C-HIGH-2): stream lazily via ``_iter_jsonl_stream``
    # instead of materialising every line, and cap the working set at
    # ``limit * 4`` so a pathological accumulating ledger cannot OOM the
    # Flask worker.  Once the cap is reached we still want to return a
    # representative slice — we sort and truncate at the end as before.
    soft_cap = max(limit * 4, 200)
    for s in target_streams:
        if len(collected) >= soft_cap:
            break
        for ev in _iter_jsonl_stream(root / f"{s}.jsonl"):
            if run_id_filter and str(ev.get("run_id") or "") != run_id_filter:
                continue
            if project_filter:
                pn = str(ev.get("project_name") or "").lower()
                if project_filter not in pn:
                    continue
            if since_filter and str(ev.get("ts") or "") < since_filter:
                continue
            if kind_filter and str(ev.get("kind") or "") != kind_filter:
                continue
            collected.append(ev)
            if len(collected) >= soft_cap:
                break

    # Sort newest first, then truncate.
    collected.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
    return jsonify({
        "events": collected[:limit],
        "total_matched": len(collected),
        "truncated": len(collected) > limit,
    })


@app.route("/api/run/<run_id>/insights")
def api_run_insights(run_id: str):
    """All insight events for a single run_id, grouped by stream."""
    root = _insights_root()
    grouped: dict[str, list[dict[str, Any]]] = {s: [] for s in _INSIGHTS_STREAMS}
    # v1.1.2 (audit fix G2-C-HIGH-2): stream lazily.  Per-run row counts
    # are bounded by the pipeline structure (one params/output, a handful
    # of debate/error events per run) so no soft cap is required here —
    # the filter itself bounds memory.
    for s in _INSIGHTS_STREAMS:
        for ev in _iter_jsonl_stream(root / f"{s}.jsonl"):
            if str(ev.get("run_id") or "") == run_id:
                grouped[s].append(ev)
    total = sum(len(v) for v in grouped.values())
    return jsonify({
        "run_id": run_id,
        "total": total,
        "streams": grouped,
    })


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
        return _safe_500(exc, "api_list_projects: SAVED_PROJECTS_DIR.iterdir")

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
        # v1.1.2 (sixth-pass H-4): redact the raw OSError before exposing
        # it.  This path 200s on partial failure (frontend renders the
        # empty-state) but historically embedded the absolute path of the
        # saved-projects directory.
        _log_id = uuid.uuid4().hex[:8]
        try:
            LOGGER.exception(
                "[webui] api_list_projects iterdir failed (log_id=%s)",
                _log_id,
            )
        except Exception:
            pass
        return jsonify({
            "runs": [],
            "error": "directory enumeration failed",
            "log_id": _log_id,
        })

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

    env_overrides = _resolve_run_insights_env_overrides(payload.get("flags", {}) or {})

    with _runs_lock:
        _runs[run_id] = {
            "id": run_id,
            "cmd": cmd,
            "stdin": stdin_text,
            "status": "starting",
            "output": [],
            # v1.1.2 (audit fix G5-C-MED-9): FIFO eviction counter for the
            # capped output ring.  See _RUNS_MAX_OUTPUT_LINES.
            "output_evicted": 0,
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

    threading.Thread(
        target=_run_worker,
        args=(run_id, cmd, stdin_text),
        kwargs={"env_overrides": env_overrides},
        daemon=True,
    ).start()
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
        # v1.1.2 (sixth-pass H-4): route through ``_safe_500`` so the raw
        # ``OSError(2, '...')`` / fd-path / pipe-name does not leak to the
        # browser; operator gets a log_id to grep the full traceback.
        return _safe_500(exc, "api_signal stdin write")

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


def _ipv6_embedded_v4(addr: "ipaddress.IPv6Address") -> Optional["ipaddress.IPv4Address"]:
    """Extract an embedded IPv4 address from various IPv6 forms.

    v1.1.0 fourth-pass (F-2): closes the SSRF bypass where attackers
    pass `::10.0.0.1`, `::192.168.1.1` (RFC 4291 §2.5.5.1 deprecated
    IPv4-compatible form), `2002:0a00:0001::` (RFC 3056 6to4 wrapping
    a private v4), or `64:ff9b::10.0.0.1` (RFC 6052 NAT64 well-known
    prefix wrapping a private v4).  Python's
    ``ipaddress.ip_address.is_global`` reports True for all of these
    because they fall in the IPv6 unicast space; only the
    ``::ffff:w.x.y.z`` 4-mapped form has the explicit ``ipv4_mapped``
    accessor.  This helper detects all four embedding patterns.
    """
    # 1. IPv4-mapped (::ffff:w.x.y.z) — handled by stdlib accessor.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    # 2. IPv4-compatible (::w.x.y.z, deprecated RFC 4291 §2.5.5.1).
    #    Identified by the upper 96 bits being zero.
    packed = addr.packed  # 16 bytes
    if packed[:12] == b"\x00" * 12 and packed[12:] != b"\x00\x00\x00\x00":
        try:
            return ipaddress.IPv4Address(packed[12:])
        except (ValueError, ipaddress.AddressValueError):
            return None
    # 3. 6to4 (2002:wxyz:abcd::/16, RFC 3056) — wraps an IPv4 in
    #    bits 16-47 (the next 32 bits after the 0x2002 prefix).
    if packed[:2] == b"\x20\x02":
        try:
            return ipaddress.IPv4Address(packed[2:6])
        except (ValueError, ipaddress.AddressValueError):
            return None
    # 4. NAT64 well-known prefix (64:ff9b::/96, RFC 6052) — wraps an
    #    IPv4 in the final 32 bits.
    if packed[:12] == b"\x00\x64\xff\x9b" + b"\x00" * 8:
        try:
            return ipaddress.IPv4Address(packed[12:])
        except (ValueError, ipaddress.AddressValueError):
            return None
    return None


def _addr_is_safe(addr: "ipaddress._BaseAddress") -> bool:
    """Return True iff *addr* is a globally-reachable unicast address
    that we should permit outbound traffic to.

    Recursively unwraps IPv4-embedded-in-IPv6 forms — an IPv6 address
    that embeds a private IPv4 is itself unsafe, even if the IPv6
    bits would otherwise pass ``is_global``.

    v1.1.0 fifth-pass (G-2): Python's ``is_global`` returns True for
    multicast (224.0.0.0/4 IPv4 + ff00::/8 IPv6), reserved, and the
    "unspecified" 0.0.0.0/:: ranges — verified live with CPython 3.x
    that ``IPv4Address('224.0.0.1').is_global is True`` and
    ``IPv4Address('239.255.255.250').is_global is True`` (SSDP/UPnP).
    Without explicit rejection a NOTIFY_WEBHOOK_URL of
    ``http://239.255.255.250/`` would broadcast the payload across
    every host on the LAN.  Reject all non-unicast-global ranges.
    """
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _ipv6_embedded_v4(addr)
        if embedded is not None:
            return _addr_is_safe(embedded)  # recurse for full check
    # Reject multicast / reserved / unspecified / loopback / link-local
    # even when is_global would mis-report True.  is_global is a
    # necessary but not sufficient condition for "safe to make an
    # outbound request to" — the additional predicates close the gap.
    if (
        addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_loopback
        or addr.is_link_local
    ):
        return False
    return bool(addr.is_global)


def _is_safe_url(url: str) -> bool:
    """Return True if *url* points to a public (non-private) HTTPS/HTTP host.

    Rejects private/loopback/link-local IPs and non-HTTP schemes to prevent
    SSRF when the server makes outbound requests on behalf of a user.
    Ollama (localhost) is intentionally allowed via the ``allow_localhost``
    flag on the caller side — this function is strict by default.

    v1.1.0 third-pass hardening:

    * Reject userinfo components (``http://attacker.com@public.example``
      pattern) — ``urlparse`` returns the post-@ host as ``hostname``
      but ``urlopen`` honours the userinfo; without this guard a
      malicious URL with userinfo could smuggle credentials past the
      check.
    * Reject IPv6 zone-id syntax (``fe80::1%eth0``) — these are link-
      local by definition.
    * Reject IPv4-mapped IPv6 (``::ffff:10.0.0.1``) whose underlying
      address is private even if ``is_global`` mis-reports True on
      older Python builds.

    v1.1.0 fourth-pass hardening (F-2): also unwrap IPv4-compatible
    IPv6 (``::w.x.y.z``), 6to4 (``2002::/16``), and NAT64
    (``64:ff9b::/96``) forms — all three can embed private IPv4 while
    ``addr.is_global`` reports True at the IPv6 layer.  See
    ``_ipv6_embedded_v4``.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    # Userinfo smuggling: ``http://victim@evil.com/`` makes urlparse
    # return hostname="evil.com" but exposes the userinfo via Auth
    # headers downstream.  Treat any userinfo as untrusted
    # (including empty-string username — ``http://@host/`` is rejected
    # because the presence of the ``@`` itself is suspicious).
    if parsed.username is not None or parsed.password is not None:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    # Reject IPv6 scope-id form (link-local by definition).
    if "%" in hostname:
        return False
    try:
        addr = ipaddress.ip_address(hostname)
        return _addr_is_safe(addr)
    except ValueError:
        pass
    # Hostname is a DNS name — resolve and check all addresses
    # v1.1.2 (audit fix G5-C-MED-7): bound the DNS lookup at ~3 seconds.
    # ``socket.getaddrinfo`` honours ``socket.getdefaulttimeout()`` which
    # Python initialises to ``None`` (infinite) — a hanging DNS server
    # (no SERVFAIL, no NXDOMAIN, just no answer) would pin a Flask worker
    # forever, and the webhook retry loop could pin multiple workers and
    # take the whole WebUI down (Slowloris-on-DNS class).  We temporarily
    # set the per-process default timeout to 3 s for the duration of the
    # lookup and restore the previous value in the finally block.  The
    # global default is used because ``getaddrinfo`` has no per-call
    # timeout argument in stdlib.
    _prior_default_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(3.0)
        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, socket.timeout, OSError):
            return False
    finally:
        socket.setdefaulttimeout(_prior_default_timeout)
    for _fam, _type, _proto, _canon, sockaddr in infos:
        try:
            addr = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not _addr_is_safe(addr):
            return False
    return True


# v1.1.0 fifth-pass (G-3): the default ``urllib.request`` opener
# transparently follows 30x redirects, which trivially defeats the
# ``_is_safe_url`` guard.  An attacker-controlled HTTPS endpoint that
# passes the SSRF check can respond ``302 Location:
# http://169.254.169.254/latest/meta-data/...`` (AWS IMDS) or
# ``http://127.0.0.1:5000/api/env`` and ``urlopen`` will dutifully
# follow — sending any Authorization Bearer header to the internal
# host.  ``_safe_urlopen`` rejects 30x responses by default, then
# manually re-validates the new URL through ``_is_safe_url`` and
# re-issues the request, capped at ``max_redirects`` hops.
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Suppresses automatic redirect following in the urllib opener.

    Returning ``None`` from ``redirect_request`` causes
    ``urllib.request.OpenerDirector`` to surface the original 30x
    response as an ``HTTPError`` to the caller, who can then decide
    whether the new location is safe.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_SAFE_URL_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _safe_urlopen(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str = "GET",
    timeout: float = 5.0,
    max_redirects: int = 3,
    allow_localhost: bool = False,
):
    """Open *url* with SSRF + redirect-loop protection.

    Re-validates the URL via ``_is_safe_url`` on every hop (including
    after each redirect), preventing both DNS rebinding mid-retry and
    "redirect to internal" bypasses.  Returns the urllib response
    object on success.  Raises ``urllib.error.URLError`` if the URL
    is blocked or the redirect budget is exhausted.

    ``allow_localhost`` is intentionally ``False`` by default; only
    the Ollama-validation path opts in (Ollama runs on loopback by
    design).  All other call sites must keep the default.
    """
    seen: set[str] = set()
    current = url
    for _hop in range(max_redirects + 1):
        if current in seen:
            raise urllib.error.URLError("redirect loop detected")
        seen.add(current)

        if not (allow_localhost and _is_localhost_url(current)):
            if not _is_safe_url(current):
                raise urllib.error.URLError(
                    "blocked: target resolves to a private/internal "
                    "address (possibly via redirect)"
                )

        req = urllib.request.Request(
            current,
            data=data,
            headers=headers or {},
            method=method,
        )
        try:
            return _SAFE_URL_OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location") if exc.headers else None
                if not loc:
                    raise urllib.error.URLError(
                        "redirect without Location header"
                    )
                current = urllib.parse.urljoin(current, loc)
                # On 303 + GET-with-body, the next hop must be GET-no-body
                # per RFC 7231 §6.4.4; we also force-clear body on 301/302
                # for POSTs to avoid replaying the request body to a
                # potentially-different endpoint.
                if exc.code in (301, 302, 303):
                    method = "GET"
                    data = None
                continue
            raise
    raise urllib.error.URLError(
        f"too many redirects (>{max_redirects})"
    )


def _is_localhost_url(url: str) -> bool:
    """Lightweight check used by ``_safe_urlopen(allow_localhost=True)``.

    Conservative: any parse failure → False (default to safe rejection).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


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

    def _do_request(
        url: str,
        headers: dict[str, str],
        timeout: float,
        *,
        allow_localhost: bool = False,
    ) -> tuple[int, str | None]:
        """Returns (status_code, error_str_or_None).

        v1.1.0 fifth-pass (G-3): routed through ``_safe_urlopen`` so
        that 30x responses from an attacker-controlled endpoint cannot
        smuggle a request to ``169.254.169.254`` (AWS IMDS) or
        ``127.0.0.1`` past the SSRF check.  The Authorization header
        previously rode along on every auto-follow — a real cred-leak
        primitive.
        """
        try:
            with _safe_urlopen(
                url,
                headers=headers,
                method="GET",
                timeout=timeout,
                allow_localhost=allow_localhost,
            ) as resp:
                return resp.status, None
        except urllib.error.HTTPError as exc:
            return exc.code, None  # HTTP error but reachable
        except urllib.error.URLError as exc:
            return -1, str(getattr(exc, "reason", exc))
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
            _is_loopback = _host in ("localhost", "127.0.0.1", "::1")
            if not _is_loopback:
                if not _is_safe_url(base_url):
                    return jsonify({"error": "Ollama base_url must be localhost or a public host"}), 400
            url = base_url + "/api/tags"
            status_code, error_msg = _do_request(
                url, {}, timeout=3.0, allow_localhost=_is_loopback,
            )

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

    env_overrides_a = _resolve_run_insights_env_overrides(variant_a.get("flags", {}) or {})
    env_overrides_b = _resolve_run_insights_env_overrides(variant_b.get("flags", {}) or {})

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
                # v1.1.2 (audit fix G5-C-MED-9): see _RUNS_MAX_OUTPUT_LINES.
                "output_evicted": 0,
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

    threading.Thread(
        target=_run_worker,
        args=(run_id_a, cmd_a, stdin_a),
        kwargs={"env_overrides": env_overrides_a},
        daemon=True,
    ).start()
    threading.Thread(
        target=_run_worker,
        args=(run_id_b, cmd_b, stdin_b),
        kwargs={"env_overrides": env_overrides_b},
        daemon=True,
    ).start()

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
        return _safe_500(exc, "api_budget_status")

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

    v1.1.0: pipeline-driven notifications now go through the same SSRF
    guard used by ``/api/notify/test``.  Any URL resolving to a private,
    loopback, link-local, or unspecified address is refused without
    making the outbound request.
    """
    if not _is_safe_url(url):
        return {
            "success":          False,
            "attempts":         0,
            "last_status_code": -1,
            "last_error":       "blocked: target resolves to a private/internal address",
        }

    body_bytes = json.dumps(payload, ensure_ascii=False).encode(
        "utf-8"
    )
    last_status = -1
    last_error: str | None = None
    success = False
    attempt = 0  # ensures defined in return even if max_attempts <= 0

    for attempt in range(1, max_attempts + 1):
        # v1.1.0 third-pass: re-validate the URL on every attempt.  A
        # hostile DNS server with TTL=0 can flip ``example.com`` from
        # a public address to ``192.168.1.1`` between attempts, and
        # the up-front ``_is_safe_url`` call would not catch the
        # second-attempt rebinding.  Re-checking each loop closes the
        # DNS-rebinding window down to the time between guard and
        # urlopen (a few milliseconds).
        if not _is_safe_url(url):
            last_error = (
                "blocked: target now resolves to a private/internal "
                "address (DNS may have rebinded mid-retry)"
            )
            last_status = -1
            break

        ts = time.time()
        status_code = -1
        error_msg: str | None = None

        try:
            # v1.1.0 fifth-pass (G-3): use the redirect-aware safe
            # opener so a webhook receiver responding 302 to a
            # private/internal address can't smuggle the request
            # body to an internal endpoint.  ``_safe_urlopen`` clears
            # the body on 301/302/303 per RFC 7231 §6.4 to avoid
            # replaying the payload to a potentially-different host.
            with _safe_urlopen(
                url,
                # v1.1.0 fourth-pass: revert to bare ``application/json``.
                # RFC 8259 explicitly states JSON's encoding is UTF-8 and
                # the charset parameter is not part of the registered
                # media type; some strict webhook receivers (older Slack
                # / Discord variants) reject ``application/json;
                # charset=utf-8`` with a 400.  Body bytes are already
                # UTF-8 (json.dumps → .encode("utf-8")), so the
                # ``charset=utf-8`` suffix was pure surface area.
                headers={"Content-Type": "application/json"},
                data=body_bytes,
                method="POST",
                timeout=timeout,
            ) as resp:
                status_code = resp.status
        except urllib.error.HTTPError as exc:
            status_code = exc.code
        except urllib.error.URLError as exc:
            error_msg = str(getattr(exc, "reason", exc))
        except Exception as exc:
            error_msg = str(exc)

        # v1.1.2 (sixth-pass H-4): redact the captured error message before
        # the retry-state assignment so the in-process retry log AND the
        # DB INSERT both store the scrubbed form.  Webhook history rows
        # were previously written with raw ``str(exc)`` (carrying internal
        # hostnames + URL credentials), then echoed back to the operator
        # by ``api_webhook_history``.
        if error_msg:
            try:
                error_msg = _redact_for_client(error_msg, max_len=500)
            except Exception:
                # If redaction itself fails, keep the raw text rather than
                # losing the entire error — best-effort defence.
                pass

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

    v1.1.0: SSRF guard.  ``NOTIFY_WEBHOOK_URL`` is operator-controlled, but
    a malicious page that already passed the X-Requested-With check (e.g. a
    same-origin XSS) could set the env value to ``http://192.168.1.1/admin``
    and then trigger ``/api/notify/test`` to make the Flask server fire a
    request from its own network position.  We refuse to call out to any
    URL that resolves to a private / loopback / link-local address.

    Response: {"success": bool, "attempts": int, "last_status_code": int, "last_error": str|null}
    """
    notify_url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not notify_url:
        return jsonify({"error": "NOTIFY_WEBHOOK_URL is not configured"}), 503
    if not _is_safe_url(notify_url):
        return jsonify({
            "error": "NOTIFY_WEBHOOK_URL resolves to a private or loopback address",
            "detail": (
                "Refusing to call out to private network ranges (RFC 1918), "
                "loopback, link-local, or unspecified addresses. Configure "
                "NOTIFY_WEBHOOK_URL to a public HTTPS endpoint instead."
            ),
        }), 400

    result = _send_notification_with_retry(
        url=notify_url,
        payload={"event": "test", "source": "quantsaas-webui", "ts": time.time()},
        max_attempts=3,
        timeout=10.0,
    )
    status_code = 200 if result["success"] else 500
    # v1.1.2 (sixth-pass H-4): scrub ``last_error`` before returning so a
    # ``URLError(getaddrinfo)`` carrying an internal hostname or a webhook
    # URL with embedded credentials cannot leak through this operator-only
    # response.  Server-side log retains the unredacted detail.
    if not result.get("success"):
        try:
            LOGGER.warning(
                "[webui] api_notify_test all retries failed "
                "(status=%s attempts=%s)",
                result.get("last_status_code"),
                result.get("attempts"),
            )
        except Exception:
            pass
    safe_result = dict(result)
    safe_result["last_error"] = _redact_for_client(result.get("last_error"))
    return jsonify(safe_result), status_code


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
        return _safe_500(exc, "api_webhook_history")

    # v1.1.2 (sixth-pass H-4): scrub ``error_msg`` on read.  Old rows were
    # persisted before the audit; running every read through
    # ``_redact_for_client`` is a defence-in-depth that keeps a webhook URL
    # with embedded credentials or an absolute path from leaking even when
    # the historical DB row carries the raw text.
    history = [
        {
            "id":          row[0],
            "ts":          row[1],
            "url":         row[2],
            "status_code": row[3],
            "success":     bool(row[4]),
            "attempt":     row[5],
            "error_msg":   _redact_for_client(row[6]) if row[6] else None,
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
            # v1.1.2 (sixth-pass H-4): emit a log_id-stamped placeholder
            # rather than the raw ``OSError`` (which carries the absolute
            # path to ``saved_projects/<run>/metrics.prom``).  text/plain
            # parser-tolerant: Prometheus' scrape ignores comment lines.
            _log_id = uuid.uuid4().hex[:8]
            try:
                LOGGER.exception(
                    "[webui] api_v169_metrics read failed (log_id=%s)", _log_id,
                )
            except Exception:
                pass
            return Response(
                f"# Error reading metrics.prom (log_id={_log_id})\n",
                mimetype="text/plain",
            )
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
        # v1.1.2 (sixth-pass H-4): same redaction pattern as the read
        # branch above; ``Exception`` here captures importlib resolution
        # failures whose message includes the absolute path to the
        # crucible/features tree.
        _log_id = uuid.uuid4().hex[:8]
        try:
            LOGGER.exception(
                "[webui] api_v169_metrics generation failed (log_id=%s)",
                _log_id,
            )
        except Exception:
            pass
        return Response(
            f"# Error generating metrics (log_id={_log_id})\n",
            mimetype="text/plain",
        )


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
        return _safe_500(exc, "grafana dashboard read")
