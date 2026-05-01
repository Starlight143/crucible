"""Regression tests for v16.9.74 — POST /api/env hot-reload into ``os.environ``.

User-reported gap. v16.9.73 fixed the *startup* load of ``.env``: when the
WebUI boots, ``_load_dotenv_into_webui_process`` now populates
``os.environ`` from the file before any reader runs.  Operators then
flagged a separate hole: changing settings *via the WebUI* and clicking
Save wrote the new values to ``.env`` on disk but the running process
still held the previous values until the operator killed the WebUI and
re-launched.  The next pipeline subprocess inherits ``os.environ`` from
the WebUI parent (``_run_worker``'s ``_child_env = {**os.environ, ...}``)
— so a Save without a restart had no observable effect.

This test module exercises the ``_apply_env_to_process`` hook that now
mirrors saved key/values into ``os.environ`` immediately after each
successful POST /api/env, ensuring:

1. Existing keys are overwritten with the freshly-saved values.
2. Brand-new keys are inserted into ``os.environ``.
3. Empty-string values are written as-is (matching how python-dotenv
   would treat ``KEY=""`` with ``override=True``).
4. ``CRUCIBLE_LOG_LEVEL`` propagates to ``app.logger.level`` so the
   WebUI's own structured-log output respects the change without a
   restart.
5. Validation errors at the input stage abort *before* any state mutation
   — neither the file nor ``os.environ`` may be touched on a 400.
6. A ``_save_env`` failure aborts *before* ``os.environ`` is mutated, so
   the in-process state never runs ahead of the on-disk file.
7. End-to-end: a successful POST /api/env mutates ``os.environ`` exactly
   once, with the same payload that hit the file.
"""

from __future__ import annotations

import logging
import os
import unittest
from unittest import mock


# Module-level import so every test class shares a single Flask app +
# test-client pair — instantiating ``Flask`` in setUp is expensive and
# pointless when the routes are stateless.
from webui import app as webui_app  # type: ignore[import]


# ── 1. _apply_env_to_process: direct unit tests ──────────────────────────────


class TestApplyEnvToProcess(unittest.TestCase):
    """Direct tests for the helper that pushes saved key/values into
    ``os.environ``.  These must run with a clean per-test environment so
    parallel pytest workers cannot leak state into one another."""

    def setUp(self) -> None:
        # ``mock.patch.dict(os.environ, clear=False)`` snapshots the
        # current environment, lets the test mutate it, then restores
        # the snapshot in tearDown.  Critically ``clear=False`` preserves
        # PATH / TEMP / etc. so subprocess-spawning side effects in
        # other modules don't crash mid-test.
        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def test_overwrites_existing_key(self):
        """When ``os.environ`` already has the key, ``_apply_env_to_process``
        must replace its value with the saved one — this is the core
        behaviour that fixes the user's reported bug."""
        os.environ["CRUCIBLE_TEST_OVERWRITE"] = "old"
        webui_app._apply_env_to_process({"CRUCIBLE_TEST_OVERWRITE": "new"})
        self.assertEqual(os.environ["CRUCIBLE_TEST_OVERWRITE"], "new")

    def test_inserts_new_key(self):
        """A key absent from ``os.environ`` must be inserted, mirroring
        python-dotenv with ``override=True``."""
        os.environ.pop("CRUCIBLE_TEST_INSERT", None)
        webui_app._apply_env_to_process({"CRUCIBLE_TEST_INSERT": "fresh"})
        self.assertEqual(os.environ["CRUCIBLE_TEST_INSERT"], "fresh")

    def test_empty_value_stored_verbatim(self):
        """Empty-string values must be written as ``""`` — every existing
        consumer in this codebase already pattern-matches ``os.environ.get
        (..., "").strip()`` followed by ``if not raw:`` so an empty string
        is treated identically to an absent key by every reader."""
        os.environ["CRUCIBLE_TEST_EMPTY"] = "previous"
        webui_app._apply_env_to_process({"CRUCIBLE_TEST_EMPTY": ""})
        self.assertEqual(os.environ["CRUCIBLE_TEST_EMPTY"], "")

    def test_multiple_keys_atomic(self):
        """A multi-key payload must apply *every* key — no partial writes
        even when one of the values is empty."""
        os.environ.pop("CRUCIBLE_TEST_A", None)
        os.environ.pop("CRUCIBLE_TEST_B", None)
        os.environ.pop("CRUCIBLE_TEST_C", None)
        webui_app._apply_env_to_process({
            "CRUCIBLE_TEST_A": "alpha",
            "CRUCIBLE_TEST_B": "",
            "CRUCIBLE_TEST_C": "gamma",
        })
        self.assertEqual(os.environ["CRUCIBLE_TEST_A"], "alpha")
        self.assertEqual(os.environ["CRUCIBLE_TEST_B"], "")
        self.assertEqual(os.environ["CRUCIBLE_TEST_C"], "gamma")

    def test_skips_non_string_values_silently(self):
        """Validation in ``api_set_env`` already rejects non-string values
        with a 400, so this branch is purely defensive — but if a future
        caller bypasses the route and passes a non-string, the helper
        must not crash."""
        os.environ["CRUCIBLE_TEST_NONSTR"] = "preserved"
        webui_app._apply_env_to_process({
            "CRUCIBLE_TEST_NONSTR": 42,            # type: ignore[dict-item]
            "CRUCIBLE_TEST_NONSTR_OK": "applied",
        })
        # The integer was skipped, so the previous value remains.
        self.assertEqual(os.environ["CRUCIBLE_TEST_NONSTR"], "preserved")
        # The valid string was still applied — partial-success semantics
        # match what the helper does with non-string values.
        self.assertEqual(os.environ["CRUCIBLE_TEST_NONSTR_OK"], "applied")

    def test_skips_non_string_keys_silently(self):
        """Same rationale as the value-type test, for keys.  We assert
        the helper does not raise *and* the valid string key still
        applies — we deliberately don't probe ``os.environ`` with the
        integer key directly because ``os.environ.__contains__(123)``
        raises ``TypeError`` on CPython 3.11+ (the env mapping enforces
        string keys at the access layer)."""
        webui_app._apply_env_to_process({
            123: "ignored",                          # type: ignore[dict-item]
            "CRUCIBLE_TEST_VALID_KEY": "kept",
        })
        self.assertEqual(os.environ["CRUCIBLE_TEST_VALID_KEY"], "kept")
        # Probe via the string-cast form — if the helper had naively
        # done ``os.environ[123] = ...`` the call itself would have
        # raised, so reaching this assertion already proves the integer
        # key was filtered out.
        self.assertNotIn("123", os.environ)

    def test_log_level_propagates_to_app_logger(self):
        """When the operator changes ``CRUCIBLE_LOG_LEVEL`` via the UI,
        Flask's ``app.logger`` level must reflect the new value so any
        WebUI-internal logs respect the change without a restart."""
        original_level = webui_app.app.logger.level
        try:
            webui_app._apply_env_to_process({"CRUCIBLE_LOG_LEVEL": "DEBUG"})
            self.assertEqual(webui_app.app.logger.level, logging.DEBUG)

            webui_app._apply_env_to_process({"CRUCIBLE_LOG_LEVEL": "WARNING"})
            self.assertEqual(webui_app.app.logger.level, logging.WARNING)

            webui_app._apply_env_to_process({"CRUCIBLE_LOG_LEVEL": "INFO"})
            self.assertEqual(webui_app.app.logger.level, logging.INFO)
        finally:
            # Restore the original level so subsequent tests are unaffected.
            webui_app.app.logger.setLevel(original_level)

    def test_log_level_unknown_value_does_not_crash(self):
        """A typo'd log level must not raise — the file write already
        succeeded, so a logger-level reconfig failure is non-fatal and
        must not leak past the helper."""
        original_level = webui_app.app.logger.level
        try:
            # ``getattr(logging, "NOTALEVEL", None)`` returns None, so the
            # branch should silently skip the setLevel call.
            webui_app._apply_env_to_process({"CRUCIBLE_LOG_LEVEL": "NOTALEVEL"})
            # Level is unchanged — nothing was applied.
            self.assertEqual(webui_app.app.logger.level, original_level)
        finally:
            webui_app.app.logger.setLevel(original_level)

    def test_log_level_blank_value_is_ignored(self):
        """Blank ``CRUCIBLE_LOG_LEVEL`` (operator cleared the field) must
        leave Flask's logger level untouched."""
        original_level = webui_app.app.logger.level
        try:
            webui_app._apply_env_to_process({"CRUCIBLE_LOG_LEVEL": ""})
            self.assertEqual(webui_app.app.logger.level, original_level)
        finally:
            webui_app.app.logger.setLevel(original_level)

    def test_payload_without_log_level_leaves_logger_alone(self):
        """If the saved payload doesn't mention ``CRUCIBLE_LOG_LEVEL``,
        ``app.logger.level`` must not change — even if other env vars
        in the payload happen to mention 'level' in some other context."""
        original_level = webui_app.app.logger.level
        try:
            webui_app._apply_env_to_process({"OPENROUTER_API_KEY": "sk-x"})
            self.assertEqual(webui_app.app.logger.level, original_level)
        finally:
            webui_app.app.logger.setLevel(original_level)


# ── 2. POST /api/env: end-to-end through Flask test client ───────────────────


class TestApiSetEndpointHotReload(unittest.TestCase):
    """End-to-end via the Flask test client — covers the full path from
    HTTP request → JSON validation → ``_save_env`` → ``_apply_env_to_process``.

    Validation errors must abort *before* any state mutation; conversely
    a successful 200 must always mutate ``os.environ``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = webui_app.app.test_client()

    def setUp(self) -> None:
        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def test_success_writes_env_and_mutates_os_environ(self):
        """A successful POST must (a) call ``_save_env`` with the validated
        payload and (b) update ``os.environ`` for the same keys."""
        os.environ.pop("CRUCIBLE_TEST_E2E_KEY", None)
        with mock.patch.object(webui_app, "_save_env") as save_mock:
            r = self.client.post(
                "/api/env",
                json={"CRUCIBLE_TEST_E2E_KEY": "live-value"},
            )
        self.assertEqual(r.status_code, 200)
        save_mock.assert_called_once_with({"CRUCIBLE_TEST_E2E_KEY": "live-value"})
        # The hot-reload hook fired.
        self.assertEqual(os.environ["CRUCIBLE_TEST_E2E_KEY"], "live-value")

    def test_save_env_failure_does_not_mutate_os_environ(self):
        """If ``_save_env`` raises, the response must be a 500 and
        ``os.environ`` must remain untouched — process state must never
        run ahead of disk."""
        os.environ.pop("CRUCIBLE_TEST_FAIL_KEY", None)
        with mock.patch.object(webui_app, "_save_env", side_effect=OSError("disk full")):
            r = self.client.post(
                "/api/env",
                json={"CRUCIBLE_TEST_FAIL_KEY": "should-not-leak"},
            )
        self.assertEqual(r.status_code, 500)
        self.assertIn("disk full", r.get_json()["error"])
        # The hot-reload hook never fired because we returned early on the
        # exception path.
        self.assertNotIn("CRUCIBLE_TEST_FAIL_KEY", os.environ)

    def test_validation_failure_does_not_mutate_os_environ(self):
        """A 400 from input validation must abort before we ever touch
        the file *or* ``os.environ``."""
        os.environ.pop("CRUCIBLE_TEST_BAD_KEY", None)
        with mock.patch.object(webui_app, "_save_env") as save_mock:
            r = self.client.post(
                "/api/env",
                json={"CRUCIBLE_TEST_BAD_KEY": "value\nwith-newline"},
            )
        self.assertEqual(r.status_code, 400)
        save_mock.assert_not_called()
        self.assertNotIn("CRUCIBLE_TEST_BAD_KEY", os.environ)

    def test_log_level_change_lifts_app_logger_synchronously(self):
        """End-to-end: saving ``CRUCIBLE_LOG_LEVEL=DEBUG`` must lift
        Flask's logger level by the time the response returns — without
        this synchronicity the user observed "still INFO mode" until
        restart."""
        original_level = webui_app.app.logger.level
        try:
            with mock.patch.object(webui_app, "_save_env"):
                r = self.client.post(
                    "/api/env",
                    json={"CRUCIBLE_LOG_LEVEL": "DEBUG"},
                )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(webui_app.app.logger.level, logging.DEBUG)
        finally:
            webui_app.app.logger.setLevel(original_level)

    def test_subsequent_subprocess_would_inherit_new_env(self):
        """Confirms the contract that motivates this whole hook: after a
        successful POST, the next subprocess spawn would see the new
        values via ``os.environ``.  We don't actually spawn one (cost,
        flake risk on Windows runners) — we just assert the
        ``os.environ`` snapshot a hypothetical ``Popen`` call would
        capture matches the saved payload.

        ``_run_worker`` builds ``_child_env = {**os.environ, ...}`` —
        so as long as ``os.environ`` reflects the new value at the
        moment of spawn, the child inherits it.  This test pins the
        invariant rather than the implementation."""
        os.environ.pop("CRUCIBLE_TEST_INHERIT_KEY", None)
        with mock.patch.object(webui_app, "_save_env"):
            r = self.client.post(
                "/api/env",
                json={"CRUCIBLE_TEST_INHERIT_KEY": "child-sees-this"},
            )
        self.assertEqual(r.status_code, 200)
        # Simulate what ``_run_worker`` does at line ~1023:
        child_env_snapshot = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        self.assertEqual(
            child_env_snapshot.get("CRUCIBLE_TEST_INHERIT_KEY"),
            "child-sees-this",
        )


# ── 3. Idempotency + last-writer-wins under concurrent POSTs ─────────────────


class TestHotReloadConcurrencySemantics(unittest.TestCase):
    """``_apply_env_to_process`` is called from request handler threads
    served by Flask's WSGI stack.  Two concurrent POSTs that both touch
    the same key should produce a last-writer-wins outcome — exactly
    what would happen against the on-disk ``.env`` file, so this hook
    does not weaken any consistency guarantee.

    These tests exercise the helper directly with two threads to verify
    no exception is raised under contention.  CPython's per-key
    ``os.environ.__setitem__`` atomicity means we don't need explicit
    locking; a thread interleaving never produces a torn write at the
    key level."""

    def setUp(self) -> None:
        self._env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patcher.start()
        self.addCleanup(self._env_patcher.stop)

    def test_repeated_calls_are_idempotent(self):
        """Calling the helper N times with the same payload yields the
        same final state as a single call."""
        os.environ.pop("CRUCIBLE_TEST_IDEMP_KEY", None)
        for _ in range(20):
            webui_app._apply_env_to_process({"CRUCIBLE_TEST_IDEMP_KEY": "v"})
        self.assertEqual(os.environ["CRUCIBLE_TEST_IDEMP_KEY"], "v")

    def test_last_writer_wins_under_thread_contention(self):
        """Two threads racing the helper with different values for the
        same key must converge to *one* of the two values — never crash,
        never produce a partial/torn read.  The exact winner is timing-
        dependent and must not be asserted."""
        import threading

        os.environ.pop("CRUCIBLE_TEST_RACE_KEY", None)
        errors: list[BaseException] = []

        def writer(value: str) -> None:
            try:
                for _ in range(100):
                    webui_app._apply_env_to_process(
                        {"CRUCIBLE_TEST_RACE_KEY": value}
                    )
            except BaseException as exc:        # pragma: no cover
                errors.append(exc)

        t1 = threading.Thread(target=writer, args=("alpha",))
        t2 = threading.Thread(target=writer, args=("bravo",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(errors, [])
        # Final value is one of the two writers — never a torn merge.
        final = os.environ.get("CRUCIBLE_TEST_RACE_KEY")
        self.assertIn(final, {"alpha", "bravo"})


if __name__ == "__main__":
    unittest.main()
