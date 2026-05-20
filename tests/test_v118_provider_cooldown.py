"""v1.1.8 extended Phase 2 (Q2) — Per-provider adaptive cooldown tests.

Coverage:

* Initial trigger → cooldown_until_ts populated with default initial.
* Consecutive trigger while still cooling → doubles last_duration.
* Doubling capped at LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS.
* Trigger after cooldown ended → resets to initial.
* ``is_cooling_down`` / ``remaining_seconds`` honour the timer.
* ``clear`` / ``clear_all`` empty the registry.
* ``snapshot`` returns a copy (safe for observability).
* Empty provider name short-circuits all methods (no raise).

Uses ``time.monotonic`` patching to avoid wall-clock sleep — keeps
tests fast and deterministic across Windows / Linux time-resolution
differences (CLAUDE.md § 7).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from crucible.web_research import cooldown as cooldown_mod
from crucible.web_research.cooldown import CooldownRegistry


@pytest.fixture
def registry(monkeypatch):
    """Fresh CooldownRegistry with explicit env overrides."""
    monkeypatch.setenv("LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS", "60")
    monkeypatch.setenv("LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS", "1800")
    CooldownRegistry.reset_default()
    yield CooldownRegistry()
    CooldownRegistry.reset_default()


class TestSingleton:
    def test_get_default_returns_same_instance(self) -> None:
        CooldownRegistry.reset_default()
        try:
            a = CooldownRegistry.get_default()
            b = CooldownRegistry.get_default()
            assert a is b
        finally:
            CooldownRegistry.reset_default()

    def test_reset_default_yields_new_instance(self) -> None:
        CooldownRegistry.reset_default()
        a = CooldownRegistry.get_default()
        CooldownRegistry.reset_default()
        b = CooldownRegistry.get_default()
        assert a is not b
        CooldownRegistry.reset_default()


class TestInitialTrigger:
    def test_first_trigger_uses_initial_seconds(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            duration = registry.trigger("websearch", reason="http_429")
        assert duration == 60

    def test_first_trigger_engages_cooldown(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="http_429")
            assert registry.is_cooling_down("websearch") is True
            # 60 seconds after trigger, still cooling.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1059.0):
            assert registry.is_cooling_down("websearch") is True

    def test_cooldown_ends_after_duration(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="http_429")
        # 61 seconds later — cooldown ended.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1061.0):
            assert registry.is_cooling_down("websearch") is False


class TestDoublingPattern:
    def test_consecutive_triggers_double(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            d1 = registry.trigger("websearch", reason="http_429")
            assert d1 == 60
        # Trigger again 10s later while still cooling → double.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1010.0):
            d2 = registry.trigger("websearch", reason="http_429")
            assert d2 == 120
        # Trigger third time while still cooling → double again.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1020.0):
            d3 = registry.trigger("websearch", reason="http_429")
            assert d3 == 240

    def test_doubling_capped(self, registry, monkeypatch) -> None:
        # Lower the cap so the test runs fast.
        monkeypatch.setenv("LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS", "200")
        # First trigger = 60s.  Then 120s.  Then 240s capped at 200.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            assert registry.trigger("websearch", reason="r") == 60
        with patch.object(cooldown_mod.time, "monotonic", return_value=1010.0):
            assert registry.trigger("websearch", reason="r") == 120
        with patch.object(cooldown_mod.time, "monotonic", return_value=1020.0):
            assert registry.trigger("websearch", reason="r") == 200
        # Subsequent triggers stay capped.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1030.0):
            assert registry.trigger("websearch", reason="r") == 200

    def test_trigger_after_cooldown_ended_resets_to_initial(self, registry) -> None:
        # First trigger: 60s.
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            assert registry.trigger("websearch", reason="r") == 60
        # Cooldown expires.  Trigger again at t=2000 — fresh start.
        with patch.object(cooldown_mod.time, "monotonic", return_value=2000.0):
            assert registry.trigger("websearch", reason="r") == 60


class TestRemainingSeconds:
    def test_not_cooling_returns_zero(self, registry) -> None:
        assert registry.remaining_seconds("nonexistent") == 0.0

    def test_cooling_returns_positive(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="r")
        with patch.object(cooldown_mod.time, "monotonic", return_value=1010.0):
            remaining = registry.remaining_seconds("websearch")
            assert 49.9 < remaining < 50.1

    def test_after_cooldown_returns_zero(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="r")
        with patch.object(cooldown_mod.time, "monotonic", return_value=2000.0):
            assert registry.remaining_seconds("websearch") == 0.0


class TestClearAndSnapshot:
    def test_clear_removes_provider(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="r")
        registry.clear("websearch")
        assert registry.is_cooling_down("websearch") is False
        with patch.object(cooldown_mod.time, "monotonic", return_value=1010.0):
            assert registry.remaining_seconds("websearch") == 0.0

    def test_clear_all_removes_everything(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="r")
            registry.trigger("github", reason="r")
            registry.trigger("arxiv", reason="r")
        registry.clear_all()
        with patch.object(cooldown_mod.time, "monotonic", return_value=1010.0):
            assert registry.is_cooling_down("websearch") is False
            assert registry.is_cooling_down("github") is False
            assert registry.is_cooling_down("arxiv") is False

    def test_snapshot_returns_copy(self, registry) -> None:
        with patch.object(cooldown_mod.time, "monotonic", return_value=1000.0):
            registry.trigger("websearch", reason="http_429")
        with patch.object(cooldown_mod.time, "monotonic", return_value=1010.0):
            snap = registry.snapshot()
            assert "websearch" in snap
            assert snap["websearch"]["trigger_count"] == 1.0
            assert 49.0 < snap["websearch"]["cooldown_remaining_seconds"] < 51.0
            # Mutating snapshot does not affect registry state.
            snap["websearch"]["cooldown_remaining_seconds"] = 9999.0
            snap2 = registry.snapshot()
            assert 49.0 < snap2["websearch"]["cooldown_remaining_seconds"] < 51.0


class TestEmptyProviderHandled:
    def test_trigger_empty_provider_returns_zero(self, registry) -> None:
        assert registry.trigger("", reason="r") == 0
        assert registry.trigger(None, reason="r") == 0  # type: ignore[arg-type]

    def test_is_cooling_down_empty_returns_false(self, registry) -> None:
        assert registry.is_cooling_down("") is False
        assert registry.is_cooling_down(None) is False  # type: ignore[arg-type]

    def test_remaining_seconds_empty_returns_zero(self, registry) -> None:
        assert registry.remaining_seconds("") == 0.0
        assert registry.remaining_seconds(None) == 0.0  # type: ignore[arg-type]


class TestEnvDefaults:
    def test_initial_seconds_default_60(self, monkeypatch) -> None:
        monkeypatch.delenv(
            "LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS", raising=False,
        )
        assert cooldown_mod._initial_seconds() == 60

    def test_max_seconds_default_1800(self, monkeypatch) -> None:
        monkeypatch.delenv(
            "LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS", raising=False,
        )
        assert cooldown_mod._max_seconds() == 1800

    def test_zero_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS", "0")
        assert cooldown_mod._initial_seconds() == 60

    def test_negative_env_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS", "-10")
        assert cooldown_mod._max_seconds() == 1800
