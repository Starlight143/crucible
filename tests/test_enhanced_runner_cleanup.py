"""Regression tests for cmd_run interactive-context cleanup leak.

Bug: _interactive_context_path was not cleaned up when cmd_run exited via:
  1. Early return after user cancels a dedup-check prompt
  2. Early return when no new run directory is found after the pipeline
  3. SystemExit raised by _core_main()

Fix: wrap the cmd_run body in try/finally so cleanup_interactive_context() is
called on ALL exit paths.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_args(**kwargs) -> argparse.Namespace:
    """Return a minimal Namespace accepted by cmd_run."""
    defaults = dict(
        interactive=True,
        dedup_check=False,
        diff_aware=False,
        use_memory=False,
        project_dir=None,
        diff_base_ref="HEAD~1",
        postprocess=False,
        ab_test=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Helpers: common patch targets
# ---------------------------------------------------------------------------

_INTERACTIVE_MODULE = "crucible.features.interactive_mode"
_DEDUP_MODULE = "crucible.features.run_deduplication"
_CLI_MODULE = "crucible.cli"
_ENHANCED_MODULE = "run_crucible_enhanced"


class TestCmdRunCleanupOnEarlyReturn:
    """cleanup_interactive_context must run on every early-return path."""

    def _make_fake_context_file(self) -> str:
        """Write a temp file and return its path (simulates pre-run context)."""
        fd, path = tempfile.mkstemp(prefix="_interactive_context_", suffix=".txt")
        os.close(fd)
        return path

    # ------------------------------------------------------------------
    # Path 1: user cancels after dedup-check finds similar runs
    # ------------------------------------------------------------------

    def test_cleanup_runs_when_user_cancels_dedup(self, tmp_path):
        """Interactive context must be cleaned up when user cancels after dedup."""
        ctx_path = str(tmp_path / "_interactive_context.txt")
        ctx_path_file = open(ctx_path, "w")
        ctx_path_file.close()

        cleanup_called_with = []

        def fake_cleanup(path):
            cleanup_called_with.append(path)
            if os.path.exists(path):
                os.unlink(path)

        # Dedup result: has similar runs → triggers prompt
        fake_dedup_result = MagicMock()
        fake_dedup_result.has_similar_runs = True
        fake_dedup_result.summary_text.return_value = "[Dedup] 1 similar run found."

        with (
            patch(f"{_INTERACTIVE_MODULE}.run_interactive_pre_run", return_value=ctx_path),
            patch(f"{_INTERACTIVE_MODULE}.cleanup_interactive_context", side_effect=fake_cleanup),
            patch(f"{_DEDUP_MODULE}.check_duplicate_run", return_value=fake_dedup_result),
            # Simulate user typing "n" at the prompt
            patch("builtins.input", return_value="n"),
            # sys.stdin.isatty() must be True to trigger the prompt
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = True

            from run_crucible_enhanced import cmd_run

            args = _make_args(dedup_check=True)
            # Should return without raising, cleanup fires via finally
            cmd_run(args)

        assert cleanup_called_with == [ctx_path], (
            "cleanup_interactive_context must be called with the context path "
            "even when the user cancels the dedup prompt"
        )

    # ------------------------------------------------------------------
    # Path 2: _core_main() succeeds but no new run directory found
    # ------------------------------------------------------------------

    def test_cleanup_runs_when_no_run_dir_found(self, tmp_path):
        """Interactive context must be cleaned up when _find_latest_run_dir returns None."""
        ctx_path = str(tmp_path / "_interactive_context.txt")
        open(ctx_path, "w").close()

        cleanup_called_with = []

        def fake_cleanup(path):
            cleanup_called_with.append(path)
            if os.path.exists(path):
                os.unlink(path)

        with (
            patch(f"{_INTERACTIVE_MODULE}.run_interactive_pre_run", return_value=ctx_path),
            patch(f"{_INTERACTIVE_MODULE}.cleanup_interactive_context", side_effect=fake_cleanup),
            # _core_main does nothing (simulates a successful but output-less run)
            patch(f"{_CLI_MODULE}.main"),
            # No run directory found after the pipeline
            patch(f"{_ENHANCED_MODULE}._find_latest_run_dir", return_value=None),
            patch(f"{_ENHANCED_MODULE}._build_core_argv", return_value=["prog"]),
        ):
            from run_crucible_enhanced import cmd_run

            cmd_run(_make_args())

        assert cleanup_called_with == [ctx_path], (
            "cleanup_interactive_context must be called when _find_latest_run_dir "
            "returns None (no post-processing, early return)"
        )

    # ------------------------------------------------------------------
    # Path 3: _core_main() raises SystemExit (normal pipeline completion)
    # ------------------------------------------------------------------

    def test_cleanup_runs_on_system_exit_from_core_main(self, tmp_path):
        """Interactive context must be cleaned up even when _core_main raises SystemExit."""
        ctx_path = str(tmp_path / "_interactive_context.txt")
        open(ctx_path, "w").close()

        cleanup_called_with = []

        def fake_cleanup(path):
            cleanup_called_with.append(path)
            if os.path.exists(path):
                os.unlink(path)

        with (
            patch(f"{_INTERACTIVE_MODULE}.run_interactive_pre_run", return_value=ctx_path),
            patch(f"{_INTERACTIVE_MODULE}.cleanup_interactive_context", side_effect=fake_cleanup),
            # Simulate the core pipeline calling sys.exit(0) on success
            patch(f"{_CLI_MODULE}.main", side_effect=SystemExit(0)),
            patch(f"{_ENHANCED_MODULE}._build_core_argv", return_value=["prog"]),
        ):
            from run_crucible_enhanced import cmd_run

            with pytest.raises(SystemExit):
                cmd_run(_make_args())

        assert cleanup_called_with == [ctx_path], (
            "cleanup_interactive_context must be called even when _core_main() "
            "raises SystemExit — the finally block must intercept it"
        )

    # ------------------------------------------------------------------
    # Path 4 (baseline): normal completion also triggers cleanup
    # ------------------------------------------------------------------

    def test_cleanup_runs_on_normal_completion(self, tmp_path):
        """cleanup_interactive_context must run on the happy path too."""
        ctx_path = str(tmp_path / "_interactive_context.txt")
        open(ctx_path, "w").close()

        cleanup_called_with = []

        def fake_cleanup(path):
            cleanup_called_with.append(path)
            if os.path.exists(path):
                os.unlink(path)

        fake_run_dir = str(tmp_path / "run_20260101_120000")
        os.makedirs(fake_run_dir, exist_ok=True)

        with (
            patch(f"{_INTERACTIVE_MODULE}.run_interactive_pre_run", return_value=ctx_path),
            patch(f"{_INTERACTIVE_MODULE}.cleanup_interactive_context", side_effect=fake_cleanup),
            patch(f"{_CLI_MODULE}.main"),
            patch(f"{_ENHANCED_MODULE}._find_latest_run_dir", return_value=fake_run_dir),
            patch(f"{_ENHANCED_MODULE}._build_core_argv", return_value=["prog"]),
            patch(f"{_ENHANCED_MODULE}._run_postprocessing"),
        ):
            from run_crucible_enhanced import cmd_run

            cmd_run(_make_args())

        assert cleanup_called_with == [ctx_path], (
            "cleanup_interactive_context must be called on the normal happy path"
        )


# ---------------------------------------------------------------------------
# cmd_batch timeout env-var override
# ---------------------------------------------------------------------------

class TestCmdBatchTimeout:
    """ENHANCED_BATCH_TIMEOUT env var must override the per-project subprocess timeout."""

    def test_batch_timeout_respects_env_var(self, monkeypatch, tmp_path):
        """
        Regression: cmd_batch used a hardcoded timeout=3600 with no
        override mechanism.  After the fix, ENHANCED_BATCH_TIMEOUT is respected.
        """
        import subprocess
        import argparse as _ap

        # Create a minimal batch directory with one fake project sub-directory
        project_dir = tmp_path / "proj_a"
        project_dir.mkdir()

        monkeypatch.setenv("ENHANCED_BATCH_TIMEOUT", "42")

        captured_timeouts = []

        def _fake_subprocess_run(cmd, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            # Simulate a fast success without actually spawning a process
            result = subprocess.CompletedProcess(cmd, returncode=0)
            return result

        from crucible.features.batch_runner import run_batch as _orig_run_batch

        def _fake_run_batch(batch_dir, run_fn, max_workers=1):
            # Call run_fn for each project dir (mimics what run_batch does)
            for entry in os.listdir(batch_dir):
                full = os.path.join(batch_dir, entry)
                if os.path.isdir(full):
                    run_fn(full)

        with (
            patch("subprocess.run", side_effect=_fake_subprocess_run),
            patch("crucible.features.batch_runner.run_batch", side_effect=_fake_run_batch),
            patch(f"{_ENHANCED_MODULE}._find_latest_run_dir", return_value=None),
        ):
            from run_crucible_enhanced import cmd_batch

            args = _ap.Namespace(
                batch_dir=str(tmp_path),
                batch_workers=1,
                security_scan=False,
                deployment_artifacts=False,
                independent_validation=False,
            )
            cmd_batch(args)

        assert captured_timeouts, "subprocess.run must have been called"
        assert captured_timeouts[0] == 42, (
            f"Expected timeout=42 from ENHANCED_BATCH_TIMEOUT, got {captured_timeouts[0]}. "
            "Hardcoded timeout was not overridden by env var."
        )
