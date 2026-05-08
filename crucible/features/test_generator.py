"""
features/test_generator.py
==========================
LLM-powered pytest test suite generation.

Given a completed run's ``code/`` directory, generates a ``code/tests/``
directory containing one ``test_<module>.py`` file per source file, plus
``conftest.py`` and ``pytest.ini``.

Only Python files are processed.  ``__init__.py`` and files already named
``test_*`` are skipped.  The LLM is called via a duck-typed interface that
supports CrewAI LLM objects, LangChain ChatModel objects, or any plain
callable.

Usage::

    from crucible.features.test_generator import generate_tests_for_run
    report = generate_tests_for_run("/path/to/run_dir", llm)
    print(f"Generated {len(report.test_files)} test file(s)")
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from crucible.output_validation import strip_reasoning_blocks

# ── Public data models ────────────────────────────────────────────────────────

@dataclass
class GeneratedTest:
    source_file: str   # relative to code/
    test_file: str     # relative to run_dir
    content: str


@dataclass
class TestGenerationReport:
    success: bool
    test_files: List[GeneratedTest] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    output_dir: str = ""


# ── AST helpers ───────────────────────────────────────────────────────────────

def _extract_public_api_summary(source_code: str) -> str:
    """
    Return a compact text listing public functions, classes, and their methods.
    Falls back to the first 1500 chars of source when parsing fails.
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return source_code[:1500]

    lines: List[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            args = [a.arg for a in node.args.args if a.arg != "self"]
            prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            lines.append(f"{prefix}def {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            methods: List[str] = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not child.name.startswith("_") or child.name in ("__init__",):
                        methods.append(child.name)
            lines.append(f"class {node.name}: [{', '.join(methods)}]")

    return "\n".join(lines[:60]) if lines else source_code[:1500]


# ── LLM interface ─────────────────────────────────────────────────────────────

def _call_llm(llm: Any, prompt: str) -> Optional[str]:
    """
    Call *llm* with *prompt*.  Supports:
    - CrewAI / LangChain objects with ``.invoke()`` returning an object
      that has a ``.content`` attribute.
    - Objects with a ``.complete()`` method.
    - Plain callables.
    Returns None on any exception or when the response is empty/None.
    """
    try:
        if hasattr(llm, "invoke"):
            response = llm.invoke(prompt)
            if hasattr(response, "content"):
                content = response.content
                # Use `is not None` (not truthiness) so that a falsy-but-valid
                # response (e.g. integer 0) is still returned, while None is
                # dropped.  str(None) == "None" passes ast.parse() and would
                # produce useless test files.
                return (str(content) or None) if content is not None else None
            return (str(response) or None) if response is not None else None
        if hasattr(llm, "complete"):
            result = llm.complete(prompt)
            return (str(result) or None) if result is not None else None
        if callable(llm):
            result = llm(prompt)
            return (str(result) or None) if result is not None else None
    except Exception:
        pass
    return None


def _strip_code_fences(text: str) -> str:
    """Remove markdown ``` fences from LLM output."""
    # Reasoning-model defence: strip <think>/<reasoning>/… blocks first so
    # the chain-of-thought (which often contains its own example fenced
    # blocks) cannot leak into the generated test file body.
    text = strip_reasoning_blocks(text or "").strip()
    # Full fence block
    match = re.match(r"^```(?:python)?\n?(.*?)```\s*$", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Leading/trailing fence lines only
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _is_valid_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _syntax_error_detail(code: str) -> str:
    """Return the SyntaxError message for *code*, or '' if code is valid."""
    try:
        ast.parse(code)
        return ""
    except SyntaxError as exc:
        return str(exc)


# ── Prompt builders ───────────────────────────────────────────────────────────

_TEST_SYSTEM_PROMPT = """\
You are an expert Python test engineer.  Generate a complete, runnable pytest
test suite for the given module.

Rules:
1. Output ONLY raw Python source code — no markdown, no explanation.
2. Start with the necessary imports (pytest, unittest.mock, the module under test).
3. Write at least 3 test functions per public function/class.
4. Cover: happy path, edge cases, and error/exception cases.
5. Use pytest.mark.parametrize where it reduces repetition.
6. Mock all external I/O (network, filesystem, databases, LLM calls) using
   unittest.mock.patch or pytest-mock.
7. Every test must be runnable without network access or live credentials.
8. Do not add any prose or docstrings beyond what is needed for test clarity.
"""


def _build_test_prompt(source_file: str, api_summary: str, source_snippet: str) -> str:
    return (
        f"{_TEST_SYSTEM_PROMPT}\n\n"
        f"File under test: {source_file}\n\n"
        f"Public API:\n{api_summary}\n\n"
        f"Source (first 1500 chars):\n{source_snippet}\n\n"
        f"Generate the pytest test file now:"
    )


def _build_syntax_fix_prompt(broken_code: str) -> str:
    return (
        "The following Python test file has syntax errors.  "
        "Fix ALL syntax errors and return ONLY the corrected Python code:\n\n"
        f"{broken_code}"
    )


# ── Static artifacts ──────────────────────────────────────────────────────────

_CONFTEST_TEMPLATE = '''\
"""pytest configuration and shared fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the generated code root is on sys.path so tests can import modules.
_CODE_ROOT = Path(__file__).parent.parent
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))
'''

_PYTEST_INI_TEMPLATE = """\
[pytest]
testpaths = code/tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
addopts = -v --tb=short
"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_tests_for_run(
    run_dir: str,
    llm: Any,
    *,
    max_files: int = 20,
) -> TestGenerationReport:
    """
    Generate a pytest test suite for all Python source files in *run_dir/code/*.

    Args:
        run_dir:   Path to a completed pipeline run output directory.
        llm:       LLM object used for test generation (see module docstring).
        max_files: Maximum number of source files to generate tests for.

    Returns:
        TestGenerationReport with generated file metadata and any errors.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return TestGenerationReport(
            success=False,
            errors=["No code/ directory found in run output."],
        )

    # Collect Python source files (skip tests and __init__)
    py_files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(code_dir):
        # Skip the tests/ sub-directory and all its descendants.
        # Use Path.parts[0] check instead of startswith("tests") to avoid
        # false positives: "testserver/", "testutils/", etc. all satisfy
        # startswith("tests") but are NOT test directories.
        rel_dir = os.path.relpath(dirpath, code_dir)
        rel_parts = Path(rel_dir).parts
        if rel_parts and rel_parts[0] == "tests":
            dirnames[:] = []  # prune: don't recurse into tests/ subdirectories
            continue
        for fname in sorted(filenames):
            if fname.endswith(".py") and not fname.startswith("test_") and fname != "__init__.py":
                py_files.append(os.path.join(dirpath, fname))

    if not py_files:
        return TestGenerationReport(
            success=False,
            errors=["No Python source files found in code/."],
        )

    tests_dir = os.path.join(code_dir, "tests")
    os.makedirs(tests_dir, exist_ok=True)

    # Write conftest.py (idempotent)
    conftest_path = os.path.join(tests_dir, "conftest.py")
    if not os.path.isfile(conftest_path):
        _tmp_conftest = conftest_path + ".tmp"
        try:
            with open(_tmp_conftest, "w", encoding="utf-8") as fh:
                fh.write(_CONFTEST_TEMPLATE)
            os.replace(_tmp_conftest, conftest_path)
        except OSError:
            try:
                os.unlink(_tmp_conftest)
            except OSError:
                pass

    # Write pytest.ini at run root (idempotent)
    pytest_ini_path = os.path.join(run_dir, "pytest.ini")
    if not os.path.isfile(pytest_ini_path):
        _tmp_ini = pytest_ini_path + ".tmp"
        try:
            with open(_tmp_ini, "w", encoding="utf-8") as fh:
                fh.write(_PYTEST_INI_TEMPLATE)
            os.replace(_tmp_ini, pytest_ini_path)
        except OSError:
            try:
                os.unlink(_tmp_ini)
            except OSError:
                pass

    report = TestGenerationReport(success=True, output_dir=tests_dir)

    for source_path in py_files[:max_files]:
        try:
            with open(source_path, "r", encoding="utf-8", errors="replace") as fh:
                source_content = fh.read()
        except OSError as exc:
            report.errors.append(f"Could not read {source_path}: {exc}")
            continue

        rel_source = os.path.relpath(source_path, code_dir)
        api_summary = _extract_public_api_summary(source_content)
        prompt = _build_test_prompt(rel_source, api_summary, source_content[:1500])

        raw_output = _call_llm(llm, prompt)
        if not raw_output or not raw_output.strip():
            report.errors.append(f"LLM returned empty output for {rel_source}")
            continue

        cleaned = _strip_code_fences(raw_output)

        if not _is_valid_python(cleaned):
            # One retry with explicit fix instruction
            retry_output = _call_llm(llm, _build_syntax_fix_prompt(cleaned))
            if retry_output:
                cleaned = _strip_code_fences(retry_output)
            if not _is_valid_python(cleaned):
                detail = _syntax_error_detail(cleaned)
                report.errors.append(
                    f"Generated test for {rel_source} has persistent syntax errors"
                    + (f" ({detail})" if detail else "")
                    + "; skipped."
                )
                continue

        base_name = os.path.splitext(os.path.basename(source_path))[0]
        test_filename = f"test_{base_name}.py"
        test_path = os.path.join(tests_dir, test_filename)

        _tmp_test = test_path + ".tmp"
        try:
            with open(_tmp_test, "w", encoding="utf-8") as fh:
                fh.write(cleaned)
            os.replace(_tmp_test, test_path)
        except OSError:
            try:
                os.unlink(_tmp_test)
            except OSError:
                pass
            raise

        report.test_files.append(
            GeneratedTest(
                source_file=rel_source,
                test_file=os.path.relpath(test_path, run_dir),
                content=cleaned,
            )
        )

    # success=True when at least one test file was generated
    report.success = len(report.test_files) > 0
    return report
