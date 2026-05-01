from __future__ import annotations

import os
import tempfile
from pathlib import Path

from crucible._temp_runtime import ensure_writable_temp_root


def test_temp_root_uses_process_scoped_session_dir() -> None:
    project_root = Path(__file__).resolve().parents[1]

    selected = Path(ensure_writable_temp_root(project_root)).resolve()

    assert selected.parent == (project_root / ".tmp" / "runtime").resolve()
    assert selected.name.startswith(f"session-{os.getpid()}-")
    assert os.environ["CODEX_TMP_DIR"] == str(selected)
    assert os.environ["TEMP"] == str(selected)
    assert os.environ["TMP"] == str(selected)
    assert os.environ["TMPDIR"] == str(selected)
    assert tempfile.tempdir == str(selected)


def test_temp_root_is_stable_within_process() -> None:
    first = ensure_writable_temp_root(Path(__file__).resolve().parents[1])
    second = ensure_writable_temp_root(Path(__file__).resolve().parents[1])

    assert second == first


def test_windows_private_mode_mkdir_under_temp_root_remains_writable() -> None:
    selected = Path(ensure_writable_temp_root(Path(__file__).resolve().parents[1]))
    child = selected / "mode-700-child"

    os.mkdir(child, mode=0o700)
    try:
        marker = child / "write.txt"
        marker.write_text("ok", encoding="utf-8")
        assert marker.read_text(encoding="utf-8") == "ok"
    finally:
        marker.unlink(missing_ok=True)
        child.rmdir()
