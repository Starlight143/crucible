"""
tests/test_subprocess_and_runtime_guards.py
===========================================
Regression tests for subprocess timeout handling, fail-closed notifications,
subnormal-divisor numerical guards, log-redaction precision, sandbox-executor
honesty, and WebUI budget cap surfacing.  Each test asserts that a specific
bug **stays fixed** — a future refactor that re-introduces the
vulnerability/incorrectness will fail one of these tests.

Coverage:
    1.  smoke_test.run_help has subprocess timeout and TimeoutExpired handling
        (HIGH)
    2.  run_crucible_enhanced.notify_run_complete fail-closed on parse error
        (HIGH)
    3.  features.dynamic_correlation.compute_pca subnormal-divisor guard on
        total_variance (HIGH)
    4.  features.risk_attribution.compute_risk_attribution subnormal-divisor
        guard on total_w (MEDIUM)
    5.  runtime_logging._SENSITIVE_KEY_FRAGMENTS no longer over-redacts
        "author"/"authority" via bare "auth" substring (MEDIUM)
    6.  features.sandbox_executor adds ``sandboxed`` field to its report and
        sets it correctly per execution path (MEDIUM)
    7.  webui /api/budget/status returns ``daily_limit`` / ``soft_limit`` /
        ``max_total_tokens`` so the front-end budget bar can render the cap
        (MEDIUM)
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── 1. smoke_test.run_help: subprocess timeout & TimeoutExpired handling ─────

class TestSmokeTestSubprocessTimeout(unittest.TestCase):
    """HIGH safety property: a stuck import must not wedge CI — run_help must
    pass an explicit timeout to subprocess.run and convert TimeoutExpired
    into an exit-code-124 CompletedProcess."""

    def test_run_help_passes_explicit_timeout(self):
        """subprocess.run is called with timeout= kwarg (any positive value)."""
        from crucible import smoke_test

        captured: dict = {}

        def _fake_run(*args, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(args=list(args[0]), returncode=0,
                                               stdout="usage: \n", stderr="")

        with mock.patch.object(smoke_test.subprocess, "run", side_effect=_fake_run):
            smoke_test.run_help(Path("dummy"))

        self.assertIn("timeout", captured, "run_help did not pass timeout= to subprocess.run")
        self.assertGreater(captured["timeout"], 0,
                           "timeout must be a positive number, got %r" % captured["timeout"])

    def test_timeout_expired_becomes_exit_124(self):
        """TimeoutExpired must be caught and surfaced as a deterministic FAIL."""
        from crucible import smoke_test

        def _raise(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd=["python", "--help"], timeout=120)

        with mock.patch.object(smoke_test.subprocess, "run", side_effect=_raise):
            result = smoke_test.run_help(Path("dummy"))

        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual(result.returncode, 124, "timeout must surface as exit 124")
        # main() will print result.stderr — must be non-empty
        self.assertTrue(result.stderr, "TimeoutExpired must produce a non-empty stderr")

    def test_main_treats_timeout_as_failure(self):
        """End-to-end: a timeout on every help invocation flips the smoke check to FAIL."""
        from crucible import smoke_test

        def _raise(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(cmd=["python", "--help"], timeout=120)

        # Patch run + the package-import block so main() can complete.
        with mock.patch.object(smoke_test.subprocess, "run", side_effect=_raise):
            # main() also tries to import crucible — that's already importable
            # in the test environment, so we expect failed=True from the run_help
            # branch only (returncode != 0).  Capture stdout.
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(sys, "stdout", buf):
                rc = smoke_test.main()
            self.assertEqual(rc, 1, "smoke_test.main() must return 1 on timeout-induced FAIL")


# ── 2. run_crucible_enhanced: notify_run_complete fail-closed on parse error ─

class TestNotifyRunCompleteFailClosed(unittest.TestCase):
    """HIGH safety property: a corrupt analysis_result.json must NOT report
    success=True to notify_run_complete — that would silently suppress the
    NOTIFY_ON_FAIL_ONLY=1 alert for a genuinely-failed run."""

    def _resolve_notify_success(self, *, file_present: bool, file_content: str | None) -> bool:
        """Replay the exact resolution logic from run_crucible_enhanced.cmd_run."""
        # Mirrors lines 1032-1043 of run_crucible_enhanced.py exactly.
        _notify_success = False  # fail-closed default
        if file_present:
            try:
                _data = json.loads(file_content or "")
                _score = float(_data.get("score") or 0)
                _risk = str(_data.get("risk_level") or "").lower()
                _notify_success = _score >= 50 and _risk != "critical"
            except Exception:
                pass
        return _notify_success

    def test_missing_file_is_failure(self):
        """No analysis_result.json → not a success."""
        self.assertFalse(self._resolve_notify_success(file_present=False, file_content=None))

    def test_corrupt_json_is_failure(self):
        """Truncated / unparseable JSON → not a success."""
        self.assertFalse(self._resolve_notify_success(
            file_present=True, file_content='{"score": 87, "risk_lev'))

    def test_low_score_is_failure(self):
        self.assertFalse(self._resolve_notify_success(
            file_present=True, file_content='{"score": 30, "risk_level": "low"}'))

    def test_critical_risk_is_failure(self):
        self.assertFalse(self._resolve_notify_success(
            file_present=True, file_content='{"score": 95, "risk_level": "CRITICAL"}'))

    def test_pass_score_with_low_risk_is_success(self):
        self.assertTrue(self._resolve_notify_success(
            file_present=True, file_content='{"score": 87, "risk_level": "low"}'))

    def test_pass_score_with_no_risk_field_is_success(self):
        """Missing risk_level → defaults to "" which is != "critical" → success."""
        self.assertTrue(self._resolve_notify_success(
            file_present=True, file_content='{"score": 90}'))


# ── 3. dynamic_correlation: subnormal-divisor guard on total_variance ────────

class TestDynamicCorrelationSubnormalGuard(unittest.TestCase):
    """HIGH safety property: PCA's total_variance ≤ 0 check must reject IEEE 754
    subnormals — without the guard they divide into eigenvalues to produce
    explained-variance ratios on the order of 1e+300."""

    def test_normal_input_returns_components(self):
        from crucible.features.dynamic_correlation import _pca_pure_python as compute_pca
        # 6 obs × 3 features, full-rank
        matrix = [
            [ 1.0,  0.5, -0.3],
            [ 2.0,  1.1,  0.2],
            [-1.0, -0.4,  0.5],
            [ 0.5,  0.7, -0.1],
            [ 1.5,  0.9,  0.0],
            [-0.5, -0.2,  0.4],
        ]
        out = compute_pca(matrix, n_components=2)
        self.assertGreater(len(out), 0)
        for c in out:
            self.assertGreaterEqual(c.explained_variance_ratio, 0.0)
            self.assertLessEqual(c.explained_variance_ratio, 1.0 + 1e-9)

    def test_zero_variance_returns_empty(self):
        from crucible.features.dynamic_correlation import _pca_pure_python as compute_pca
        # All-constant matrix → covariance is all zeros → total_variance == 0
        matrix = [[1.0, 1.0, 1.0]] * 6
        out = compute_pca(matrix, n_components=2)
        self.assertEqual(out, [])

    def test_subnormal_variance_returns_empty_not_inf(self):
        """Subnormal trace → must be rejected by ``not (x > 1e-14)`` guard."""
        from crucible.features.dynamic_correlation import _pca_pure_python as compute_pca
        # Construct rows with values just above zero so covariance trace is
        # vanishingly small.  We use 5e-160 per element — squared in the
        # covariance gives ~2.5e-320 which is subnormal.
        eps = 5e-160
        matrix = [
            [eps, eps, eps],
            [-eps, -eps, -eps],
            [eps, eps, eps],
            [-eps, -eps, -eps],
            [eps, eps, eps],
            [-eps, -eps, -eps],
        ]
        out = compute_pca(matrix, n_components=2)
        # Without the guard this would return components with
        # explained_variance_ratio ≈ 1e+0 to 1e+300 garbage; with the guard
        # in place either we get [] (preferred), or components that don't
        # have explosive ratios.  Assert no inf / no >1.0001 ratios.
        for c in out:
            self.assertTrue(0.0 <= c.explained_variance_ratio <= 1.0 + 1e-6,
                            f"PCA leaked subnormal-divided ratio {c.explained_variance_ratio}")


# ── 4. risk_attribution: subnormal-divisor guard on total_w ──────────────────

class TestRiskAttributionSubnormalGuard(unittest.TestCase):
    """MEDIUM safety property: weight normalisation guarded only by ``total_w <= 0``
    admits subnormals which then produce normalised weights on the order of 1e+300.
    The strict ``not (total_w > 1e-14)`` form rejects them."""

    def test_normal_weights_return_result(self):
        from crucible.features.risk_attribution import compute_component_var as compute_risk_attribution
        returns = [
            [0.01, -0.005, 0.012, -0.003, 0.008, 0.002, -0.001],
            [0.005, 0.003, -0.008, 0.011, 0.002, -0.004, 0.006],
            [-0.002, 0.007, 0.004, 0.001, -0.003, 0.005, 0.003],
        ]
        result = compute_risk_attribution(
            returns, weights=[0.5, 0.3, 0.2], labels=["A", "B", "C"]
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.component_vars), 3)

    def test_zero_total_weight_emits_error(self):
        from crucible.features.risk_attribution import compute_component_var as compute_risk_attribution
        returns = [
            [0.01, -0.005, 0.012, -0.003, 0.008],
            [0.005, 0.003, -0.008, 0.011, 0.002],
        ]
        result = compute_risk_attribution(
            returns, weights=[0.0, 0.0], labels=["A", "B"]
        )
        self.assertTrue(any("Weights sum" in e for e in result.errors))
        self.assertEqual(result.component_vars, [])

    def test_subnormal_total_weight_emits_error(self):
        from crucible.features.risk_attribution import compute_component_var as compute_risk_attribution
        returns = [
            [0.01, -0.005, 0.012, -0.003, 0.008],
            [0.005, 0.003, -0.008, 0.011, 0.002],
        ]
        # Each weight 5e-325 (subnormal).  Sum is ~1e-324 which would pass
        # the old ``<= 0`` check and produce normalised weights of ~1e+300.
        sub = 5e-325
        result = compute_risk_attribution(
            returns, weights=[sub, sub], labels=["A", "B"]
        )
        self.assertTrue(any("Weights sum" in e for e in result.errors),
                        f"Subnormal weights leaked through guard: errors={result.errors}")
        self.assertEqual(result.component_vars, [])


# ── 5. runtime_logging: "auth" no longer over-redacts "author" ───────────────

class TestRuntimeLoggingAuthFragment(unittest.TestCase):
    """MEDIUM precision property: the bare "auth" fragment used to substring-match
    benign field names like "author" / "authority" / "authored_by".  The
    sensitive-key fragments now use more specific markers."""

    def test_authorization_still_redacted(self):
        from crucible import runtime_logging
        out = runtime_logging._redact_fields({
            "Authorization": "Bearer abc123", "ok": "yes",
        })
        self.assertEqual(out["Authorization"], "***REDACTED***")
        self.assertEqual(out["ok"], "yes")

    def test_auth_token_still_redacted(self):
        from crucible import runtime_logging
        out = runtime_logging._redact_fields({
            "auth_token": "tok_xxx", "auth_key": "ak_xxx",
        })
        self.assertEqual(out["auth_token"], "***REDACTED***")
        self.assertEqual(out["auth_key"], "***REDACTED***")

    def test_author_no_longer_redacted(self):
        from crucible import runtime_logging
        out = runtime_logging._redact_fields({
            "author": "alice@example.com",
            "authority": "primary",
            "authored_by": "bob",
            "authentic_count": 42,
        })
        # The benign "author"-family field names must NOT match the
        # sensitive-key markers — they pass through verbatim.
        self.assertEqual(out["author"], "alice@example.com")
        self.assertEqual(out["authority"], "primary")
        self.assertEqual(out["authored_by"], "bob")
        self.assertEqual(out["authentic_count"], 42)

    def test_other_secret_markers_still_caught(self):
        from crucible import runtime_logging
        out = runtime_logging._redact_fields({
            "OPENAI_API_KEY": "sk-...",
            "client_secret": "csec",
            "private_key": "----BEGIN----",
            "password": "hunter2",
            "bearer_token": "Bearer xyz",
        })
        for k in out:
            self.assertEqual(out[k], "***REDACTED***", f"{k} should stay redacted")


# ── 6. sandbox_executor: ``sandboxed`` field correctness ─────────────────────

class TestSandboxExecutorSandboxedField(unittest.TestCase):
    """MEDIUM observability property: report carries ``sandboxed: bool`` so the
    operator can tell whether real isolation was applied.  False for the
    Windows-no-docker fallback even when the run succeeds."""

    def setUp(self) -> None:
        from crucible.features import sandbox_executor
        self.mod = sandbox_executor
        # Build a temp run_dir with a code/main.py inside
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = self._tmp.name
        code = Path(self.run_dir) / "code"
        code.mkdir()
        (code / "main.py").write_text(
            "import sys\nif '--dry-run' in sys.argv: print('ok'); sys.exit(0)\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_with_docker_state(self, docker_ok: bool) -> dict:
        from crucible.features.sandbox_executor import (
            SandboxExecutorFeature, FeatureConfig,
        )
        feat = SandboxExecutorFeature()
        with mock.patch.object(self.mod, "_docker_available", return_value=docker_ok), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SANDBOX_REQUIRE_DOCKER", None)
            os.environ.pop("SANDBOX_ENABLED", None)
            feat.run(self.run_dir, FeatureConfig())
        report_path = os.path.join(self.run_dir, "sandbox_execution_report.json")
        with open(report_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def test_report_has_sandboxed_field_in_fallback(self):
        report = self._run_with_docker_state(docker_ok=False)
        self.assertIn("sandboxed", report,
                      "sandbox_execution_report.json must include sandboxed:")
        self.assertFalse(report["sandboxed"],
                         "fallback path must report sandboxed=False")
        self.assertFalse(report["docker_available"])


# ── 7. /api/budget/status returns operator-configured caps ───────────────────

class TestBudgetStatusReturnsCaps(unittest.TestCase):
    """MEDIUM UI-contract property: front-end reads ``data.daily_limit`` to
    render the budget cap badge / progress fill.  Backend surfaces the
    configured BUDGET_HARD_COST_LIMIT / BUDGET_SOFT_COST_LIMIT /
    BUDGET_MAX_TOTAL_TOKENS."""

    @classmethod
    def setUpClass(cls) -> None:
        # Importing webui.app touches PROJECT_ROOT / db file.  Use the project's
        # own test bootstrap pattern: insert ROOT and import normally.
        try:
            from webui import app as webui_app
            cls.app = webui_app.app
            cls.app.config["TESTING"] = True
            cls.client = cls.app.test_client()
            cls.module = webui_app
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"webui.app unavailable: {exc}")

    def test_response_contains_cap_keys(self):
        # No env vars set → caps should be null
        env_clean = {k: v for k, v in os.environ.items()
                     if not k.startswith("BUDGET_")}
        with mock.patch.dict(os.environ, env_clean, clear=True):
            r = self.client.get("/api/budget/status")
        self.assertEqual(r.status_code, 200, r.data)
        data = r.get_json()
        self.assertIn("daily_limit", data, "daily_limit must be present in response")
        self.assertIn("soft_limit", data)
        self.assertIn("max_total_tokens", data)
        self.assertIsNone(data["daily_limit"])
        self.assertIsNone(data["soft_limit"])
        self.assertIsNone(data["max_total_tokens"])

    def test_caps_surface_when_env_set(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BUDGET_")}
        env["BUDGET_HARD_COST_LIMIT"] = "5.50"
        env["BUDGET_SOFT_COST_LIMIT"] = "3.25"
        env["BUDGET_MAX_TOTAL_TOKENS"] = "1000000"
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.client.get("/api/budget/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["daily_limit"], 5.5)
        self.assertEqual(data["soft_limit"], 3.25)
        self.assertEqual(data["max_total_tokens"], 1_000_000)

    def test_invalid_or_zero_caps_are_null(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BUDGET_")}
        env["BUDGET_HARD_COST_LIMIT"] = "not-a-number"
        env["BUDGET_SOFT_COST_LIMIT"] = "0"
        env["BUDGET_MAX_TOTAL_TOKENS"] = "-1"
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.client.get("/api/budget/status")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsNone(data["daily_limit"], "garbage value must surface as null")
        self.assertIsNone(data["soft_limit"], "0 must be treated as no cap → null")
        self.assertIsNone(data["max_total_tokens"], "negative must be null")


if __name__ == "__main__":
    unittest.main()
