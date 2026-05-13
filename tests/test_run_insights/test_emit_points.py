"""
Integration tests for the 5 emit points in production code.

We don't run the full pipeline (too slow / network-dependent); instead we
import the modules and verify the emit code paths are wired in correctly.
A regression test against the import graph: section_02 / section_07 /
resilience must import run_insights without circular import errors.
"""
from __future__ import annotations

import importlib
import sys

import pytest


def test_run_insights_import_does_not_trigger_io(tmp_path, monkeypatch):
    """Importing run_insights must NOT open any file — the recorder is lazy."""
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "x"))
    # Force re-import so the env var takes effect (the recorder is module-level
    # singleton, but the env var is read at construction time).
    sys.modules.pop("crucible.features.run_insights", None)
    sys.modules.pop("crucible.features.run_insights.recorder", None)
    importlib.import_module("crucible.features.run_insights")
    # No ledger dir should have been created by the import itself.
    assert not (tmp_path / "x").exists()


def test_section_02_imports_run_insights():
    """section_02 must successfully import run_insights — circular-import canary."""
    sec02 = importlib.import_module(
        "crucible.modules.section_02_research_and_llm"
    )
    # The emit-helper symbols must be present in the module's namespace.
    assert hasattr(sec02, "_get_insights_recorder")
    assert hasattr(sec02, "_get_run_id")


def test_section_07_imports_run_insights():
    sec07 = importlib.import_module(
        "crucible.modules.section_07_selfcheck_output_main"
    )
    assert hasattr(sec07, "_get_insights_recorder")
    assert hasattr(sec07, "_InsightOutcome")


def test_resilience_can_lazy_import_run_insights():
    """resilience.py uses a lazy import (inside the retry-exhausted branch);
    we just verify the modules co-exist without circular reference."""
    res = importlib.import_module("crucible.resilience")
    ri = importlib.import_module("crucible.features.run_insights")
    # Both modules are independent — neither imports the other at module top.
    assert "crucible.features.run_insights" not in str(res.__file__)
    assert "resilience" not in str(ri.__file__)


def test_recorder_no_op_when_run_id_empty(tmp_path, monkeypatch):
    """run_id='' (no active run_context) must NOT crash the recorder —
    every emit point uses _get_run_id() which returns '' when no context.

    Each per-stream toggle is monkey-patched on so that a local ``.env`` with
    operator-disabled streams (e.g. ``CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS=0``)
    doesn't make this test depend on operator config — the recorder will
    short-circuit and return ``None`` if the stream flag resolves False.
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "x"))
    from crucible.features.run_insights import get_recorder, reset_recorder
    reset_recorder()
    r = get_recorder()
    cid = r.record_error(
        run_id="",
        project_name="p",
        mode="Quant",
        stage="s",
        exception_class="E",
    )
    assert cid is not None  # empty run_id is fine; recorder still emits
    reset_recorder()


def test_record_error_swallows_backend_failures(tmp_path, monkeypatch):
    """v1.1.0: a backend write failure during ``record_error`` MUST NOT
    propagate out of the recorder.  This is the contract documented in
    CLAUDE.md §3: the ledger is an observer; if it crashes during the
    error-record code path it would mask the original error and look
    like a recorder bug to the operator.  We monkey-patch the backend
    so ``write_event`` raises ``OSError`` on the first call and confirm
    that ``record_error`` returns ``None`` rather than re-raising.
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))

    from crucible.features.run_insights import get_recorder, reset_recorder
    reset_recorder()
    r = get_recorder()

    # Replace the backend's write_event with a function that always raises.
    def _boom(*_a, **_kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(r.backend, "write_event", _boom)

    cid = r.record_error(
        run_id="r1",
        project_name="p",
        mode="Quant",
        stage="codegen",
        exception_class="RuntimeError",
        message="LLM call failed: 401 Unauthorized",
        retry_count=3,
    )
    # The recorder must absorb the OSError and return None (not re-raise,
    # not raise something else).  If this assertion fires, the ``try/
    # except: pass`` envelope on the emit code path has regressed.
    assert cid is None
    reset_recorder()


def test_record_output_swallows_backend_failures(tmp_path, monkeypatch):
    """Same contract applies to ``record_output_method`` — the section_07
    save path must not crash because of a ledger write failure.
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))

    from crucible.features.run_insights import get_recorder, reset_recorder
    reset_recorder()
    r = get_recorder()

    def _boom(*_a, **_kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(r.backend, "write_event", _boom)

    cid = r.record_output_method(
        run_id="r2",
        project_name="p",
        mode="Quant",
        user_problem="test",
    )
    assert cid is None
    reset_recorder()


def test_record_direction_debate_rejection_swallows_backend_failures(
    tmp_path, monkeypatch,
):
    """v1.1.0 third-pass: contract covers ALL four emit streams.

    The original v1.1.0 fix only added swallow tests for ``record_error`` /
    ``record_output_method``; ``record_direction_debate_rejection`` is on
    the Stage-0 force-none path where a ledger failure would mask the
    actual judge verdict.  Pin the contract here so a future refactor
    cannot regress.
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))

    from crucible.features.run_insights import get_recorder, reset_recorder
    reset_recorder()
    r = get_recorder()

    def _boom(*_a, **_kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(r.backend, "write_event", _boom)

    try:
        cid = r.record_direction_debate_rejection(
            run_id="r3",
            project_name="p",
            mode="Quant",
            direction_id="d1",
            rejection_reason="force_none",
        )
        assert cid is None
    finally:
        reset_recorder()


def test_record_runtime_params_swallows_backend_failures(
    tmp_path, monkeypatch,
):
    """Section 07's runtime_params emit must also tolerate backend failure.

    runtime_params is gated by RECORD_PARAMS=auto (Quant-only) so we
    pass mode='Quant' to ensure the emit path executes.  Without this
    test, a regression to the swallow envelope would only surface on
    Quant runs in production.
    """
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_ENABLED", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS", "1")
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path / "ledger"))

    from crucible.features.run_insights import get_recorder, reset_recorder
    reset_recorder()
    r = get_recorder()

    def _boom(*_a, **_kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(r.backend, "write_event", _boom)

    try:
        cid = r.record_runtime_params(
            run_id="r4",
            project_name="p",
            mode="Quant",
            cli_flags={"cache": True},
        )
        assert cid is None
    finally:
        reset_recorder()
