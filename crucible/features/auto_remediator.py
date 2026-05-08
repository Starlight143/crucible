"""
features/auto_remediator.py
============================
Closed-loop auto-remediation for security and validation findings.

Wires the existing ``security_scan.build_security_fix_prompt()`` and
``independent_validator`` adversarial findings into an LLM-driven fix loop:

1. Collect actionable findings (security HIGH+ and/or adversarial HIGH+).
2. For each affected file, build a fix prompt and call the LLM.
3. Validate the patched code (syntax check + re-scan).
4. Accept the patch only if it passes validation.
5. Repeat up to ``max_rounds`` times.

Usage::

    from crucible.features.auto_remediator import remediate_run
    report = remediate_run("/path/to/run_dir", llm, max_rounds=3)
    print(report.summary_text())
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from crucible.output_validation import strip_reasoning_blocks

# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class RemediationPatch:
    """One attempted fix for a source file."""
    file: str               # relative to code/
    round_number: int
    source: str             # "security" | "validation"
    issues_targeted: int
    applied: bool = False
    syntax_valid: bool = False
    issues_remaining: int = 0
    error: str = ""


@dataclass
class RemediationReport:
    success: bool
    rounds_executed: int
    total_patches_attempted: int = 0
    total_patches_applied: int = 0
    initial_issue_count: int = 0
    final_issue_count: int = 0
    patches: List[RemediationPatch] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "rounds_executed": self.rounds_executed,
            "total_patches_attempted": self.total_patches_attempted,
            "total_patches_applied": self.total_patches_applied,
            "initial_issue_count": self.initial_issue_count,
            "final_issue_count": self.final_issue_count,
            "patches": [
                {
                    "file": p.file,
                    "round": p.round_number,
                    "source": p.source,
                    "issues_targeted": p.issues_targeted,
                    "applied": p.applied,
                    "syntax_valid": p.syntax_valid,
                    "issues_remaining": p.issues_remaining,
                    "error": p.error,
                }
                for p in self.patches
            ],
            "errors": self.errors,
        }

    def summary_text(self) -> str:
        lines = [
            "Auto-Remediation Report",
            f"  Rounds: {self.rounds_executed}",
            f"  Patches: {self.total_patches_applied}/{self.total_patches_attempted} applied",
            f"  Issues: {self.initial_issue_count} → {self.final_issue_count}",
        ]
        for p in self.patches[:10]:
            status = "APPLIED" if p.applied else "SKIPPED"
            lines.append(f"  [{status:7s}] {p.file} (round {p.round_number}, {p.source})")
            if p.error:
                lines.append(f"            {p.error[:100]}")
        if self.errors:
            lines.append("\nErrors:")
            for e in self.errors:
                lines.append(f"  ! {e}")
        return "\n".join(lines)


# ── LLM interface ────────────────────────────────────────────────────────────

def _call_llm(llm: Any, prompt: str) -> Optional[str]:
    """Call *llm* with *prompt*.  Returns None on exception or empty/None response."""
    try:
        if hasattr(llm, "invoke"):
            response = llm.invoke(prompt)
            if hasattr(response, "content"):
                content = response.content
                # `content is not None` guards against actual None objects.
                # The `or None` then converts empty strings to None so callers
                # can rely on truthiness to detect "no usable response".
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
    # Normalise Windows CRLF → LF first so all patterns work uniformly.
    text = (text or "").replace("\r\n", "\n")
    # Reasoning-model defence: strip <think>/<reasoning>/… blocks before
    # fence detection.  Without this the patched-file content can leak the
    # model's chain-of-thought, which then fails ast.parse() on the leading
    # "<" character and rejects every fix attempt.
    text = strip_reasoning_blocks(text).strip()
    match = re.match(r"^```(?:python)?\n?(.*?)```\s*$", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    text = re.sub(r"^```\w*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _is_valid_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


# ── Issue collection ─────────────────────────────────────────────────────────

def _collect_security_issues(
    run_dir: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Group HIGH+ security issues by file (relative to code/)."""
    report_path = os.path.join(run_dir, "security_report.json")
    if not os.path.isfile(report_path):
        return {}
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for issue in data.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        sev = str(issue.get("severity", "")).upper()
        if sev not in ("HIGH", "CRITICAL"):
            continue
        fname = str(issue.get("file", ""))
        if fname:
            by_file.setdefault(fname, []).append(issue)
    return by_file


def _collect_validation_issues(
    run_dir: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """Group HIGH+ adversarial findings by file (relative to code/)."""
    report_path = os.path.join(run_dir, "independent_validation_report.json")
    if not os.path.isfile(report_path):
        return {}
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for finding in data.get("adversarial_findings") or []:
        if not isinstance(finding, dict):
            continue
        sev = str(finding.get("severity", "")).upper()
        if sev not in ("HIGH", "CRITICAL"):
            continue
        fname = str(finding.get("file", ""))
        if fname:
            by_file.setdefault(fname, []).append(finding)
    return by_file


# ── Fix prompt builders ──────────────────────────────────────────────────────

def _build_security_fix_prompt(
    issues: List[Dict[str, Any]],
    source_code: str,
    filename: str,
) -> str:
    issue_lines = []
    for idx, i in enumerate(issues[:10], 1):
        sev = str(i.get("severity", ""))
        rule = str(i.get("rule_id", ""))
        desc = str(i.get("description", ""))
        line = i.get("line", "?")
        issue_lines.append(f"{idx}. [{sev}] {rule}: {desc} (line {line})")
    return (
        f"File: {filename}\n"
        f"Fix the following security issues in the Python code below.\n"
        f"Return ONLY the complete corrected Python file, no explanation.\n\n"
        f"Issues to fix:\n"
        + "\n".join(issue_lines)
        + f"\n\nCode:\n{source_code}"
    )


def _build_validation_fix_prompt(
    findings: List[Dict[str, Any]],
    source_code: str,
    filename: str,
) -> str:
    finding_lines = []
    for idx, f in enumerate(findings[:10], 1):
        sev = str(f.get("severity", ""))
        cat = str(f.get("category", ""))
        desc = str(f.get("description", ""))
        line = f.get("line", "?")
        finding_lines.append(f"{idx}. [{sev}] {cat}: {desc} (line {line})")
    return (
        f"File: {filename}\n"
        f"Fix the following code review findings in the Python code below.\n"
        f"Return ONLY the complete corrected Python file, no explanation.\n\n"
        f"Findings to fix:\n"
        + "\n".join(finding_lines)
        + f"\n\nCode:\n{source_code}"
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def remediate_run(
    run_dir: str,
    llm: Any,
    *,
    max_rounds: int = 3,
    fix_security: bool = True,
    fix_validation: bool = True,
) -> RemediationReport:
    """
    Run closed-loop auto-remediation on a completed pipeline run.

    Collects HIGH+ issues from security scan and/or adversarial validation,
    generates LLM-powered patches, validates syntax, and applies fixes.
    Repeats up to *max_rounds* or until no HIGH+ issues remain.

    Args:
        run_dir:         Path to a completed run output directory.
        llm:             LLM object for generating fix code (duck-typed).
        max_rounds:      Maximum remediation iterations (default: 3).
        fix_security:    Include security_report.json issues (default: True).
        fix_validation:  Include adversarial findings (default: True).

    Returns:
        RemediationReport summarising all attempted patches.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return RemediationReport(
            success=False,
            rounds_executed=0,
            errors=["No code/ directory found in run output."],
        )

    report = RemediationReport(success=True, rounds_executed=0)

    # Count initial issues
    sec_issues = _collect_security_issues(run_dir) if fix_security else {}
    val_issues = _collect_validation_issues(run_dir) if fix_validation else {}
    all_files: Set[str] = set(sec_issues.keys()) | set(val_issues.keys())
    report.initial_issue_count = sum(len(v) for v in sec_issues.values()) + sum(
        len(v) for v in val_issues.values()
    )

    if not all_files:
        report.final_issue_count = 0
        return report

    # Track validation files that were successfully patched this session.
    # The independent_validator is not re-run between rounds, so its report
    # JSON never changes.  Without this set, the same validation findings
    # would be re-collected and re-patched every round until max_rounds is
    # exhausted — wasting LLM calls and risking regressions on already-fixed code.
    _patched_val_files: Set[str] = set()

    for round_num in range(1, max_rounds + 1):
        report.rounds_executed = round_num
        round_had_patch = False

        for rel_file in sorted(all_files):
            fpath = os.path.join(code_dir, rel_file)
            if not os.path.isfile(fpath):
                continue

            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError:
                continue

            # Determine which issues to fix for this file
            file_sec = sec_issues.get(rel_file, [])
            file_val = val_issues.get(rel_file, [])

            if not file_sec and not file_val:
                continue

            # Build prompt (prefer security issues first, then validation)
            if file_sec:
                prompt = _build_security_fix_prompt(file_sec, source, rel_file)
                source_type = "security"
                issue_count = len(file_sec)
            else:
                prompt = _build_validation_fix_prompt(file_val, source, rel_file)
                source_type = "validation"
                issue_count = len(file_val)

            patch = RemediationPatch(
                file=rel_file,
                round_number=round_num,
                source=source_type,
                issues_targeted=issue_count,
            )
            report.total_patches_attempted += 1

            raw = _call_llm(llm, prompt)
            if not raw or not raw.strip():
                patch.error = "LLM returned empty response."
                report.patches.append(patch)
                continue

            cleaned = _strip_code_fences(raw)

            if not _is_valid_python(cleaned):
                patch.syntax_valid = False
                patch.error = "Patched code has syntax errors — skipped."
                report.patches.append(patch)
                continue

            patch.syntax_valid = True

            # Skip if LLM returned identical code
            if cleaned == source:
                patch.error = "LLM returned unchanged code."
                report.patches.append(patch)
                continue

            # Write the patch atomically: truncating fpath to zero bytes before
            # completing the write would destroy the original working code if
            # the process is killed mid-write.  Use a sibling .tmp file and
            # os.replace() so the original is never touched until the new
            # content is fully persisted.
            _tmp_fpath = fpath + ".tmp"
            try:
                with open(_tmp_fpath, "w", encoding="utf-8") as fh:
                    fh.write(cleaned)
                os.replace(_tmp_fpath, fpath)
                patch.applied = True
                report.total_patches_applied += 1
                round_had_patch = True
                # Record that this validation file was patched so it is not
                # retried in future rounds from the same (stale) report JSON.
                # Only mark as patched when the validation branch was actually
                # executed; a security-branch patch does not resolve the
                # validation issues for this file.
                if source_type == "validation" and rel_file in val_issues:
                    _patched_val_files.add(rel_file)
            except Exception as exc:
                try:
                    os.unlink(_tmp_fpath)
                except OSError:
                    pass
                patch.applied = False
                patch.error = f"Write error: {exc}"

            report.patches.append(patch)

        if not round_had_patch:
            break

        # Re-scan after this round to check remaining issues
        # Re-run security scan if we fixed security issues
        if fix_security:
            try:
                from crucible.features.security_scan import scan_run_directory
                scan_run_directory(run_dir)
            except Exception as _re_scan_exc:
                report.errors.append(f"Re-scan failed (round {round_num}): {_re_scan_exc}")

        # Re-collect issues for next round.
        # Security: report is updated by scan_run_directory above — re-read it.
        # Validation: report JSON is not re-generated between rounds; exclude
        # files already patched this session to avoid redundant LLM calls.
        sec_issues = _collect_security_issues(run_dir) if fix_security else {}
        _val_all = _collect_validation_issues(run_dir) if fix_validation else {}
        val_issues = {k: v for k, v in _val_all.items() if k not in _patched_val_files}
        all_files = set(sec_issues.keys()) | set(val_issues.keys())

        if not all_files:
            break

    # Final issue count
    final_sec = _collect_security_issues(run_dir) if fix_security else {}
    final_val = _collect_validation_issues(run_dir) if fix_validation else {}
    report.final_issue_count = sum(len(v) for v in final_sec.values()) + sum(
        len(v) for v in final_val.values()
    )

    report.success = report.final_issue_count == 0

    # Persist report (atomic write to prevent a partial file on crash/interrupt)
    report_path = os.path.join(run_dir, "auto_remediation_report.json")
    _tmp_report = report_path + ".tmp"
    try:
        with open(_tmp_report, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(_tmp_report, report_path)
    except OSError:
        try:
            os.unlink(_tmp_report)
        except OSError:
            pass

    return report
