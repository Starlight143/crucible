from __future__ import annotations

from pathlib import Path

import pytest

from crucible._temp_runtime import ensure_writable_temp_root

ensure_writable_temp_root(Path(__file__).resolve().parents[1])


@pytest.fixture(autouse=True)
def _isolate_run_insights_ledger_dir(tmp_path, monkeypatch):
    """v1.1.4 — Prevent tests from emitting to the real
    ``.crucible_insights/`` ledger directory.

    Without this, any test that ends up exercising a code path which calls
    ``record_output_method`` / ``record_runtime_params`` / ``record_error`` /
    ``record_direction_debate_rejection`` (directly or indirectly — e.g.
    ``test_failure_banner.py`` invokes ``save_project_output``) writes to
    the operator's REAL ledger at ``<repo>/.crucible_insights/`` and
    poisons the dataset that v1.2.0 retrieval is supposed to read from.
    Empirical diagnosis at v1.1.4 ship time found 897 of 952 events in the
    operator's real ledger were test pollution (``project_name`` ∈
    ``{banner_test, Quant_analysis, agent_analysis, test, ...}`` with
    ``run_id=""``) versus 3 real user runs — a 94 % signal-to-noise
    inversion.

    This autouse fixture sets ``CRUCIBLE_RUN_INSIGHTS_DIR`` to a unique
    ``tmp_path``-rooted directory for every test, and resets the
    module-level recorder singleton so the env var actually takes effect
    on the next ``get_recorder()`` call.  Tests that already
    ``monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(tmp_path/...))``
    explicitly (per CLAUDE.md § 9.5) are unaffected — their per-test
    monkeypatch overrides this autouse fixture's, and their explicit
    ``reset_recorder()`` calls compose cleanly with ours.

    The recorder reset is best-effort: import failures are swallowed so
    suites that don't depend on the run-insights subsystem (legacy
    fixtures, smoke tests under sandboxed CI) aren't broken by an
    autouse fixture they don't touch.
    """
    ledger_root = tmp_path / "_crucible_insights"
    ledger_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CRUCIBLE_RUN_INSIGHTS_DIR", str(ledger_root))
    try:
        from crucible.features.run_insights import recorder as _rec_mod
        _rec_mod.reset_recorder()
    except Exception:
        pass
    yield
    try:
        from crucible.features.run_insights import recorder as _rec_mod
        _rec_mod.reset_recorder()
    except Exception:
        pass


def pytest_configure(config: pytest.Config) -> None:
    """Register opt-in markers used to gate slow / network-dependent tests.

    CI runs the regular suite via ``pytest -m "not slow and not network"`` so
    long-running or network-fragile cases stay out of the default pull-request
    check.  Developers can opt in locally via ``pytest -m slow`` /
    ``pytest -m network``.

    Markers
    -------
    ``slow``
        Tests that take more than ~1 second of wall-clock time (typically
        because they exercise real ``time.sleep`` semantics, subprocess
        spawning, or large-input fuzzing).
    ``network``
        Tests that initiate real outbound HTTP traffic.  Use ``respx`` /
        ``monkeypatch`` mocks instead when at all possible; only mark with
        ``network`` when a behaviour can only be exercised against a live
        third-party endpoint.
    ``integration``
        End-to-end tests that exercise multiple subsystems together (e.g. the
        full pipeline from CLI invocation through report generation).  These
        are not automatically skipped — the marker is informational.
    """
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (> ~1s wall-clock); CI excludes by default",
    )
    config.addinivalue_line(
        "markers",
        "network: marks tests that initiate real outbound network traffic",
    )
    config.addinivalue_line(
        "markers",
        "integration: marks end-to-end tests covering multiple subsystems",
    )
