"""
crucible/_file_cache.py
================================
LRU file-content cache for the analysis pipeline.

Inspired by Claude Code's file-state LRU cache: the runtime validation and
quality loop in ``section_06`` reads generated source files multiple times
across crew iterations.  This module provides a lightweight, thread-safe LRU
cache so repeated reads of the same unchanged file avoid redundant I/O.

Design notes
------------
* Pure stdlib — no external dependencies.
* Cache key = (absolute path, mtime_ns).  Any filesystem modification
  automatically invalidates the cached entry on the next access.
* Configurable max entries via constructor or ``FILE_CACHE_MAX_ENTRIES`` env var.
* ``get_or_read()`` is the primary API: returns cached bytes/text and
  populates on cache miss.
* Safe to share across threads (internal ``threading.Lock``).
* ``FileCache`` instances are cheap; the module also exposes a process-wide
  singleton via ``get_default_cache()``.

Usage::

    from crucible._file_cache import get_default_cache

    cache = get_default_cache()

    # Read text file (UTF-8, cached):
    content = cache.read_text("/path/to/generated_code.py")

    # Read bytes (cached):
    raw = cache.read_bytes("/path/to/artifact.json")

    # Invalidate a path explicitly (e.g., after writing):
    cache.invalidate("/path/to/generated_code.py")

    # Stats:
    print(cache.stats())   # {"hits": 42, "misses": 8, "size": 10}
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_MAX_ENTRIES = 128


try:
    from . import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default, clamp_min=1)


# ── Cache entry ───────────────────────────────────────────────────────────────

@dataclass
class _CacheEntry:
    """One cached file read."""
    path: str
    mtime_ns: int
    content: Union[bytes, str]


# ── FileCache ─────────────────────────────────────────────────────────────────

class FileCache:
    """
    Thread-safe LRU cache for file contents.

    Entries are keyed by ``(absolute_path, mtime_ns)``.  A file modification
    automatically produces a cache miss on the next read.

    Parameters
    ----------
    max_entries:
        Maximum number of entries to hold before evicting the least-recently-
        used entry.  Defaults to ``FILE_CACHE_MAX_ENTRIES`` env var → 128.
    """

    def __init__(self, *, max_entries: Optional[int] = None) -> None:
        self._max = int(
            max_entries
            if max_entries is not None
            else _env_int("FILE_CACHE_MAX_ENTRIES", _DEFAULT_MAX_ENTRIES)
        )
        self._max = max(1, self._max)
        # OrderedDict used as LRU: most-recently-used at the end (move_to_end)
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _abspath(path: str) -> str:
        return os.path.abspath(path)

    def _mtime_ns(self, path: str) -> int:
        """Return file mtime_ns, or -1 if the file does not exist."""
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return -1

    def _get_cached(self, abs_path: str, mtime_ns: int) -> Optional[Union[bytes, str]]:
        """Return cached content if the entry exists and is current."""
        entry = self._cache.get(abs_path)
        if entry is None or entry.mtime_ns != mtime_ns:
            return None
        # Move to end (most-recently-used)
        self._cache.move_to_end(abs_path)
        return entry.content

    def _put(self, abs_path: str, mtime_ns: int, content: Union[bytes, str]) -> None:
        """Insert or update a cache entry, evicting LRU if at capacity."""
        if abs_path in self._cache:
            self._cache.move_to_end(abs_path)
            self._cache[abs_path] = _CacheEntry(abs_path, mtime_ns, content)
        else:
            if len(self._cache) >= self._max:
                # Evict least-recently-used (first item)
                self._cache.popitem(last=False)
            self._cache[abs_path] = _CacheEntry(abs_path, mtime_ns, content)

    # ── Public API ────────────────────────────────────────────────────────────

    def read_bytes(self, path: str) -> bytes:
        """
        Return the binary contents of *path*, using the cache when possible.

        Raises ``OSError`` / ``FileNotFoundError`` on read failure (same as
        the built-in ``open()``).
        """
        abs_path = self._abspath(path)
        mtime_ns = self._mtime_ns(abs_path)
        with self._lock:
            cached = self._get_cached(abs_path, mtime_ns)
            if cached is not None:
                if isinstance(cached, bytes):
                    self._hits += 1
                    return cached
                # Type mismatch: entry was cached as text but bytes requested.
                # Evict inside the lock so a concurrent reader cannot get a stale
                # text hit before we overwrite the entry with bytes below.
                self._cache.pop(abs_path, None)
            self._misses += 1

        # Read outside the lock to avoid holding it during I/O
        with open(abs_path, "rb") as fh:
            content: bytes = fh.read()

        with self._lock:
            # Only cache when the file hasn't changed since we started reading.
            # If the file was modified *during* our read, current_mtime (the
            # post-read snapshot) will differ from mtime_ns (the pre-read
            # snapshot).  Storing stale bytes under the new mtime would cause
            # the next access to hit the cache and return wrong content.
            # Skipping the store forces a fresh read on the next call, which
            # will then cache correctly when the file is stable.
            current_mtime = self._mtime_ns(abs_path)
            if current_mtime == mtime_ns:
                self._put(abs_path, current_mtime, content)

        return content

    def read_text(self, path: str, *, encoding: str = "utf-8",
                  errors: str = "replace") -> str:
        """
        Return the text contents of *path*, using the cache when possible.

        Raises ``OSError`` / ``FileNotFoundError`` on read failure.
        """
        abs_path = self._abspath(path)
        mtime_ns = self._mtime_ns(abs_path)
        with self._lock:
            cached = self._get_cached(abs_path, mtime_ns)
            if cached is not None:
                if isinstance(cached, str):
                    self._hits += 1
                    return cached
                # Type mismatch: entry was cached as bytes but text requested.
                # Evict inside the lock so a concurrent reader cannot get a stale
                # bytes hit before we overwrite the entry with text below.
                self._cache.pop(abs_path, None)
            self._misses += 1

        with open(abs_path, "r", encoding=encoding, errors=errors) as fh:
            text: str = fh.read()

        with self._lock:
            # Same mtime-stability guard as read_bytes: only cache when the
            # file was not modified between our pre-read stat and post-read stat.
            current_mtime = self._mtime_ns(abs_path)
            if current_mtime == mtime_ns:
                self._put(abs_path, current_mtime, text)

        return text

    def get_or_read(
        self,
        path: str,
        *,
        binary: bool = False,
        encoding: str = "utf-8",
    ) -> Union[str, bytes]:
        """
        Unified read method: returns ``bytes`` when *binary=True*, else ``str``.
        """
        if binary:
            return self.read_bytes(path)
        return self.read_text(path, encoding=encoding)

    def invalidate(self, path: str) -> None:
        """Remove the cached entry for *path* (if any)."""
        abs_path = self._abspath(path)
        with self._lock:
            self._cache.pop(abs_path, None)

    def clear(self) -> None:
        """Evict all cached entries."""
        with self._lock:
            self._cache.clear()

    def stats(self) -> Dict[str, int]:
        """Return a snapshot of cache statistics."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
                "max_entries": self._max,
            }

    @property
    def hit_rate(self) -> float:
        """Cache hit rate in [0.0, 1.0].  Returns 0.0 when no reads occurred."""
        with self._lock:
            total = self._hits + self._misses
            return self._hits / total if total > 0 else 0.0


# ── Module-level singleton ────────────────────────────────────────────────────

_DEFAULT_CACHE: Optional[FileCache] = None
_CACHE_LOCK = threading.Lock()


def get_default_cache() -> FileCache:
    """Return the process-wide default ``FileCache`` (lazy-init, thread-safe)."""
    global _DEFAULT_CACHE
    with _CACHE_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = FileCache()
    return _DEFAULT_CACHE


def reset_default_cache() -> None:
    """Reset the process-wide cache (mainly for tests)."""
    global _DEFAULT_CACHE
    with _CACHE_LOCK:
        _DEFAULT_CACHE = None
