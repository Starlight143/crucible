from __future__ import annotations
"""Docker sandbox executor for generated Crucible code.

The feature executes ``run_dir/code`` with strict time, network, CPU, and memory
constraints when Docker is available, and falls back to a local subprocess with
best-effort Unix resource limits otherwise.
"""

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

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


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(int(os.environ.get(name, str(default))), minimum)
    except ValueError:
        return default


def _docker_available() -> bool:
    try:
        process = subprocess.run(["docker", "info"], timeout=5, capture_output=True, text=True)
        return process.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _command_for_code(code_dir: str) -> List[str]:
    if os.path.isfile(os.path.join(code_dir, "main.py")):
        return ["python", "main.py", "--dry-run"]
    return ["python", "-c", "import main"]


def _unix_preexec(memory_mb: int, timeout_seconds: int) -> Optional[Any]:
    if os.name == "nt":
        return None

    def apply_limits() -> None:
        try:
            import resource
            # RLIMIT_AS limits total virtual address space (including shared libs /
            # memory-mapped files).  Python's fork footprint alone can exceed 512 MB
            # on systems with large stdlib / site-packages, killing the child process
            # before any user code runs.  Use only RLIMIT_CPU (wall-clock-safe CPU
            # time limit) which is safe for sandboxing without false positives.
            resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
        except (ImportError, OSError, ValueError):
            return

    return apply_limits


def _run_process(command: List[str], cwd: str, timeout_seconds: int, memory_mb: int) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            preexec_fn=_unix_preexec(memory_mb, timeout_seconds),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stderr = process.stderr or ""
        return {"exit_code": process.returncode, "stdout_snippet": (process.stdout or "")[:2000], "stderr_snippet": stderr[:2000], "execution_ms": elapsed_ms, "oom_killed": process.returncode in (137, -9) or "out of memory" in stderr.lower()}
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        return {"exit_code": 124, "stdout_snippet": stdout[:2000], "stderr_snippet": "execution timed out", "execution_ms": elapsed_ms, "oom_killed": False}
    except OSError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {"exit_code": 127, "stdout_snippet": "", "stderr_snippet": str(exc), "execution_ms": elapsed_ms, "oom_killed": False}


@register("sandbox_executor")
class SandboxExecutorFeature(BaseFeature):
    name = "sandbox_executor"
    label = "Docker Sandbox Executor"
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        start = time.monotonic()
        if os.environ.get("SANDBOX_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            return FeatureResult(feature=self.name, success=True, summary="disabled", skipped=True, skip_reason="disabled")
        report_path = os.path.join(run_dir, "sandbox_execution_report.json")
        try:
            code_dir = os.path.join(run_dir, "code")
            memory_mb = _int_env("SANDBOX_MEMORY_MB", 512)
            timeout_seconds = _int_env("SANDBOX_TIMEOUT_SECONDS", 30)
            docker_ok = _docker_available()
            require_docker = os.environ.get("SANDBOX_REQUIRE_DOCKER", "0").strip().lower() in ("1", "true", "yes", "on")
            if not os.path.isdir(code_dir):
                report = {"docker_available": docker_ok, "sandboxed": False, "exit_code": 2, "stdout_snippet": "", "stderr_snippet": f"code directory not found: {code_dir}", "execution_ms": 0, "oom_killed": False}
                _write_json(report_path, report)
                return FeatureResult(feature=self.name, success=False, summary="Code directory not found", details=report, error=report["stderr_snippet"], duration_seconds=time.monotonic() - start)
            sandboxed = False  # set True only on the docker path
            if docker_ok:
                image = os.environ.get("SANDBOX_DOCKER_IMAGE", "python:3.11-slim")
                command = [
                    "docker",
                    "run",
                    f"--memory={memory_mb}m",
                    "--cpus=1",
                    "--network=none",
                    "--rm",
                    "-v",
                    f"{os.path.abspath(code_dir)}:/app:ro",
                    "--workdir",
                    "/app",
                    image,
                    *_command_for_code(code_dir),
                ]
                result = _run_process(command, run_dir, timeout_seconds, memory_mb)
                sandboxed = True
            elif require_docker:
                result = {"exit_code": 125, "stdout_snippet": "", "stderr_snippet": "Docker is required but unavailable", "execution_ms": 0, "oom_killed": False}
            else:
                # Unsandboxed fallback: only a subprocess timeout.  On Windows
                # there is NO RLIMIT_CPU, NO memory cap, NO network isolation
                # (see _unix_preexec at line 64-65 — returns None on os.name=='nt').
                # Surface this clearly in the report so downstream consumers (and
                # the operator reading sandbox_execution_report.json) can tell
                # the generated code did NOT actually run inside an isolation
                # boundary.  Set SANDBOX_REQUIRE_DOCKER=1 to opt into fail-closed
                # behaviour when docker is unavailable.
                if os.name == "nt":
                    print(
                        "[sandbox_executor] WARNING: docker unavailable on Windows; "
                        "running generated code WITHOUT isolation (no RLIMIT_CPU, "
                        "no memory cap, no network block).  Set "
                        "SANDBOX_REQUIRE_DOCKER=1 to refuse this fallback.",
                        file=sys.stderr,
                        flush=True,
                    )
                command = [sys.executable, *(_command_for_code(code_dir)[1:])]
                result = _run_process(command, code_dir, timeout_seconds, memory_mb)
            report = {"docker_available": docker_ok, "sandboxed": sandboxed, **result}
            _write_json(report_path, report)
            success = int(report["exit_code"]) == 0
            return FeatureResult(feature=self.name, success=success, summary="Sandbox execution completed" if success else "Sandbox execution failed", details={**report, "report_path": report_path}, error=None if success else str(report["stderr_snippet"]), duration_seconds=time.monotonic() - start)
        except Exception as exc:
            report = {"docker_available": False, "sandboxed": False, "exit_code": 1, "stdout_snippet": "", "stderr_snippet": str(exc), "execution_ms": int((time.monotonic() - start) * 1000), "oom_killed": False}
            try:
                _write_json(report_path, report)
            except Exception:
                pass
            return FeatureResult(feature=self.name, success=False, summary="Sandbox execution failed", details=report, error=str(exc), duration_seconds=time.monotonic() - start)
