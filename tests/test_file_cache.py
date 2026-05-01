"""Tests for crucible._file_cache"""
from __future__ import annotations

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible._file_cache import (
    FileCache,
    get_default_cache,
    reset_default_cache,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_default_cache()
    yield
    reset_default_cache()


@pytest.fixture
def tmp_file(tmp_path):
    """Create a temp file and return its path."""
    p = tmp_path / "testfile.txt"
    p.write_text("hello world", encoding="utf-8")
    return str(p)


@pytest.fixture
def tmp_bytes_file(tmp_path):
    p = tmp_path / "bytes.bin"
    p.write_bytes(b"\x00\x01\x02\x03")
    return str(p)


# ── read_text ─────────────────────────────────────────────────────────────────

class TestReadText:
    def test_reads_content(self, tmp_file):
        cache = FileCache()
        assert cache.read_text(tmp_file) == "hello world"

    def test_returns_cached_on_second_call(self, tmp_file):
        cache = FileCache()
        r1 = cache.read_text(tmp_file)
        r2 = cache.read_text(tmp_file)
        assert r1 == r2
        assert cache.stats()["hits"] == 1

    def test_miss_on_modified_file(self, tmp_path):
        import time as _time
        p = tmp_path / "mod.txt"
        p.write_text("version1")
        cache = FileCache()
        cache.read_text(str(p))

        # Write new content first, then force mtime to a future value so the
        # cache (keyed on mtime_ns) definitely sees a change.  Calling os.utime
        # *before* write_text is wrong: write_text resets mtime to the current
        # wall clock, which can collide with the originally cached mtime_ns on
        # a fast machine and cause a spurious cache hit.
        p.write_text("version2")
        future = _time.time() + 2.0
        os.utime(str(p), (future, future))

        content = cache.read_text(str(p))
        assert content == "version2"
        assert cache.stats()["misses"] == 2  # initial + after modification

    def test_missing_file_raises(self, tmp_path):
        cache = FileCache()
        with pytest.raises((FileNotFoundError, OSError)):
            cache.read_text(str(tmp_path / "nonexistent.txt"))


# ── read_bytes ────────────────────────────────────────────────────────────────

class TestReadBytes:
    def test_reads_bytes(self, tmp_bytes_file):
        cache = FileCache()
        data = cache.read_bytes(tmp_bytes_file)
        assert data == b"\x00\x01\x02\x03"

    def test_bytes_cached(self, tmp_bytes_file):
        cache = FileCache()
        cache.read_bytes(tmp_bytes_file)
        cache.read_bytes(tmp_bytes_file)
        assert cache.stats()["hits"] == 1

    def test_missing_file_raises(self, tmp_path):
        cache = FileCache()
        with pytest.raises((FileNotFoundError, OSError)):
            cache.read_bytes(str(tmp_path / "no.bin"))


# ── get_or_read ───────────────────────────────────────────────────────────────

class TestGetOrRead:
    def test_text_mode(self, tmp_file):
        cache = FileCache()
        result = cache.get_or_read(tmp_file)
        assert isinstance(result, str)
        assert result == "hello world"

    def test_binary_mode(self, tmp_bytes_file):
        cache = FileCache()
        result = cache.get_or_read(tmp_bytes_file, binary=True)
        assert isinstance(result, bytes)


# ── invalidate ────────────────────────────────────────────────────────────────

class TestInvalidate:
    def test_invalidate_forces_re_read(self, tmp_path):
        p = tmp_path / "inv.txt"
        p.write_text("v1")
        cache = FileCache()
        cache.read_text(str(p))
        assert cache.stats()["size"] == 1

        cache.invalidate(str(p))
        assert cache.stats()["size"] == 0

        p.write_text("v2")
        content = cache.read_text(str(p))
        assert content == "v2"

    def test_invalidate_nonexistent_is_no_op(self, tmp_path):
        cache = FileCache()
        cache.invalidate(str(tmp_path / "ghost.txt"))  # must not raise


# ── clear ─────────────────────────────────────────────────────────────────────

class TestClear:
    def test_clear_empties_cache(self, tmp_file):
        cache = FileCache()
        cache.read_text(tmp_file)
        assert cache.stats()["size"] == 1
        cache.clear()
        assert cache.stats()["size"] == 0


# ── LRU eviction ─────────────────────────────────────────────────────────────

class TestLRUEviction:
    def test_evicts_lru_when_full(self, tmp_path):
        cache = FileCache(max_entries=3)
        files = []
        for i in range(4):
            p = tmp_path / f"f{i}.txt"
            p.write_text(f"content{i}")
            files.append(str(p))

        for f in files:
            cache.read_text(f)

        # Cache should hold at most 3 entries
        assert cache.stats()["size"] == 3

    def test_recently_used_not_evicted(self, tmp_path):
        cache = FileCache(max_entries=2)
        p0 = tmp_path / "f0.txt"
        p0.write_text("c0")
        p1 = tmp_path / "f1.txt"
        p1.write_text("c1")
        p2 = tmp_path / "f2.txt"
        p2.write_text("c2")

        cache.read_text(str(p0))  # miss
        cache.read_text(str(p1))  # miss
        cache.read_text(str(p0))  # hit → p0 becomes MRU
        cache.read_text(str(p2))  # miss → should evict p1 (LRU), not p0

        # p0 and p2 should be cached; p1 should have been evicted
        pre_hits = cache.stats()["hits"]
        cache.read_text(str(p0))  # should be a hit
        assert cache.stats()["hits"] == pre_hits + 1


# ── stats / hit_rate ──────────────────────────────────────────────────────────

class TestStats:
    def test_initial_stats(self):
        cache = FileCache()
        s = cache.stats()
        assert s["hits"] == 0
        assert s["misses"] == 0
        assert s["size"] == 0

    def test_hit_rate_zero_before_reads(self):
        cache = FileCache()
        assert cache.hit_rate == 0.0

    def test_hit_rate_computed_correctly(self, tmp_file):
        cache = FileCache()
        cache.read_text(tmp_file)  # miss
        cache.read_text(tmp_file)  # hit
        cache.read_text(tmp_file)  # hit
        assert cache.hit_rate == pytest.approx(2 / 3)


# ── Thread safety ─────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_concurrent_reads(self, tmp_file):
        cache = FileCache()
        results = []
        errors = []

        def read():
            try:
                results.append(cache.read_text(tmp_file))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r == "hello world" for r in results)
        assert cache.stats()["size"] == 1


# ── Singleton ─────────────────────────────────────────────────────────────────

class TestSingleton:
    def test_returns_same_instance(self):
        c1 = get_default_cache()
        c2 = get_default_cache()
        assert c1 is c2

    def test_reset_creates_new_instance(self):
        c1 = get_default_cache()
        reset_default_cache()
        c2 = get_default_cache()
        assert c1 is not c2

    def test_max_entries_env_var(self, monkeypatch):
        monkeypatch.setenv("FILE_CACHE_MAX_ENTRIES", "5")
        reset_default_cache()
        cache = get_default_cache()
        assert cache._max == 5


# ── Race condition guard ──────────────────────────────────────────────────────

class TestRaceConditionGuard:
    """
    Regression tests for the mtime-stability race condition in read_bytes /
    read_text:

        Problem (pre-fix): if a file is modified *during* a cache-miss read,
        the post-read mtime differs from the pre-read mtime.  The old code
        stored (post-read mtime, stale content), causing the next access to
        hit the cache and silently return stale bytes.

        Fix: only populate the cache when pre-read mtime == post-read mtime.
        If they differ the content is returned as-is but NOT cached, so the
        next call will perform a fresh read.
    """

    def _make_cache_with_racing_mtime(self, original_mtime: int):
        """
        Return a FileCache whose _mtime_ns is patched to simulate a file
        modification during the read:
          - 1st call (pre-open): returns *original_mtime*
          - 2nd call (post-read): returns *original_mtime + 1* (file changed)
        """
        cache = FileCache()
        call_count = {"n": 0}

        def patched_mtime(path: str) -> int:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return original_mtime
            return original_mtime + 1  # simulated race: file changed during read

        cache._mtime_ns = patched_mtime  # type: ignore[method-assign]
        return cache

    def test_read_bytes_does_not_cache_on_mtime_race(self, tmp_path):
        """
        When a file is modified during read_bytes, the result must not be
        stored in the cache (size remains 0 after the call).
        """
        p = tmp_path / "race.bin"
        p.write_bytes(b"original")
        original_mtime = os.stat(str(p)).st_mtime_ns

        cache = self._make_cache_with_racing_mtime(original_mtime)
        data = cache.read_bytes(str(p))

        assert data == b"original", "content should still be returned"
        assert cache.stats()["size"] == 0, (
            "must NOT cache when mtime changed during read (race guard)"
        )

    def test_read_text_does_not_cache_on_mtime_race(self, tmp_path):
        """
        When a file is modified during read_text, the result must not be
        stored in the cache (size remains 0 after the call).
        """
        p = tmp_path / "race.txt"
        p.write_text("original content", encoding="utf-8")
        original_mtime = os.stat(str(p)).st_mtime_ns

        cache = self._make_cache_with_racing_mtime(original_mtime)
        text = cache.read_text(str(p))

        assert text == "original content", "content should still be returned"
        assert cache.stats()["size"] == 0, (
            "must NOT cache when mtime changed during read (race guard)"
        )

    def test_read_bytes_caches_when_mtime_stable(self, tmp_path):
        """
        Sanity check: when the file is NOT modified during read, the entry
        must be cached normally (size == 1 after the call).
        """
        p = tmp_path / "stable.bin"
        p.write_bytes(b"stable")
        cache = FileCache()
        cache.read_bytes(str(p))
        assert cache.stats()["size"] == 1

    def test_read_text_caches_when_mtime_stable(self, tmp_path):
        """
        Sanity check: when the file is NOT modified during read, the entry
        must be cached normally (size == 1 after the call).
        """
        p = tmp_path / "stable.txt"
        p.write_text("stable", encoding="utf-8")
        cache = FileCache()
        cache.read_text(str(p))
        assert cache.stats()["size"] == 1

    def test_subsequent_read_after_race_returns_correct_content(self, tmp_path):
        """
        After a racing read (not cached), the next read with a stable mtime
        should return the latest file content and populate the cache.
        """
        import time as _time

        p = tmp_path / "seq.txt"
        p.write_text("v1", encoding="utf-8")

        # First read: simulate race — file "changes" during read, not cached
        original_mtime = os.stat(str(p)).st_mtime_ns
        cache = self._make_cache_with_racing_mtime(original_mtime)
        r1 = cache.read_text(str(p))
        assert r1 == "v1"
        assert cache.stats()["size"] == 0  # not cached due to race

        # Write new content and advance mtime so a real cache uses the new entry
        p.write_text("v2", encoding="utf-8")
        future = _time.time() + 2.0
        os.utime(str(p), (future, future))

        # Second read with a *real* FileCache (stable mtime): should cache v2
        cache2 = FileCache()
        r2 = cache2.read_text(str(p))
        assert r2 == "v2"
        assert cache2.stats()["size"] == 1
