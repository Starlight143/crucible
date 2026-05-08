"""
features/code_lockfile_generator.py
=====================================
Generates ``pyproject.toml`` + pinned ``requirements.txt`` for the AI-generated
code inside a pipeline run directory.

Does NOT modify any AI-generated source files.  All generated packaging files
are placed alongside the code in ``{run_dir}/code/``.

Usage::

    from crucible.features.code_lockfile_generator import generate_lockfiles

    result = generate_lockfiles("/path/to/saved_projects/my_run")
    print(f"Detected {len(result.detected_deps)} dependencies")
    print(f"pyproject.toml: {result.pyproject_path}")
    print(f"requirements.txt: {result.requirements_path}")
"""
from __future__ import annotations

import ast
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

_log = logging.getLogger(__name__)

# ── Env helpers ───────────────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_bool(name: str, default: bool) -> bool:
    return _env.env_bool(name, default)

def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)

def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default)

def _env_str(name: str, default: str) -> str:
    return _env.env_str_passthrough(name, default)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class LockfileConfig:
    include_dev_deps: bool = True
    python_version: str = ">=3.9"
    package_manager: str = "uv"


@dataclass
class DetectedDependency:
    name: str               # PyPI package name
    import_name: str        # Python import name
    pinned_version: Optional[str]
    category: str           # "core" | "data" | "optional"
    is_stdlib: bool


@dataclass
class LockfileResult:
    detected_deps: List[DetectedDependency] = field(default_factory=list)
    pyproject_path: str = ""
    requirements_path: str = ""
    requirements_dev_path: str = ""   # requirements-dev.txt (dev/test dependencies)
    python_version_path: str = ""     # .python-version file
    errors: List[str] = field(default_factory=list)


# ── Hardcoded known package versions ─────────────────────────────────────────

_KNOWN_VERSIONS: Dict[str, str] = {
    "yfinance": "0.2.51",
    "ccxt": "4.3.95",
    "pandas": "2.2.2",
    "numpy": "1.26.4",
    "scipy": "1.13.1",
    "matplotlib": "3.9.0",
    "optuna": "3.6.1",
    "mlflow": "2.14.1",
    "crewai": "0.67.1",
    "langchain-openai": "0.1.25",
    "pydantic": "2.7.4",
    "httpx": "0.27.0",
    "requests": "2.32.3",
    "fastapi": "0.111.1",
    "uvicorn": "0.30.1",
    "sqlalchemy": "2.0.31",
    "alembic": "1.13.2",
    "pytest": "8.2.2",
    "ta": "0.11.0",
    "ta-lib": "0.4.32",
    "backtrader": "1.9.78.123",
    "quantstats": "0.0.62",
    "statsmodels": "0.14.2",
    # Additional common scientific/ML packages
    "scikit-learn": "1.5.0",
    "xgboost": "2.1.0",
    "lightgbm": "4.3.0",
    "plotly": "5.22.0",
    "seaborn": "0.13.2",
    "joblib": "1.4.2",
    "tqdm": "4.66.4",
    "rich": "13.7.1",
    "click": "8.1.7",
    "python-dotenv": "1.0.1",
    "aiohttp": "3.9.5",
    "websockets": "12.0",
    "redis": "5.0.7",
    "celery": "5.4.0",
    "psycopg2-binary": "2.9.9",
    "pymongo": "4.8.0",
    "boto3": "1.34.144",
    "langchain": "0.2.6",
    "langchain-core": "0.2.10",
    "langchain-community": "0.2.6",
    "openai": "1.35.10",
    "anthropic": "0.28.1",
    "tiktoken": "0.7.0",
}

# ── Import name → PyPI package name mapping ───────────────────────────────────

_IMPORT_TO_PACKAGE: Dict[str, str] = {
    # Data science
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "skimage": "scikit-image",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "pkg_resources": "setuptools",
    # Trading / Finance
    "ta": "ta",
    "talib": "ta-lib",
    "backtrader": "backtrader",
    "quantstats": "quantstats",
    "yfinance": "yfinance",
    "ccxt": "ccxt",
    "statsmodels": "statsmodels",
    # ML
    "xgboost": "xgboost",
    "lightgbm": "lightgbm",
    "optuna": "optuna",
    "mlflow": "mlflow",
    # Web
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "starlette": "starlette",
    "aiohttp": "aiohttp",
    "websockets": "websockets",
    # Database
    "sqlalchemy": "sqlalchemy",
    "alembic": "alembic",
    "psycopg2": "psycopg2-binary",
    "pymongo": "pymongo",
    "redis": "redis",
    # LLM / AI
    "crewai": "crewai",
    "langchain_openai": "langchain-openai",
    "langchain_core": "langchain-core",
    "langchain_community": "langchain-community",
    "langchain": "langchain",
    "openai": "openai",
    "anthropic": "anthropic",
    "tiktoken": "tiktoken",
    # Cloud
    "boto3": "boto3",
    "botocore": "botocore",
    # Utilities
    "celery": "celery",
    "pydantic": "pydantic",
    "httpx": "httpx",
    "requests": "requests",
    "tqdm": "tqdm",
    "rich": "rich",
    "click": "click",
    "typer": "typer",
    "loguru": "loguru",
    "plotly": "plotly",
    "seaborn": "seaborn",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "joblib": "joblib",
}

# ── Data category classification ──────────────────────────────────────────────

_DATA_PACKAGES: Set[str] = {
    "yfinance", "ccxt", "pandas", "numpy", "scipy",
    "statsmodels", "ta", "ta-lib", "backtrader", "quantstats",
    "scikit-learn", "xgboost", "lightgbm",
}

_DEV_PACKAGES: Set[str] = {
    "pytest", "pytest-cov", "pytest-mock", "mypy", "black", "ruff",
    "isort", "flake8", "pylint", "hypothesis",
}

# ── Python stdlib module names ────────────────────────────────────────────────

def _get_stdlib_modules() -> Set[str]:
    """Return the set of stdlib module names, using sys.stdlib_module_names when available."""
    if hasattr(sys, "stdlib_module_names"):
        return set(sys.stdlib_module_names)   # Python 3.10+
    # Fallback: hardcoded common stdlib set for Python 3.9+
    return {
        "abc", "ast", "asyncio", "base64", "builtins", "calendar", "cgi",
        "cmath", "cmd", "code", "codecs", "collections", "colorsys", "compileall",
        "concurrent", "configparser", "contextlib", "copy", "copyreg", "csv",
        "ctypes", "dataclasses", "datetime", "decimal", "difflib", "dis",
        "email", "encodings", "enum", "errno", "faulthandler", "filecmp",
        "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
        "getpass", "gettext", "glob", "grp", "gzip", "hashlib", "heapq",
        "hmac", "html", "http", "idlelib", "imaplib", "importlib", "inspect",
        "io", "ipaddress", "itertools", "json", "keyword", "lib2to3",
        "linecache", "locale", "logging", "lzma", "mailbox", "math",
        "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc",
        "nis", "nntplib", "numbers", "operator", "os", "pathlib", "pdb",
        "pickle", "pickletools", "platform", "plistlib", "poplib",
        "posix", "posixpath", "pprint", "profile", "pstats", "pty",
        "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri",
        "random", "re", "readline", "reprlib", "rlcompleter", "runpy",
        "sched", "secrets", "select", "selectors", "shelve", "shlex",
        "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr",
        "socket", "socketserver", "spwd", "sqlite3", "sre_compile",
        "sre_constants", "sre_parse", "ssl", "stat", "statistics",
        "string", "stringprep", "struct", "subprocess", "sunau",
        "symtable", "sys", "sysconfig", "syslog", "tabnanny", "tarfile",
        "telnetlib", "tempfile", "termios", "test", "textwrap", "threading",
        "time", "timeit", "tkinter", "token", "tokenize", "tomllib",
        "trace", "traceback", "tracemalloc", "tty", "turtle", "turtledemo",
        "types", "typing", "unicodedata", "unittest", "urllib", "uu",
        "uuid", "venv", "warnings", "wave", "weakref", "webbrowser",
        "winreg", "winsound", "wsgiref", "xdrlib", "xml", "xmlrpc",
        "zipapp", "zipfile", "zipimport", "zlib", "zoneinfo",
        "_thread", "__future__", "antigravity", "cProfile", "this",
    }


_STDLIB_MODULES: Set[str] = _get_stdlib_modules()


# ── Import detection ──────────────────────────────────────────────────────────

def _detect_imports(code_dir: str) -> List[str]:
    """
    Walk all .py files in code_dir (recursively), parse with ast.parse, and
    return a sorted list of unique top-level package names after filtering
    stdlib modules.
    """
    found: Set[str] = set()
    if not os.path.isdir(code_dir):
        return []

    for dirpath, _dirs, filenames in os.walk(code_dir):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                continue

            try:
                tree = ast.parse(source, filename=fpath)
            except SyntaxError:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top and not top.startswith("_"):
                            found.add(top)
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:   # absolute import
                        top = node.module.split(".")[0]
                        if top and not top.startswith("_"):
                            found.add(top)

    # Remove stdlib and built-in modules
    result = sorted(
        pkg for pkg in found
        if pkg not in _STDLIB_MODULES and pkg not in {"__builtins__", ""}
    )
    return result


# ── Package resolution ────────────────────────────────────────────────────────

def _resolve_package(import_name: str) -> DetectedDependency:
    """
    Map a Python import name to a PyPI package, look up its pinned version,
    and classify it.
    """
    # Normalize
    pkg_name = _IMPORT_TO_PACKAGE.get(import_name, import_name)

    pinned = _KNOWN_VERSIONS.get(pkg_name)
    if pinned is None:
        # Try lowercase / dash-normalised lookup
        lower_pkg = pkg_name.lower().replace("_", "-")
        pinned = _KNOWN_VERSIONS.get(lower_pkg)
        if pinned:
            pkg_name = lower_pkg

    is_stdlib = import_name in _STDLIB_MODULES

    if is_stdlib:
        category = "core"
    elif pkg_name in _DATA_PACKAGES:
        category = "data"
    elif pkg_name in _DEV_PACKAGES:
        category = "optional"
    else:
        category = "core"

    return DetectedDependency(
        name=pkg_name,
        import_name=import_name,
        pinned_version=pinned,
        category=category,
        is_stdlib=is_stdlib,
    )


# ── File generators ───────────────────────────────────────────────────────────

def _generate_pyproject_toml(
    deps: List[DetectedDependency],
    config: LockfileConfig,
    project_name: str,
) -> str:
    """Generate a valid TOML string for pyproject.toml."""
    runtime_deps: List[str] = []
    optional_deps: List[str] = []

    for dep in deps:
        if dep.is_stdlib:
            continue
        pin = f"=={dep.pinned_version}" if dep.pinned_version else ""
        entry = f'"{dep.name}{pin}"'
        if dep.category == "optional":
            optional_deps.append(entry)
        else:
            runtime_deps.append(entry)

    # Dev deps: always include pytest + coverage + type checking
    dev_lines = [
        '"pytest==8.2.2"',
        '"pytest-cov==5.0.0"',
        '"mypy==1.10.0"',
        '"ruff==0.4.9"',
    ]

    lines: List[str] = [
        "[build-system]",
        'requires = ["hatchling"]',
        'build-backend = "hatchling.build"',
        "",
        "[project]",
        f'name = "{project_name}"',
        'version = "0.1.0"',
        'description = "Auto-generated trading strategy — Crucible"',
        f'requires-python = "{config.python_version}"',
        "dependencies = [",
    ]
    for dep_str in runtime_deps:
        lines.append(f"    {dep_str},")
    lines.append("]")
    lines.append("")
    if optional_deps:
        lines.append("[project.optional-dependencies]")
        lines.append("optional = [")
        for dep_str in optional_deps:
            lines.append(f"    {dep_str},")
        lines.append("]")
        if config.include_dev_deps:
            lines.append("dev = [")
            for dep_str in dev_lines:
                lines.append(f"    {dep_str},")
            lines.append("]")
        lines.append("")
    elif config.include_dev_deps:
        lines.append("[project.optional-dependencies]")
        lines.append("dev = [")
        for dep_str in dev_lines:
            lines.append(f"    {dep_str},")
        lines.append("]")
        lines.append("")

    lines += [
        "[tool.hatch.build.targets.wheel]",
        'packages = ["."]',
        "",
        "[tool.ruff]",
        "line-length = 100",
        'target-version = "py39"',
        "",
        "[tool.mypy]",
        'python_version = "3.11"',
        "strict = false",
        "ignore_missing_imports = true",
        "",
        "[tool.pytest.ini_options]",
        'testpaths = ["tests"]',
        'python_files = ["test_*.py", "*_test.py"]',
    ]
    return "\n".join(lines) + "\n"


def _generate_requirements_txt(deps: List[DetectedDependency]) -> str:
    """Generate a pinned requirements.txt."""
    lines: List[str] = [
        "# Auto-generated by Crucible — code_lockfile_generator",
        "# Review and adjust versions before production deployment.",
        "",
    ]
    for dep in sorted(deps, key=lambda d: d.name.lower()):
        if dep.is_stdlib or dep.category == "optional":
            continue
        if dep.pinned_version:
            lines.append(f"{dep.name}=={dep.pinned_version}")
        else:
            lines.append(f"{dep.name}  # version unknown — pin manually")
    return "\n".join(lines) + "\n"


def _generate_requirements_dev_txt() -> str:
    """Generate requirements-dev.txt with pinned dev dependencies."""
    return "\n".join([
        "# Development dependencies — auto-generated by Crucible",
        "",
        "pytest==8.2.2",
        "pytest-cov==5.0.0",
        "pytest-mock==3.14.0",
        "mypy==1.10.0",
        "ruff==0.4.9",
        "black==24.4.2",
        "isort==5.13.2",
        "hypothesis==6.104.1",
        "freezegun==1.5.1",
        "",
    ])


# ── Run-mode guard ────────────────────────────────────────────────────────────

def _is_quant_run(run_dir: str) -> bool:
    """Return True if this is a quant-mode run (or mode unknown)."""
    result_path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(result_path):
        return True
    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode = str(data.get("mode", "")).lower()
        return mode in ("quant", "")
    except (OSError, json.JSONDecodeError):
        return True


# ── Public API ────────────────────────────────────────────────────────────────

def generate_lockfiles(
    run_dir: str,
    config: Optional[LockfileConfig] = None,
) -> LockfileResult:
    """
    Detect imports from ``{run_dir}/code/``, generate packaging files, and
    save a summary report to ``{run_dir}/lockfile_report.json``.

    Files written to ``{run_dir}/code/``:
    - ``pyproject.toml``
    - ``requirements.txt``
    - ``requirements-dev.txt``
    - ``.python-version``

    Returns a LockfileResult (never raises).
    """
    if config is None:
        config = LockfileConfig()

    result = LockfileResult()

    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        result.errors.append(
            f"Code directory not found: {code_dir}"
        )
        _save_lockfile_report(run_dir, result)
        return result

    # Detect imports
    raw_imports = _detect_imports(code_dir)
    _log.info("Lockfile: detected %d raw imports in %s", len(raw_imports), code_dir)

    seen_packages: Set[str] = set()
    for import_name in raw_imports:
        dep = _resolve_package(import_name)
        if dep.is_stdlib:
            continue
        if dep.name in seen_packages:
            continue
        seen_packages.add(dep.name)
        result.detected_deps.append(dep)

    # Derive project name from run_dir basename
    project_name = os.path.basename(run_dir).replace(" ", "_").replace("-", "_")
    if not project_name:
        project_name = "quant_strategy"

    # Generate pyproject.toml
    pyproject_content = _generate_pyproject_toml(
        result.detected_deps, config, project_name
    )
    pyproject_path = os.path.join(code_dir, "pyproject.toml")
    _tmp_lf = pyproject_path + ".tmp"
    try:
        with open(_tmp_lf, "w", encoding="utf-8") as fh:
            fh.write(pyproject_content)
        os.replace(_tmp_lf, pyproject_path)
        result.pyproject_path = pyproject_path
        _log.info("Lockfile: wrote %s", pyproject_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_lf)
        except OSError:
            pass
        result.errors.append(f"Failed to write pyproject.toml: {exc}")

    # Generate requirements.txt
    req_content = _generate_requirements_txt(result.detected_deps)
    req_path = os.path.join(code_dir, "requirements.txt")
    _tmp_lf = req_path + ".tmp"
    try:
        with open(_tmp_lf, "w", encoding="utf-8") as fh:
            fh.write(req_content)
        os.replace(_tmp_lf, req_path)
        result.requirements_path = req_path
        _log.info("Lockfile: wrote %s", req_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_lf)
        except OSError:
            pass
        result.errors.append(f"Failed to write requirements.txt: {exc}")

    # Generate requirements-dev.txt
    dev_content = _generate_requirements_dev_txt()
    dev_path = os.path.join(code_dir, "requirements-dev.txt")
    _tmp_lf = dev_path + ".tmp"
    try:
        with open(_tmp_lf, "w", encoding="utf-8") as fh:
            fh.write(dev_content)
        os.replace(_tmp_lf, dev_path)
        result.requirements_dev_path = dev_path
        _log.info("Lockfile: wrote %s", dev_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_lf)
        except OSError:
            pass
        result.errors.append(f"Failed to write requirements-dev.txt: {exc}")

    # Generate .python-version
    pv_path = os.path.join(code_dir, ".python-version")
    _tmp_lf = pv_path + ".tmp"
    try:
        with open(_tmp_lf, "w", encoding="utf-8") as fh:
            fh.write("3.11\n")
        os.replace(_tmp_lf, pv_path)
        result.python_version_path = pv_path
    except OSError as exc:
        try:
            os.unlink(_tmp_lf)
        except OSError:
            pass
        result.errors.append(f"Failed to write .python-version: {exc}")

    _save_lockfile_report(run_dir, result)
    return result


def _save_lockfile_report(run_dir: str, result: LockfileResult) -> None:
    """Save lockfile_report.json to run_dir."""
    report_path = os.path.join(run_dir, "lockfile_report.json")
    payload = {
        "detected_deps": [
            {
                "name": d.name,
                "import_name": d.import_name,
                "pinned_version": d.pinned_version,
                "category": d.category,
                "is_stdlib": d.is_stdlib,
            }
            for d in result.detected_deps
        ],
        "pyproject_path": result.pyproject_path,
        "requirements_path": result.requirements_path,
        "requirements_dev_path": result.requirements_dev_path,
        "python_version_path": result.python_version_path,
        "errors": result.errors,
    }
    _tmp_report = report_path + ".tmp"
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(_tmp_report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(_tmp_report, report_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_report)
        except OSError:
            pass
        result.errors.append(f"Failed to save lockfile report: {exc}")
