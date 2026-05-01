"""Tests for crucible.feature_registry"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.feature_registry import (
    BaseFeature,
    FeatureConfig,
    FeatureResult,
    register,
    get_feature,
    list_features,
    resolve_order,
    run_features,
    format_results,
    CircularDependencyError,
    _REGISTRY,
)
from crucible.cancellation import (
    CancellationToken,
    OperationCancelledError,
    cancellation_scope,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_simple_feature(name: str, requires: list | None = None,
                          success: bool = True, raises: bool = False,
                          critical: bool = False):
    """Dynamically create and register a simple feature for testing."""
    _requires = list(requires or [])
    _success = success
    _raises = raises
    _critical = critical

    # Build run() inside the factory so closures capture the right values.
    def _run_impl(self, run_dir, config):
        if _raises:
            raise RuntimeError(f"{name} failed!")
        return FeatureResult(feature=name, success=_success,
                             summary="ok" if _success else "failed")

    # Create the class dynamically with run() already in its body so ABC
    # does not consider it abstract at instantiation time.
    cls = type(
        f"_TestFeature_{name}",
        (BaseFeature,),
        {
            "name": name,
            "label": f"Test {name}",
            "requires": _requires,
            "critical": _critical,
            "run": _run_impl,
        },
    )
    # Register manually (bypass the decorator to avoid double-registration issues)
    _REGISTRY[name] = cls
    return cls


# ── Cleanup registry between tests ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_registry():
    """Remove test-specific features after each test."""
    before = set(_REGISTRY.keys())
    yield
    for key in list(_REGISTRY.keys()):
        if key not in before:
            del _REGISTRY[key]


# ── register decorator ────────────────────────────────────────────────────────

class TestRegister:
    def test_registers_feature(self):
        _make_simple_feature("_test_reg_a")
        assert get_feature("_test_reg_a") is not None

    def test_name_set_on_class(self):
        cls = _make_simple_feature("_test_reg_b")
        assert cls.name == "_test_reg_b"

    def test_overwrite_warns(self, caplog):
        import logging

        # Use @register directly (not the test helper) to trigger the overwrite warning.
        @register("_test_overwrite_direct")
        class _A(BaseFeature):
            requires = []
            def run(self, run_dir, config):
                return FeatureResult(feature="_test_overwrite_direct", success=True)

        with caplog.at_level(logging.WARNING):
            @register("_test_overwrite_direct")
            class _B(BaseFeature):
                requires = []
                def run(self, run_dir, config):
                    return FeatureResult(feature="_test_overwrite_direct", success=True)

        assert any("overwriting" in r.message.lower() for r in caplog.records)

    def test_non_base_feature_raises(self):
        with pytest.raises(TypeError):
            @register("_bad")
            class NotAFeature:
                pass

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            @register("")
            class _F(BaseFeature):
                def run(self, run_dir, config):
                    return FeatureResult(feature="", success=True)


# ── list_features ─────────────────────────────────────────────────────────────

class TestListFeatures:
    def test_returns_sorted_list(self):
        _make_simple_feature("_zz")
        _make_simple_feature("_aa")
        names = list_features()
        assert names == sorted(names)


# ── get_feature ───────────────────────────────────────────────────────────────

class TestGetFeature:
    def test_returns_none_for_unknown(self):
        assert get_feature("__nonexistent__") is None

    def test_returns_class_for_known(self):
        _make_simple_feature("_test_gf")
        cls = get_feature("_test_gf")
        assert cls is not None
        assert issubclass(cls, BaseFeature)


# ── resolve_order ─────────────────────────────────────────────────────────────

class TestResolveOrder:
    def test_no_deps_returns_sorted(self):
        _make_simple_feature("_ro_a")
        _make_simple_feature("_ro_b")
        order = resolve_order(["_ro_b", "_ro_a"])
        assert set(order) == {"_ro_a", "_ro_b"}

    def test_respects_dependency(self):
        _make_simple_feature("_ro_base")
        _make_simple_feature("_ro_dep", requires=["_ro_base"])
        order = resolve_order(["_ro_dep", "_ro_base"])
        assert order.index("_ro_base") < order.index("_ro_dep")

    def test_circular_raises(self):
        # Manually inject a cycle without using register (to avoid decorator complexity)
        @register("_cycle_a")
        class CycleA(BaseFeature):
            requires = ["_cycle_b"]
            def run(self, run_dir, config):
                return FeatureResult(feature="_cycle_a", success=True)

        @register("_cycle_b")
        class CycleB(BaseFeature):
            requires = ["_cycle_a"]
            def run(self, run_dir, config):
                return FeatureResult(feature="_cycle_b", success=True)

        with pytest.raises(CircularDependencyError):
            resolve_order(["_cycle_a", "_cycle_b"])

    def test_unregistered_feature_included(self):
        # Unregistered features in the list should still appear in output
        order = resolve_order(["_nonexistent_feat"])
        assert "_nonexistent_feat" in order

    def test_empty_list(self):
        assert resolve_order([]) == []


# ── run_features ──────────────────────────────────────────────────────────────

class TestRunFeatures:
    def test_runs_registered_feature(self, tmp_path):
        _make_simple_feature("_rf_ok")
        results = run_features(str(tmp_path), enabled_features=["_rf_ok"])
        assert len(results) == 1
        assert results[0].success

    def test_skips_unregistered(self, tmp_path):
        results = run_features(str(tmp_path), enabled_features=["__no_such_feature__"])
        assert len(results) == 1
        assert results[0].skipped

    def test_failed_feature_captured(self, tmp_path):
        _make_simple_feature("_rf_fail", raises=True)
        results = run_features(str(tmp_path), enabled_features=["_rf_fail"])
        assert len(results) == 1
        assert not results[0].success
        assert results[0].error is not None

    def test_critical_failure_raises(self, tmp_path):
        _make_simple_feature("_rf_crit", raises=True, critical=True)
        with pytest.raises(RuntimeError):
            run_features(str(tmp_path), enabled_features=["_rf_crit"])

    def test_dependency_order_respected(self, tmp_path):
        execution_order = []

        @register("_dep_first")
        class DepFirst(BaseFeature):
            requires = []
            def run(self, run_dir, config):
                execution_order.append("first")
                return FeatureResult(feature="_dep_first", success=True)

        @register("_dep_second")
        class DepSecond(BaseFeature):
            requires = ["_dep_first"]
            def run(self, run_dir, config):
                execution_order.append("second")
                return FeatureResult(feature="_dep_second", success=True)

        run_features(str(tmp_path), enabled_features=["_dep_second", "_dep_first"])
        assert execution_order == ["first", "second"]

    def test_config_passed_to_feature(self, tmp_path):
        received = {}

        @register("_config_test")
        class ConfigTest(BaseFeature):
            requires = []
            def run(self, run_dir, config):
                received["llm"] = config.llm
                return FeatureResult(feature="_config_test", success=True)

        sentinel = object()
        run_features(str(tmp_path), enabled_features=["_config_test"], llm=sentinel)
        assert received["llm"] is sentinel

    def test_is_available_false_skips(self, tmp_path):
        @register("_unavailable")
        class Unavailable(BaseFeature):
            requires = []
            def is_available(self, config):
                return False
            def skip_reason_if_unavailable(self, config):
                return "No LLM available"
            def run(self, run_dir, config):
                return FeatureResult(feature="_unavailable", success=True)

        results = run_features(str(tmp_path), enabled_features=["_unavailable"])
        assert results[0].skipped
        assert "LLM" in results[0].skip_reason

    def test_duration_recorded(self, tmp_path):
        _make_simple_feature("_dur_test")
        results = run_features(str(tmp_path), enabled_features=["_dur_test"])
        # ``>= 0.0`` is trivially true and Windows' 15 ms timer resolution
        # often gives exactly 0.0 for fast features.  At minimum verify the
        # field is the right type and non-negative — this catches regressions
        # that drop the field or set it to ``None`` / a wrong type.
        assert isinstance(results[0].duration_seconds, float)
        assert results[0].duration_seconds >= 0.0

    def test_returns_empty_for_empty_input(self, tmp_path):
        assert run_features(str(tmp_path), enabled_features=[]) == []


# ── format_results ────────────────────────────────────────────────────────────

class TestFormatResults:
    def test_returns_string(self):
        results = [
            FeatureResult(feature="f1", success=True, summary="ok"),
            FeatureResult(feature="f2", success=False, error="boom"),
        ]
        text = format_results(results)
        assert isinstance(text, str)
        assert "f1" in text
        assert "f2" in text

    def test_skipped_shown(self):
        results = [FeatureResult(feature="f", success=True,
                                  skipped=True, skip_reason="no llm")]
        text = format_results(results)
        assert "SKIP" in text

    def test_empty_results(self):
        text = format_results([])
        assert isinstance(text, str)


# ── Cancellation integration ──────────────────────────────────────────────────

class TestRunFeaturesCancellation:
    """Regression tests: OperationCancelledError must propagate from run_features."""

    def setup_method(self):
        # Snapshot and clear the registry so tests start with a known-empty state.
        # We must restore on teardown — not just clear — to avoid corrupting the
        # autouse _clean_registry fixture's `before` snapshot for subsequent tests.
        self._registry_backup = dict(_REGISTRY)
        _REGISTRY.clear()

    def teardown_method(self):
        # Restore original features so the autouse fixture's cleanup loop sees the
        # correct `before` set and does not leave _REGISTRY empty for later tests.
        _REGISTRY.clear()
        _REGISTRY.update(self._registry_backup)

    def _make_cancelling_feature(self, name: str, critical: bool = False):
        """Feature whose run() raises OperationCancelledError (simulates internal checkpoint)."""
        _critical = critical

        def _run_impl(self, run_dir, config):
            raise OperationCancelledError("cancelled inside feature")

        cls = type(
            f"_CancellingFeature_{name}",
            (BaseFeature,),
            {
                "name": name,
                "label": name,
                "requires": [],
                "critical": _critical,
                "run": _run_impl,
            },
        )
        _REGISTRY[name] = cls
        return cls

    def _make_tracking_feature(self, name: str, ran: list):
        """Feature that records it ran."""
        _ran = ran

        def _run_impl(self, run_dir, config):
            _ran.append(name)
            return FeatureResult(feature=name, success=True, summary="ok")

        cls = type(
            f"_TrackingFeature_{name}",
            (BaseFeature,),
            {"name": name, "label": name, "requires": [], "critical": False, "run": _run_impl},
        )
        _REGISTRY[name] = cls
        return cls

    def test_cancelled_inside_non_critical_feature_propagates(self):
        """
        Regression: OperationCancelledError raised inside instance.run() for a
        non-critical feature was caught by `except Exception` and silently
        converted to FeatureResult(success=False), allowing subsequent features
        to still execute.  It must now propagate unconditionally.
        """
        ran: list = []
        self._make_cancelling_feature("cancel_me", critical=False)
        self._make_tracking_feature("should_not_run", ran)

        with pytest.raises(OperationCancelledError):
            run_features("/tmp", enabled_features=["cancel_me", "should_not_run"])

        assert ran == [], "subsequent feature must not run after OperationCancelledError"

    def test_cancelled_inside_critical_feature_also_propagates(self):
        """Cancellation propagates even for critical=True features (already did, parity check)."""
        ran: list = []
        self._make_cancelling_feature("cancel_critical", critical=True)
        self._make_tracking_feature("should_not_run", ran)

        with pytest.raises(OperationCancelledError):
            run_features("/tmp", enabled_features=["cancel_critical", "should_not_run"])

        assert ran == []

    def test_between_feature_cancellation_via_scope(self):
        """Cancellation via token fires at the between-feature checkpoint."""
        ran: list = []
        token = CancellationToken()
        self._make_tracking_feature("f1", ran)
        self._make_tracking_feature("f2", ran)

        token.cancel()
        with cancellation_scope(token):
            with pytest.raises(OperationCancelledError):
                run_features("/tmp", enabled_features=["f1", "f2"])

        # f1 may or may not have run depending on timing; f2 must not have run
        assert "f2" not in ran
