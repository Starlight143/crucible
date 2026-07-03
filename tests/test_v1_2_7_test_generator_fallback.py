"""
v1.2.7 — ``--generate-tests`` must never silently produce ZERO tests.

Root cause (`test_generator.py`): when the LLM returned empty/None (or
persistently invalid Python) for a source file, the loop did ``continue`` and
emitted no test — so a single LLM hiccup produced only ``conftest.py`` and
pytest collected 0 tests (observed in the E2E run
``20260703_173342_..._personal_saas_subscription_tracker``).

Fix: a deterministic import-smoke fallback is written whenever the model yields
nothing usable, guaranteeing at least one real, collectable test per module.
"""
from __future__ import annotations

import ast
import os
import tempfile

from crucible.features.test_generator import (
    _generate_fallback_smoke_test,
    _module_import_name,
    generate_tests_for_run,
)


def test_module_import_name_from_relpath() -> None:
    assert _module_import_name("main.py") == "main"
    assert _module_import_name(os.path.join("pkg", "mod.py")) == "pkg.mod"
    assert _module_import_name("pkg/sub/thing.py") == "pkg.sub.thing"


def test_fallback_smoke_test_is_valid_python_and_imports_module() -> None:
    src = _generate_fallback_smoke_test("main.py")
    ast.parse(src)  # must be syntactically valid Python
    assert "importlib.import_module('main')" in src
    assert "def test_main_importable" in src


def _run_with_llm(run_dir: str, source_name: str, source_body: str, llm):
    code = os.path.join(run_dir, "code")
    os.makedirs(code, exist_ok=True)
    with open(os.path.join(code, source_name), "w", encoding="utf-8") as fh:
        fh.write(source_body)
    return generate_tests_for_run(run_dir, llm=llm), code


def test_fallback_when_llm_returns_empty() -> None:
    """Empty LLM output → a real collectable fallback test, not zero tests."""
    with tempfile.TemporaryDirectory() as run_dir:
        report, code = _run_with_llm(run_dir, "main.py", "def add(a, b):\n    return a + b\n", lambda _p: "")
        assert report.success
        assert len(report.test_files) == 1
        test_path = os.path.join(code, "tests", "test_main.py")
        assert os.path.isfile(test_path)
        content = open(test_path, encoding="utf-8").read()
        ast.parse(content)
        assert "importlib.import_module('main')" in content


def test_fallback_when_llm_returns_invalid_python() -> None:
    """Persistently invalid Python (even after the retry) must fall back too."""
    with tempfile.TemporaryDirectory() as run_dir:
        report, code = _run_with_llm(run_dir, "svc.py", "x = 1\n", lambda _p: "def broken( : ???")
        assert report.success
        assert len(report.test_files) == 1
        assert os.path.isfile(os.path.join(code, "tests", "test_svc.py"))


def test_valid_llm_output_used_verbatim_not_fallback() -> None:
    """When the model returns valid Python, it is used as-is (no fallback)."""
    good = "def test_ok():\n    assert 1 + 1 == 2\n"
    with tempfile.TemporaryDirectory() as run_dir:
        report, code = _run_with_llm(run_dir, "m.py", "y = 2\n", lambda _p: good)
        assert report.success and len(report.test_files) == 1
        content = open(os.path.join(code, "tests", "test_m.py"), encoding="utf-8").read()
        assert "def test_ok" in content
        assert "importlib.import_module" not in content  # not the fallback
