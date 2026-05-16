"""One-shot maintenance helpers for the Run Insights local ledger.

This module exists because the v1.1.0 ledger writer is **append-only**:
once an event lands on disk it persists until the prune-by-cap policy
(``CRUCIBLE_RUN_INSIGHTS_MAX_ENTRIES_PER_STREAM``) evicts it.  Operators
upgrading from v1.1.0 / v1.1.1 / v1.1.2 / v1.1.3 may have accumulated
test-pollution events (``run_id=""`` from tests that bypassed the
ledger-dir override pattern documented in CLAUDE.md § 9.5) which v1.1.4's
conftest autouse fixture stops creating *new* but does not retroactively
clean up.  The 897-of-952 pollution rate observed at v1.1.4 ship time
made the local ledger unusable as a v1.2.0 retrieval source until the
orphaned rows were filtered out.

This module ships ``prune_orphan_events()`` so operators can run the
cleanup themselves::

    python -c "from crucible.features.run_insights.maintenance import prune_orphan_events; \\
               print(prune_orphan_events('.crucible_insights'))"

The function:

* Walks every JSONL stream file under the supplied ledger root.
* Keeps events whose ``run_id`` is non-empty AND non-whitespace.
* Rewrites each file atomically via ``tempfile.mkstemp`` → ``os.replace``
  (mirrors the v1.1.0 H2 / H3 cross-process write discipline).
* Returns a ``{stream: events_removed}`` summary the caller can log.
* Defaults to ``dry_run=False`` (apply the change); set ``dry_run=True``
  to preview without writing.
* Skips the ``blobs/`` subdirectory entirely — blobs are deduplicated by
  content_id and are valid even when their referencing event was pruned;
  garbage-collection of orphan blobs is a separate concern handled by
  the existing ``_cleanup_orphan_tempfiles`` path in ``backends.py``.

The function is **idempotent**: running twice in a row is harmless
(second run reports zero removals).

Acquires the same per-stream sidecar lock that ``write_event`` and
``prune_stream`` use (``backends._stream_lock_path``), so a concurrent
writer cannot interleave a half-line during the rewrite.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional


_STREAM_FILENAMES: tuple[str, ...] = (
    "output.jsonl",
    "error.jsonl",
    "debate.jsonl",
    "params.jsonl",
)


def _is_orphan_event(line: str) -> bool:
    """Return True when ``line`` is a JSONL event with empty / whitespace
    ``run_id`` field — the signature of v1.1.0 test pollution before the
    v1.1.4 conftest autouse fixture closed the leak.

    Malformed lines (not parseable as JSON, or JSON but not a dict) are
    treated as **non-orphan** to be conservative: dropping them silently
    would lose forensic information about prior writer crashes.
    The separate ``prune_stream`` writer-crash-tail handling in
    ``backends.py`` already handles half-line cases at write time.
    """
    s = line.strip()
    if not s:
        # Blank lines are pure noise — safe to drop, but not "orphans" by
        # the policy this function targets.  ``_filter_lines`` strips them
        # implicitly by only emitting non-empty content.
        return False
    try:
        obj = json.loads(s)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    rid = str(obj.get("run_id") or "").strip()
    return rid == ""


def _stream_lock_path(stream_path: Path) -> Path:
    """Mirror ``backends._stream_lock_path`` so prune cannot race against
    a concurrent ``write_event`` on Windows (where ``os.replace`` cannot
    overwrite a held-open file)."""
    return stream_path.parent / f".{stream_path.name}.lock"


def _atomic_rewrite(stream_path: Path, kept_lines: Iterable[str]) -> None:
    """Write ``kept_lines`` to ``stream_path`` via tempfile + os.replace.

    Each line in ``kept_lines`` MUST already include its terminating
    newline (matches the JSONL writer contract — ``write_event`` writes
    ``line + "\\n"`` so the kept slice we pass through is byte-identical
    to the originals).
    """
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".prune_orphan_{stream_path.name}_",
        suffix=".tmp",
        dir=str(stream_path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            for line in kept_lines:
                fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # fsync on some pseudo-filesystems (e.g. WSL2 9p drvfs) is
                # not supported; the rename below is still atomic at the
                # NTFS / ext4 / APFS layer.
                pass
        os.replace(str(tmp_path), str(stream_path))
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def prune_orphan_events(
    root: str | os.PathLike[str] = ".crucible_insights",
    *,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Remove events with empty / whitespace ``run_id`` from each stream.

    Parameters
    ----------
    root
        Filesystem path to the ledger root directory (default
        ``.crucible_insights`` relative to the current working directory).
    dry_run
        When ``True``, compute the removal count without rewriting any
        file.  When ``False`` (default), atomically rewrite each stream
        with only the non-orphan lines retained.

    Returns
    -------
    ``{stream_filename: events_removed}`` for every stream that existed
    on disk; streams not present on disk are omitted from the result
    (this is the same convention ``backends.read_events`` uses for
    missing streams — they're treated as "no events recorded yet" rather
    than an error).
    """
    root_path = Path(root)
    summary: Dict[str, int] = {}

    if not root_path.exists() or not root_path.is_dir():
        return summary

    # Lazy import: the lock helpers live in ``backends.py`` which pulls in
    # the full backend protocol surface; importing it eagerly at module
    # top would force every CLI invocation of ``maintenance`` to load the
    # Cloudflare stubs too.  Inline import keeps the dependency surface
    # narrow.
    try:
        if __package__ == "crucible.features.run_insights":
            from .backends import _file_lock_ctx  # type: ignore[attr-defined]
        else:  # pragma: no cover — flat-launcher fallback
            from backends import _file_lock_ctx  # type: ignore[no-redef]
    except Exception:
        _file_lock_ctx = None  # type: ignore[assignment]

    for filename in _STREAM_FILENAMES:
        stream_path = root_path / filename
        if not stream_path.exists():
            continue

        # Open the sidecar lock file (creates if absent) — _file_lock_ctx
        # operates on the file HANDLE, mirroring backends.write_event's
        # ``with open(lock_path, "a+") as lock_fh: with _file_lock_ctx(...)``
        # pattern so prune cannot race against a concurrent writer.
        lock_path: Path = _stream_lock_path(stream_path)
        try:
            lock_fh = open(lock_path, "a+", encoding="utf-8")
        except OSError:
            lock_fh = None  # type: ignore[assignment]

        try:
            if lock_fh is not None and _file_lock_ctx is not None:
                lock_ctx = _file_lock_ctx(lock_fh)
                lock_ctx.__enter__()
            else:
                lock_ctx = None

            try:
                removed = 0
                kept_lines: list[str] = []
                # Read entire file content; acceptable because the
                # write_event policy caps each stream at
                # MAX_ENTRIES_PER_STREAM (default 20_000), so worst-case
                # memory footprint is a few MB.
                with stream_path.open("r", encoding="utf-8", newline="") as fh:
                    for raw in fh:
                        if _is_orphan_event(raw):
                            removed += 1
                            continue
                        if raw.strip():
                            # Preserve trailing newline; if the file's
                            # last line is missing one (writer-crash
                            # tail) we add it on the rewrite so the
                            # rewritten stream is cleanly delimited.
                            kept_lines.append(raw if raw.endswith("\n") else raw + "\n")
                summary[filename] = removed
                if not dry_run and removed > 0:
                    _atomic_rewrite(stream_path, kept_lines)
            finally:
                if lock_ctx is not None:
                    try:
                        lock_ctx.__exit__(None, None, None)
                    except Exception:
                        pass
        finally:
            if lock_fh is not None:
                try:
                    lock_fh.close()
                except Exception:
                    pass

    return summary


__all__ = ["prune_orphan_events"]
