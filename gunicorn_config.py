"""
gunicorn_config.py
==================
Production Gunicorn configuration for the Crucible WebUI.

Usage
-----
From the repository root::

    gunicorn --config gunicorn_config.py "webui.app:app"

Or equivalently::

    gunicorn -c gunicorn_config.py webui.app:app

Environment variable overrides (all optional)
----------------------------------------------
GUNICORN_WORKERS          Number of worker processes (default: 2*CPU+1, max 8).
GUNICORN_THREADS          Threads per worker (default: 2).
GUNICORN_BIND             Bind address (default: "0.0.0.0:8080").
GUNICORN_TIMEOUT          Worker silent timeout in seconds (default: 300).
GUNICORN_KEEPALIVE        Keep-alive timeout in seconds (default: 5).
GUNICORN_MAX_REQUESTS     Requests before worker restart — prevents memory leak
                          (default: 500).
GUNICORN_LOG_LEVEL        Log verbosity: debug/info/warning/error (default: info).
GUNICORN_ACCESS_LOG       Path to access log file; "-" = stdout (default: "-").
GUNICORN_ERROR_LOG        Path to error log file; "-" = stderr (default: "-").
GUNICORN_PRELOAD          Preload app before forking (default: False — safer for
                          threads; set to "true" to enable).

Notes
-----
* **Worker class**: ``sync`` (default). The WebUI uses SSE streaming which works
  correctly with sync workers because each SSE request blocks one worker thread
  for its duration. For higher concurrency, switch to ``gevent`` or ``eventlet``
  and install the matching library.
* **Timeout**: Set to 300 s to accommodate long pipeline runs sent to background
  subprocesses. The worker itself does not block (pipeline runs in a subprocess),
  but SSE stream connections stay open for the full run duration.
* **Graceful timeout**: 30 s — allows in-flight SSE streams to drain cleanly
  before a worker is forcibly killed during rolling restarts.
"""
from __future__ import annotations

import logging
import os

# ── Binding ──────────────────────────────────────────────────────────────────

bind: str = os.environ.get("GUNICORN_BIND", "0.0.0.0:8080")

# ── Workers & concurrency ─────────────────────────────────────────────────────

def _default_workers() -> int:
    """2 * CPU cores + 1, capped at 8 to avoid OOM on agent-heavy hosts."""
    # os.cpu_count() is the preferred stdlib API (returns None if unknown,
    # rather than raising NotImplementedError like multiprocessing.cpu_count).
    cores = os.cpu_count() or 1
    return min(2 * cores + 1, 8)


_workers_env = os.environ.get("GUNICORN_WORKERS", "").strip()
# isdigit() alone would accept "0" (invalid for Gunicorn); enforce > 0
if _workers_env.isdigit() and int(_workers_env) > 0:
    workers: int = int(_workers_env)
else:
    workers = _default_workers()

_threads_env = os.environ.get("GUNICORN_THREADS", "").strip()
# threads=0 is invalid for Gunicorn; enforce > 0 same as workers
if _threads_env.isdigit() and int(_threads_env) > 0:
    threads: int = int(_threads_env)
else:
    threads = 2

worker_class: str = "sync"

# ── Timeouts ──────────────────────────────────────────────────────────────────

_timeout_env = os.environ.get("GUNICORN_TIMEOUT", "").strip()
# timeout=0 would disable worker kill-on-hang, which is unsafe in production;
# require > 0 and default to 300 s (long enough for SSE pipeline streams).
if _timeout_env.isdigit() and int(_timeout_env) > 0:
    timeout: int = int(_timeout_env)
else:
    timeout = 300

graceful_timeout: int = 30

_keepalive_env = os.environ.get("GUNICORN_KEEPALIVE", "").strip()
# keepalive=0 disables HTTP keep-alive entirely (every connection becomes
# Connection: close), causing extra TCP setup overhead on every request and
# breaking SSE streams that rely on a persistent connection.  Require > 0.
if _keepalive_env.isdigit() and int(_keepalive_env) > 0:
    keepalive: int = int(_keepalive_env)
else:
    keepalive = 5

# ── Request recycling ─────────────────────────────────────────────────────────

_max_req_env = os.environ.get("GUNICORN_MAX_REQUESTS", "").strip()
# max_requests=0 means "restart worker after every single request", which
# is a performance disaster (process fork on every request).  Require > 0.
if _max_req_env.isdigit() and int(_max_req_env) > 0:
    max_requests: int = int(_max_req_env)
else:
    max_requests = 500
# Jitter prevents all workers from restarting simultaneously
max_requests_jitter: int = 50

# ── Logging ───────────────────────────────────────────────────────────────────

# Whitelist Gunicorn-recognised levels — passing an unknown value (typo,
# old-style numeric level, etc.) makes Gunicorn raise ConfigurationError at
# startup with an opaque "invalid log level" message, killing every worker
# before serving any traffic.  Default to "info" on unknown input so a
# misconfigured env var degrades observability rather than denying service.
_loglevel_raw = os.environ.get("GUNICORN_LOG_LEVEL", "info").strip().lower()
loglevel: str = _loglevel_raw if _loglevel_raw in {"debug", "info", "warning", "error", "critical"} else "info"
accesslog: str = os.environ.get("GUNICORN_ACCESS_LOG", "-")
errorlog: str = os.environ.get("GUNICORN_ERROR_LOG", "-")
access_log_format: str = (
    '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sµs'
)

# ── App loading ───────────────────────────────────────────────────────────────

_preload_env = os.environ.get("GUNICORN_PRELOAD", "false").strip().lower()
preload_app: bool = _preload_env in ("1", "true", "yes")

# ── Process naming ────────────────────────────────────────────────────────────

proc_name: str = "crucible-webui"

# ── Security ──────────────────────────────────────────────────────────────────

# Prevent information disclosure via the Server header
# Default: trust X-Forwarded-For only from localhost (direct reverse proxy).
# In production, set GUNICORN_FORWARDED_ALLOW_IPS to the upstream proxy IP(s),
# e.g. "10.0.0.1" or "10.0.0.1,10.0.0.2".  Use "*" only if Gunicorn is behind
# a trusted network boundary that guarantees the header cannot be spoofed.
forwarded_allow_ips: str = os.environ.get(
    "GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1"
)
limit_request_line: int = 8192
limit_request_fields: int = 100
limit_request_field_size: int = 16384

# ── Server hooks ─────────────────────────────────────────────────────────────

def on_starting(server: object) -> None:  # noqa: ARG001
    """Log effective configuration on startup."""
    log = logging.getLogger("gunicorn.error")
    log.info(
        "Crucible WebUI starting — bind=%s workers=%d threads=%d timeout=%ds",
        bind,
        workers,
        threads,
        timeout,
    )


def worker_exit(server: object, worker: object) -> None:  # noqa: ARG001
    """Clean shutdown hook called by Gunicorn just before a worker process exits.

    RunRegistry instances are created per-request and hold a per-instance
    SQLite connection with WAL journal mode.  Python's reference counter and
    SQLite's own process-exit handling close those connections automatically,
    so no explicit teardown is needed here.

    This hook is intentionally kept as a no-op stub so that future per-worker
    cleanup can be added without changing the Gunicorn config interface.
    """
    pass
