# ruff: noqa: E402
"""Tests for crucible.features.dependency_auditor."""
import json
import os
import sys
import tempfile
import unittest

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.dependency_auditor import (
    DependencyAuditReport,
    DependencyVulnerability,
    _count_packages,
    _find_requirements_file,
    _parse_pip_audit_results,
    audit_dependencies,
)


class TestDependencyVulnerability(unittest.TestCase):
    def test_is_cve(self) -> None:
        v = DependencyVulnerability(
            package="requests", installed_version="2.19.0",
            fix_version="2.31.0", vuln_id="CVE-2023-1234",
            description="Vuln",
        )
        self.assertTrue(v.is_cve)

    def test_not_cve(self) -> None:
        v = DependencyVulnerability(
            package="urllib3", installed_version="1.25.0",
            fix_version="1.26.0", vuln_id="PYSEC-2023-5678",
            description="Vuln",
        )
        self.assertFalse(v.is_cve)


class TestDependencyAuditReport(unittest.TestCase):
    def test_to_dict(self) -> None:
        r = DependencyAuditReport(
            passed=False, scanner_used="pip-audit",
            requirements_file="requirements.txt",
            scanned_packages=5,
            vulnerabilities=[
                DependencyVulnerability(
                    package="x", installed_version="1.0",
                    fix_version="2.0", vuln_id="CVE-1", description="d",
                ),
            ],
        )
        d = r.to_dict()
        self.assertFalse(d["passed"])
        self.assertEqual(d["vulnerability_count"], 1)
        self.assertEqual(d["vulnerabilities"][0]["package"], "x")

    def test_summary_text(self) -> None:
        r = DependencyAuditReport(
            passed=True, scanner_used="pip-audit",
            requirements_file="requirements.txt",
            scanned_packages=10,
        )
        text = r.summary_text()
        self.assertIn("PASS", text)
        self.assertIn("pip-audit", text)


class TestFindRequirementsFile(unittest.TestCase):
    def test_finds_in_code_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            code_dir = os.path.join(td, "code")
            os.makedirs(code_dir)
            req_path = os.path.join(code_dir, "requirements.txt")
            with open(req_path, "w") as f:
                f.write("requests\n")
            found = _find_requirements_file(td)
            self.assertEqual(found, req_path)

    def test_finds_in_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            req_path = os.path.join(td, "requirements.txt")
            with open(req_path, "w") as f:
                f.write("flask\n")
            found = _find_requirements_file(td)
            self.assertEqual(found, req_path)

    def test_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_find_requirements_file(td))


class TestCountPackages(unittest.TestCase):
    def test_counts_non_comment_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "requirements.txt")
            with open(path, "w") as f:
                f.write("# comment\nrequests\nflask\n\n# another\ndjango\n")
            self.assertEqual(_count_packages(path), 3)


class TestParsePipAuditResults(unittest.TestCase):
    def test_parses_vulnerabilities(self) -> None:
        data = [
            {
                "name": "urllib3",
                "version": "1.25.0",
                "vulns": [
                    {
                        "id": "CVE-2023-1234",
                        "fix_versions": ["1.26.18"],
                        "description": "Header injection",
                    }
                ],
            },
            {
                "name": "requests",
                "version": "2.31.0",
                "vulns": [],
            },
        ]
        vulns = _parse_pip_audit_results(data)
        self.assertEqual(len(vulns), 1)
        self.assertEqual(vulns[0].package, "urllib3")
        self.assertEqual(vulns[0].vuln_id, "CVE-2023-1234")
        self.assertEqual(vulns[0].fix_version, "1.26.18")

    def test_empty_input(self) -> None:
        self.assertEqual(_parse_pip_audit_results([]), [])


class TestAuditDependencies(unittest.TestCase):
    def test_no_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = audit_dependencies(td)
            self.assertTrue(report.passed)
            self.assertEqual(report.scanner_used, "none")
            self.assertTrue(report.errors, "expected at least one error message")
            self.assertIn("No requirements.txt", report.errors[0])

    @pytest.mark.slow
    def test_persists_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Create requirements.txt
            with open(os.path.join(td, "requirements.txt"), "w") as f:
                f.write("requests\n")
            audit_dependencies(td)
            report_path = os.path.join(td, "dependency_audit_report.json")
            self.assertTrue(os.path.isfile(report_path))
            with open(report_path) as f:
                data = json.load(f)
            self.assertIn("passed", data)


if __name__ == "__main__":
    unittest.main()
