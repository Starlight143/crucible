"""
crucible/_atomic_io.py
======================
Atomic file-write helpers with POSIX directory fsync.

v1.1.9 (H1): factored from the multiple ad-hoc copies that lived inside
``section_07_selfcheck_output_main.py:_atomic_write_text``,
``features/quant_analytics.py`` (walk-forward + analytics report writers),
and the duplicate ``_fsync_dir`` inside ``features/run_insights/backends.py``.

Why a shared helper
-------------------
Every "open .tmp → write → close → os.replace(tmp, final)" pattern that
omits a parent-directory fsync is silently power-loss-unsafe on POSIX
filesystems: ``os.replace`` updates the directory entry in the page cache
but the metadata commit can happen seconds later.  If a kernel panic or
power loss occurs in that window, the directory entry is rolled back to
the *old* inode while the new file content sits on disk with no name
referring to it.  Callers see "the file vanished" or "the old version
came back" after reboot.

Windows NTFS commits metadata via ``FlushFileBuffers`` on the file handle
itself, so the directory fsync is a no-op there — but it must remain a
silent no-op rather than raise (``O_DIRECTORY`` doesn't exist on Win32).

This module deliberately has zero non-stdlib imports so it can be reached
from any layer (modules/, features/, top-level entry points) under either
``python -m crucible`` (package mode) or
``python crucible/__main__.py`` (flat-launcher mode) without the tri-modal
import dance other helpers need.
"""
from __future__ import annotations

import os
from typing import Union

__all__ = ["fsync_dir", "atomic_write_text"]


def fsync_dir(path: Union[str, os.PathLike]) -> None:
    """Best-effort fsync the directory entry at *path*.

    POSIX-only; silent no-op on Windows.  All failures (missing dir,
    permission error, ``O_DIRECTORY`` not available, etc.) are swallowed
    because durability is best-effort — the caller has already done the
    primary write + replace and we must not raise after that succeeds.
    """
    if os.name != "posix":
        return
    dirfd = None
    try:
        dirfd = os.open(os.fspath(path), getattr(os, "O_DIRECTORY", os.O_RDONLY))
        os.fsync(dirfd)
    except (OSError, ValueError, AttributeError):
        return
    finally:
        if dirfd is not None:
            try:
                os.close(dirfd)
            except OSError:
                pass


def atomic_write_text(
    path: Union[str, os.PathLike],
    content: str,
    encoding: str = "utf-8",
    *,
    fsync_parent: bool = True,
) -> None:
    """Write *content* to *path* atomically via a sibling ``.tmp`` file.

    If the process is killed between ``open()`` and ``close()`` the
    original *path* is left intact.  ``os.replace`` is atomic on POSIX
    and best-effort on Windows.  When *fsync_parent* is True (the
    default) the parent directory is fsynced after the rename so the
    new directory entry survives a power loss on POSIX.
    """
    target = os.fspath(path)
    tmp_path = target + ".tmp"
    try:
        with open(tmp_path, "w", encoding=encoding) as _fh:
            _fh.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if fsync_parent:
        parent = os.path.dirname(target) or "."
        fsync_dir(parent)
