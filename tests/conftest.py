from __future__ import annotations

from pathlib import Path

import pytest

from crucible._temp_runtime import ensure_writable_temp_root

ensure_writable_temp_root(Path(__file__).resolve().parents[1])


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
