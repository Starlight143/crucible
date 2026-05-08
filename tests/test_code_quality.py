# ruff: noqa: E402
"""Tests for crucible.features.code_quality."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ast

from crucible.features.code_quality import (
    CodeQualityReport,
    FileMetric,
    FunctionMetric,
    _count_complexity,
    _max_nesting,
    analyse_code_quality,
)


class TestFunctionMetric(unittest.TestCase):
    def test_default_values(self) -> None:
        m = FunctionMetric(
            name="foo", file="main.py", line=1,
            complexity=3, line_count=10, param_count=2,
        )
        self.assertFalse(m.is_async)


class TestFileMetric(unittest.TestCase):
    def test_default_values(self) -> None:
        m = FileMetric(
            file="main.py", total_lines=100, code_lines=80,
            blank_lines=10, comment_lines=10,
            function_count=5, class_count=1,
        )
        self.assertEqual(m.max_complexity, 0)
        self.assertEqual(m.avg_complexity, 0.0)
        self.assertEqual(m.functions, [])
        self.assertEqual(m.warnings, [])


class TestCodeQualityReport(unittest.TestCase):
    def test_to_dict(self) -> None:
        r = CodeQualityReport(
            success=True, total_files=3, total_code_lines=150,
            total_functions=10, total_classes=2,
            max_complexity=8, avg_complexity=3.5,
        )
        d = r.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["total_files"], 3)
        self.assertEqual(d["avg_complexity"], 3.5)

    def test_summary_text(self) -> None:
        r = CodeQualityReport(
            success=True, total_files=5, total_code_lines=300,
            total_functions=20, total_classes=4,
            max_complexity=12, avg_complexity=4.2,
            high_complexity_functions=2,
            warnings=["warning1"],
        )
        text = r.summary_text()
        self.assertIn("Code Quality Report", text)
        self.assertIn("Files: 5", text)
        self.assertIn("High complexity (>10): 2", text)
        self.assertIn("warning1", text)

    def test_to_dict_filters_low_complexity_functions(self) -> None:
        fm = FileMetric(
            file="x.py", total_lines=10, code_lines=8,
            blank_lines=1, comment_lines=1,
            function_count=2, class_count=0,
            functions=[
                FunctionMetric(name="simple", file="x.py", line=1,
                               complexity=2, line_count=5, param_count=1),
                FunctionMetric(name="complex", file="x.py", line=10,
                               complexity=8, line_count=20, param_count=3),
            ],
        )
        r = CodeQualityReport(success=True, files=[fm])
        d = r.to_dict()
        # Only functions with complexity > 5 are included
        file_data = d["files"][0]
        self.assertEqual(len(file_data["functions"]), 1)
        self.assertEqual(file_data["functions"][0]["name"], "complex")


class TestCountComplexity(unittest.TestCase):
    def _complexity_of(self, code: str) -> int:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return _count_complexity(node)
        raise AssertionError("No function found in code")

    def test_simple_function(self) -> None:
        code = "def foo():\n    return 1\n"
        self.assertEqual(self._complexity_of(code), 1)

    def test_if_else(self) -> None:
        code = "def foo(x):\n    if x > 0:\n        return 1\n    return 0\n"
        self.assertEqual(self._complexity_of(code), 2)

    def test_for_loop(self) -> None:
        code = "def foo(items):\n    for i in items:\n        pass\n"
        self.assertEqual(self._complexity_of(code), 2)

    def test_boolean_ops(self) -> None:
        code = "def foo(a, b, c):\n    if a and b and c:\n        pass\n"
        # 1 (base) + 1 (if) + 2 (two extra 'and' values)
        self.assertEqual(self._complexity_of(code), 4)

    def test_except_handler(self) -> None:
        code = "def foo():\n    try:\n        pass\n    except ValueError:\n        pass\n"
        self.assertEqual(self._complexity_of(code), 2)

    def test_list_comprehension(self) -> None:
        code = "def foo(items):\n    return [x for x in items]\n"
        self.assertEqual(self._complexity_of(code), 2)

    def test_nested_function_bodies_excluded(self) -> None:
        # Outer has no decision points of its own; inner's if/for must NOT
        # inflate outer's complexity count.
        code = (
            "def outer(items):\n"
            "    def inner(x):\n"
            "        if x > 0:\n"
            "            return x\n"
            "        for i in range(x):\n"
            "            pass\n"
            "    return inner\n"
        )
        self.assertEqual(self._complexity_of(code), 1)

    def test_outer_own_branch_plus_nested(self) -> None:
        # Outer has one if-branch; inner's for-loop must NOT be added to outer.
        code = (
            "def outer(x):\n"
            "    if x > 0:\n"
            "        def inner(y):\n"
            "            for i in range(y):\n"
            "                pass\n"
            "    return x\n"
        )
        self.assertEqual(self._complexity_of(code), 2)


class TestMaxNesting(unittest.TestCase):
    def _nesting_of(self, code: str) -> int:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return _max_nesting(node)
        raise AssertionError("No function found")

    def test_no_nesting(self) -> None:
        code = "def foo():\n    return 1\n"
        self.assertEqual(self._nesting_of(code), 0)

    def test_single_if(self) -> None:
        code = "def foo(x):\n    if x:\n        pass\n"
        self.assertEqual(self._nesting_of(code), 1)

    def test_nested_if_for(self) -> None:
        code = (
            "def foo(items):\n"
            "    for i in items:\n"
            "        if i > 0:\n"
            "            pass\n"
        )
        self.assertEqual(self._nesting_of(code), 2)

    def test_nested_function_nesting_excluded(self) -> None:
        # outer has no control structures; inner's for/if must NOT count.
        code = (
            "def outer():\n"
            "    def inner():\n"
            "        for x in range(10):\n"
            "            if x:\n"
            "                pass\n"
        )
        self.assertEqual(self._nesting_of(code), 0)


class TestAnalyseCodeQuality(unittest.TestCase):
    def test_no_code_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = analyse_code_quality(td)
            self.assertFalse(report.success)
            self.assertTrue(report.errors, "expected at least one error message")
            self.assertIn("No code/", report.errors[0])

    def test_no_python_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            report = analyse_code_quality(td)
            self.assertTrue(report.success)
            # "no files" is a warning (benign skip), not an error
            self.assertTrue(report.warnings, "expected at least one warning message")
            self.assertIn("No Python files", report.warnings[0])
            self.assertEqual(report.errors, [])

    def test_analyses_python_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write(
                    "class App:\n"
                    "    def run(self, items):\n"
                    "        for item in items:\n"
                    "            if item > 0:\n"
                    "                print(item)\n"
                    "\n"
                    "# comment line\n"
                )
            report = analyse_code_quality(td)
            self.assertTrue(report.success)
            self.assertEqual(report.total_files, 1)
            self.assertEqual(report.total_classes, 1)
            self.assertEqual(report.total_functions, 1)
            self.assertGreater(report.total_code_lines, 0)

    def test_persists_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "x.py"), "w") as f:
                f.write("x = 1\n")
            analyse_code_quality(td)
            report_path = os.path.join(td, "code_quality_report.json")
            self.assertTrue(os.path.isfile(report_path))
            with open(report_path) as f:
                data = json.load(f)
            self.assertTrue(data["success"])

    def test_syntax_error_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "bad.py"), "w") as f:
                f.write("def foo(:\n    pass\n")
            report = analyse_code_quality(td)
            self.assertTrue(report.success)
            self.assertEqual(report.total_files, 1)
            self.assertEqual(report.total_functions, 0)

    def test_high_complexity_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            # Generate a function with high complexity
            lines = ["def complex_func(x):"]
            for i in range(12):
                lines.append(f"    if x == {i}:")
                lines.append(f"        return {i}")
            lines.append("    return -1")
            with open(os.path.join(code_dir, "complex.py"), "w") as f:
                f.write("\n".join(lines) + "\n")
            report = analyse_code_quality(td)
            self.assertGreater(report.high_complexity_functions, 0)
            self.assertTrue(any("complexity=" in w for w in report.warnings))

    def test_ignores_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            cache_dir = os.path.join(code_dir, "__pycache__")
            os.makedirs(cache_dir)
            with open(os.path.join(cache_dir, "cached.py"), "w") as f:
                f.write("x = 1\n")
            with open(os.path.join(code_dir, "main.py"), "w") as f:
                f.write("y = 2\n")
            report = analyse_code_quality(td)
            self.assertEqual(report.total_files, 1)


class TestParamCountComprehensive(unittest.TestCase):
    """
    Regression: param_count only counted len(node.args.args),
    which is positional/positional-or-keyword arguments only.  Functions
    that express complexity via *args, keyword-only, or **kwargs parameters
    were under-counted, producing false-negative param threshold warnings.

    Fix: count all parameter kinds:
        len(args.args) + len(args.kwonlyargs) + bool(vararg) + bool(kwarg)
    """

    def _get_param_count(self, source: str) -> int:
        """Parse *source*, run analyse_code_quality, return param_count of first fn."""
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            with open(os.path.join(code_dir, "m.py"), "w") as f:
                f.write(source)
            report = analyse_code_quality(td)
            self.assertTrue(report.files, "Expected at least one file metric")
            self.assertTrue(report.files[0].functions, "Expected at least one function metric")
            return report.files[0].functions[0].param_count

    def test_positional_only_counted(self) -> None:
        count = self._get_param_count("def f(a, b, c): pass\n")
        self.assertEqual(count, 3)

    def test_vararg_counted(self) -> None:
        """*args must add 1 to param_count."""
        count = self._get_param_count("def f(a, *args): pass\n")
        self.assertEqual(count, 2, "a + *args = 2")

    def test_kwarg_counted(self) -> None:
        """**kwargs must add 1 to param_count."""
        count = self._get_param_count("def f(a, **kwargs): pass\n")
        self.assertEqual(count, 2, "a + **kwargs = 2")

    def test_kwonly_counted(self) -> None:
        """Keyword-only args (after *) must be counted."""
        count = self._get_param_count("def f(a, *, b, c): pass\n")
        self.assertEqual(count, 3, "a + b + c = 3")

    def test_all_param_kinds(self) -> None:
        """Comprehensive count: positional + *args + keyword-only + **kwargs."""
        # def f(a, b, *args, c=1, d=2, **kwargs)
        # positional=2, vararg=1, kwonly=2, kwarg=1 → total=6
        count = self._get_param_count(
            "def f(a, b, *args, c=1, d=2, **kwargs): pass\n"
        )
        self.assertEqual(count, 6, (
            f"Expected 6 (2 positional + *args + 2 keyword-only + **kwargs), got {count}"
        ))


if __name__ == "__main__":
    unittest.main()
