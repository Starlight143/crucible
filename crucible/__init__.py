# ruff: noqa: E402
from __future__ import annotations

from ._temp_runtime import ensure_writable_temp_root

ensure_writable_temp_root()

# Import ``runtime_logging`` first so its module-level ``_load_dotenv_once()``
# populates ``os.environ`` from ``.env`` *before* any downstream module reads
# env vars at import time (e.g. ``backtest_runner.BACKTEST_TIMEOUT =
# _env_int(...)``).  Without this, ``CRUCIBLE_LOG_LEVEL=DEBUG`` set in
# ``.env`` would be silently ignored because no caller would have invoked
# ``load_dotenv`` before logging was first configured.  Importing the module
# is enough — module-level code runs the loader.  The ``# noqa: F401`` flag
# tells linters this is an intentional side-effect import.
from . import runtime_logging  # noqa: F401  side-effect: loads .env

from .analysis import build_analysis_crew, build_codegen_crew, build_crew
from .bootstrap import init_llm, load_api_key
from .cli import main, run_self_check
from .models import AnalysisReport, CodeBundle, DirectionDecision, GateDecision, ReviewReport
from .quality import run_api_version_check, run_quality_loop, run_runtime_validation
from .research import run_direction_debate, run_librarian_research
from .runtime_api import get_runtime

# v1.1.2 (audit fix G6-D-HIGH-1): single source-of-truth for the package
# version.  Mirrors ``pyproject.toml::project.version`` so callers can do
# ``crucible.__version__`` without an extra ``importlib.metadata`` round-trip.
# Keep both in sync on every release; the regression test in
# ``tests/test_v1_1_2_audit_fixes.py`` pins them to be equal.
__version__ = "1.1.12"

__all__ = [
    # NOTE: ``__version__`` is intentionally NOT in __all__.  The
    # existing ``test_crucible_runtime`` invariant requires every
    # ``__all__`` entry to also be exposed on ``module_runtime``;
    # ``__version__`` is package metadata, not a runtime API.  It is
    # still accessible as ``crucible.__version__`` (Python module
    # attribute lookup), just not exported via ``from crucible import *``.
    "get_runtime",
    "main",
    "run_self_check",
    "load_api_key",
    "init_llm",
    "AnalysisReport",
    "CodeBundle",
    "ReviewReport",
    "DirectionDecision",
    "GateDecision",
    "run_librarian_research",
    "run_direction_debate",
    "build_crew",
    "build_analysis_crew",
    "build_codegen_crew",
    "run_runtime_validation",
    "run_quality_loop",
    "run_api_version_check",
]
