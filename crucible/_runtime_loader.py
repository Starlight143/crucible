from __future__ import annotations

from types import SimpleNamespace

if __package__ == "crucible":
    from .module_runtime import get_runtime as _get_runtime
else:
    from module_runtime import get_runtime as _get_runtime


def load_runtime() -> SimpleNamespace:
    return _get_runtime()
