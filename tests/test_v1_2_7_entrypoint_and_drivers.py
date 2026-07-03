"""
v1.2.7 (round 2) — "clean container boots" fixes surfaced by a second E2E run
(``20260703_200921_..._team_shared_link_library``):

1. **Dockerfile CMD entrypoint.** The ``COPY code/ ./`` build-context fix exposed
   that the CMD was hardcoded ``main:app``.  A package-structured app
   (``code/app/main.py``) lands at ``/app/app/main.py`` → ``app.main:app``, so
   ``main:app`` crashed with ``ModuleNotFoundError``.  ``_detect_app_target`` now
   derives the real target from where the app object lives.
2. **DB-driver inference.** SQLAlchemy drivers referenced only via
   ``dialect+driver://`` connection strings (e.g. ``postgresql+asyncpg://``) are
   never ``import``ed, so AST detection missed them and a clean ``pip install``
   failed at startup.  ``_add_connection_drivers`` now infers them.
"""
from __future__ import annotations

import os
import tempfile

from crucible.features.code_lockfile_generator import (
    generate_lockfiles,
    requirements_text_from_sources,
    write_requirements_for_code,
)
from crucible.features.deployment_artifacts import (
    _detect_app_target,
    _make_framework_cmd,
    generate_deployment_artifacts,
)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ── CMD entrypoint (_detect_app_target) ──────────────────────────────────────

def test_detect_app_target_package_layout() -> None:
    with tempfile.TemporaryDirectory() as code:
        _write(os.path.join(code, "app", "main.py"), "from fastapi import FastAPI\napp = FastAPI()\n")
        assert _detect_app_target(code) == "app.main:app"


def test_detect_app_target_flat_layout() -> None:
    with tempfile.TemporaryDirectory() as code:
        _write(os.path.join(code, "main.py"), "from fastapi import FastAPI\napp = FastAPI()\n")
        assert _detect_app_target(code) == "main:app"


def test_detect_app_target_flask_var_name() -> None:
    with tempfile.TemporaryDirectory() as code:
        _write(os.path.join(code, "server.py"), "import flask\napp = flask.Flask(__name__)\n")
        assert _detect_app_target(code) == "server:app"


def test_detect_app_target_none_when_no_app() -> None:
    with tempfile.TemporaryDirectory() as code:
        _write(os.path.join(code, "util.py"), "def f():\n    return 1\n")
        assert _detect_app_target(code) is None


def test_make_framework_cmd_uses_app_target() -> None:
    assert '"app.main:app"' in _make_framework_cmd("fastapi", "8000", "app.main:app")
    assert '"app.main:app"' in _make_framework_cmd("flask", "8000", "app.main:app")
    assert '"main:app"' in _make_framework_cmd("fastapi", "8000")  # default preserved


def test_deployment_cmd_matches_package_app_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as run_dir:
        _write(
            os.path.join(run_dir, "code", "app", "main.py"),
            "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/healthz')\ndef h():\n    return {}\n",
        )
        report = generate_deployment_artifacts(run_dir)
        assert report.success
        df = open(os.path.join(run_dir, "deployment", "Dockerfile"), encoding="utf-8").read()
        assert "app.main:app" in df
        assert '"main:app"' not in df  # the wrong hardcoded target is gone


# ── DB-driver inference (_add_connection_drivers) ─────────────────────────────

def test_driver_inference_asyncpg() -> None:
    req = requirements_text_from_sources([("db.py", "URL = 'postgresql+asyncpg://u:p@h/db'\n")])
    assert "asyncpg==" in req


def test_driver_inference_various_dialects() -> None:
    req = requirements_text_from_sources(
        [("db.py", "A='mysql+aiomysql://x'\nB='postgresql+psycopg2://y'\n")]
    )
    assert "aiomysql" in req
    assert "psycopg2-binary" in req  # psycopg2 driver → binary package


def test_driver_inference_ignores_non_db_schemes() -> None:
    req = requirements_text_from_sources(
        [("cfg.py", "DEP = 'git+ssh://github.com/x/y.git'\nX = 'amqp+sentinel://h'\n")]
    )
    deps = [ln for ln in req.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert deps == []  # no phantom driver packages from non-DB schemes


def test_driver_inference_no_duplicate_when_imported() -> None:
    req = requirements_text_from_sources([("db.py", "import asyncpg\nURL='postgresql+asyncpg://z'\n")])
    assert req.count("asyncpg") == 1  # imported + connection-string → listed once


def test_write_requirements_includes_driver_on_disk() -> None:
    with tempfile.TemporaryDirectory() as code:
        _write(
            os.path.join(code, "database.py"),
            "from sqlalchemy.ext.asyncio import create_async_engine\n"
            "engine = create_async_engine('postgresql+asyncpg://u:p@h/db')\n",
        )
        write_requirements_for_code(code)
        req = open(os.path.join(code, "requirements.txt"), encoding="utf-8").read()
        assert "asyncpg==" in req
        assert "sqlalchemy==" in req


def test_generate_lockfiles_matches_write_requirements() -> None:
    """--lockfile-gen (generate_lockfiles) and the section_07 mirror
    (write_requirements_for_code) MUST produce identical requirements — both
    including connection-string drivers — so code/requirements.txt and the
    run-root mirror never diverge (the --lockfile-gen overwrite used to drop the
    driver, leaving code/ missing a runtime dep)."""
    with tempfile.TemporaryDirectory() as run_dir:
        code = os.path.join(run_dir, "code")
        _write(os.path.join(code, "app", "db.py"),
               "import sqlalchemy\nURL = 'postgresql+asyncpg://u:p@h/db'\n")
        _write(os.path.join(code, "app", "main.py"),
               "from fastapi import FastAPI\napp = FastAPI()\n")

        write_req = open(write_requirements_for_code(code), encoding="utf-8").read()
        result = generate_lockfiles(run_dir)          # overwrites code/requirements.txt
        lockfile_req = open(result.requirements_path, encoding="utf-8").read()

        assert write_req == lockfile_req, "the two requirements generators diverged"
        assert "asyncpg==" in lockfile_req


def test_import_to_package_name_mappings() -> None:
    """Import names whose PyPI package differs must map correctly, not ship a
    wrong/uninstallable package name."""
    req = requirements_text_from_sources(
        [("m.py", "import jwt\nimport prometheus_client\nimport pydantic_settings\n")]
    )
    assert "PyJWT" in req and "\njwt " not in req and "\njwt=" not in req
    assert "prometheus-client" in req
    assert "pydantic-settings" in req
