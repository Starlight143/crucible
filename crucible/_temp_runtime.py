from __future__ import annotations

import os
import shutil
import tempfile
import threading
import uuid
from functools import lru_cache
from pathlib import Path

_CONFIGURED_TEMP_ROOT: str | None = None
_TEMP_ROOT_LOCK = threading.Lock()
_ORIGINAL_MKDIR = os.mkdir
_ORIGINAL_MKDTEMP = tempfile.mkdtemp
_ORIGINAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


def _assert_writable_temp_root(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe_dir = path / f".tmp-probe-{uuid.uuid4().hex}"
    probe_dir.mkdir(parents=False, exist_ok=False)
    probe_file: Path | None = None
    try:
        probe_file = probe_dir / "write.txt"
        probe_file.write_text("ok", encoding="utf-8")
    finally:
        if probe_file is not None:
            try:
                probe_file.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            probe_dir.rmdir()
        except Exception:
            pass


@lru_cache(maxsize=1)
def _process_temp_root(base_root: str) -> str:
    base = Path(base_root).resolve()
    _assert_writable_temp_root(base)

    for _ in range(100):
        candidate = base / f"session-{os.getpid()}-{uuid.uuid4().hex}"
        if candidate.exists():
            continue
        try:
            _assert_writable_temp_root(candidate)
            return str(candidate)
        except OSError:
            try:
                candidate.rmdir()
            except Exception:
                pass
            continue

    raise RuntimeError(f"Could not allocate a writable temp session under {base}")


def _is_under_configured_temp_root(path: str | os.PathLike[str]) -> bool:
    if not _CONFIGURED_TEMP_ROOT:
        return False
    try:
        resolved = Path(path).resolve(strict=False)
        root = Path(_CONFIGURED_TEMP_ROOT).resolve(strict=False)
        return resolved == root or root in resolved.parents
    except Exception:
        return False


def _repo_safe_mkdir(
    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    mode: int = 0o777,
    *,
    dir_fd: int | None = None,
) -> None:
    if (
        os.name == "nt"
        and dir_fd is None
        and not isinstance(path, bytes)
        and _is_under_configured_temp_root(path)
    ):
        mode = 0o777

    if dir_fd is None:
        _ORIGINAL_MKDIR(path, mode)
    else:  # pragma: no cover - dir_fd is unavailable on Windows.
        _ORIGINAL_MKDIR(path, mode, dir_fd=dir_fd)


def ensure_writable_temp_root(project_root: str | Path | None = None) -> str:
    global _CONFIGURED_TEMP_ROOT

    with _TEMP_ROOT_LOCK:
        # Re-check inside the lock: another thread may have completed setup
        # between our outer check (if any) and acquiring the lock.
        if _CONFIGURED_TEMP_ROOT:
            return _CONFIGURED_TEMP_ROOT

        if project_root is None:
            project_root = Path(__file__).resolve().parents[1]
        root = Path(project_root).resolve()
        temp_root = root / ".tmp" / "runtime"

        selected = _process_temp_root(str(temp_root))
        os.environ["CODEX_TMP_DIR"] = selected
        os.environ["TMPDIR"] = selected
        os.environ["TEMP"] = selected
        os.environ["TMP"] = selected
        tempfile.tempdir = selected
        os.mkdir = _repo_safe_mkdir
        tempfile.mkdtemp = _repo_safe_mkdtemp
        tempfile.TemporaryDirectory = RepoTemporaryDirectory
        _CONFIGURED_TEMP_ROOT = selected
        return selected


def _normalize_temp_dir(dir_path: str | os.PathLike[str] | None) -> Path:
    if dir_path is None:
        configured = ensure_writable_temp_root()
        return Path(configured)
    return Path(dir_path).resolve()


def _repo_safe_mkdtemp(
    suffix: str | None = None,
    prefix: str | None = None,
    dir: str | os.PathLike[str] | None = None,
) -> str:
    base_dir = _normalize_temp_dir(dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    # Retry loop: UUID4 collisions are astronomically rare but stale temp dirs
    # from previous process runs (e.g. on container restart) can trigger
    # FileExistsError without a retry.  10 attempts is sufficient.
    for _ in range(10):
        name = f"{prefix or 'tmp'}{uuid.uuid4().hex}{suffix or ''}"
        candidate = base_dir / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return str(candidate)
        except FileExistsError:
            continue
    raise RuntimeError(
        f"_repo_safe_mkdtemp: could not allocate temp directory under {base_dir} "
        f"after 10 attempts"
    )


class RepoTemporaryDirectory:
    def __init__(
        self,
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
        ignore_cleanup_errors: bool = False,
    ) -> None:
        self.name = _repo_safe_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
        self._ignore_cleanup_errors = ignore_cleanup_errors
        self._closed = False

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()
        return None

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            shutil.rmtree(self.name, ignore_errors=self._ignore_cleanup_errors)
        except Exception:
            if not self._ignore_cleanup_errors:
                raise
