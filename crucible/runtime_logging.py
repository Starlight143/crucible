from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

# Keys whose values are redacted in structured log fields.
# Match is case-insensitive substring: "api_key" matches "OPENAI_API_KEY".
#
# Use specific markers ("auth_token", "auth_key") rather than a bare "auth"
# fragment, because a substring match on "auth" would silently redact benign
# audit-trail field names like "author", "authority", "authored_by",
# "authentic_count".  The bare key "auth" itself is still redacted via the
# _SENSITIVE_KEY_EXACT set below.
_SENSITIVE_KEY_FRAGMENTS: frozenset[str] = frozenset({
    "api_key", "apikey", "token", "password", "passwd", "secret",
    "bearer", "authorization", "auth_token", "auth_key", "credential",
    "private_key", "access_key", "secret_key", "client_secret",
})

# Keys that match exactly (case-insensitive) but where a substring match
# would over-redact legitimate fields.  "auth" is the canonical example —
# a generic field literally named "auth" is almost certainly a credential,
# but "author"/"authority" are not.
_SENSITIVE_KEY_EXACT: frozenset[str] = frozenset({"auth"})

_REDACTED = "***REDACTED***"


def _redact_fields(fields: dict) -> dict:
    """Return a copy of *fields* with sensitive values replaced by _REDACTED.

    Two-tier match (case-insensitive):
        1. Exact equality against _SENSITIVE_KEY_EXACT — catches generic names
           like "auth" that would over-match as a substring (vs "author").
        2. Substring against _SENSITIVE_KEY_FRAGMENTS — catches conventional
           prefixed/suffixed names like "OPENAI_API_KEY" or "auth_token".
    """
    result: dict = {}
    for k, v in fields.items():
        k_lower = str(k).lower()
        if k_lower in _SENSITIVE_KEY_EXACT:
            result[k] = _REDACTED
        elif any(frag in k_lower for frag in _SENSITIVE_KEY_FRAGMENTS):
            result[k] = _REDACTED
        else:
            result[k] = v
    return result


_LOG_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "crucible_log_context", default=None
)
_CONFIGURED = False
_CONFIGURE_LOCK = threading.Lock()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


# Load ``.env`` exactly once, *before* any helper that consults ``os.environ``
# for log level / quiet flags.  Without this, operators who set
# ``CRUCIBLE_LOG_LEVEL=DEBUG`` in ``.env`` (per the WebUI Env Vars panel and
# ``README.md`` table) would be silently downgraded to ``INFO`` because the env
# var would never be read into ``os.environ``.  Stays completely silent if
# ``python-dotenv`` is not installed (the package is optional — operators who
# export their env vars from the shell stay on the same code path as before).
_DOTENV_LOADED: bool = False
_DOTENV_LOAD_LOCK: threading.Lock = threading.Lock()


def _load_dotenv_once() -> None:
    """Load the project-root ``.env`` into ``os.environ`` exactly once.

    Idempotent and silent on absence of either the file or the
    ``python-dotenv`` package.  Existing real environment variables
    always win — ``override=False`` mirrors the long-standing convention
    that shell-exported values take priority over file-defined ones, so
    this never silently rewrites a value the operator chose to set
    explicitly via ``export VAR=value`` before launching the run.

    Protected by ``_DOTENV_LOAD_LOCK`` so two threads racing through this
    function (a real possibility under
    Python 3.13+ free-threaded mode where the GIL no longer linearises
    them) cannot both call ``load_dotenv`` simultaneously and produce a
    half-applied env state.  Under CPython 3.12 with the GIL this is
    redundant but cheap.  Double-checked locking pattern keeps the hot
    path (already-loaded) lock-free.
    """
    global _DOTENV_LOADED
    # Hot-path fast exit: once the load is fully complete the flag is
    # set, and every subsequent call returns immediately without
    # acquiring the lock.  This makes the steady-state cost effectively
    # zero (one volatile read of a Python attribute).
    if _DOTENV_LOADED:
        return
    with _DOTENV_LOAD_LOCK:
        # Re-check inside the lock so the second thread that lost the
        # race observes the completed-load flag and returns without
        # re-loading.  Critically the flag is set *after* load_dotenv
        # returns (or raises) — never before — so any reader that sees
        # ``_DOTENV_LOADED == True`` is guaranteed os.environ already
        # reflects the loaded values.
        if _DOTENV_LOADED:
            return
        try:
            from dotenv import load_dotenv
        except Exception:
            # ``python-dotenv`` not installed — degrade silently.  Mark
            # the load complete so future calls hit the fast path; the
            # runtime falls back to whatever was already exported in
            # the shell, exactly as it did before this hook existed.
            _DOTENV_LOADED = True
            return
        # Walk up from this module's location to find the repository root.
        # ``runtime_logging.py`` lives in ``<repo>/crucible/``, so the
        # ``.env`` is at ``../.env`` — but be tolerant in case the package is
        # installed elsewhere (site-packages, vendored, etc.) by also checking
        # the current working directory.  The first existing ``.env`` wins.
        candidates = []
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.normpath(os.path.join(here, "..", ".env")))
        candidates.append(os.path.join(os.getcwd(), ".env"))
        seen: set[str] = set()
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            try:
                if os.path.isfile(path):
                    load_dotenv(path, override=False)
                    # Stop after the first successful load — we don't want
                    # a CWD-rooted ``.env`` to silently merge into a
                    # different repo's ``.env``.  The repo-root candidate
                    # is checked first so it always wins when both exist.
                    break
            except Exception:
                # ``load_dotenv`` should not raise, but treat any failure
                # as "skip this candidate" rather than crashing import.
                continue
        # Set the flag last — only after every load attempt has returned —
        # so any thread that exits the wait at the lock above is
        # guaranteed to observe a consistent post-load env state.
        _DOTENV_LOADED = True


# Loading at module import time guarantees the env vars are populated
# before *anything* in this module reads them — including
# ``_configured_log_level()`` below and the third-party-quiet flag in
# ``configure_logging``.  Other modules that read env vars at import time
# (e.g. ``backtest_runner.BACKTEST_TIMEOUT``) also benefit, provided they
# import ``runtime_logging`` (or any module that does) first.  The
# ``crucible/__init__.py`` re-exports below ensure this happens.
_load_dotenv_once()


def _configured_log_level() -> int:
    raw = str(os.environ.get("CRUCIBLE_LOG_LEVEL", "INFO") or "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO)


class StructuredFormatter(logging.Formatter):
    def __init__(self, *, json_mode: bool = False) -> None:
        super().__init__()
        self.json_mode = json_mode

    def format(self, record: logging.LogRecord) -> str:
        base_fields: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event_name = getattr(record, "event", None)
        if event_name:
            base_fields["event"] = event_name
        context_fields = _redact_fields(dict(_LOG_CONTEXT.get(None) or {}))
        record_fields = _redact_fields(dict(getattr(record, "structured_fields", {}) or {}))
        # Only exclude None; keep 0, 0.0, False, [], {} — these are legitimate
        # structured log field values (e.g. retries=0, cost=0.0, passes=False).
        merged_fields = {
            key: value
            for key, value in {**context_fields, **record_fields}.items()
            if value is not None
        }
        if record.exc_info:
            exc_type = record.exc_info[0]
            exc_value = record.exc_info[1]
            if exc_type is not None:
                base_fields["exc_type"] = exc_type.__name__
            if exc_value is not None:
                base_fields["exc_message"] = str(exc_value)
        if self.json_mode:
            payload = {**base_fields, **merged_fields}
            if record.exc_info:
                payload["traceback"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        detail_suffix = " ".join(f"{key}={value}" for key, value in merged_fields.items())
        rendered = (
            f"{base_fields['timestamp']} {base_fields['level']} {base_fields['logger']}: "
            f"{base_fields['message']}"
        )
        if event_name:
            rendered += f" event={event_name}"
        if detail_suffix:
            rendered += " " + detail_suffix
        if record.exc_info:
            rendered += "\n" + self.formatException(record.exc_info)
        return rendered


# Third-party loggers that emit DEBUG floods at our default INFO level
# whenever a downstream library (CrewAI verbose mode, LiteLLM, …) calls
# `logging.basicConfig(level=DEBUG)` or otherwise raises the global level.
# A real production capture showed ~71 ``DEBUG asyncio: Using proactor:
# IocpProactor`` lines plus hundreds of ``DEBUG httpcore.http11`` /
# ``DEBUG openai._base_client`` lines — these flood the WebUI terminal
# AND race with CrewAI's verbose Printer (whose ``┌──── 🤖 Agent Started
# ────┐`` box-drawing output ends up physically interleaved with
# DEBUG-level log records, producing corrupted lines like
# ``┌─2026-04-27T... DEBUG openai._base_client: Sending HTTP Request``).
# Pinning these loggers to WARNING silences them at the source regardless
# of any third-party basicConfig override.
_NOISY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "asyncio",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpx",
    "openai._base_client",
    "openai._client",
    "anthropic._base_client",
    "litellm",
    "LiteLLM",
)


def configure_logging(*, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    with _CONFIGURE_LOCK:
        if _CONFIGURED and not force:
            return
        root_logger = logging.getLogger()
        if root_logger.handlers and not force:
            _CONFIGURED = True
            _silence_noisy_third_party_loggers()
            return
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(StructuredFormatter(json_mode=_env_flag("CRUCIBLE_JSON_LOGS", False)))
        root_logger.handlers = [handler]
        root_logger.setLevel(_configured_log_level())
        _silence_noisy_third_party_loggers()
        _CONFIGURED = True


def _silence_noisy_third_party_loggers() -> None:
    """Pin noisy third-party loggers to WARNING.

    Called from :func:`configure_logging` after the root handler is in
    place.  Idempotent — re-running it (e.g., from ``force=True``) only
    raises the level for loggers that are still below WARNING.
    Respects an explicit override via the ``CRUCIBLE_QUIET_THIRDPARTY``
    env var (set to ``0``/``false``/``off`` to keep DEBUG output for
    troubleshooting).
    """
    if not _env_flag("CRUCIBLE_QUIET_THIRDPARTY", True):
        return
    for name in _NOISY_THIRD_PARTY_LOGGERS:
        logger = logging.getLogger(name)
        # Only raise the level — never lower it past whatever an operator
        # explicitly configured for this child logger.
        if logger.level == logging.NOTSET or logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


def current_log_context() -> Dict[str, Any]:
    return dict(_LOG_CONTEXT.get(None) or {})


def set_log_context(**fields: Any) -> None:
    # Exclude None only; keep 0, False, [], {} as valid context values.
    _LOG_CONTEXT.set({k: v for k, v in fields.items() if v is not None})


def update_log_context(**fields: Any) -> None:
    merged: Dict[str, Any] = current_log_context()
    for key, value in fields.items():
        if value is None or (isinstance(value, str) and value == ""):
            # Only treat the empty string "" as a removal signal, not any
            # other type whose __eq__ might raise (e.g. numpy arrays) or
            # return non-bool when compared with "".
            merged.pop(key, None)
        else:
            merged[key] = value
    _LOG_CONTEXT.set(merged)


def clear_log_context(*keys: str) -> None:
    if not keys:
        _LOG_CONTEXT.set({})
        return
    merged = current_log_context()
    for key in keys:
        merged.pop(key, None)
    _LOG_CONTEXT.set(merged)


@contextlib.contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    previous = current_log_context()
    update_log_context(**fields)
    try:
        yield
    finally:
        _LOG_CONTEXT.set(previous)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str,
    **fields: Any,
) -> None:
    logger.log(level, message, extra={"event": event, "structured_fields": fields})


def log_exception(
    logger: logging.Logger,
    event: str,
    message: str,
    *,
    exc_info: Any = True,
    **fields: Any,
) -> None:
    logger.error(
        message,
        exc_info=exc_info,
        extra={"event": event, "structured_fields": fields},
    )
