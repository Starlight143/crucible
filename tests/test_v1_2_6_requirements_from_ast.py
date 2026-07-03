"""
v1.2.6 regression — the generated bundle's ``requirements.txt`` must reflect the
APP's real imports (AST scan of ``code/``), NOT the Crucible pipeline's own venv
snapshot (crewai / langchain-openai / pydantic / fastapi / uvicorn).

Root cause that shipped in <=v1.2.5:
  * ``section_07.save_project_output`` wrote ``requirements.txt`` from
    ``collect_dependency_versions()`` (pipeline env via importlib.metadata) —
    so every bundle advertised the pipeline's deps regardless of what the app
    imported.  Clean-container ``pip install`` then crashed (ModuleNotFoundError
    for the real deps) and ``pip-audit`` reported phantom CVEs.
  * ``code_lockfile_generator._detect_imports`` / ``_generate_requirements_txt``
    were the correct AST detector but were never wired into the save flow.

Fix: ``write_requirements_for_code(code_dir)`` (AST) is called right after the
code bundle is written; the pipeline-snapshot write was removed.

These are CLAUDE.md §9.6 producer→consumer wiring tests: they assert the
producer (AST detector) is actually wired into the consumer (save flow +
dependency auditor), not merely internally self-consistent.
"""
from __future__ import annotations

import inspect
import os
import tempfile

from crucible.features.code_lockfile_generator import (
    _KNOWN_VERSIONS,
    write_requirements_for_code,
)
from crucible.features.dependency_auditor import _find_requirements_file
from crucible.modules import section_07_selfcheck_output_main as _s07

_PHANTOM_PIPELINE_DEPS = ("crewai", "langchain-openai", "langchain", "fastapi", "uvicorn")

_FLASK_APP_SRC = (
    "import os\n"
    "import json\n"
    "import flask\n"
    "import requests\n"
    "from openai import OpenAI\n"
    "\n"
    "app = flask.Flask(__name__)\n"
)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_flask_openai_requests_are_pinned() -> None:
    """The report's required pins must exist in the known-versions table."""
    for pkg in ("flask", "openai", "requests"):
        assert pkg in _KNOWN_VERSIONS, f"{pkg} must be pinned in _KNOWN_VERSIONS"


def test_generated_requirements_matches_code_imports() -> None:
    """AST scan of a Flask app yields flask/requests/openai — not pipeline deps."""
    with tempfile.TemporaryDirectory() as td:
        code_dir = os.path.join(td, "code")
        _write(os.path.join(code_dir, "main.py"), _FLASK_APP_SRC)
        _write(os.path.join(code_dir, "scraper.py"), "import requests\n")

        req_path = write_requirements_for_code(code_dir)
        assert req_path == os.path.join(code_dir, "requirements.txt")

        with open(req_path, "r", encoding="utf-8") as fh:
            content = fh.read()

        # Real app deps present (pinned from _KNOWN_VERSIONS).
        assert "flask==" in content
        assert "requests==" in content
        assert "openai==" in content
        # Pipeline's own deps must NOT leak into the app manifest.
        for phantom in _PHANTOM_PIPELINE_DEPS:
            assert phantom not in content, f"phantom pipeline dep {phantom!r} leaked"
        # stdlib filtered out.
        assert "\nos" not in content and "\njson" not in content


def test_first_party_modules_are_not_emitted() -> None:
    """A generated app's own sibling modules (config, scraper, a subpackage)
    must NOT leak into requirements.txt as phantom PyPI packages — otherwise
    ``pip install -r requirements.txt`` fails trying to fetch them."""
    with tempfile.TemporaryDirectory() as td:
        code_dir = os.path.join(td, "code")
        _write(
            os.path.join(code_dir, "main.py"),
            "import flask\n"
            "from config import Settings\n"
            "from scraper import crawl\n"
            "from pkg.util import helper\n",
        )
        _write(os.path.join(code_dir, "config.py"), "Settings = object\n")
        _write(os.path.join(code_dir, "scraper.py"), "def crawl():\n    pass\n")
        _write(os.path.join(code_dir, "pkg", "__init__.py"), "")
        _write(os.path.join(code_dir, "pkg", "util.py"), "def helper():\n    pass\n")

        req_path = write_requirements_for_code(code_dir)
        with open(req_path, "r", encoding="utf-8") as fh:
            content = fh.read()

        assert "flask==" in content
        for local in ("config", "scraper", "pkg"):
            assert local not in content, f"first-party module {local!r} leaked into requirements"


def test_write_requirements_returns_none_for_missing_dir() -> None:
    """Best-effort contract: missing code/ never raises, returns None."""
    with tempfile.TemporaryDirectory() as td:
        assert write_requirements_for_code(os.path.join(td, "nope")) is None


def test_requirements_source_is_ast_not_env_snapshot() -> None:
    """save_project_output must no longer write requirements.txt from the
    pipeline dependency_versions snapshot — the write to project root and the
    DEPENDENCY_PIP_NAMES mapping it used are gone."""
    src = inspect.getsource(_s07.save_project_output)
    assert 'os.path.join(project_dir, "requirements.txt")' not in src, (
        "save_project_output still writes the pipeline-snapshot requirements.txt "
        "to the run root"
    )
    assert "DEPENDENCY_PIP_NAMES" not in src, (
        "the pipeline pip-name mapping is only used by the removed snapshot write"
    )


def test_save_project_output_wires_ast_requirements() -> None:
    """The AST generator must actually be called in the save flow (§9.6:
    assert the wire-up code exists, not just that the producer is correct)."""
    src = inspect.getsource(_s07.save_project_output)
    assert "write_requirements_for_code(code_dir)" in src
    assert "code_lockfile_generator import write_requirements_for_code" in src


def test_dependency_audit_targets_code_requirements() -> None:
    """Producer→consumer: after write_requirements_for_code populates
    code/requirements.txt, the auditor's finder resolves to it — preferring the
    AST manifest over any stale phantom snapshot left at the run root."""
    with tempfile.TemporaryDirectory() as run_dir:
        # Simulate the OLD bug's leftover at the run root.
        _write(
            os.path.join(run_dir, "requirements.txt"),
            "crewai==0.67.1\nlangchain-openai==0.1.25\nfastapi==0.111.1\nuvicorn==0.30.1\n",
        )
        code_dir = os.path.join(run_dir, "code")
        _write(os.path.join(code_dir, "main.py"), _FLASK_APP_SRC)

        written = write_requirements_for_code(code_dir)
        assert written is not None

        resolved = _find_requirements_file(run_dir)
        assert resolved == os.path.join(run_dir, "code", "requirements.txt"), (
            "auditor must audit the AST-generated code/requirements.txt, not the "
            "phantom root snapshot"
        )
        with open(resolved, "r", encoding="utf-8") as fh:
            audited = fh.read()
        assert "flask==" in audited
        for phantom in _PHANTOM_PIPELINE_DEPS:
            assert phantom not in audited
