"""Tests for crucible.features.interactive_mode"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.interactive_mode import (
    InteractiveContext,
    _ENV_VAR,
    _CONTEXT_FILENAME,
    _is_interactive_tty,
    cleanup_interactive_context,
    collect_interactive_context,
    run_interactive_pre_run,
    write_context_file,
)


# ── InteractiveContext ─────────────────────────────────────────────────────────

class TestInteractiveContext:
    def test_is_empty_when_all_blank(self):
        ctx = InteractiveContext()
        assert ctx.is_empty()

    def test_not_empty_with_focus_areas(self):
        ctx = InteractiveContext(focus_areas=["momentum signals"])
        assert not ctx.is_empty()

    def test_not_empty_with_free_text(self):
        ctx = InteractiveContext(free_text="some notes")
        assert not ctx.is_empty()

    def test_not_empty_with_constraints(self):
        ctx = InteractiveContext(constraints=["max drawdown 10%"])
        assert not ctx.is_empty()

    def test_not_empty_with_hypotheses(self):
        ctx = InteractiveContext(hypotheses=["H1: volume precedes price"])
        assert not ctx.is_empty()

    def test_free_text_whitespace_only_is_empty(self):
        ctx = InteractiveContext(free_text="   \n  ")
        assert ctx.is_empty()

    def test_to_text_contains_risk_tolerance(self):
        ctx = InteractiveContext(risk_tolerance="aggressive")
        text = ctx.to_text()
        assert "aggressive" in text

    def test_to_text_contains_focus_areas(self):
        ctx = InteractiveContext(focus_areas=["alpha generation", "execution cost"])
        text = ctx.to_text()
        assert "alpha generation" in text
        assert "execution cost" in text

    def test_to_text_contains_constraints(self):
        ctx = InteractiveContext(constraints=["no overnight positions"])
        text = ctx.to_text()
        assert "no overnight positions" in text

    def test_to_text_contains_hypotheses(self):
        ctx = InteractiveContext(hypotheses=["momentum persists for 5 bars"])
        text = ctx.to_text()
        assert "momentum persists for 5 bars" in text

    def test_to_text_contains_free_text(self):
        ctx = InteractiveContext(free_text="focus on crypto pairs")
        text = ctx.to_text()
        assert "focus on crypto pairs" in text

    def test_to_text_header_and_footer(self):
        ctx = InteractiveContext(focus_areas=["x"])
        text = ctx.to_text()
        assert "=== Interactive Research Guidance ===" in text
        assert "=== End of Interactive Guidance ===" in text

    def test_to_text_collected_at_present(self):
        ctx = InteractiveContext()
        text = ctx.to_text()
        assert "Collected at:" in text

    def test_collected_at_is_set(self):
        ctx = InteractiveContext()
        assert ctx.collected_at  # non-empty ISO timestamp


# ── _is_interactive_tty ────────────────────────────────────────────────────────

class TestIsInteractiveTty:
    def test_returns_false_in_test_environment(self):
        # pytest runs with piped stdin, so this should return False
        assert _is_interactive_tty() is False


# ── collect_interactive_context ───────────────────────────────────────────────

class TestCollectInteractiveContext:
    def test_returns_empty_context_in_non_tty(self, tmp_path):
        # In the test runner stdin is not a TTY → should return empty immediately
        ctx = collect_interactive_context(str(tmp_path))
        assert ctx.is_empty()
        assert isinstance(ctx, InteractiveContext)

    def test_returns_interactive_context_type(self, tmp_path):
        ctx = collect_interactive_context(str(tmp_path))
        assert isinstance(ctx, InteractiveContext)


# ── write_context_file ────────────────────────────────────────────────────────

class TestWriteContextFile:
    def test_returns_none_for_empty_context(self, tmp_path):
        ctx = InteractiveContext()
        result = write_context_file(ctx, str(tmp_path))
        assert result is None

    def test_writes_file_for_non_empty_context(self, tmp_path):
        ctx = InteractiveContext(focus_areas=["momentum"])
        path = write_context_file(ctx, str(tmp_path))
        assert path is not None
        assert os.path.isfile(path)

    def test_written_file_path_is_in_workspace(self, tmp_path):
        ctx = InteractiveContext(constraints=["no leverage"])
        path = write_context_file(ctx, str(tmp_path))
        assert path is not None
        assert str(tmp_path) in path

    def test_written_file_contains_context_text(self, tmp_path):
        ctx = InteractiveContext(focus_areas=["volatility clustering"])
        path = write_context_file(ctx, str(tmp_path))
        assert path is not None
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "volatility clustering" in content

    def test_sets_env_var(self, tmp_path):
        os.environ.pop(_ENV_VAR, None)
        ctx = InteractiveContext(focus_areas=["test"])
        path = write_context_file(ctx, str(tmp_path))
        try:
            assert os.environ.get(_ENV_VAR) == path
        finally:
            os.environ.pop(_ENV_VAR, None)
            if path and os.path.isfile(path):
                os.remove(path)

    def test_does_not_set_env_var_for_empty_context(self, tmp_path):
        os.environ.pop(_ENV_VAR, None)
        ctx = InteractiveContext()
        write_context_file(ctx, str(tmp_path))
        assert _ENV_VAR not in os.environ

    def test_filename_is_correct(self, tmp_path):
        ctx = InteractiveContext(hypotheses=["H1"])
        path = write_context_file(ctx, str(tmp_path))
        assert path is not None
        assert os.path.basename(path) == _CONTEXT_FILENAME


# ── cleanup_interactive_context ───────────────────────────────────────────────

class TestCleanupInteractiveContext:
    def test_removes_file(self, tmp_path):
        ctx = InteractiveContext(focus_areas=["x"])
        path = write_context_file(ctx, str(tmp_path))
        assert path is not None and os.path.isfile(path)
        cleanup_interactive_context(path)
        assert not os.path.isfile(path)

    def test_unsets_env_var(self, tmp_path):
        ctx = InteractiveContext(focus_areas=["x"])
        path = write_context_file(ctx, str(tmp_path))
        cleanup_interactive_context(path)
        assert _ENV_VAR not in os.environ

    def test_safe_with_none(self):
        # Should not raise
        cleanup_interactive_context(None)

    def test_safe_when_file_already_deleted(self, tmp_path):
        path = str(tmp_path / "nonexistent.txt")
        cleanup_interactive_context(path)  # should not raise

    def test_safe_when_env_var_not_set(self, tmp_path):
        os.environ.pop(_ENV_VAR, None)
        cleanup_interactive_context(None)  # should not raise


# ── run_interactive_pre_run ───────────────────────────────────────────────────

class TestRunInteractivePreRun:
    def test_returns_none_in_non_tty(self, tmp_path):
        # Non-TTY → empty context → no file written → None
        result = run_interactive_pre_run(str(tmp_path))
        assert result is None

    def test_no_file_written_in_non_tty(self, tmp_path):
        run_interactive_pre_run(str(tmp_path))
        context_file = tmp_path / _CONTEXT_FILENAME
        assert not context_file.exists()
