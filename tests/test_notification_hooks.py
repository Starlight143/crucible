# ruff: noqa: E402
"""Tests for crucible.features.notification_hooks."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crucible.features.notification_hooks import (
    NotificationReport,
    NotificationResult,
    _build_message,
    _load_run_summary,
    notify_run_complete,
)


class TestNotificationResult(unittest.TestCase):
    def test_default_values(self) -> None:
        r = NotificationResult(channel="webhook", sent=True, status_code=200)
        self.assertTrue(r.sent)
        self.assertEqual(r.error, "")

    def test_failed_result(self) -> None:
        r = NotificationResult(channel="slack", sent=False, status_code=500, error="timeout")
        self.assertFalse(r.sent)
        self.assertIn("timeout", r.error)


class TestNotificationReport(unittest.TestCase):
    def test_to_dict(self) -> None:
        r = NotificationReport(
            notifications_sent=1, notifications_failed=1,
            results=[
                NotificationResult(channel="webhook", sent=True, status_code=200),
                NotificationResult(channel="slack", sent=False, error="timeout"),
            ],
        )
        d = r.to_dict()
        self.assertEqual(d["notifications_sent"], 1)
        self.assertEqual(d["notifications_failed"], 1)
        self.assertEqual(len(d["results"]), 2)
        self.assertTrue(d["results"][0]["sent"])
        self.assertFalse(d["results"][1]["sent"])


class TestLoadRunSummary(unittest.TestCase):
    def test_loads_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            analysis = {"project_name": "test_proj", "score": 85}
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump(analysis, f)
            result = _load_run_summary(td)
            self.assertEqual(result["project_name"], "test_proj")
            self.assertEqual(result["score"], 85)

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(_load_run_summary(td), {})


class TestBuildMessage(unittest.TestCase):
    def test_success_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            analysis = {"project_name": "myproject", "score": 74, "risk_level": "Medium"}
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump(analysis, f)
            msg = _build_message(td, success=True)
            self.assertIn("SUCCESS", msg["text"])
            self.assertIn("myproject", msg["text"])
            self.assertIn("74/100", msg["text"])
            self.assertEqual(msg["status"], "SUCCESS")

    def test_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            msg = _build_message(td, success=False)
            self.assertIn("FAILURE", msg["text"])

    def test_security_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "analysis_result.json"), "w") as f:
                json.dump({"project_name": "x"}, f)
            with open(os.path.join(td, "security_report.json"), "w") as f:
                json.dump({"passed": False}, f)
            msg = _build_message(td, success=True)
            self.assertIn("FAIL", msg["text"])
            self.assertFalse(msg["security_passed"])


class TestNotifyRunComplete(unittest.TestCase):
    def test_no_urls_configured(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Ensure no webhook env vars
            env_backup = {}
            for key in ("NOTIFY_WEBHOOK_URL", "NOTIFY_SLACK_WEBHOOK_URL",
                        "NOTIFY_DISCORD_WEBHOOK_URL", "NOTIFY_ON_FAIL_ONLY"):
                env_backup[key] = os.environ.pop(key, None)
            try:
                report = notify_run_complete(td)
                self.assertEqual(report.notifications_sent, 0)
                self.assertEqual(report.notifications_failed, 0)
                self.assertEqual(len(report.results), 0)
            finally:
                for key, val in env_backup.items():
                    if val is not None:
                        os.environ[key] = val
                    else:
                        os.environ.pop(key, None)

    def test_fail_only_skips_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # Back up and clear all webhook URL env vars to prevent real HTTP calls
            url_keys = [
                "NOTIFY_ON_FAIL_ONLY",
                "NOTIFY_WEBHOOK_URL",
                "NOTIFY_SLACK_WEBHOOK_URL",
                "NOTIFY_DISCORD_WEBHOOK_URL",
            ]
            env_backup = {k: os.environ.pop(k, None) for k in url_keys}
            os.environ["NOTIFY_ON_FAIL_ONLY"] = "1"
            try:
                report = notify_run_complete(td, success=True)
                self.assertEqual(report.notifications_sent, 0)
            finally:
                for key, val in env_backup.items():
                    if val is not None:
                        os.environ[key] = val
                    else:
                        os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
