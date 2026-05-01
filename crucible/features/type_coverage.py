from __future__ import annotations
"""Type annotation coverage analyzer for generated Crucible code.

The feature walks ``run_dir/code`` Python files, computes annotation coverage
from AST nodes, optionally runs mypy, and writes a JSON report suitable for
quality gates.
"""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List

from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
)


def _write_text(path: str, content: str) -> None:
    _tmp = path + ".tmp"
    try:
        with open(_tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(_tmp, path)
    except OSError as exc:
        try:
            os.unlink(_tmp)
        except OSError:
            pass
        raise RuntimeError(f"cannot write {path}: {exc}") from exc


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc


def _iter_py_files(code_dir: str) -> List[str]:
    files: List[str] = []
    try:
        for root, dirnames, filenames in os.walk(code_dir):
            dirnames[:] = [name for name in dirnames if name not in {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", "build", "dist"}]
            for filename in filenames:
                if filename.endswith(".py"):
                    files.append(os.path.join(root, filename))
    except OSError:
        return []
    return sorted(files)


def _function_params(node: ast.AST) -> List[ast.arg]:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    if node.args.vararg is not None:
        args.append(node.args.vararg)
    if node.args.kwarg is not None:
        args.append(node.args.kwarg)
    return [arg for arg in args if arg.arg not in {"self", "cls"}]


def _analyze_file(path: str) -> Dict[str, Any]:
    try:
        tree = ast.parse(_read_text(path), filename=path)
    except (SyntaxError, RuntimeError) as exc:
        return {"path": path, "parse_error": str(exc), "total_params": 0, "annotated_params": 0, "total_returns": 0, "annotated_returns": 0, "annotated_variables": 0, "coverage_pct": 0.0, "below_min": True}
    total_params = 0
    annotated_params = 0
    total_returns = 0
    annotated_returns = 0
    annotated_variables = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = _function_params(node)
            total_params += len(params)
            annotated_params += sum(1 for param in params if param.annotation is not None)
            total_returns += 1
            if node.returns is not None:
                annotated_returns += 1
        elif isinstance(node, ast.AnnAssign):
            annotated_variables += 1
    denominator = max(total_params + total_returns, 1)
    coverage_pct = (annotated_params + annotated_returns) / denominator * 100.0
    return {"path": path, "total_params": total_params, "annotated_params": annotated_params, "total_returns": total_returns, "annotated_returns": annotated_returns, "annotated_variables": annotated_variables, "coverage_pct": round(coverage_pct, 2)}


def _run_mypy(code_dir: str, enabled: bool) -> Dict[str, Any]:
    available = importlib.util.find_spec("mypy") is not None
    if not enabled or not available:
        return {"mypy_available": available, "mypy_errors": 0, "mypy_stdout": "", "mypy_stderr": ""}
    try:
        process = subprocess.run([sys.executable, "-m", "mypy", code_dir], text=True, capture_output=True, timeout=120)
        output = (process.stdout or "") + "\n" + (process.stderr or "")
        error_count = 0 if process.returncode == 0 else max(1, output.count(": error:"))
        return {"mypy_available": True, "mypy_errors": error_count, "mypy_stdout": (process.stdout or "")[-4000:], "mypy_stderr": (process.stderr or "")[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"mypy_available": True, "mypy_errors": 1, "mypy_stdout": "", "mypy_stderr": str(exc)}


@register("type_coverage")
class TypeCoverageFeature(BaseFeature):
    name = "type_coverage"
    label = "Type Annotation Coverage"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        if os.environ.get("TYPE_COVERAGE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        report_path = os.path.join(run_dir, "type_coverage_report.json")
        try:
            code_dir = os.path.join(run_dir, "code")
            try:
                min_pct = float(os.environ.get("TYPE_COVERAGE_MIN_PCT", "50"))
            except ValueError:
                min_pct = 50.0
            files = [_analyze_file(path) for path in _iter_py_files(code_dir)]
            for item in files:
                item["below_min"] = float(item.get("coverage_pct", 0.0)) < min_pct
            aggregate = round(sum(float(item.get("coverage_pct", 0.0)) for item in files) / max(len(files), 1), 2)
            mypy_result = _run_mypy(code_dir, os.environ.get("TYPE_COVERAGE_RUN_MYPY", "1").strip().lower() not in ("0", "false", "no", "off"))
            report = {
                "total_files": len(files),
                "aggregate_coverage_pct": aggregate,
                "mypy_available": mypy_result["mypy_available"],
                "mypy_errors": mypy_result["mypy_errors"],
                "minimum_coverage_pct": min_pct,
                "below_minimum": aggregate < min_pct,
                "files": files,
            }
            if mypy_result.get("mypy_stdout") or mypy_result.get("mypy_stderr"):
                report["mypy_output"] = {"stdout": mypy_result.get("mypy_stdout", ""), "stderr": mypy_result.get("mypy_stderr", "")}
            _write_json(report_path, report)
            success = aggregate >= min_pct and int(mypy_result["mypy_errors"]) == 0
            return FeatureResult(feature=self.name, success=success, summary="Type coverage analyzed", details={**report, "report_path": report_path}, error=None if success else "type coverage below threshold or mypy errors present", duration_seconds=time.monotonic() - start)
        except Exception as exc:
            return FeatureResult(feature=self.name, success=False, summary="Type coverage analysis failed", error=str(exc), duration_seconds=time.monotonic() - start)
