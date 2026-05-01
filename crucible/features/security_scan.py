"""
features/security_scan.py
=========================
Static security analysis for generated code.

Primary scanner: ``bandit`` (if installed as a Python package).
Fallback scanner: built-in regex-pattern rules that cover the most common
OWASP Top-10 issues (eval/exec injection, hardcoded secrets, SQL injection,
unsafe deserialization, shell injection, weak crypto, etc.).

HIGH or CRITICAL severity issues cause ``SecurityScanReport.passed`` to be
``False``.  The enhanced runner can optionally feed those issues back into an
LLM fix loop.

A ``security_report.json`` is written to *run_dir* automatically.

Usage::

    from crucible.features.security_scan import scan_run_directory
    report = scan_run_directory("/path/to/run_dir")
    if not report.passed:
        print(f"FAILED: {len(report.high_severity_issues)} HIGH issues")
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Severity ordering ─────────────────────────────────────────────────────────

_SEVERITY_RANK: Dict[str, int] = {
    "INFO": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}


# ── Public data models ────────────────────────────────────────────────────────

@dataclass
class SecurityIssue:
    severity: str     # INFO | LOW | MEDIUM | HIGH | CRITICAL
    confidence: str   # LOW | MEDIUM | HIGH
    rule_id: str
    description: str
    file: str
    line: int
    code_snippet: str = ""

    def is_high_or_above(self) -> bool:
        return _SEVERITY_RANK.get(self.severity.upper(), 0) >= _SEVERITY_RANK["HIGH"]


@dataclass
class SecurityScanReport:
    passed: bool
    scanner_used: str   # "bandit" | "pattern" | "none"
    issues: List[SecurityIssue] = field(default_factory=list)
    scanned_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def high_severity_issues(self) -> List[SecurityIssue]:
        return [i for i in self.issues if i.is_high_or_above()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "scanner_used": self.scanner_used,
            "total_issues": len(self.issues),
            "high_severity_count": len(self.high_severity_issues),
            "issues": [
                {
                    "severity": i.severity,
                    "confidence": i.confidence,
                    "rule_id": i.rule_id,
                    "description": i.description,
                    "file": i.file,
                    "line": i.line,
                    "code_snippet": i.code_snippet,
                }
                for i in sorted(
                    self.issues,
                    key=lambda x: _SEVERITY_RANK.get(x.severity.upper(), 0),
                    reverse=True,
                )
            ],
            "scanned_files": self.scanned_files,
            "errors": self.errors,
        }


# ── Pattern-based fallback rules ──────────────────────────────────────────────

@dataclass
class _PatternRule:
    rule_id: str
    pattern: re.Pattern  # type: ignore[type-arg]
    description: str
    severity: str
    confidence: str


_PATTERN_RULES: List[_PatternRule] = [
    _PatternRule(
        "PAT001",
        re.compile(r"\beval\s*\(", re.IGNORECASE),
        "Use of eval() may allow arbitrary code execution.",
        "HIGH", "HIGH",
    ),
    _PatternRule(
        "PAT002",
        re.compile(r"\bexec\s*\(", re.IGNORECASE),
        "Use of exec() may allow arbitrary code execution.",
        "HIGH", "HIGH",
    ),
    _PatternRule(
        "PAT003",
        re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
        "os.system() is vulnerable to shell injection; use subprocess with a list.",
        "HIGH", "MEDIUM",
    ),
    _PatternRule(
        "PAT004",
        re.compile(
            r"\bsubprocess\s*\.\s*(?:run|call|Popen|check_output|check_call)"
            r"\s*\([^)]*\bshell\s*=\s*True",
            re.IGNORECASE | re.DOTALL,
        ),
        "subprocess with shell=True is vulnerable to shell injection.",
        "HIGH", "HIGH",
    ),
    _PatternRule(
        "PAT005",
        re.compile(r"\bhashlib\s*\.\s*(?:md5|sha1)\s*\(", re.IGNORECASE),
        "MD5/SHA1 are cryptographically weak; use SHA-256 or stronger.",
        "MEDIUM", "HIGH",
    ),
    _PatternRule(
        "PAT006",
        re.compile(
            r"(?:password|secret|api_key|api_secret|token|private_key)\s*=\s*['\"][^'\"]{4,}['\"]",
            re.IGNORECASE,
        ),
        "Possible hardcoded credential or secret detected.",
        "HIGH", "MEDIUM",
    ),
    _PatternRule(
        "PAT007",
        re.compile(r"\bpickle\s*\.\s*loads?\s*\(", re.IGNORECASE),
        "pickle.load() on untrusted data allows arbitrary code execution.",
        "HIGH", "MEDIUM",
    ),
    _PatternRule(
        "PAT008",
        # Match all yaml.load() calls.  The previous negative-lookahead
        # (?!.*Loader\s*=) caused false positives for multi-line calls where
        # Loader= appeared on a continuation line — the single-line `.*` could
        # not cross the newline, so safe multi-line invocations were flagged.
        # Flagging all yaml.load() is the conservative-correct approach;
        # callers should use yaml.safe_load() or pass Loader=yaml.SafeLoader
        # explicitly.
        re.compile(r"\byaml\s*\.\s*load\s*\(", re.IGNORECASE),
        "yaml.load() is unsafe; use yaml.safe_load() or yaml.load(data, Loader=yaml.SafeLoader).",
        "HIGH", "HIGH",
    ),
    _PatternRule(
        "PAT009",
        re.compile(
            # Catch both bare calls (execute(...)) and method calls
            # (cursor.execute(...), conn.query(...)) — the previous pattern
            # lacked \. matching so it already caught substrings like
            # "cursor.execute"; but adding an explicit optional-dot group and
            # \b word-boundary on the method name makes intent clear and
            # prevents matching inside identifiers like "re_execute_plan(...)".
            r"""\b(?:execute|query)\s*\(\s*(?:f['"]|['"].*?(?:%s|\+\s*\w))""",
            re.IGNORECASE,
        ),
        "Possible SQL injection via string formatting in query/execute call.",
        "HIGH", "MEDIUM",
    ),
    _PatternRule(
        "PAT010",
        re.compile(r"\bverify\s*=\s*False\b", re.IGNORECASE),
        "SSL certificate verification disabled; susceptible to MITM attacks.",
        "MEDIUM", "HIGH",
    ),
    _PatternRule(
        "PAT011",
        re.compile(r"\ballow_pickle\s*=\s*True\b", re.IGNORECASE),
        "numpy allow_pickle=True on untrusted data allows code execution.",
        "HIGH", "MEDIUM",
    ),
    _PatternRule(
        "PAT012",
        re.compile(r"\bDEBUG\s*=\s*True\b"),
        "DEBUG=True should not be enabled in production code.",
        "MEDIUM", "MEDIUM",
    ),
    _PatternRule(
        "PAT013",
        re.compile(r"\bRandom\(\s*\)\b|\brandom\.random\b|\brandom\.randint\b", re.IGNORECASE),
        "Use of non-cryptographic random in a security context; consider secrets module.",
        "LOW", "LOW",
    ),
    _PatternRule(
        "PAT014",
        re.compile(r"\bos\.path\.join\s*\([^)]*request\.", re.IGNORECASE),
        "Potential path traversal: user-supplied input used in os.path.join.",
        "HIGH", "MEDIUM",
    ),
]


def _pattern_scan_file(filepath: str, rel_path: str) -> List[SecurityIssue]:
    issues: List[SecurityIssue] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return issues

    content = "".join(lines)
    for rule in _PATTERN_RULES:
        for match in rule.pattern.finditer(content):
            line_num = content[: match.start()].count("\n") + 1
            snippet = lines[line_num - 1].strip() if 0 < line_num <= len(lines) else ""
            issues.append(
                SecurityIssue(
                    severity=rule.severity,
                    confidence=rule.confidence,
                    rule_id=rule.rule_id,
                    description=rule.description,
                    file=rel_path,
                    line=line_num,
                    code_snippet=snippet[:200],
                )
            )
    return issues


# ── Bandit integration ────────────────────────────────────────────────────────

def _run_bandit(code_dir: str) -> Optional[Dict[str, Any]]:
    """
    Invoke bandit as ``python -m bandit`` and return its JSON output dict.
    Returns None when bandit is not installed or the invocation fails.
    Only MEDIUM severity or above is reported (``-ll`` = ``--level MEDIUM``).
    """
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "bandit",
                "-r", code_dir,
                "-f", "json",
                "-ll",          # report only medium+ severity
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # bandit exits non-zero when issues are found; stdout still contains JSON
        stdout = result.stdout.strip()
        if stdout:
            return json.loads(stdout)
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return None


def _safe_parse_line(value: Any) -> int:
    """Convert a bandit line_number field to int, defaulting to 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_bandit_results(
    bandit_data: Dict[str, Any],
    code_dir: str,
) -> List[SecurityIssue]:
    issues: List[SecurityIssue] = []
    for item in bandit_data.get("results", []):
        filepath = str(item.get("filename", ""))
        try:
            rel_path = os.path.relpath(filepath, code_dir)
        except ValueError:
            rel_path = filepath
        issues.append(
            SecurityIssue(
                severity=str(item.get("issue_severity", "LOW")).upper(),
                confidence=str(item.get("issue_confidence", "LOW")).upper(),
                rule_id=str(item.get("test_id", "")),
                description=str(item.get("issue_text", "")),
                file=rel_path,
                line=_safe_parse_line(item.get("line_number", 0)),
                code_snippet=str(item.get("code", ""))[:200].strip(),
            )
        )
    return issues


# ── Public API ────────────────────────────────────────────────────────────────

def scan_run_directory(run_dir: str) -> SecurityScanReport:
    """
    Run a security scan on all Python files in *run_dir/code/*.

    Saves ``security_report.json`` to *run_dir*.
    Returns a SecurityScanReport; ``passed`` is False when any HIGH/CRITICAL
    issue is found.
    """
    code_dir = os.path.join(run_dir, "code")
    if not os.path.isdir(code_dir):
        return SecurityScanReport(
            passed=True,
            scanner_used="none",
            errors=["No code/ directory found — nothing to scan."],
        )

    # Collect files
    scanned_files: List[str] = []
    for dirpath, _, filenames in os.walk(code_dir):
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, fname), code_dir)
                scanned_files.append(rel)

    if not scanned_files:
        return SecurityScanReport(
            passed=True,
            scanner_used="none",
            scanned_files=[],
            errors=["No Python files found to scan."],
        )

    errors: List[str] = []

    # Try bandit first
    bandit_data = _run_bandit(code_dir)
    if bandit_data is not None:
        issues = _parse_bandit_results(bandit_data, code_dir)
        scanner = "bandit"
    else:
        issues = []
        for rel_fname in scanned_files:
            full_path = os.path.join(code_dir, rel_fname)
            issues.extend(_pattern_scan_file(full_path, rel_fname))
        scanner = "pattern"

    high_count = sum(1 for i in issues if i.is_high_or_above())
    passed = high_count == 0

    report = SecurityScanReport(
        passed=passed,
        scanner_used=scanner,
        issues=issues,
        scanned_files=scanned_files,
        errors=errors,
    )

    # Persist report (atomic write)
    report_path = os.path.join(run_dir, "security_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(_tmp_path, report_path)
    except OSError as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        report.errors.append(f"Could not write security_report.json: {exc}")

    return report


def build_security_fix_prompt(
    issues: List[SecurityIssue],
    file_content: str,
    filename: str = "",
) -> str:
    """
    Build an LLM prompt to fix security issues in *file_content*.

    Call this with ``report.high_severity_issues`` to construct a targeted
    remediation prompt for the LLM fix loop.
    """
    issue_lines = [
        f"{idx}. [{i.severity}] {i.rule_id}: {i.description} "
        f"(line {i.line})"
        for idx, i in enumerate(issues[:10], 1)
    ]
    header = f"File: {filename}\n" if filename else ""
    return (
        f"{header}"
        f"Fix the following security issues in the Python code below.\n"
        f"Return ONLY the complete corrected Python file, no explanation.\n\n"
        f"Issues to fix:\n"
        + "\n".join(issue_lines)
        + f"\n\nCode:\n{file_content}"
    )
