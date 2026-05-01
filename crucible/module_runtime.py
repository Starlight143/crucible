# ruff: noqa: I001
from __future__ import annotations

import os
import threading
import types
from pathlib import Path
from typing import Iterable


PACKAGE_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PACKAGE_ROOT.parent
ROOT_LAUNCHER_FILE = WORKSPACE_ROOT / "run_crucible.py"
DEFAULT_ENV_FILE = WORKSPACE_ROOT / ".env"

if "CRUCIBLE_ENV_FILE" not in os.environ and DEFAULT_ENV_FILE.is_file():
    os.environ["CRUCIBLE_ENV_FILE"] = str(DEFAULT_ENV_FILE)

if __package__ == "crucible":
    from .modules import (
        section_00_bootstrap_and_utils as m00,
        section_01_extraction_and_reformat as m01,
        section_02_research_and_llm as m02,
        section_03_models_and_context as m03,
        section_04_web_research_and_direction as m04,
        section_05_analysis_and_codegen as m05,
        section_06_runtime_quality_api as m06,
        section_07_selfcheck_output_main as m07,
    )
else:
    from modules import (
        section_00_bootstrap_and_utils as m00,
        section_01_extraction_and_reformat as m01,
        section_02_research_and_llm as m02,
        section_03_models_and_context as m03,
        section_04_web_research_and_direction as m04,
        section_05_analysis_and_codegen as m05,
        section_06_runtime_quality_api as m06,
        section_07_selfcheck_output_main as m07,
    )


SECTION_MODULES = (m00, m01, m02, m03, m04, m05, m06, m07)
_RUNTIME_SINGLETON: types.SimpleNamespace | None = None
_RUNTIME_LOCK = threading.Lock()


def _shared_items(module: types.ModuleType) -> dict[str, object]:
    items: dict[str, object] = {}
    for key, value in module.__dict__.items():
        if key.startswith("__") and key not in {"__annotations__", "__doc__"}:
            continue
        items[key] = value
    return items


def _sync_module_namespaces(modules: Iterable[types.ModuleType]) -> dict[str, object]:
    # Materialise the iterable once so that the two-pass algorithm works even
    # when the caller passes a generator or other single-use iterator.
    module_list = list(modules)
    shared: dict[str, object] = {}
    for module in module_list:
        shared.update(_shared_items(module))
    for module in module_list:
        module.__dict__.update(shared)
    return shared


def _resolved_env_path(fallback: object) -> object:
    configured = os.environ.get("CRUCIBLE_ENV_FILE", "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = (WORKSPACE_ROOT / candidate).resolve()
        return str(candidate)
    return fallback


def _apply_root_path_context(
    modules: Iterable[types.ModuleType], shared: dict[str, object]
) -> dict[str, object]:
    # Materialise the iterable: same single-use-iterator guard as _sync_module_namespaces.
    module_list = list(modules)
    root_file = str(ROOT_LAUNCHER_FILE)
    root_dir = str(WORKSPACE_ROOT)
    loaded_env_file = _resolved_env_path(shared.get("LOADED_ENV_FILE"))

    shared["PROJECT_ROOT"] = root_dir
    shared["LOADED_ENV_FILE"] = loaded_env_file
    shared["__file__"] = root_file

    for module in module_list:
        module.__dict__["PROJECT_ROOT"] = root_dir
        module.__dict__["LOADED_ENV_FILE"] = loaded_env_file
        # NOTE: Do NOT overwrite module.__dict__["__file__"]; each section module
        # carries its own source path and code like Path(__file__).parent depends on it.


    return shared


def get_runtime() -> types.SimpleNamespace:
    global _RUNTIME_SINGLETON
    # Always hold the lock for both the check and the return so that callers
    # never observe a partially-constructed SimpleNamespace.  Under standard
    # CPython the GIL makes the outer unsynchronised read safe in practice,
    # but Python 3.13+ free-threaded mode (no-GIL) makes it a genuine data
    # race.  The lock is uncontended on every call after the first
    # initialisation so the overhead is negligible.
    with _RUNTIME_LOCK:
        if _RUNTIME_SINGLETON is None:
            shared = _sync_module_namespaces(SECTION_MODULES)
            shared = _apply_root_path_context(SECTION_MODULES, shared)
            _RUNTIME_SINGLETON = types.SimpleNamespace(**shared)
        return _RUNTIME_SINGLETON
