"""
v1.2.7 — deterministic dependency declaration inside the quality loop.

Through v1.2.6 the runtime cross-reference lint (``mode_validation_matrix``)
flagged "framework imported but not declared in any requirements file" every
round when the LLM output omitted ``requirements.txt``.  After 3 stagnant rounds
the loop emitted ``QUALITY_LOOP_GAVE_UP`` and marked otherwise-fine code
"not production-ready" (observed in the real run
``20260703_013923_..._pricing_sensitivity_gtm_validation``).

Fix: ``section_06.run_quality_loop`` calls ``_ensure_bundle_requirements`` before
each validation round, synthesising ``requirements.txt`` from the bundle's actual
imports (AST, in memory) via
``code_lockfile_generator.requirements_text_from_sources``.

These include a §9.6 producer→consumer test: the synthesised manifest is checked
against the REAL review consumer (``analyse_saas_lint_from_files``).
"""
from __future__ import annotations

import inspect
import os
import tempfile
from typing import Optional

from crucible.features.code_lockfile_generator import (
    _detect_imports,
    detect_imports_from_sources,
    requirements_text_from_sources,
)
from crucible.features.mode_validation_matrix import analyse_saas_lint_from_files
from crucible.models import CodeBundle, GeneratedFile
from crucible.modules import section_06_runtime_quality_api as _s06

_FLASK_MAIN = (
    "import os\n"
    "import flask\n"
    "from config import Settings\n"
    "app = flask.Flask(__name__)\n"
)
_H001 = "H001-web-framework-undeclared"


def _flask_bundle(with_requirements: Optional[str] = None) -> CodeBundle:
    files = [
        GeneratedFile(path="main.py", content=_FLASK_MAIN),
        GeneratedFile(path="config.py", content="Settings = object\n"),
    ]
    if with_requirements is not None:
        files.append(GeneratedFile(path="requirements.txt", content=with_requirements))
    return CodeBundle(project_type="saas", files=files)


def test_requirements_text_from_sources_flask_app() -> None:
    text = requirements_text_from_sources(
        [("main.py", _FLASK_MAIN), ("config.py", "Settings = object\n")]
    )
    assert "flask==" in text
    assert "config" not in text          # first-party module filtered
    assert "\nos" not in text            # stdlib filtered


def test_detect_imports_from_sources_matches_disk_detector() -> None:
    """In-memory detection must match on-disk _detect_imports exactly."""
    sources = [
        ("main.py", _FLASK_MAIN),
        ("config.py", "Settings = object\n"),
        ("scraper.py", "import requests\n"),
    ]
    with tempfile.TemporaryDirectory() as td:
        code = os.path.join(td, "code")
        os.makedirs(code)
        for name, content in sources:
            with open(os.path.join(code, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        on_disk = _detect_imports(code)
    in_mem = detect_imports_from_sources(sources)
    assert on_disk == in_mem
    assert "flask" in in_mem and "requests" in in_mem
    assert "config" not in in_mem and "scraper" not in in_mem


def test_ensure_bundle_requirements_adds_when_missing() -> None:
    bundle = _flask_bundle()
    assert not any(f.path == "requirements.txt" for f in bundle.files)
    _s06._ensure_bundle_requirements(bundle)
    reqs = [f for f in bundle.files if f.path == "requirements.txt"]
    assert len(reqs) == 1
    assert "flask==" in reqs[0].content
    assert "config" not in reqs[0].content


def test_ensure_bundle_requirements_keeps_existing_real_manifest() -> None:
    bundle = _flask_bundle(with_requirements="flask>=3.0\nrequests>=2.31\n")
    _s06._ensure_bundle_requirements(bundle)
    reqs = [f for f in bundle.files if f.path == "requirements.txt"]
    assert len(reqs) == 1
    assert reqs[0].content == "flask>=3.0\nrequests>=2.31\n"   # untouched


def test_ensure_bundle_requirements_replaces_comment_only_stub() -> None:
    """A comment-only skeleton stub is not a real manifest — it gets synthesised."""
    bundle = _flask_bundle(with_requirements="# add deps here\n")
    _s06._ensure_bundle_requirements(bundle)
    reqs = [f for f in bundle.files if f.path == "requirements.txt"]
    assert len(reqs) == 1
    assert "flask==" in reqs[0].content


def test_ensure_bundle_requirements_backfills_incomplete_manifest() -> None:
    """An INCOMPLETE existing manifest must be backfilled with omitted imports —
    not blindly trusted.  (The app imports starlette directly but the LLM manifest
    only declared fastapi, so the cross-reference lint kept failing the loop.)"""
    bundle = CodeBundle(
        project_type="saas",
        files=[
            GeneratedFile(
                path="app/main.py",
                content="import fastapi\nimport starlette\napp = fastapi.FastAPI()\n",
            ),
            GeneratedFile(path="requirements.txt", content="fastapi==0.111.1\n"),  # omits starlette
        ],
    )
    _s06._ensure_bundle_requirements(bundle)
    req = [f for f in bundle.files if f.path == "requirements.txt"][0].content
    assert "fastapi" in req      # the LLM's declaration is preserved
    assert "starlette" in req    # the omitted import is backfilled
    # and the real review check is now satisfied for starlette
    after = analyse_saas_lint_from_files([(f.path, f.content) for f in bundle.files])
    assert not any(getattr(i, "rule", "") == _H001 and "starlette" in i.description for i in after)


def test_synthesized_requirements_satisfies_review_check() -> None:
    """Producer→consumer: the real cross-reference lint flags flask-undeclared,
    and stops flagging once _ensure_bundle_requirements injects the manifest."""
    bundle = _flask_bundle()
    before = analyse_saas_lint_from_files([(f.path, f.content) for f in bundle.files])
    assert any(getattr(i, "rule", "") == _H001 for i in before), (
        "expected the review to flag flask-undeclared before injection"
    )
    _s06._ensure_bundle_requirements(bundle)
    after = analyse_saas_lint_from_files([(f.path, f.content) for f in bundle.files])
    assert not any(getattr(i, "rule", "") == _H001 for i in after), (
        "review must not flag flask-undeclared after the manifest is synthesised"
    )


def test_quality_loop_wires_ensure_requirements() -> None:
    """§9.6: the loop must call the injector immediately before validation."""
    src = inspect.getsource(_s06.run_quality_loop)
    assert "_ensure_bundle_requirements(current_code)" in src
    after_inject = src.split("_ensure_bundle_requirements(current_code)", 1)[1]
    assert "run_runtime_validation(" in after_inject[:400], (
        "injection must sit immediately before the runtime-validation call"
    )
