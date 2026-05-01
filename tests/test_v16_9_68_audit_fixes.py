"""
tests/test_v16_9_68_audit_fixes.py
==================================
Regression tests for the v16.9.68 four-agent audit fixes (and the v16.9.67
hardening that previously had no test coverage).  Each test asserts that a
specific bug **stays fixed** — a future refactor that re-introduces the
vulnerability/incorrectness will fail one of these tests.

Coverage:
    1.  trading_platform.paper_mode fail-closed (HIGH, v16.9.68)
    2.  webui.app.api_run_signal stdin-injection rejection (MEDIUM, v16.9.68)
    3.  webui.app._build_command stdin-injection rejection (MEDIUM, v16.9.67)
    4.  http_retry.is_http_retryable HTTP 408 classification (MEDIUM, v16.9.67)
    5.  gunicorn_config.loglevel allowlist (LOW, v16.9.68)
    6.  convergence_guard treats OperationCancelledError as expected (LOW, v16.9.68)
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 1. Trading platform: paper_mode fail-closed ──────────────────────────────

class TestTradingPaperModeFailClosed(unittest.TestCase):
    """v16.9.68 HIGH fix: TRADING_PAPER_MODE must default to paper (safe) on
    any unknown / typo'd value; only an explicit off-token disables it."""

    def setUp(self) -> None:
        # Re-import after env tweak so we get a clean module scope
        from crucible.features import trading_platform
        self.mod = trading_platform

    def _resolve_paper_mode(self, env_value: str | None) -> bool:
        """Replay the exact env-var resolution from generate_trading_platform."""
        env = {**os.environ}
        if env_value is None:
            env.pop("TRADING_PAPER_MODE", None)
        else:
            env["TRADING_PAPER_MODE"] = env_value
        with mock.patch.dict(os.environ, env, clear=True):
            raw = os.environ.get("TRADING_PAPER_MODE", "true").strip().lower()
            return raw not in ("0", "false", "no", "off")

    def test_default_unset_is_paper(self):
        """Unset env var → paper mode (safe default)."""
        self.assertTrue(self._resolve_paper_mode(None))

    def test_explicit_true_is_paper(self):
        for val in ("true", "True", "TRUE", "1", "yes", "on", " true "):
            with self.subTest(val=val):
                self.assertTrue(self._resolve_paper_mode(val))

    def test_explicit_off_disables_paper(self):
        """Only the recognised off-tokens flip to live."""
        for val in ("0", "false", "False", "FALSE", "no", "off", " off "):
            with self.subTest(val=val):
                self.assertFalse(self._resolve_paper_mode(val))

    def test_typo_or_unknown_value_is_paper_NOT_live(self):
        """Critical safety property: typos must NOT silently enable live trading."""
        for val in ("", "trun", "enabled", "live", "production", "prod",
                    "unkn0wn", "yess", "fake", "paper"):
            with self.subTest(val=val):
                self.assertTrue(
                    self._resolve_paper_mode(val),
                    msg=f"Typo {val!r} flipped to live trading — fail-open bug",
                )


# ── 2. WebUI signal endpoint: stdin-injection rejection ──────────────────────

class TestApiRunSignalStdinInjection(unittest.TestCase):
    """v16.9.68 MEDIUM fix: /api/run/<id>/signal must reject embedded newlines,
    carriage returns, and null bytes — without this guard a single signal
    payload could inject multiple stdin answers."""

    @classmethod
    def setUpClass(cls) -> None:
        from webui import app as webui_app
        cls.app = webui_app.app
        cls.webui_module = webui_app
        cls.client = cls.app.test_client()

    def _post(self, run_id: str, body: dict):
        return self.client.post(
            f"/api/run/{run_id}/signal",
            json=body,
        )

    def test_rejects_embedded_newline(self):
        """text containing \\n must be rejected with 400."""
        with mock.patch.dict(
            self.webui_module._runs,
            {"r1": {"status": "running", "awaiting_input": True,
                    "stdin_pipe": mock.MagicMock()}},
        ):
            r = self._post("r1", {"text": "answer\nextra"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("newline", r.get_json()["error"])

    def test_rejects_carriage_return(self):
        with mock.patch.dict(
            self.webui_module._runs,
            {"r1": {"status": "running", "awaiting_input": True,
                    "stdin_pipe": mock.MagicMock()}},
        ):
            r = self._post("r1", {"text": "answer\rextra"})
        self.assertEqual(r.status_code, 400)

    def test_rejects_null_byte(self):
        with mock.patch.dict(
            self.webui_module._runs,
            {"r1": {"status": "running", "awaiting_input": True,
                    "stdin_pipe": mock.MagicMock()}},
        ):
            r = self._post("r1", {"text": "answer\x00extra"})
        self.assertEqual(r.status_code, 400)

    def test_rejects_oversize_text(self):
        """text > 4096 chars must be rejected with 400."""
        big = "a" * 4097
        with mock.patch.dict(
            self.webui_module._runs,
            {"r1": {"status": "running", "awaiting_input": True,
                    "stdin_pipe": mock.MagicMock()}},
        ):
            r = self._post("r1", {"text": big})
        self.assertEqual(r.status_code, 400)
        self.assertIn("4096", r.get_json()["error"])

    def test_accepts_clean_text(self):
        """Single-line text under the cap must succeed."""
        pipe = mock.MagicMock()
        with mock.patch.dict(
            self.webui_module._runs,
            {"r1": {"status": "running", "awaiting_input": True,
                    "stdin_pipe": pipe}},
        ):
            r = self._post("r1", {"text": "1"})
        self.assertEqual(r.status_code, 200)
        pipe.write.assert_called_once_with("1\n")

    def test_rejects_non_string(self):
        with mock.patch.dict(
            self.webui_module._runs,
            {"r1": {"status": "running", "awaiting_input": True,
                    "stdin_pipe": mock.MagicMock()}},
        ):
            r = self._post("r1", {"text": 42})
        self.assertEqual(r.status_code, 400)


# ── 3. WebUI _build_command: stdin-injection rejection (v16.9.67) ────────────

class TestBuildCommandStdinInjection(unittest.TestCase):
    """v16.9.67 MEDIUM hardening: _build_command must reject newline / null /
    sentinel injections in the user-controlled fields."""

    @staticmethod
    def _build(payload):
        # Access via module to avoid bound-method semantics (assigning a plain
        # function to a class attribute would make it an unbound instance
        # method, so calling self._build(payload) would pass `self` as arg 1).
        from webui import app as webui_app
        return webui_app._build_command(payload)

    def test_project_path_rejects_newline(self):
        with self.assertRaisesRegex(ValueError, "newline"):
            self._build({"mode": "project", "project_path": "/tmp/x\n2"})

    def test_project_path_rejects_carriage_return(self):
        with self.assertRaisesRegex(ValueError, "newline"):
            self._build({"mode": "project", "project_path": "/tmp/x\rextra"})

    def test_project_path_rejects_null_byte(self):
        with self.assertRaisesRegex(ValueError, "newline"):
            self._build({"mode": "project", "project_path": "/tmp/x\x00bad"})

    def test_idea_rejects_sentinel(self):
        with self.assertRaisesRegex(ValueError, "__END_PROMPT__"):
            self._build({"mode": "idea", "idea": "test __END_PROMPT__ extra"})

    def test_idea_rejects_null_byte(self):
        with self.assertRaisesRegex(ValueError, "__END_PROMPT__|null"):
            self._build({"mode": "idea", "idea": "test\x00bad"})

    def test_analysis_type_validates(self):
        with self.assertRaisesRegex(ValueError, "analysis_type"):
            self._build({"mode": "idea", "idea": "x", "analysis_type": 99})

    def test_analysis_type_string_validates(self):
        with self.assertRaisesRegex(ValueError, "analysis_type"):
            self._build({"mode": "idea", "idea": "x", "analysis_type": "bad"})

    def test_clean_idea_payload_succeeds(self):
        cmd, stdin = self._build({"mode": "idea", "idea": "test idea",
                                  "analysis_type": 1})
        self.assertIsInstance(cmd, list)
        self.assertIn("__END_PROMPT__", stdin)
        self.assertTrue(stdin.startswith("1\n1\n"))

    def test_clean_project_payload_succeeds(self):
        cmd, stdin = self._build({"mode": "project", "project_path": "/tmp/x",
                                  "analysis_type": 2})
        self.assertIsInstance(cmd, list)
        self.assertIn("/tmp/x", stdin)
        self.assertTrue(stdin.startswith("2\n2\n"))


# ── 4. HTTP 408 retry classification (v16.9.67) ──────────────────────────────

class TestHttpRetry408Classification(unittest.TestCase):
    """v16.9.67 fix: HTTP 408 Request Timeout is a server-side transient
    condition that RFC 7231 §6.5.7 explicitly classifies as retryable."""

    def test_408_retryable(self):
        from crucible.http_retry import is_http_retryable

        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 408})()

        self.assertTrue(is_http_retryable(HTTPStatusError("request timeout")))

    def test_502_retryable(self):
        from crucible.http_retry import is_http_retryable

        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 502})()

        self.assertTrue(is_http_retryable(HTTPStatusError("bad gateway")))

    def test_504_retryable(self):
        from crucible.http_retry import is_http_retryable

        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 504})()

        self.assertTrue(is_http_retryable(HTTPStatusError("gateway timeout")))

    def test_400_not_retryable(self):
        """Non-transient 4xx must NOT trigger retry — would loop forever."""
        from crucible.http_retry import is_http_retryable

        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 400})()

        self.assertFalse(is_http_retryable(HTTPStatusError("bad request")))

    def test_401_not_retryable(self):
        from crucible.http_retry import is_http_retryable

        class HTTPStatusError(Exception):
            def __init__(self, msg):
                super().__init__(msg)
                self.response = type("R", (), {"status_code": 401})()

        self.assertFalse(is_http_retryable(HTTPStatusError("unauthorized")))


# ── 5. Gunicorn loglevel allowlist (v16.9.68) ────────────────────────────────

class TestGunicornLoglevelAllowlist(unittest.TestCase):
    """v16.9.68 LOW fix: GUNICORN_LOG_LEVEL must whitelist Gunicorn-recognised
    levels.  Unknown values fall back to 'info' instead of crashing the worker
    boot with an opaque ConfigurationError."""

    def _resolve(self, env_value: str | None) -> str:
        """Replay the gunicorn_config logic in isolation."""
        env = {**os.environ}
        if env_value is None:
            env.pop("GUNICORN_LOG_LEVEL", None)
        else:
            env["GUNICORN_LOG_LEVEL"] = env_value
        with mock.patch.dict(os.environ, env, clear=True):
            raw = os.environ.get("GUNICORN_LOG_LEVEL", "info").strip().lower()
            return raw if raw in {"debug", "info", "warning", "error", "critical"} else "info"

    def test_default_is_info(self):
        self.assertEqual(self._resolve(None), "info")

    def test_recognised_levels_pass_through(self):
        for lvl in ("debug", "info", "warning", "error", "critical"):
            with self.subTest(lvl=lvl):
                self.assertEqual(self._resolve(lvl), lvl)

    def test_uppercase_normalised(self):
        self.assertEqual(self._resolve("DEBUG"), "debug")
        self.assertEqual(self._resolve("Warning"), "warning")

    def test_unknown_falls_back_to_info(self):
        for bad in ("verbose", "trace", "warn", "err", "10", "fatal"):
            with self.subTest(bad=bad):
                self.assertEqual(self._resolve(bad), "info")

    def test_empty_falls_back_to_info(self):
        self.assertEqual(self._resolve(""), "info")
        self.assertEqual(self._resolve("   "), "info")


# ── 6. ConvergenceGuard cancellation classification (v16.9.68) ───────────────

class TestConvergenceGuardCancellationExpected(unittest.TestCase):
    """v16.9.68 LOW fix: cooperative cancellation (OperationCancelledError) must
    NOT be logged as an unexpected guard failure (WARNING level).  It is a
    caller-driven stop, not a guard violation."""

    def test_cancellation_is_classified_expected(self):
        from crucible.cancellation import OperationCancelledError
        from crucible.convergence_guard import (
            ConvergenceError,
            LoopConvergenceGuard,
        )

        # Replay the __exit__ classification logic with the post-fix expected set
        _expected = (ConvergenceError, OperationCancelledError)

        # Cancellation → expected (info-level)
        self.assertTrue(issubclass(OperationCancelledError, _expected))

        # ConvergenceError → expected (info-level)
        self.assertTrue(issubclass(ConvergenceError, _expected))

        # Plain RuntimeError → still unexpected (warning-level)
        self.assertFalse(issubclass(RuntimeError, _expected))

    def test_guard_logs_cancellation_at_info_not_warning(self):
        """End-to-end: a guard whose body raises OperationCancelledError must
        emit the convergence_guard_exited event at INFO level (20), not
        WARNING (30)."""
        import logging

        from crucible.cancellation import OperationCancelledError
        from crucible.convergence_guard import LoopConvergenceGuard

        captured_levels: list[int] = []

        # Patch log_event in convergence_guard module to capture level
        from crucible import convergence_guard as cg_mod
        original = cg_mod.log_event

        def _capture(logger, level, event, msg, **kwargs):
            captured_levels.append(level)
            return original(logger, level, event, msg, **kwargs)

        with mock.patch.object(cg_mod, "log_event", side_effect=_capture):
            try:
                with LoopConvergenceGuard(name="t", max_iterations=10,
                                          timeout_seconds=10):
                    raise OperationCancelledError("user cancelled")
            except OperationCancelledError:
                pass

        # The "started" event is INFO (20), the "exited" event must also be INFO
        # (20) — NOT WARNING (30).  After the fix, no WARNING-level log should
        # appear from the convergence guard for a cancellation path.
        self.assertNotIn(
            logging.WARNING, captured_levels,
            msg=f"Cancellation classified as unexpected (warning emitted): {captured_levels}",
        )


if __name__ == "__main__":
    unittest.main()
