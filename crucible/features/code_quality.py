"""
features/code_quality.py
=========================
Code quality metrics for generated code.

Computes per-file and aggregate metrics without external dependencies:

- **Lines of Code** (LOC): total, blank, comment, code
- **Cyclomatic Complexity** (AST-based): per-function McCabe complexity
- **Maintainability heuristics**: functions too long (>50 lines), deeply
  nested (>4 levels), too many parameters (>7), files too large (>500 LOC)

Usage::

    from crucible.features.code_quality import analyse_code_quality
    report = analyse_code_quality("/path/to/run_dir")
    print(report.summary_text())
"""
from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class FunctionMetric:
    """Metrics for a single function/method."""
    name: str
    file: str           # relative to code/
    line: int
    complexity: int     # cyclomatic complexity (McCabe)
    line_count: int     # lines in function body
    param_count: int
    is_async: bool = False


@dataclass
class FileMetric:
    """Metrics for a single Python file."""
    file: str           # relative to code/
    total_lines: int
    code_lines: int
    blank_lines: int
    comment_lines: int
    function_count: int
    class_count: int
    max_complexity: int = 0
    avg_complexity: float = 0.0
    functions: List[FunctionMetric] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class CodeQualityReport:
    success: bool
    total_files: int = 0
    total_code_lines: int = 0
    total_functions: int = 0
    total_classes: int = 0
    max_complexity: int = 0
    avg_complexity: float = 0.0
    high_complexity_functions: int = 0     # complexity > 10
    files: List[FileMetric] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "total_files": self.total_files,
            "total_code_lines": self.total_code_lines,
            "total_functions": self.total_functions,
            "total_classes": self.total_classes,
            "max_complexity": self.max_complexity,
            "avg_complexity": round(self.avg_complexity, 2),
            "high_complexity_functions": self.high_complexity_functions,
            "warnings": self.warnings,
            "errors": self.errors,
            "files": [
                {
                    "file": f.file,
                    "total_lines": f.total_lines,
                    "code_lines": f.code_lines,
                    "blank_lines": f.blank_lines,
                    "comment_lines": f.comment_lines,
                    "function_count": f.function_count,
                    "class_count": f.class_count,
                    "max_complexity": f.max_complexity,
                    "avg_complexity": round(f.avg_complexity, 2),
                    "warnings": f.warnings,
                    "functions": [
                        {
                            "name": fn.name,
                            "line": fn.line,
                            "complexity": fn.complexity,
                            "line_count": fn.line_count,
                            "param_count": fn.param_count,
                        }
                        for fn in f.functions
                        if fn.complexity > 5
                    ],
                }
                for f in self.files
            ],
        }

    def summary_text(self) -> str:
        lines = [
            "Code Quality Report",
            f"  Files: {self.total_files}",
            f"  Code lines: {self.total_code_lines}",
            f"  Functions: {self.total_functions} | Classes: {self.total_classes}",
            f"  Complexity: avg={self.avg_complexity:.1f}, max={self.max_complexity}",
            f"  High complexity (>10): {self.high_complexity_functions}",
        ]
        if self.warnings:
            lines.append(f"\nWarnings ({len(self.warnings)}):")
            for w in self.warnings[:15]:
                lines.append(f"  ! {w}")
        return "\n".join(lines)


# ── AST-based complexity ─────────────────────────────────────────────────────

_IGNORED_DIRS: Set[str] = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    ".tox", "dist", "build", ".eggs",
}

# Threshold constants
_MAX_FUNCTION_LINES = 50
_MAX_NESTING_DEPTH = 4
_MAX_PARAMS = 7
_MAX_FILE_LOC = 500
_HIGH_COMPLEXITY = 10


def _count_complexity(node: ast.AST) -> int:
    """
    Compute McCabe cyclomatic complexity for a function/method AST node.

    Complexity = 1 + (number of decision points).
    Decision points: if, elif, for, while, and, or, except, with,
    assert, ternary (IfExp), comprehension.

    Nested function and class bodies are intentionally excluded — each nested
    callable is an independent complexity unit.  Using ast.walk() would
    traverse into them, inflating the outer function's count.
    """
    complexity = 1
    # Use an explicit stack instead of ast.walk() so we can stop descending
    # into nested function/class definitions without visiting their bodies.
    stack: list = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        # Nested functions/classes are independent units — skip their bodies.
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, (ast.If, ast.IfExp)):
            complexity += 1
        elif isinstance(child, (ast.For, ast.AsyncFor)):
            complexity += 1
        elif isinstance(child, (ast.While,)):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # Each additional 'and'/'or' is a decision point
            complexity += len(child.values) - 1
        elif isinstance(child, ast.ExceptHandler):
            complexity += 1
        elif isinstance(child, (ast.With, ast.AsyncWith)):
            complexity += 1
        elif isinstance(child, ast.Assert):
            complexity += 1
        elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            complexity += sum(1 for _ in child.generators)
        stack.extend(ast.iter_child_nodes(child))
    return complexity


def _max_nesting(node: ast.AST, depth: int = 0) -> int:
    """Compute max nesting depth of control structures, excluding nested function bodies."""
    max_d = depth
    for child in ast.iter_child_nodes(node):
        # Nested functions/classes are independent units — do not descend.
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, (ast.If, ast.For, ast.While, ast.AsyncFor,
                              ast.With, ast.AsyncWith, ast.Try)):
            max_d = max(max_d, _max_nesting(child, depth + 1))
        else:
            max_d = max(max_d, _max_nesting(child, depth))
    return max_d


def _analyse_file(code_dir: str, rel_path: str) -> Optional[FileMetric]:
    """Analyse one Python file and return metrics."""
    fpath = os.path.join(code_dir, rel_path)
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
            source_lines = fh.readlines()
    except OSError:
        return None

    total = len(source_lines)
    blank = sum(1 for line in source_lines if not line.strip())
    comment = sum(1 for line in source_lines if line.strip().startswith("#"))
    code = total - blank - comment

    warnings: List[str] = []

    if code > _MAX_FILE_LOC:
        warnings.append(f"{rel_path}: {code} code lines (>{_MAX_FILE_LOC})")

    # Parse AST
    source = "".join(source_lines)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FileMetric(
            file=rel_path,
            total_lines=total,
            code_lines=code,
            blank_lines=blank,
            comment_lines=comment,
            function_count=0,
            class_count=0,
            warnings=warnings,
        )

    functions: List[FunctionMetric] = []
    class_count = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_count += 1

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cplx = _count_complexity(node)
            end_line = getattr(node, "end_lineno", node.lineno)
            line_count = (end_line or node.lineno) - node.lineno + 1
            # Count all parameter kinds so the threshold catches functions that
            # spread complexity across *args, keyword-only, and **kwargs params.
            _a = node.args
            params = (
                len(_a.args)           # positional / positional-or-keyword
                + len(_a.kwonlyargs)   # keyword-only (after *)
                + bool(_a.vararg)      # *args
                + bool(_a.kwarg)       # **kwargs
            )
            is_async = isinstance(node, ast.AsyncFunctionDef)

            fn = FunctionMetric(
                name=node.name,
                file=rel_path,
                line=node.lineno,
                complexity=cplx,
                line_count=line_count,
                param_count=params,
                is_async=is_async,
            )
            functions.append(fn)

            # Check thresholds
            if cplx > _HIGH_COMPLEXITY:
                warnings.append(
                    f"{rel_path}:{node.lineno} {node.name}() complexity={cplx} (>{_HIGH_COMPLEXITY})"
                )
            if line_count > _MAX_FUNCTION_LINES:
                warnings.append(
                    f"{rel_path}:{node.lineno} {node.name}() {line_count} lines (>{_MAX_FUNCTION_LINES})"
                )
            if params > _MAX_PARAMS:
                warnings.append(
                    f"{rel_path}:{node.lineno} {node.name}() {params} params (>{_MAX_PARAMS})"
                )

            nesting = _max_nesting(node)
            if nesting > _MAX_NESTING_DEPTH:
                warnings.append(
                    f"{rel_path}:{node.lineno} {node.name}() nesting depth={nesting} (>{_MAX_NESTING_DEPTH})"
                )

    complexities = [f.complexity for f in functions]
    max_cplx = max(complexities) if complexities else 0
    avg_cplx = sum(complexities) / len(complexities) if complexities else 0.0

    return FileMetric(
        file=rel_path,
        total_lines=total,
        code_lines=code,
        blank_lines=blank,
        comment_lines=comment,
        function_count=len(functions),
        class_count=class_count,
        max_complexity=max_cplx,
        avg_complexity=avg_cplx,
        functions=functions,
        warnings=warnings,
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def analyse_code_quality(run_dir: str) -> CodeQualityReport:
    """
    Analyse code quality for all Python files in *run_dir/code/*.

    Produces per-file and aggregate metrics.  Results are saved to
    ``{run_dir}/code_quality_report.json``.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return CodeQualityReport(
            success=False,
            errors=["No code/ directory found in run output."],
        )

    # Collect Python files
    py_files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(code_dir):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, fname), code_dir)
                py_files.append(rel)

    if not py_files:
        return CodeQualityReport(
            success=True,
            warnings=["No Python files found to analyse."],
        )

    file_metrics: List[FileMetric] = []
    all_warnings: List[str] = []
    errors: List[str] = []

    for rel_file in py_files:
        fm = _analyse_file(code_dir, rel_file)
        if fm is None:
            errors.append(f"Could not read {rel_file}")
            continue
        file_metrics.append(fm)
        all_warnings.extend(fm.warnings)

    # Aggregate
    total_code_lines = sum(f.code_lines for f in file_metrics)
    total_functions = sum(f.function_count for f in file_metrics)
    total_classes = sum(f.class_count for f in file_metrics)
    all_complexities = [
        fn.complexity
        for f in file_metrics
        for fn in f.functions
    ]
    max_complexity = max(all_complexities) if all_complexities else 0
    avg_complexity = (
        sum(all_complexities) / len(all_complexities) if all_complexities else 0.0
    )
    high_count = sum(1 for c in all_complexities if c > _HIGH_COMPLEXITY)

    report = CodeQualityReport(
        success=True,
        total_files=len(file_metrics),
        total_code_lines=total_code_lines,
        total_functions=total_functions,
        total_classes=total_classes,
        max_complexity=max_complexity,
        avg_complexity=avg_complexity,
        high_complexity_functions=high_count,
        files=file_metrics,
        warnings=all_warnings,
        errors=errors,
    )

    # Persist (atomic write)
    report_path = os.path.join(run_dir, "code_quality_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(_tmp_path, report_path)
    except OSError:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass

    return report
