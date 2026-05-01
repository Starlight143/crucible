from __future__ import annotations

if __package__ == "crucible":
    from .runtime_api import get_runtime
else:
    from runtime_api import get_runtime


_rt = get_runtime()

main = _rt.main
run_self_check = _rt.run_self_check

__all__ = ["main", "run_self_check"]
