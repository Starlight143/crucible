from __future__ import annotations

from types import SimpleNamespace

if __package__ == "crucible":
    from ._temp_runtime import ensure_writable_temp_root
    from .module_runtime import get_runtime as _get_runtime
else:
    from _temp_runtime import ensure_writable_temp_root
    from module_runtime import get_runtime as _get_runtime

ensure_writable_temp_root()


def get_runtime() -> SimpleNamespace:
    return _get_runtime()
