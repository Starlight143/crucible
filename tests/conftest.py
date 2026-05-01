from __future__ import annotations

from pathlib import Path

from crucible._temp_runtime import ensure_writable_temp_root

ensure_writable_temp_root(Path(__file__).resolve().parents[1])
