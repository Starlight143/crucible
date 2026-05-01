"""
features/independent_validator.py
=================================
Independent validation agent for completed pipeline runs.

Runs two validation phases on the generated code WITHOUT relying on the
generation crew's perspective:

**Phase B — Subprocess Execution Validation**

  1. **Syntax check** (``py_compile``) on every ``.py`` file in ``code/``.
  2. **pytest execution** if ``code/tests/`` exists and contains test files.
  3. **Smoke check** (``python main.py --help``) if ``main.py`` exists.

**Phase A — LLM Adversarial Code Review**

  Sends the generated source + ``analysis_result.json`` claims to an LLM
  with a critical "devil's advocate" persona.  The LLM independently reviews
  the code for correctness, security, design, and requirements conformance.
  Produces structured JSON findings.

Both phases are optional and independent — Phase B runs without LLM,
Phase A requires LLM.  Each phase's failure does not block the other.

Usage::

    from crucible.features.independent_validator import validate_run
    report = validate_run("/path/to/run_dir", llm=my_llm)
    print(report.summary_text())
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Keys that must never be forwarded to LLM-generated subprocess code.
# Mirror of backtest_runner._SENSITIVE_ENV_KEY_PATTERNS.
_SENSITIVE_ENV_KEY_PATTERNS = (
    "API_KEY", "API_SECRET", "SECRET_KEY", "TOKEN", "PASSWORD",
    "CREDENTIAL", "OPENROUTER", "OPENAI_API", "ANTHROPIC_API",
    "ALIBABA_", "AWS_SECRET", "TELEGRAM_",
)


def _make_safe_env(code_dir: str) -> Dict[str, str]:
    """
    Build a sanitised environment for an LLM-generated subprocess.

    Strips keys matching ``_SENSITIVE_ENV_KEY_PATTERNS`` to prevent credential
    leakage, and strips any inherited PYTHONPATH entirely.

    Inheriting the caller's PYTHONPATH is dangerous: if the user has other
    projects on their PYTHONPATH that contain same-named packages (e.g. ``src``),
    Python's namespace-package scan can prefer a *regular* package (one with
    ``__init__.py``) found later on PYTHONPATH over a *namespace* package found
    first in the script's own directory — causing wrong-module imports inside
    the generated code.  Setting PYTHONPATH to *only* ``code_dir`` eliminates
    this class of false-positive import failures.
    """
    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        upper = k.upper()
        if any(pat in upper for pat in _SENSITIVE_ENV_KEY_PATTERNS):
            continue
        if upper == "PYTHONPATH":
            # Dropped intentionally — we set a clean value below.
            continue
        env[k] = v
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Only the generated code directory should be on the path.
    env["PYTHONPATH"] = code_dir
    return env


# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class FileCheckResult:
    """Result of a per-file check (e.g. syntax)."""
    file: str            # relative to code/
    passed: bool
    error: str = ""


@dataclass
class ExecutionPhaseResult:
    """Result of one subprocess-based validation phase."""
    phase: str           # "syntax_check" | "pytest" | "smoke_check"
    passed: bool
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    file_results: List[FileCheckResult] = field(default_factory=list)


@dataclass
class AdversarialFinding:
    """One issue discovered during adversarial LLM review."""
    severity: str        # "critical" | "high" | "medium" | "low"
    category: str        # "logic_error" | "security" | "requirements_mismatch" | "design" | "correctness" | "missing_error_handling"
    file: str
    description: str
    line: Optional[int] = None


@dataclass
class IndependentValidationReport:
    """Full validation report combining both phases."""
    success: bool
    overall_verdict: str = "unknown"    # "pass" | "fail" | "warning"
    execution_phases: List[ExecutionPhaseResult] = field(default_factory=list)
    adversarial_findings: List[AdversarialFinding] = field(default_factory=list)
    adversarial_summary: str = ""
    adversarial_verdict: str = "unknown"  # top-level verdict returned by LLM reviewer
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "overall_verdict": self.overall_verdict,
            "adversarial_verdict": self.adversarial_verdict,
            "execution_phases": [
                {
                    "phase": p.phase,
                    "passed": p.passed,
                    "return_code": p.return_code,
                    "timed_out": p.timed_out,
                    "stdout": p.stdout[:2000],
                    "stderr": p.stderr[:2000],
                    "file_results": [
                        {"file": f.file, "passed": f.passed, "error": f.error}
                        for f in p.file_results
                    ],
                }
                for p in self.execution_phases
            ],
            "adversarial_findings": [
                {
                    "severity": f.severity,
                    "category": f.category,
                    "file": f.file,
                    "line": f.line,
                    "description": f.description,
                }
                for f in self.adversarial_findings
            ],
            "adversarial_summary": self.adversarial_summary,
            "errors": self.errors,
        }

    def summary_text(self) -> str:
        lines = [
            "Independent Validation Report",
            f"  Verdict: {self.overall_verdict.upper()}",
            "",
        ]

        for phase in self.execution_phases:
            if phase.timed_out:
                status = "TIMEOUT"
            elif phase.passed:
                status = "PASS"
            else:
                status = "FAIL"
            lines.append(f"  [{status:7s}] {phase.phase}")

            if phase.file_results:
                failed = [f for f in phase.file_results if not f.passed]
                passed_count = len(phase.file_results) - len(failed)
                lines.append(
                    f"           {passed_count} passed, {len(failed)} failed"
                )
                for fr in failed[:5]:
                    lines.append(f"           \u2717 {fr.file}: {fr.error[:80]}")
            elif not phase.passed and phase.stderr:
                for err_line in phase.stderr.strip().splitlines()[:3]:
                    lines.append(f"           {err_line[:100]}")

        if self.adversarial_findings:
            lines.append(
                f"\nAdversarial Review ({len(self.adversarial_findings)} finding(s)):"
            )
            for finding in self.adversarial_findings[:10]:
                sev = finding.severity.upper()
                loc = finding.file
                if finding.line:
                    loc += f":{finding.line}"
                lines.append(f"  [{sev:8s}] {loc}")
                lines.append(f"             {finding.description[:120]}")

        if self.adversarial_summary:
            lines.append(f"\nReview Summary: {self.adversarial_summary[:300]}")

        if self.errors:
            lines.append("\nErrors:")
            for err in self.errors:
                lines.append(f"  ! {err}")

        return "\n".join(lines)


# ── LLM interface ────────────────────────────────────────────────────────────

def _call_llm(llm: Any, prompt: str) -> Optional[str]:
    """
    Call *llm* with *prompt*.  Supports:
    - CrewAI / LangChain objects with ``.invoke()`` returning ``.content``.
    - Objects with a ``.complete()`` method.
    - Plain callables.
    Returns None on exception or empty response.
    """
    try:
        if hasattr(llm, "invoke"):
            response = llm.invoke(prompt)
            if hasattr(response, "content"):
                content = response.content
                # Guard: str(None) == "None" which would pass downstream checks.
                # Use `is not None` so that a falsy-but-valid response (e.g. 0,
                # False, "") is still converted; only None is dropped.
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


# ── Phase B: Subprocess Execution ────────────────────────────────────────────

_IGNORED_DIRS = {
    "__pycache__", ".git", ".mypy_cache", ".pytest_cache",
    ".tox", "dist", "build", ".eggs",
}


def _syntax_check(code_dir: str) -> ExecutionPhaseResult:
    """Run ``py_compile`` on every ``.py`` file under *code_dir*."""
    results: List[FileCheckResult] = []
    for dirpath, dirnames, filenames in os.walk(code_dir):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, code_dir)
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "py_compile", fpath],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode == 0:
                    results.append(FileCheckResult(file=rel, passed=True))
                else:
                    err_msg = (proc.stderr or proc.stdout).strip()
                    results.append(FileCheckResult(
                        file=rel, passed=False, error=err_msg[:300],
                    ))
            except subprocess.TimeoutExpired:
                results.append(FileCheckResult(
                    file=rel, passed=False, error="py_compile timed out",
                ))
            except OSError as exc:
                results.append(FileCheckResult(
                    file=rel, passed=False, error=str(exc)[:200],
                ))

    all_passed = all(r.passed for r in results) if results else True
    return ExecutionPhaseResult(
        phase="syntax_check",
        passed=all_passed,
        file_results=results,
    )


def _run_pytest_suite(run_dir: str, timeout: int = 120) -> ExecutionPhaseResult:
    """Run ``pytest`` on ``code/tests/`` if tests exist."""
    tests_dir = os.path.join(run_dir, "code", "tests")
    if not os.path.isdir(tests_dir):
        return ExecutionPhaseResult(
            phase="pytest",
            passed=True,
            stdout="No code/tests/ directory — skipped.",
        )

    test_files = [
        f for f in os.listdir(tests_dir)
        if f.startswith("test_") and f.endswith(".py")
    ]
    if not test_files:
        return ExecutionPhaseResult(
            phase="pytest",
            passed=True,
            stdout="No test_*.py files found — skipped.",
        )

    # Pass the code directory so generated modules are importable even when
    # no conftest.py adds code/ to sys.path explicitly.
    env = _make_safe_env(os.path.join(run_dir, "code"))

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                tests_dir,
                "-q", "--tb=short", "--no-header",
                "-p", "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=run_dir,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        return ExecutionPhaseResult(
            phase="pytest",
            passed=(proc.returncode == 0),
            return_code=proc.returncode,
            stdout=proc.stdout[:5000],
            stderr=proc.stderr[:2000],
        )
    except subprocess.TimeoutExpired:
        return ExecutionPhaseResult(
            phase="pytest",
            passed=False,
            timed_out=True,
            stderr=f"pytest timed out after {timeout}s",
        )
    except OSError as exc:
        return ExecutionPhaseResult(
            phase="pytest",
            passed=False,
            stderr=str(exc),
        )


def _smoke_check(code_dir: str, timeout: int = 30) -> ExecutionPhaseResult:
    """Try running ``python main.py --help`` if ``main.py`` exists.

    ``--help`` is chosen because argparse-based scripts handle it before
    executing any real business logic, making it a safe smoke test that
    verifies imports and basic initialisation without side-effects.
    """
    main_py = os.path.join(code_dir, "main.py")
    if not os.path.isfile(main_py):
        return ExecutionPhaseResult(
            phase="smoke_check",
            passed=True,
            stdout="No main.py found — skipped.",
        )

    env = _make_safe_env(code_dir)

    try:
        proc = subprocess.run(
            [sys.executable, main_py, "--help"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=code_dir,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        # --help typically exits 0.  For scripts without argparse, accept
        # any non-traceback exit in [1, 2] (unrecognised arg is not a crash).
        # Negative codes indicate signal-terminated processes (SIGSEGV=-11,
        # SIGKILL=-9 etc.) which must be treated as failures.
        has_traceback = "Traceback (most recent call last)" in proc.stderr
        passed = proc.returncode == 0 or (
            0 < proc.returncode <= 2 and not has_traceback
        )
        return ExecutionPhaseResult(
            phase="smoke_check",
            passed=passed,
            return_code=proc.returncode,
            stdout=proc.stdout[:3000],
            stderr=proc.stderr[:2000],
        )
    except subprocess.TimeoutExpired:
        return ExecutionPhaseResult(
            phase="smoke_check",
            passed=False,
            timed_out=True,
            stderr=f"main.py --help timed out after {timeout}s",
        )
    except OSError as exc:
        return ExecutionPhaseResult(
            phase="smoke_check",
            passed=False,
            stderr=str(exc),
        )


# ── Phase A: LLM Adversarial Review ─────────────────────────────────────────

_ADVERSARIAL_SYSTEM_PROMPT = """\
You are an adversarial senior code reviewer.  Your job is to find real,
actionable problems in the code below — NOT to praise it.

Focus on:
1. Logic errors, off-by-one, incorrect conditions, unhandled edge cases.
2. Security vulnerabilities (injection, hardcoded secrets, unsafe deserialization).
3. Requirements mismatch: does the code actually do what analysis_result claims?
4. Missing error handling for I/O, network, or external service calls.
5. Design anti-patterns that will cause real maintenance or runtime issues.

DO NOT report:
- Style preferences, naming conventions, or missing type hints.
- Hypothetical issues with no concrete trigger scenario.
- Issues already present in analysis_result's review_report.

Output ONLY a JSON object with this exact schema (no markdown fences, no prose):
{
  "verdict": "pass | fail | warning",
  "findings": [
    {
      "severity": "critical | high | medium | low",
      "category": "logic_error | security | requirements_mismatch | design | correctness | missing_error_handling",
      "file": "filename.py",
      "line": 42,
      "description": "concise description of the problem"
    }
  ],
  "summary": "one-paragraph overall assessment"
}

If the code is genuinely clean, return verdict "pass" with an empty findings array.
Do not fabricate findings to look thorough.
"""


def _collect_source_for_review(
    code_dir: str,
    max_total_chars: int = 30000,
) -> str:
    """Collect source code from *code_dir* for LLM review, within budget."""
    parts: List[str] = []
    total = 0
    budget_exceeded = False
    for dirpath, dirnames, filenames in os.walk(code_dir):
        if budget_exceeded:
            break
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, code_dir)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                continue
            if len(content) > 8000:
                content = content[:8000] + "\n# ... (truncated)\n"
            chunk = f"--- {rel} ---\n{content}\n"
            if total + len(chunk) > max_total_chars:
                parts.append(f"--- {rel} --- (omitted: token budget exceeded)\n")
                budget_exceeded = True
                break
            parts.append(chunk)
            total += len(chunk)
    return "\n".join(parts)


def _extract_json_from_response(raw: str) -> Optional[dict]:
    """Extract a JSON object from LLM response, handling markdown fences."""
    raw = raw.strip()
    # Direct parse
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Markdown code fence extraction
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Brace-matching fallback — string-aware to avoid counting { and }
    # that appear inside JSON string values as structural delimiters.
    # A naive depth counter without string tracking would mis-detect the
    # boundary for payloads like '{"summary": "score={3}"}', breaking the
    # subsequent json.loads() call and silently returning None.
    brace_start = raw.find("{")
    if brace_start >= 0:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(brace_start, len(raw)):
            ch = raw[i]
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if ch == "\\":
                    escape_next = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(raw[brace_start : i + 1])
                            if isinstance(data, dict):
                                return data
                        except json.JSONDecodeError:
                            pass
                        break
    return None


def _safe_int_or_none(value: Any) -> Optional[int]:
    """Convert *value* to ``int`` or ``None``."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _adversarial_review(
    code_dir: str,
    analysis: Dict[str, Any],
    llm: Any,
) -> Tuple[List[AdversarialFinding], str, str]:
    """
    Run adversarial LLM review.

    Returns ``(findings, summary, verdict)``.
    """
    source_text = _collect_source_for_review(code_dir)
    if not source_text.strip():
        return [], "No source files found for review.", "pass"

    # Build concise claims summary from analysis_result.json
    claims_parts: List[str] = []
    for key in ("summary", "score", "risk_level", "confidence",
                "direction_decision", "consensus_summary"):
        val = analysis.get(key)
        if val is None:
            continue
        if isinstance(val, dict):
            claims_parts.append(
                f"{key}: {json.dumps(val, ensure_ascii=False)[:500]}"
            )
        else:
            claims_parts.append(f"{key}: {val}")
    claims_text = (
        "\n".join(claims_parts) if claims_parts
        else "(no analysis claims available)"
    )

    prompt = (
        f"{_ADVERSARIAL_SYSTEM_PROMPT}\n\n"
        f"=== ANALYSIS CLAIMS ===\n{claims_text}\n\n"
        f"=== SOURCE CODE ===\n{source_text}\n\n"
        f"Output your JSON review now:"
    )

    raw = _call_llm(llm, prompt)
    if not raw or not raw.strip():
        return [], "LLM returned empty response.", "unknown"

    parsed = _extract_json_from_response(raw)
    if parsed is None:
        return (
            [],
            f"Could not parse LLM response as JSON. Raw: {raw[:300]}",
            "unknown",
        )

    verdict = str(parsed.get("verdict", "unknown")).lower()
    if verdict not in ("pass", "fail", "warning"):
        verdict = "warning"
    summary = str(parsed.get("summary", ""))

    findings: List[AdversarialFinding] = []
    for item in parsed.get("findings") or []:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "medium")).lower()
        if sev not in ("critical", "high", "medium", "low"):
            sev = "medium"
        cat = str(item.get("category", "correctness")).lower()
        findings.append(AdversarialFinding(
            severity=sev,
            category=cat,
            file=str(item.get("file", "")),
            description=str(item.get("description", "")),
            line=_safe_int_or_none(item.get("line")),
        ))

    return findings, summary, verdict


# ── Main entry point ─────────────────────────────────────────────────────────

def validate_run(
    run_dir: str,
    *,
    llm: Any = None,
    timeout: int = 60,
) -> IndependentValidationReport:
    """
    Run independent validation on a completed pipeline run.

    Phase B (subprocess execution) always runs.
    Phase A (adversarial LLM review) runs only when *llm* is provided.

    Args:
        run_dir:  Path to a completed pipeline run output directory.
        llm:      Optional LLM object for adversarial review (duck-typed).
        timeout:  Subprocess timeout in seconds for pytest / smoke checks.

    Returns:
        IndependentValidationReport with execution results and LLM findings.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return IndependentValidationReport(
            success=False,
            overall_verdict="fail",
            errors=["No code/ directory found in run output."],
        )

    report = IndependentValidationReport(success=True)

    # ── Phase B: Subprocess Execution ────────────────────────────────────────

    # B1: Syntax check (py_compile — safe, no code execution)
    syntax_result = _syntax_check(code_dir)
    report.execution_phases.append(syntax_result)

    # B2: pytest execution (runs generated tests, if any)
    pytest_result = _run_pytest_suite(run_dir, timeout=timeout)
    report.execution_phases.append(pytest_result)

    # B3: Smoke check (main.py --help — triggers imports only)
    smoke_result = _smoke_check(code_dir, timeout=min(timeout, 30))
    report.execution_phases.append(smoke_result)

    # ── Phase A: Adversarial LLM Review ──────────────────────────────────────

    if llm is not None:
        try:
            analysis: Dict[str, Any] = {}
            analysis_path = os.path.join(run_dir, "analysis_result.json")
            if os.path.isfile(analysis_path):
                try:
                    with open(analysis_path, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        analysis = loaded
                except (json.JSONDecodeError, OSError):
                    pass

            findings, summary, adv_verdict = _adversarial_review(
                code_dir, analysis, llm,
            )
            report.adversarial_findings = findings
            report.adversarial_summary = summary
            report.adversarial_verdict = adv_verdict
        except Exception as exc:
            report.errors.append(f"Adversarial review failed: {exc}")

    # ── Determine overall verdict ────────────────────────────────────────────

    execution_passed = all(p.passed for p in report.execution_phases)
    has_critical = any(
        f.severity in ("critical", "high")
        for f in report.adversarial_findings
    )

    # The LLM's top-level verdict is an independent signal that must not be
    # silently discarded.  An adversarial reviewer may return "fail" with only
    # medium/low-severity findings when the overall picture warrants it (e.g.
    # multiple medium issues, or a subtle logic error it cannot pin to one line).
    # Treating such a verdict as merely "warning" would under-report the risk.
    adv_verdict_fail = (report.adversarial_verdict == "fail")

    if not execution_passed or has_critical or adv_verdict_fail:
        report.overall_verdict = "fail"
        report.success = False
    elif report.adversarial_findings or report.adversarial_verdict == "warning":
        report.overall_verdict = "warning"
    else:
        report.overall_verdict = "pass"

    # ── Persist ──────────────────────────────────────────────────────────────

    report_path = os.path.join(run_dir, "independent_validation_report.json")
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
