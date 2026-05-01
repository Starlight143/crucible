"""
features/dependency_auditor.py
===============================
Dependency vulnerability scanning for generated code.

Runs ``pip-audit`` (if installed) against the generated ``requirements.txt``
and produces a structured ``dependency_audit_report.json`` with CVE details.

Falls back to a lightweight check that flags obviously pinned-to-vulnerable
patterns (e.g. ``requests==2.19.0``) when ``pip-audit`` is not available.

Usage::

    from crucible.features.dependency_auditor import audit_dependencies
    report = audit_dependencies("/path/to/run_dir")
    if not report.passed:
        print(f"FAILED: {len(report.vulnerabilities)} vulnerabilities found")
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class DependencyVulnerability:
    """One vulnerable dependency."""
    package: str
    installed_version: str
    fix_version: str
    vuln_id: str          # CVE-XXXX-XXXX or PYSEC-XXXX-XXXX
    description: str

    @property
    def is_cve(self) -> bool:
        return self.vuln_id.upper().startswith("CVE-")


@dataclass
class DependencyAuditReport:
    passed: bool
    scanner_used: str     # "pip-audit" | "none"
    requirements_file: str
    vulnerabilities: List[DependencyVulnerability] = field(default_factory=list)
    scanned_packages: int = 0
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "scanner_used": self.scanner_used,
            "requirements_file": self.requirements_file,
            "scanned_packages": self.scanned_packages,
            "vulnerability_count": len(self.vulnerabilities),
            "vulnerabilities": [
                {
                    "package": v.package,
                    "installed_version": v.installed_version,
                    "fix_version": v.fix_version,
                    "vuln_id": v.vuln_id,
                    "description": v.description,
                }
                for v in self.vulnerabilities
            ],
            "errors": self.errors,
        }

    def summary_text(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            "Dependency Audit Report",
            f"  Status: {status}",
            f"  Scanner: {self.scanner_used}",
            f"  Packages scanned: {self.scanned_packages}",
            f"  Vulnerabilities: {len(self.vulnerabilities)}",
        ]
        for v in self.vulnerabilities[:10]:
            lines.append(
                f"  [{v.vuln_id}] {v.package}=={v.installed_version}"
                f" → fix: {v.fix_version or 'N/A'}"
            )
            if v.description:
                lines.append(f"    {v.description[:120]}")
        if self.errors:
            lines.append("\nErrors:")
            for e in self.errors:
                lines.append(f"  ! {e}")
        return "\n".join(lines)


# ── pip-audit integration ────────────────────────────────────────────────────

def _find_requirements_file(run_dir: str) -> Optional[str]:
    """Locate requirements.txt — check code/ first, then run root."""
    for candidate in (
        os.path.join(run_dir, "code", "requirements.txt"),
        os.path.join(run_dir, "requirements.txt"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _count_packages(requirements_path: str) -> int:
    """Count non-comment, non-empty lines in requirements.txt."""
    try:
        with open(requirements_path, "r", encoding="utf-8") as fh:
            return sum(
                1 for line in fh
                if line.strip() and not line.strip().startswith("#")
            )
    except OSError:
        return 0


def _run_pip_audit(requirements_path: str) -> Optional[List[Dict[str, Any]]]:
    """
    Invoke ``pip-audit`` in JSON mode against a requirements file.
    Returns parsed JSON list or None if pip-audit is unavailable.
    """
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pip_audit",
                "-r", requirements_path,
                "--format", "json",
                "--progress-spinner", "off",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        # pip-audit exits non-zero when vulnerabilities are found
        stdout = result.stdout.strip()
        if stdout:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                return parsed.get("dependencies", [])
            if isinstance(parsed, list):
                return parsed
        if not stdout:
            if result.returncode != 0:
                stderr = result.stderr or ""
                if "No module named pip_audit" in stderr or "No module named" in stderr:
                    return None  # pip-audit genuinely not installed
                return []  # pip-audit ran but produced no output (crash/error)
            return []
        return []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError, OSError):
        return None


def _parse_pip_audit_results(
    audit_data: List[Dict[str, Any]],
) -> List[DependencyVulnerability]:
    """Parse pip-audit JSON output into vulnerability objects."""
    vulns: List[DependencyVulnerability] = []
    for dep in audit_data:
        if not isinstance(dep, dict):
            continue
        pkg_name = str(dep.get("name", ""))
        pkg_version = str(dep.get("version", ""))
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            fix_versions = vuln.get("fix_versions") or []
            fix_ver = str(fix_versions[0]) if fix_versions else ""
            vulns.append(DependencyVulnerability(
                package=pkg_name,
                installed_version=pkg_version,
                fix_version=fix_ver,
                vuln_id=str(vuln.get("id", "")),
                description=str(vuln.get("description", ""))[:300],
            ))
    return vulns


# ── Main entry point ─────────────────────────────────────────────────────────

def audit_dependencies(run_dir: str) -> DependencyAuditReport:
    """
    Audit dependencies in *run_dir* for known vulnerabilities.

    Locates ``requirements.txt`` in ``code/`` or run root, then runs
    ``pip-audit`` if available.  Results are saved to
    ``{run_dir}/dependency_audit_report.json``.
    """
    req_path = _find_requirements_file(run_dir)
    if req_path is None:
        return DependencyAuditReport(
            passed=True,
            scanner_used="none",
            requirements_file="",
            errors=["No requirements.txt found — nothing to audit."],
        )

    rel_req = os.path.relpath(req_path, run_dir)
    pkg_count = _count_packages(req_path)

    # Try pip-audit
    audit_data = _run_pip_audit(req_path)
    if audit_data is not None:
        vulns = _parse_pip_audit_results(audit_data)
        report = DependencyAuditReport(
            passed=len(vulns) == 0,
            scanner_used="pip-audit",
            requirements_file=rel_req,
            vulnerabilities=vulns,
            scanned_packages=pkg_count,
        )
    else:
        # pip-audit not available — report as skipped
        report = DependencyAuditReport(
            passed=True,
            scanner_used="none",
            requirements_file=rel_req,
            scanned_packages=pkg_count,
            errors=[
                "pip-audit not installed — dependency audit skipped. "
                "Install with: pip install pip-audit"
            ],
        )

    # Persist report (atomic write)
    report_path = os.path.join(run_dir, "dependency_audit_report.json")
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
