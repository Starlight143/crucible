"""Disk-persistent SQLite cache for librarian search-provider responses.

v1.1.8 extended (Phase 2, Q1).  Eliminates the rate-limit burn caused by
repeated runs on the same topic — refinement iterations hit cache instead
of re-fetching from the provider.  Typical repeat-run HTTP cost drops 80%+.

Per-provider TTL (configured via LIBRARIAN_SEARCH_CACHE_TTL_*_HOURS env
vars).  Cache key: SHA256 of (provider, normalised_query).  Cache value:
JSON-serialised list of dicts (a ``ResearchCitation`` dump).

Concurrency:
- Per-process: ``threading.Lock`` guards the SQLite connection.
- Cross-process: SQLite WAL mode allows multiple Crucible processes to
  share the same DB file safely (the OS file lock is brief).

Failure modes (all logged + swallowed; never raises):
- Cache file corrupt → treat as miss, log warning.
- Disk full → skip write, log warning.
- TTL expired → treat as miss.
- ``LIBRARIAN_SEARCH_DISK_CACHE_ENABLED=0`` → ``get_default()`` returns
  ``None`` and callers fall back to direct provider calls.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Tri-modal import — see ``crucible/features/run_insights/recorder.py``
# for the rationale (three distinct package layouts based on entry point).
try:
    from .._env import env_bool, env_int, env_str
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool, env_int, env_str  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


# Default per-provider TTLs in hours (overridable via env).  Reflects how
# fast each source's results drift: DDG news drifts fast, arXiv papers
# essentially never change.  Providers not listed default to 12 hours.
_DEFAULT_TTL_HOURS_BY_PROVIDER: Dict[str, int] = {
    "websearch": 12,           # DDG via _search_websearch
    "context7": 6,
    "github": 24,
    "arxiv": 168,              # 1 week
    "paperswithcode": 168,
    "grep_app": 24,
    "openalex": 168,           # v1.1.8 Phase 3
    "crossref": 720,           # 30 days (DOIs essentially never change)
    "wikipedia": 168,
    "searxng": 12,             # opt-in (v1.1.8 Phase 3)
}


_SCHEMA_VERSION = 1


def _normalize_query(query: str) -> str:
    """Normalise a query for stable cache keys.

    - Strip leading / trailing whitespace.
    - Collapse internal whitespace runs.
    - Lowercase.

    Does NOT strip ``site:`` directives or other operator-injected scopes
    — those legitimately change the result set and stripping them would
    cause cache poisoning (the same key resolving to different result
    sets).
    """
    return " ".join((query or "").strip().lower().split())


def _cache_key(provider: str, query: str) -> str:
    """Compute the SQLite primary key for a (provider, query) pair.

    SHA256 truncated to 32 hex chars (128 bits).  Collision probability
    is astronomically low for the realistic cache sizes (< 10^9 entries).
    """
    normalized = _normalize_query(query)
    payload = f"{provider}\x1f{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _resolve_ttl_seconds(provider: str) -> int:
    """Resolve per-provider TTL from env, falling back to defaults.

    Env name template: ``LIBRARIAN_SEARCH_CACHE_TTL_<PROVIDER_UPPER>_HOURS``.
    Returns the TTL in seconds.  Non-positive values (typo / hostile env)
    fall back to the hardcoded default for that provider.
    """
    env_name = f"LIBRARIAN_SEARCH_CACHE_TTL_{provider.upper()}_HOURS"
    default_hours = _DEFAULT_TTL_HOURS_BY_PROVIDER.get(provider, 12)
    hours = env_int(env_name, default_hours)
    if hours is None or hours <= 0:
        hours = default_hours
    return int(hours) * 3600


def _resolve_cache_path() -> Path:
    """Resolve LIBRARIAN_SEARCH_CACHE_PATH to an absolute Path.

    Repo-relative paths (default
    ``saved_projects/.cache/search_cache.sqlite3``) are resolved against
    the repo root.  Absolute paths pass through unchanged.
    """
    raw = env_str(
        "LIBRARIAN_SEARCH_CACHE_PATH",
        "saved_projects/.cache/search_cache.sqlite3",
    )
    p = Path(raw)
    if not p.is_absolute():
        # repo root is three levels up from this file
        # (crucible/web_research/search_cache.py → repo root).
        repo_root = Path(__file__).resolve().parents[2]
        p = repo_root / raw
    return p


class SearchCache:
    """SQLite-backed cache for search-provider responses.

    Thread-safe within a single process via ``threading.Lock``.  Cross-
    process safe via SQLite WAL mode.

    Typical usage::

        cache = SearchCache.get_default()
        if cache is not None:
            hit = cache.get("websearch", "ETH funding rate")
            if hit is not None:
                return hit  # cache hit
        # ... do the actual provider call ...
        if cache is not None:
            cache.set("websearch", "ETH funding rate", results)
    """

    _instance_lock = threading.Lock()
    _instance: Optional["SearchCache"] = None

    @classmethod
    def get_default(cls) -> Optional["SearchCache"]:
        """Get the process-wide default ``SearchCache`` instance.

        Returns ``None`` if ``LIBRARIAN_SEARCH_DISK_CACHE_ENABLED=0`` or
        if the cache file cannot be opened (graceful degradation — the
        librarian falls back to direct provider calls).
        """
        if not env_bool("LIBRARIAN_SEARCH_DISK_CACHE_ENABLED", True):
            return None
        with cls._instance_lock:
            if cls._instance is None:
                try:
                    cls._instance = cls(_resolve_cache_path())
                except Exception as exc:  # pragma: no cover - rare FS error
                    LOGGER.warning(
                        "search_cache: failed to open cache file: %s",
                        exc,
                    )
                    return None
            return cls._instance

    @classmethod
    def reset_default(cls) -> None:
        """Reset the process-wide default instance.  Test-only.

        Closes the current connection before clearing the reference so
        we don't leak SQLite handles across tests.
        """
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.close()
                except Exception:
                    pass
            cls._instance = None

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path),
            timeout=5.0,
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        # WAL mode = safe concurrent access; faster than default rollback.
        # NORMAL synchronous mode is acceptable for cache (durability is
        # not required — at worst we lose a few entries on power loss).
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        except Exception:
            # If the file is already open in non-WAL mode (another
            # process), this can fail.  Not fatal — fall back to default.
            pass
        self._migrate()

    def _migrate(self) -> None:
        """Create schema if missing.  Idempotent — safe to call repeatedly."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta ("
                "  key TEXT PRIMARY KEY,"
                "  value TEXT NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS search_cache ("
                "  cache_key TEXT PRIMARY KEY,"
                "  provider TEXT NOT NULL,"
                "  query TEXT NOT NULL,"
                "  payload TEXT NOT NULL,"
                "  expires_at INTEGER NOT NULL,"
                "  created_at INTEGER NOT NULL"
                ")"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_search_cache_expires_at "
                "ON search_cache (expires_at)"
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES "
                "('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )

    def get(
        self,
        provider: str,
        query: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """Look up cached results for (provider, query).

        Returns the deserialised payload (a list of dicts) if found and
        unexpired, otherwise ``None``.  Never raises.
        """
        if not provider or not query:
            return None
        key = _cache_key(provider, query)
        now = int(time.time())
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT payload, expires_at FROM search_cache "
                    "WHERE cache_key = ?",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            payload_str, expires_at = row
            if int(expires_at) <= now:
                return None
            data = json.loads(payload_str)
            if not isinstance(data, list):
                return None
            return data
        except Exception as exc:
            LOGGER.warning(
                "search_cache: get failed for %s: %s", provider, exc,
            )
            return None

    def set(
        self,
        provider: str,
        query: str,
        payload: List[Dict[str, Any]],
    ) -> bool:
        """Write a cache entry.

        Returns ``True`` on success, ``False`` on failure.  Failures are
        swallowed; this method never raises.

        Empty payloads are intentionally cached (negative cache — same
        query producing zero hits is a useful signal to avoid repeating).
        Use ``set_negative=False`` semantics by simply not calling set()
        on empty results if you want to retry next run.
        """
        if not provider or not query:
            return False
        if not isinstance(payload, list):
            return False
        key = _cache_key(provider, query)
        now = int(time.time())
        ttl = _resolve_ttl_seconds(provider)
        expires_at = now + ttl
        try:
            payload_str = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            LOGGER.warning(
                "search_cache: payload not JSON-serialisable for %s: %s",
                provider, exc,
            )
            return False
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO search_cache "
                    "(cache_key, provider, query, payload, expires_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, provider, query[:512], payload_str, expires_at, now),
                )
            return True
        except Exception as exc:
            LOGGER.warning(
                "search_cache: set failed for %s: %s", provider, exc,
            )
            return False

    def purge_expired(self) -> int:
        """Delete all entries past TTL.  Returns count deleted.

        Cheap to call; safe to invoke at end of each librarian stage to
        keep the DB size bounded.
        """
        now = int(time.time())
        try:
            with self._lock:
                cur = self._conn.execute(
                    "DELETE FROM search_cache WHERE expires_at <= ?",
                    (now,),
                )
                return int(cur.rowcount or 0)
        except Exception:
            return 0

    def count(self) -> int:
        """Return total number of cache entries.  For observability."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM search_cache"
                ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def close(self) -> None:
        """Close the SQLite connection.  Safe to call repeatedly."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
