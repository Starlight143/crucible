"""
features/run_insights/backends.py
==================================
Storage backends for the Run Insights ledger.

v1.1.0 ships **only** :class:`LocalJSONLBackend`.  :class:`CloudflareBackend`
and :class:`DualWriteBackend` are stubs that fail-fast at construction time,
to surface the architectural seam without dragging an HTTP client into the
runtime dependency set.

Cloudflare Workers + D1 + R2 integration contract (v1.2 / v2.0 plan)
====================================================================
The following contract is **frozen** here so that when the Worker is
implemented later, both ends produce byte-compatible records.

D1 schema
---------
.. code-block:: sql

    CREATE TABLE insight_events (
        content_id      TEXT PRIMARY KEY,            -- "sha256:<hex>"
        stream          TEXT NOT NULL,               -- 'output'|'error'|'debate'|'params'
        ts              TEXT NOT NULL,               -- ISO-8601 UTC, "...Z"
        run_id          TEXT NOT NULL,
        project_name    TEXT NOT NULL,
        mode            TEXT NOT NULL,               -- 'Quant'|'SaaS'|'Agent'|'Scientist'
        kind            TEXT NOT NULL,               -- EventKind value
        stage           TEXT,
        schema_version  INTEGER NOT NULL,
        payload_inline  TEXT,                        -- JSON if ≤ inline limit
        payload_r2_key  TEXT,                        -- 'insights/<run_id>/<content_id>.json' otherwise
        env_fingerprint TEXT,                        -- JSON
        outcome_status  TEXT,
        outcome_score   REAL,
        ingested_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_run        ON insight_events(run_id);
    CREATE INDEX idx_project_ts ON insight_events(project_name, ts);
    CREATE INDEX idx_stream_ts  ON insight_events(stream, ts);
    CREATE INDEX idx_outcome    ON insight_events(outcome_status, ts);

R2 object layout
----------------
.. code-block:: text

    insights/<run_id>/<content_id>.json      # full event when > inline limit
    archives/<yyyy-mm>/<project>.tar.gz      # monthly archive snapshots

Workers HTTP API
----------------
.. code-block:: text

    POST /v1/insights/events            # flat body: {event: {...}}
    POST /v1/insights/batch             # gzip body: {events: [...]}
    GET  /v1/insights/events?run_id=&stream=&since=&cursor=&limit=
    GET  /v1/insights/runs/:run_id/summary
    GET  /v1/insights/events/:content_id

Auth: ``Authorization: Bearer <CRUCIBLE_RUN_INSIGHTS_API_TOKEN>``.

JavaScript canonicalisation equivalent
--------------------------------------
.. code-block:: javascript

    function canonicalJson(event) {
      const e = {...event};
      delete e.content_id;
      const norm = (v) => {
        if (typeof v === 'number' && !Number.isFinite(v)) return null;
        if (Array.isArray(v)) return v.map(norm);
        if (v && typeof v === 'object') {
          const sorted = {};
          for (const k of Object.keys(v).sort()) sorted[k] = norm(v[k]);
          return sorted;
        }
        return v;
      };
      return new TextEncoder().encode(JSON.stringify(norm(e)));
    }

    async function contentId(event) {
      const buf = await crypto.subtle.digest('SHA-256', canonicalJson(event));
      return 'sha256:' + Array.from(new Uint8Array(buf))
        .map(b => b.toString(16).padStart(2, '0')).join('');
    }

Local file layout (LocalJSONLBackend)
-------------------------------------
.. code-block:: text

    .crucible_insights/
        output.jsonl       # one event per line, append-only
        error.jsonl
        debate.jsonl
        params.jsonl
        blobs/
            <content_id_hex>.json    # full payload when > INLINE_MAX_BYTES
        .schema_version              # plain text "1"

Cross-process safety
--------------------
* POSIX: ``fcntl.lockf(LOCK_EX)`` during append + prune.
* Windows: ``msvcrt.locking(LK_LOCK)`` on a sentinel byte during append + prune.
  Both platforms now serialise concurrent writers across OS processes; the
  ledger is safe to share between e.g. a WebUI worker and a parallel CLI run.
  An in-process ``threading.Lock`` still serialises threads within the same
  interpreter (cheaper than syscalling per write).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Mapping, Optional, Protocol, Tuple

# Tri-modal import (see recorder.py for the launcher matrix).
try:
    from ...runtime_logging import get_logger
    from .schema import canonical_record_line as _canonical_record_line
except ImportError:  # pragma: no cover — flat-launcher fallback
    from runtime_logging import get_logger  # type: ignore[no-redef]
    try:
        from features.run_insights.schema import (  # type: ignore[no-redef]
            canonical_record_line as _canonical_record_line,
        )
    except ImportError:  # pragma: no cover — both layouts failed
        _canonical_record_line = None  # type: ignore[assignment]

LOGGER = get_logger(__name__)

# Optional POSIX file lock.
try:
    import fcntl as _fcntl
    _HAS_FCNTL: bool = True
except ImportError:
    _fcntl = None  # type: ignore[assignment]
    _HAS_FCNTL = False

# Optional Windows file lock (msvcrt is present on every CPython Windows
# build; absent on POSIX).  We use ``msvcrt.locking`` which is the documented
# kernel-level byte-range lock equivalent of ``fcntl.lockf`` and is honoured
# across processes (unlike threading.Lock, which is in-process only).
try:
    import msvcrt as _msvcrt
    _HAS_MSVCRT: bool = True
except ImportError:
    _msvcrt = None  # type: ignore[assignment]
    _HAS_MSVCRT = False

# ── Constants ────────────────────────────────────────────────────────────────

_STREAM_FILENAMES = {
    "output": "output.jsonl",
    "error": "error.jsonl",
    "debate": "debate.jsonl",
    "params": "params.jsonl",
}

_VALID_STREAMS = frozenset(_STREAM_FILENAMES.keys())

_SCHEMA_MARKER_FILENAME = ".schema_version"
_BLOBS_DIRNAME = "blobs"

# Per-stream sidecar lock filename suffix.  We need a separate lockable
# file so the cross-process exclusive lock can survive the close+rename
# cycle of ``prune_stream`` — Windows ``os.replace`` will refuse to
# overwrite a file the same process holds open, so we cannot lock the
# stream file directly across read-then-rewrite.  The sidecar is small
# (0 bytes), never read, and persists for the lifetime of the ledger.
_STREAM_LOCK_SUFFIX = ".lock"


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync the directory entry of *path*.

    POSIX requires ``fsync(dirfd)`` to durably commit a rename — without
    it, a power loss after ``os.replace`` can leave the new file content
    on disk while the directory still points at the old inode.  Windows
    NTFS commits metadata via ``FlushFileBuffers`` on the file handle,
    so the dir fsync is a POSIX-only step (and a silent no-op on
    Windows).  Failures are swallowed — durability is best-effort.
    """
    if os.name != "posix":
        return
    try:
        dirfd = os.open(str(path), getattr(os, "O_DIRECTORY", os.O_RDONLY))
    except OSError:
        return
    try:
        os.fsync(dirfd)
    except (OSError, ValueError):
        pass
    finally:
        try:
            os.close(dirfd)
        except OSError:
            pass


# ── Storage backend protocol ─────────────────────────────────────────────────

class StorageBackend(Protocol):
    """Abstract storage interface for the Run Insights ledger.

    All methods must be thread-safe.  Implementations should swallow
    transient I/O errors (logging once at ``warning`` level) rather than
    propagating to call sites — the insights subsystem must never break
    the main pipeline.
    """

    def write_event(self, stream: str, event: Mapping[str, Any]) -> str:
        """Persist *event* to *stream*.  Returns the stored ``content_id``."""
        ...

    def write_blob(self, content_id: str, payload: bytes) -> str:
        """Persist a binary payload addressed by *content_id*.

        Returns the storage key (local: filename relative to ``.crucible_insights/``;
        future cloud: R2 object key).
        """
        ...

    def read_blob(self, content_id: str) -> Optional[bytes]:
        """Return the binary payload addressed by *content_id*, or ``None``."""
        ...

    def read_events(
        self,
        stream: str,
        *,
        since: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[dict], Optional[str]]:
        """Return ``(events, next_cursor)`` from *stream* in ts-ascending order."""
        ...

    def prune_stream(self, stream: str, max_entries: int) -> int:
        """Trim *stream* to at most *max_entries* recent entries.  Returns
        the number of entries pruned (≥ 0).
        """
        ...

    def flush(self) -> None:
        """Flush any in-memory buffers to durable storage."""
        ...

    def close(self) -> None:
        """Release any resources held by the backend."""
        ...


# ── Lock context manager ─────────────────────────────────────────────────────

class _NoOpLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _file_lock_ctx(fh: Any):
    """Cross-process exclusive lock context manager.

    * POSIX: ``fcntl.lockf(LOCK_EX)`` covering the whole file.
    * Windows: ``msvcrt.locking(LK_LOCK, 1)`` on a single sentinel byte at
      offset 0.  ``LK_LOCK`` blocks until the lock is granted (up to ~10 s
      internally) and retries indefinitely on ``OSError(EDEADLK)``.  Locks
      the same single byte every time so that two processes contend on the
      same region regardless of the file size.
    * Neither available: silent no-op (matches v1.0.x behaviour; only seen
      on exotic embedded Pythons).
    """
    if _HAS_FCNTL and _fcntl is not None:
        class _PosixLock:
            def __enter__(self_inner):
                try:
                    _fcntl.lockf(fh, _fcntl.LOCK_EX)
                except OSError as exc:
                    if exc.errno not in (errno.ENOLCK, errno.ENOSYS):
                        raise
                return self_inner

            def __exit__(self_inner, *a):
                # v1.1.0 fourth-pass (F-7): catch ValueError in
                # addition to OSError.  In pathological GC interleavings
                # the fd can be closed between __enter__ and __exit__
                # (e.g. finaliser ran) → fcntl.lockf raises ValueError
                # ("I/O operation on closed file") which previously
                # leaked the lock to the kernel.  Mirror the Windows
                # widened-catch.
                try:
                    _fcntl.lockf(fh, _fcntl.LOCK_UN)
                except (OSError, ValueError):
                    pass
                return False

        return _PosixLock()

    if _HAS_MSVCRT and _msvcrt is not None:
        class _WindowsLock:
            def __enter__(self_inner):
                # msvcrt.locking locks ``nbytes`` starting at the current file
                # position.  We rewind to 0, acquire 1 byte, and restore the
                # position so the caller's append behaviour is unchanged.
                # ``LK_LOCK`` retries every second for ~10 s before raising.
                self_inner._restored_pos: Optional[int] = None
                try:
                    self_inner._restored_pos = fh.tell()
                except (OSError, ValueError):
                    self_inner._restored_pos = None
                try:
                    fh.seek(0)
                except (OSError, ValueError):
                    return self_inner
                # Retry briefly on transient EDEADLK / EACCES — the LK_LOCK
                # mode already retries for ~10 s but on Windows + Antivirus
                # the call can spuriously return; bounded retry is safer.
                for _ in range(3):
                    try:
                        _msvcrt.locking(fh.fileno(), _msvcrt.LK_LOCK, 1)
                        break
                    except OSError as exc:
                        if exc.errno in (errno.EDEADLK, errno.EACCES, errno.EAGAIN):
                            time.sleep(0.05)
                            continue
                        break  # give up silently on truly unsupported errors
                if self_inner._restored_pos is not None:
                    try:
                        fh.seek(self_inner._restored_pos)
                    except (OSError, ValueError):
                        pass
                return self_inner

            def __exit__(self_inner, *a):
                # v1.1.0 third-pass: unlock independently of every
                # other step.  Previously ``fh.seek(0)`` and the
                # ``locking(LK_UNLCK)`` call shared one ``try``, so a
                # failed seek (closed handle, EBADF) skipped the
                # unlock entirely — leaving the kernel byte-range
                # lock held until the process died.  Now each phase
                # has its own ``try`` so the unlock always runs
                # regardless of seek / tell outcomes.
                try:
                    pos = fh.tell()
                except (OSError, ValueError):
                    pos = None
                try:
                    fh.seek(0)
                except (OSError, ValueError):
                    pass
                # Release lock unconditionally — no exception path
                # below this point may keep the lock alive.
                try:
                    _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
                except (OSError, ValueError):
                    pass
                if pos is not None:
                    try:
                        fh.seek(pos)
                    except (OSError, ValueError):
                        pass
                return False

        return _WindowsLock()

    return _NoOpLock()


# ── LocalJSONLBackend ─────────────────────────────────────────────────────────

class LocalJSONLBackend:
    """Append-only JSONL backend.

    Each ``EventKind`` is stored in its own ``<stream>.jsonl`` file under
    ``root``.  Blobs above ``inline_max_bytes`` are sidecared into
    ``blobs/<content_id_hex>.json`` (matches the future R2 object layout
    minus the ``insights/<run_id>/`` prefix).
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        inline_max_bytes: int = 4096,
    ) -> None:
        self._root = Path(root).resolve()
        self._inline_max_bytes = max(0, int(inline_max_bytes))
        # v1.1.0 fourth-pass (F-6): RLock so accidental re-entry from
        # within an emit-on-error path doesn't deadlock (e.g. a future
        # contributor adding a self-recording error inside _emit).
        # Same overhead as Lock for the uncontended case.
        self._lock = threading.RLock()
        self._closed = False
        # _init_layout sets _init_failed=True if the layout can't be
        # written; callers (recorder factory) inspect this and fall back
        # to NullRecorder rather than letting every emit silently no-op
        # against a non-existent ledger directory.
        self._init_failed = False
        # v1.1.0 fifth-pass (G-19): rate-limit OSError warnings.  A
        # read-only mount or full-disk produces multiple WARNING-level
        # log lines per event × 4 streams × N events/run, drowning
        # real diagnostics.  Track which (scope, key) tuples we've
        # already warned about — cap at 100 entries so a pathological
        # case can't blow up memory.
        self._warned_paths: set[tuple[str, str]] = set()
        self._init_layout()

    def _warn_once(self, scope: str, key: str, msg: str, *args: Any) -> None:
        """Log a WARNING the first time *(scope, key)* is seen, then suppress.

        Keeps a bounded set per backend instance.  When the set hits
        100 entries we stop tracking new keys — the trade-off is that
        every distinct error pattern after the 100th becomes spammy
        again, but that's a much better failure mode than unbounded
        memory growth on a runaway log loop.
        """
        marker = (scope, key)
        if marker in self._warned_paths:
            return
        if len(self._warned_paths) < 100:
            self._warned_paths.add(marker)
        LOGGER.warning(msg, *args)

    # -- layout / init ----------------------------------------------------------

    def _init_layout(self) -> None:
        """Create the on-disk layout idempotently.

        Two concurrent processes booting at the same instant both see
        ``marker.exists()`` as ``False``; without an exclusive lock both
        would race on the migration hook (currently a no-op, but reserved
        for v1.0→v1.1 / v1.1→v1.2 import passes).  We serialise on a
        ``.schema_version.lock`` file: open it ``O_CREAT``, take the
        platform exclusive lock, then check the marker contents under the
        lock.  After the marker is written once with the expected value,
        the lock turns into a cheap fast-path.
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            (self._root / _BLOBS_DIRNAME).mkdir(exist_ok=True)
            # v1.1.0 fifth-pass (G-18): best-effort cleanup of orphaned
            # tmp / lock sidecars from earlier crashed processes.
            # ``tempfile.mkstemp`` in three places (schema-marker
            # write, prune, blob spill) leaves behind ``.prune_*.jsonl``,
            # ``.blob_*.tmp``, ``.schema_*.tmp`` files when the
            # process is SIGKILL'd between mkstemp and os.replace.
            # Over months of crashes the ledger directory accumulates
            # pollution.  Files older than 24h are safe to remove —
            # any legitimate operation completes in seconds.
            self._cleanup_orphan_tempfiles()
            marker = self._root / _SCHEMA_MARKER_FILENAME
            # v1.1.0 third-pass: parse marker as an integer and treat
            # ``>= current_version`` as a no-op.  This guards against
            # a v1.2 process being launched against an existing
            # ledger; it will see ``marker=="2"`` and the v1.1
            # process must NOT roll the marker back to ``"1"``.
            expected = 1

            def _marker_at_least(target: int) -> bool:
                """Return True iff the on-disk marker is >= *target*.

                v1.1.0 fifth-pass (G-17): use ``utf-8-sig`` so Notepad's
                default UTF-8-BOM save doesn't break parsing.  Without
                the BOM strip, ``int("﻿2")`` raises ValueError and
                the migration re-writes the marker on every startup
                even though the on-disk version is correct.
                """
                try:
                    content = marker.read_text(encoding="utf-8-sig").strip()
                except OSError:
                    return False
                try:
                    return int(content) >= target
                except ValueError:
                    return False

            # Fast-path: marker already correct or newer → no lock needed.
            if marker.exists() and _marker_at_least(expected):
                return
            lock_path = self._root / (_SCHEMA_MARKER_FILENAME + ".lock")
            # ``open(... "a+")`` creates the file if absent and is the only
            # mode that works with both fcntl.lockf and msvcrt.locking.
            with open(lock_path, "a+", encoding="utf-8") as lock_fh:
                with _file_lock_ctx(lock_fh):
                    # Re-check under the lock — another process may have
                    # finished the migration while we waited.
                    if marker.exists() and _marker_at_least(expected):
                        return
                    # Atomic write: temp + replace so a crash never
                    # leaves a partial marker.
                    fd, tmp = tempfile.mkstemp(
                        dir=self._root, prefix=".schema_", suffix=".tmp",
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(f"{expected}\n")
                            fh.flush()
                            try:
                                os.fsync(fh.fileno())
                            except (OSError, ValueError):
                                pass
                        os.replace(tmp, marker)
                        # POSIX durability: also fsync the directory
                        # entry so the rename survives power loss.
                        _fsync_dir(self._root)
                    except Exception:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                        raise
        except OSError as exc:
            # v1.1.0 fourth-pass (F-6): mark the backend as init-failed
            # AND close it so the recorder factory can detect this and
            # substitute _NullRecorder.  Previously the OSError was
            # swallowed silently and the backend instance lived on,
            # turning every subsequent write_event into a silent no-op
            # with no operator-visible warning.
            self._init_failed = True
            self._closed = True
            LOGGER.warning(
                "run_insights: failed to initialise layout at %s: %s — "
                "ledger DISABLED for this process (events will be lost)",
                self._root, exc,
            )

    def _cleanup_orphan_tempfiles(self, max_age_seconds: float = 86400.0) -> None:
        """Best-effort sweep of stale tempfiles in the ledger root.

        Removes ``.prune_*.jsonl`` / ``.blob_*.tmp`` / ``.schema_*.tmp``
        sidecars older than *max_age_seconds* (default 24h).  All errors
        are swallowed — this is a janitor, not an authority.  Called
        exactly once per backend construction, so the cost is bounded.

        v1.1.0 fifth-pass (G-18).
        """
        try:
            cutoff = time.time() - max_age_seconds
            roots = [self._root, self._root / _BLOBS_DIRNAME]
            for root in roots:
                try:
                    entries = list(root.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    name = entry.name
                    if not (
                        name.startswith(".prune_")
                        or name.startswith(".blob_")
                        or name.startswith(".schema_")
                    ):
                        continue
                    if not (name.endswith(".tmp") or name.endswith(".jsonl")):
                        continue
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mtime < cutoff:
                        try:
                            entry.unlink()
                        except OSError:
                            pass
        except Exception:  # noqa: BLE001
            # Janitor failure must NEVER block backend init.
            pass

    def _stream_path(self, stream: str) -> Path:
        if stream not in _VALID_STREAMS:
            raise ValueError(
                f"unknown insights stream {stream!r}; "
                f"expected one of {sorted(_VALID_STREAMS)}"
            )
        return self._root / _STREAM_FILENAMES[stream]

    def _stream_lock_path(self, stream: str) -> Path:
        """Return the sidecar lock-file path for *stream*.

        Both :meth:`write_event` and :meth:`prune_stream` acquire the
        cross-process exclusive lock on this sidecar so the prune's
        read-then-rewrite cycle is fully serialised against writers.
        Locking the stream file directly is unsafe on Windows because
        ``os.replace`` refuses to overwrite a file held open by the
        same process — the sidecar gives us a stable lock target that
        outlives the close+rename inside prune.
        """
        fname = _STREAM_FILENAMES[stream]
        return self._root / f".{fname}{_STREAM_LOCK_SUFFIX}"

    def _blob_path(self, content_id: str) -> Path:
        # Strip the "sha256:" prefix for the on-disk filename — keeps the
        # filename short and avoids any colon-on-Windows surprises (NTFS
        # treats ``foo:bar`` as an alternate data stream).
        digest = content_id.split(":", 1)[-1] if content_id else "unknown"
        # Defensive: prevent path traversal even though content_id should
        # only ever be ``sha256:<hex>``.
        digest_safe = "".join(c for c in digest if c.isalnum())
        return self._root / _BLOBS_DIRNAME / f"{digest_safe}.json"

    # -- write -----------------------------------------------------------------

    def write_event(self, stream: str, event: Mapping[str, Any]) -> str:
        if self._closed:
            return ""
        path = self._stream_path(stream)
        lock_path = self._stream_lock_path(stream)
        record = dict(event)
        content_id = str(record.get("content_id") or "")
        if not content_id:
            LOGGER.warning(
                "run_insights: event missing content_id; refusing to write"
            )
            return ""
        # v1.1.2 (audit fix G2-B-MED-2): serialise the disk line with the
        # same canonical encoder used to compute content_id (V8 float
        # formatter, sort_keys=True, separators=(",", ":")) so the on-disk
        # JSONL form IS the canonical form.  Previously this used Python-
        # default ``json.dumps`` (insertion-order keys, native float repr),
        # which diverged from canonical_json byte-for-byte — fine for
        # today's read path (which re-parses + recomputes content_id),
        # but a footgun for the v1.2.0 DualWriteBackend that will byte-
        # copy lines to R2: the Cloudflare Worker can now strip
        # ``content_id`` from the parsed line, re-canonicalise the rest,
        # and verify the hash directly.  Falls back to the legacy
        # ``json.dumps`` form only if the helper import failed
        # (defensive — should be reachable in all tri-modal layouts).
        if _canonical_record_line is not None:
            try:
                line = _canonical_record_line(record).decode("utf-8") + "\n"
            except Exception:
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        else:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                # v1.1.0 third-pass: hold a sidecar lock for the entire
                # append.  Prune runs under the same sidecar so it
                # cannot ``os.replace`` away an in-flight append.  The
                # old design locked the stream file directly, but
                # Windows ``os.replace`` rejects rename of a held-open
                # target — making the close→rename cycle of prune race
                # against writers.  A sidecar lock survives that
                # cycle.
                with open(lock_path, "a+", encoding="utf-8") as lock_fh:
                    with _file_lock_ctx(lock_fh):
                        with open(path, "a", encoding="utf-8") as fh:
                            fh.write(line)
                            fh.flush()
                            # fsync forces the kernel page cache to
                            # durable storage so a Ctrl-C / power loss
                            # between this call and the next emit
                            # cannot lose the record.  Best-effort:
                            # some filesystems (FAT-style mounts,
                            # certain Windows network shares) silently
                            # ignore fsync; we never propagate the
                            # failure.
                            try:
                                os.fsync(fh.fileno())
                            except (OSError, ValueError):
                                pass
            except OSError as exc:
                # v1.1.0 fifth-pass (G-19): rate-limit per stream path.
                self._warn_once(
                    "write_event", str(path),
                    "run_insights: append to %s failed: %s "
                    "(subsequent identical failures suppressed)",
                    path, exc,
                )
                return ""
        return content_id

    def write_blob(self, content_id: str, payload: bytes) -> str:
        if self._closed or not content_id or not payload:
            return ""
        path = self._blob_path(content_id)
        # v1.1.2 (sixth-pass M-1): content-addressable short-circuit.  Two
        # concurrent writers with the same content_id were previously racing
        # on the temp-file ``os.replace``; on Windows one of the renames can
        # raise ``PermissionError`` if the read side has the target open via
        # ``read_blob`` (the OS still serialises atomic-rename through a
        # FILE_SHARE_DELETE flag that Python's stdlib doesn't request).
        # Skipping the second write here is correctness-preserving because
        # both writers would have produced byte-identical content; it also
        # halves the disk I/O for retry-heavy emit paths.
        try:
            if path.exists():
                return f"{_BLOBS_DIRNAME}/{path.name}"
        except OSError:
            # ``path.exists`` can raise on permission-denied parent dirs;
            # fall through to the full write so the eventual OSError below
            # surfaces with the canonical ``write_blob`` warn-once.
            pass
        try:
            # Atomic write: temp + replace, so partial blobs are never read.
            fd, tmp = tempfile.mkstemp(
                dir=path.parent, prefix=".blob_", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                    fh.flush()
                    # fsync the blob before the rename so the durability
                    # contract matches write_event.
                    try:
                        os.fsync(fh.fileno())
                    except (OSError, ValueError):
                        pass
                os.replace(tmp, path)
                # v1.1.0 third-pass: also fsync the directory entry so
                # the rename is durable on POSIX (no-op on Windows).
                _fsync_dir(path.parent)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._warn_once(
                "write_blob", str(path),
                "run_insights: blob write %s failed: %s "
                "(subsequent identical failures suppressed)",
                path, exc,
            )
            return ""
        # Return path relative to the ledger root, matching the future R2
        # key shape (``insights/<run_id>/<content_id>.json`` is the cloud
        # equivalent of local ``blobs/<digest>.json``).
        return f"{_BLOBS_DIRNAME}/{path.name}"

    def read_blob(self, content_id: str) -> Optional[bytes]:
        path = self._blob_path(content_id)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._warn_once(
                "read_blob", str(path),
                "run_insights: blob read %s failed: %s "
                "(subsequent identical failures suppressed)",
                path, exc,
            )
            return None

    # -- read ------------------------------------------------------------------

    def read_events(
        self,
        stream: str,
        *,
        since: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Tuple[List[dict], Optional[str]]:
        """Return up to *limit* events from *stream*.

        v1.1.0 cursor encoding: the byte offset of the next line to read
        (decimal string).  This keeps the call shape forward-compatible
        with the future Cloudflare cursor (opaque base64 D1 row marker)
        without requiring callers to interpret the value.

        v1.1.0 fourth-pass note: byte-offset cursors are NOT stable
        across :meth:`prune_stream` invocations — when prune rewrites
        the file with fewer leading lines, an old cursor may point
        into the middle of a different record or past EOF.  Callers
        must treat cursors as single-session opaque tokens; for
        durable resumption across process restarts, persist
        ``(content_id, ts)`` of the last consumed event and re-seek
        on resume.  When the future Cloudflare cursor lands, this
        local cursor will be replaced with a content-stable token
        and the prune-invalidation concern disappears.
        """
        path = self._stream_path(stream)
        lock_path = self._stream_lock_path(stream)
        events: List[dict] = []
        next_cursor: Optional[str] = None
        try:
            start_offset = int(cursor) if cursor else 0
        except (ValueError, TypeError):
            start_offset = 0
        if limit <= 0:
            return events, None
        try:
            # v1.1.2 (sixth-pass M-1): acquire the same sidecar lock that
            # write_event / prune_stream hold for the duration of the read.
            # On Windows ``os.replace`` (the rename step of prune) cannot
            # overwrite a file that another process holds open — without
            # this lock, a prune racing against an in-flight read silently
            # aborts with ``PermissionError``, gets warn-once-suppressed,
            # and the file grows unbounded until the next quiet moment.
            # Holding an exclusive lock here serialises reads with each
            # other and with prune; both are infrequent enough that the
            # serialisation is unobservable in practice.
            with open(lock_path, "a+", encoding="utf-8") as lock_fh:
                with _file_lock_ctx(lock_fh):
                    with open(path, "rb") as fh:
                        if start_offset:
                            try:
                                fh.seek(start_offset)
                            except OSError:
                                pass
                        while True:
                            line_bytes = fh.readline()
                            if not line_bytes:
                                break
                            try:
                                obj = json.loads(line_bytes.decode("utf-8"))
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            if not isinstance(obj, dict):
                                continue
                            if since and str(obj.get("ts") or "") < since:
                                continue
                            events.append(obj)
                            if len(events) >= limit:
                                # Record byte offset *after* this line so the
                                # next call resumes from the next event.
                                next_cursor = str(fh.tell())
                                break
        except FileNotFoundError:
            return [], None
        except OSError as exc:
            self._warn_once(
                "read_events", str(path),
                "run_insights: read %s failed: %s "
                "(subsequent identical failures suppressed)",
                path, exc,
            )
        return events, next_cursor

    # -- prune -----------------------------------------------------------------

    def prune_stream(self, stream: str, max_entries: int) -> int:
        """Atomically trim *stream* to the most-recent *max_entries* lines.

        Uses a single-pass ``collections.deque`` of size *max_entries* to
        buffer just the tail window during scanning (O(*max_entries*)
        memory rather than O(N) for the whole file), followed by
        temp-file + ``os.replace`` so a crash mid-prune cannot lose
        data.  Returns the number of lines dropped.

        v1.1.0 hardening: the previous implementation called
        ``readlines()`` which loaded the entire file into memory.  At
        ``max_entries=20 000 × ~500 B/line ≈ 10 MB`` that became
        meaningful, and worse, blocked every emit on every stream for
        the duration of the read.  The new path bounds memory to the
        kept-window size.  The whole prune is serialised against
        concurrent writers via the platform exclusive file lock
        acquired on the source file before reading, so a concurrent
        ``write_event`` cannot interleave bytes into the partially-read
        window on Windows where ``msvcrt.locking`` denies access to
        locked byte ranges.
        """
        if self._closed or max_entries < 0:
            return 0
        path = self._stream_path(stream)
        if not path.exists():
            return 0

        import collections as _collections
        _CHUNK = 65536

        # v1.1.0 third-pass: acquire the per-stream sidecar lock for
        # the ENTIRE read-then-rewrite-then-replace cycle.  Writers
        # take the same sidecar so they cannot append between our
        # scan and our ``os.replace`` (which would have lost the
        # in-flight write).  ``self._lock`` still serialises threads
        # in this process; the file lock handles cross-process.
        lock_path = self._stream_lock_path(stream)
        with self._lock:
            dropped = 0
            try:
                with open(lock_path, "a+", encoding="utf-8") as lock_fh:
                    with _file_lock_ctx(lock_fh):
                        # Re-check existence under the lock — another
                        # process may have just pruned and not yet
                        # re-created the file.
                        if not path.exists():
                            return 0
                        try:
                            source_fh = open(path, "rb")
                        except OSError as exc:
                            self._warn_once(
                                "prune_open", str(path),
                                "run_insights: prune open %s failed: %s "
                                "(subsequent identical failures suppressed)",
                                path, exc,
                            )
                            return 0
                        try:
                            kept: "_collections.deque[bytes]" = (
                                _collections.deque(maxlen=max_entries)
                                if max_entries > 0
                                else _collections.deque()
                            )
                            total_non_empty = 0
                            pending = b""
                            for chunk in iter(lambda: source_fh.read(_CHUNK), b""):
                                pending += chunk
                                while True:
                                    nl = pending.find(b"\n")
                                    if nl < 0:
                                        break
                                    line = pending[:nl + 1]
                                    pending = pending[nl + 1:]
                                    if not line.strip():
                                        continue
                                    total_non_empty += 1
                                    if max_entries > 0:
                                        kept.append(line)
                                    # max_entries == 0 → drop everything
                                    # (deque stays empty); fall through
                                    # to total count.
                            if pending.strip():
                                # v1.1.2 (audit fix G2-B-MED-3): if the
                                # final byte chunk has content after the
                                # last ``\n``, that tail is necessarily a
                                # writer-crash artefact: ``write_event``
                                # writes the full ``line + "\n"`` atomically
                                # under the sidecar lock and only fsyncs
                                # after the newline.  Previously this
                                # branch silently appended ``\n`` and
                                # promoted the partial record into a
                                # ``well-formed but JSON-invalid`` kept
                                # line; the read path swallows the
                                # downstream JSONDecodeError so the bad
                                # row vanishes from queries — but the
                                # original crash symptom (unterminated
                                # tail) is also erased, making incident
                                # forensics impossible.  We now drop the
                                # partial pending and emit a one-time
                                # warning so the operator sees the
                                # writer-crash signal exactly once per
                                # backend lifetime.
                                self._warn_once(
                                    "prune_partial_tail",
                                    str(path),
                                    "run_insights: prune detected un-newline-"
                                    "terminated tail (%d bytes) in %s — "
                                    "treating as writer-crash artefact and "
                                    "dropping; the original record is lost "
                                    "(subsequent identical events suppressed)",
                                    len(pending),
                                    path,
                                )
                                # Intentionally do NOT promote to total_non_empty
                                # or kept — the partial bytes are discarded.
                        finally:
                            try:
                                source_fh.close()
                            except OSError:
                                pass

                        if total_non_empty <= max_entries:
                            return 0
                        dropped = total_non_empty - len(kept)

                        # Write the kept window to a temp file under the
                        # same dir, fsync, then atomically replace the
                        # source.  Crash between write and replace
                        # leaves the original intact.
                        fd, tmp = tempfile.mkstemp(
                            dir=path.parent,
                            prefix=".prune_",
                            suffix=".jsonl",
                        )
                        try:
                            with os.fdopen(fd, "wb") as out_fh:
                                out_fh.writelines(kept)
                                out_fh.flush()
                                try:
                                    os.fsync(out_fh.fileno())
                                except (OSError, ValueError):
                                    pass
                            os.replace(tmp, path)
                            _fsync_dir(path.parent)
                        except Exception:
                            try:
                                os.unlink(tmp)
                            except OSError:
                                pass
                            raise
            except OSError as exc:
                self._warn_once(
                    "prune_write", str(path),
                    "run_insights: prune write %s failed: %s "
                    "(subsequent identical failures suppressed)",
                    path, exc,
                )
                return 0
        return dropped

    # -- lifecycle -------------------------------------------------------------

    def flush(self) -> None:
        # We always write+fsync per append, so there's no buffered state.
        pass

    def close(self) -> None:
        self._closed = True

    # -- introspection ---------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    def list_streams(self) -> List[str]:
        return list(_STREAM_FILENAMES.keys())


# ── Cloudflare backend stub (planned for v1.2+) ──────────────────────────────

class CloudflareBackend:
    """Stub for the future Cloudflare Workers + D1 + R2 backend.

    Construction always raises ``NotImplementedError`` in v1.1.0.  The class
    and its docstring exist so that:

    1. The architectural seam is visible — anyone scanning ``backends.py``
       sees exactly where the cloud variant will plug in.
    2. The HTTP contract is documented in this module's top docstring; the
       Worker can be implemented against that contract without coordination.
    3. Setting ``CRUCIBLE_RUN_INSIGHTS_BACKEND=cloudflare`` fails immediately
       at startup with a clear pointer rather than silently falling back to
       local storage (which would let an operator believe they are uploading
       to the cloud while data sits on disk).
    """

    def __init__(self, *, api_url: str, api_token: str, **_kw: Any) -> None:
        raise NotImplementedError(
            "CloudflareBackend is planned for v1.2.0. "
            "See crucible/features/run_insights/backends.py module docstring "
            "for the D1 / R2 / Workers contract. "
            "v1.1.0 supports CRUCIBLE_RUN_INSIGHTS_BACKEND=local only."
        )


class DualWriteBackend:
    """Stub for a future "local + cloud" dual-write backend.

    Same fail-fast pattern as :class:`CloudflareBackend`.  When implemented
    in v1.2.0+, this backend will write to ``LocalJSONLBackend`` synchronously
    (durability guarantee) and enqueue async batches to ``CloudflareBackend``
    (cloud sync).  A cloud-side failure must never block the local write.
    """

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        raise NotImplementedError(
            "DualWriteBackend is planned for v1.2.0. "
            "v1.1.0 supports CRUCIBLE_RUN_INSIGHTS_BACKEND=local only."
        )


# ── Backend factory ──────────────────────────────────────────────────────────

def make_backend(
    backend: str,
    *,
    root: str | os.PathLike[str],
    inline_max_bytes: int = 4096,
    api_url: str = "",
    api_token: str = "",
) -> StorageBackend:
    """Construct the backend named by *backend* (``local``/``cloudflare``/``dual``).

    * ``local``: returns :class:`LocalJSONLBackend`.
    * ``cloudflare`` / ``dual``: raises ``NotImplementedError`` via the stubs.

    Unrecognised values raise ``ValueError`` (no silent fallback to local —
    a typo in ``CRUCIBLE_RUN_INSIGHTS_BACKEND`` must be loud, not buried).
    """
    name = (backend or "local").strip().lower()
    if name == "local":
        return LocalJSONLBackend(root, inline_max_bytes=inline_max_bytes)
    if name == "cloudflare":
        if not api_url or not api_token:
            raise NotImplementedError(
                "CRUCIBLE_RUN_INSIGHTS_BACKEND=cloudflare requires both "
                "CRUCIBLE_RUN_INSIGHTS_API_URL and CRUCIBLE_RUN_INSIGHTS_API_TOKEN "
                "to be set, AND the backend itself is not yet implemented in v1.1.0."
            )
        return CloudflareBackend(api_url=api_url, api_token=api_token)  # type: ignore[return-value]
    if name == "dual":
        return DualWriteBackend(  # type: ignore[return-value]
            root=root, api_url=api_url, api_token=api_token,
            inline_max_bytes=inline_max_bytes,
        )
    raise ValueError(
        f"unknown CRUCIBLE_RUN_INSIGHTS_BACKEND={backend!r}; "
        "expected one of: local, cloudflare, dual"
    )


__all__ = [
    "StorageBackend",
    "LocalJSONLBackend",
    "CloudflareBackend",
    "DualWriteBackend",
    "make_backend",
]
