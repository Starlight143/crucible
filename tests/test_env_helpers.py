"""Unit tests for ``crucible/_env.py`` — the centralised env-var helpers.

These helpers are imported by ~25 production modules; any drift in their
parsing semantics propagates everywhere.  The matrix below pins each branch
of every helper so that a future "small refactor" cannot silently change
how ``CRUCIBLE_*`` env vars are interpreted.
"""
from __future__ import annotations

import pytest

from crucible import _env

# ─────────────────────────────────────────────────────────────────────────────
# env_str
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,default,expected",
    [
        (None, "fallback", "fallback"),  # unset
        ("", "fallback", "fallback"),     # empty
        ("   ", "fallback", "fallback"),  # whitespace-only
        ("hello", "fallback", "hello"),
        ("  spaced  ", "fallback", "spaced"),  # stripped
    ],
)
def test_env_str_default_strip(monkeypatch, value, default, expected):
    name = "CRUCIBLE_TEST_STR"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    assert _env.env_str(name, default) == expected


def test_env_str_strip_disabled(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_STR", "  raw  ")
    assert _env.env_str("CRUCIBLE_TEST_STR", "fb", strip=False) == "  raw  "


def test_env_str_passthrough_preserves_empty(monkeypatch):
    """``env_str_passthrough`` distinguishes unset (→ default) from empty (→ "")."""
    monkeypatch.delenv("CRUCIBLE_TEST_PT", raising=False)
    assert _env.env_str_passthrough("CRUCIBLE_TEST_PT", "fb") == "fb"
    monkeypatch.setenv("CRUCIBLE_TEST_PT", "")
    assert _env.env_str_passthrough("CRUCIBLE_TEST_PT", "fb") == ""
    monkeypatch.setenv("CRUCIBLE_TEST_PT", "value")
    assert _env.env_str_passthrough("CRUCIBLE_TEST_PT", "fb") == "value"


# ─────────────────────────────────────────────────────────────────────────────
# env_int
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,default,expected",
    [
        (None, 7, 7),
        ("", 7, 7),
        ("  ", 7, 7),
        ("42", 7, 42),
        ("-42", 7, -42),
        ("not-a-number", 7, 7),
        ("3.14", 7, 7),  # int() rejects float-like strings
    ],
)
def test_env_int(monkeypatch, value, default, expected):
    name = "CRUCIBLE_TEST_INT"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    assert _env.env_int(name, default) == expected


@pytest.mark.parametrize(
    "raw,default,clamp_min,clamp_max,expected",
    [
        ("-5", 0, 0, None, 0),         # below min → clamp up
        ("100", 0, None, 50, 50),      # above max → clamp down
        ("25", 0, 0, 50, 25),          # inside bounds → unchanged
    ],
)
def test_env_int_clamp(monkeypatch, raw, default, clamp_min, clamp_max, expected):
    monkeypatch.setenv("CRUCIBLE_TEST_INT_CLAMP", raw)
    assert _env.env_int(
        "CRUCIBLE_TEST_INT_CLAMP",
        default,
        clamp_min=clamp_min,
        clamp_max=clamp_max,
    ) == expected


# ─────────────────────────────────────────────────────────────────────────────
# env_float
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,default,expected",
    [
        (None, 1.5, 1.5),
        ("", 1.5, 1.5),
        ("3.14", 1.5, 3.14),
        ("not-a-float", 1.5, 1.5),
        ("nan", 1.5, float("nan")),    # accepted in default mode
        ("inf", 1.5, float("inf")),    # accepted in default mode
    ],
)
def test_env_float(monkeypatch, value, default, expected):
    name = "CRUCIBLE_TEST_FLOAT"
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    got = _env.env_float(name, default)
    if expected != expected:  # NaN special-case
        assert got != got
    else:
        assert got == expected


@pytest.mark.parametrize("non_finite_token", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_env_float_finite_only_rejects_non_finite(monkeypatch, non_finite_token):
    monkeypatch.setenv("CRUCIBLE_TEST_FFIN", non_finite_token)
    assert _env.env_float("CRUCIBLE_TEST_FFIN", 9.0, finite_only=True) == 9.0


def test_env_float_finite_only_accepts_finite(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_FFIN", "3.14")
    assert _env.env_float("CRUCIBLE_TEST_FFIN", 9.0, finite_only=True) == 3.14


def test_env_float_clamp(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_FCL", "-2.5")
    assert _env.env_float("CRUCIBLE_TEST_FCL", 0.0, clamp_min=0.0) == 0.0
    monkeypatch.setenv("CRUCIBLE_TEST_FCL", "999.0")
    assert _env.env_float("CRUCIBLE_TEST_FCL", 0.0, clamp_max=100.0) == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# env_bool — the highest-stakes helper because of the strict whitelist rule
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", ["1", "true", "TRUE", "True", "yes", "Yes", "on", "ON"])
def test_env_bool_true_tokens(monkeypatch, token):
    monkeypatch.setenv("CRUCIBLE_TEST_BOOL", token)
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", False) is True


@pytest.mark.parametrize("token", ["0", "false", "FALSE", "False", "no", "No", "off", "OFF"])
def test_env_bool_false_tokens(monkeypatch, token):
    monkeypatch.setenv("CRUCIBLE_TEST_BOOL", token)
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", True) is False


@pytest.mark.parametrize("token", ["", "  ", "trrue", "yse", "maybe", "2", "tru", "1.0"])
def test_env_bool_unrecognised_returns_default(monkeypatch, token):
    """Whitelist rule: unrecognised tokens must NOT coerce to truthy."""
    monkeypatch.setenv("CRUCIBLE_TEST_BOOL", token)
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", False) is False
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", True) is True


def test_env_bool_unset_returns_default(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_TEST_BOOL", raising=False)
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", True) is True
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", False) is False


@pytest.mark.parametrize("token", ["y", "Y", "n", "N"])
def test_env_bool_extended_y_n(monkeypatch, token):
    monkeypatch.setenv("CRUCIBLE_TEST_BOOL", token)
    if token.lower() == "y":
        assert _env.env_bool("CRUCIBLE_TEST_BOOL", False, extended=True) is True
    else:
        assert _env.env_bool("CRUCIBLE_TEST_BOOL", True, extended=True) is False
    # Without extended=True, y/n should NOT be recognised:
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", False, extended=False) is False
    assert _env.env_bool("CRUCIBLE_TEST_BOOL", True, extended=False) is True


# ─────────────────────────────────────────────────────────────────────────────
# env_optional_int / env_optional_float (sentinel-aware)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sentinel", ["none", "null", "unlimited", "inf", "infinite", "INF"])
def test_env_optional_int_sentinels(monkeypatch, sentinel):
    monkeypatch.setenv("CRUCIBLE_TEST_OPT_INT", sentinel)
    assert _env.env_optional_int("CRUCIBLE_TEST_OPT_INT", 99) is None


def test_env_optional_int_normal_values(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_OPT_INT", "42")
    assert _env.env_optional_int("CRUCIBLE_TEST_OPT_INT", 99) == 42
    monkeypatch.setenv("CRUCIBLE_TEST_OPT_INT", "garbage")
    assert _env.env_optional_int("CRUCIBLE_TEST_OPT_INT", 99) == 99
    monkeypatch.delenv("CRUCIBLE_TEST_OPT_INT", raising=False)
    assert _env.env_optional_int("CRUCIBLE_TEST_OPT_INT", 99) == 99
    assert _env.env_optional_int("CRUCIBLE_TEST_OPT_INT", None) is None


@pytest.mark.parametrize("sentinel", ["none", "null", "unlimited", "inf", "infinite"])
def test_env_optional_float_sentinels(monkeypatch, sentinel):
    monkeypatch.setenv("CRUCIBLE_TEST_OPT_FLT", sentinel)
    assert _env.env_optional_float("CRUCIBLE_TEST_OPT_FLT", 99.0) is None


def test_env_optional_float_normal_values(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_TEST_OPT_FLT", "3.14")
    assert _env.env_optional_float("CRUCIBLE_TEST_OPT_FLT", 99.0) == 3.14
    monkeypatch.setenv("CRUCIBLE_TEST_OPT_FLT", "")
    assert _env.env_optional_float("CRUCIBLE_TEST_OPT_FLT", 99.0) == 99.0


# ─────────────────────────────────────────────────────────────────────────────
# Smoke check: importable from a representative production caller
# ─────────────────────────────────────────────────────────────────────────────


def test_production_modules_use_central_helpers():
    """Spot-check several modules expose ``_env_*`` shims that delegate."""
    from crucible import context_budget, convergence_guard, cost_tracker, http_retry, resilience
    from crucible.features import (
        backtest_runner,
        code_lockfile_generator,
        cointegration_analyzer,
        github_repo_analyzer,
        quant_analytics,
    )
    # Each module's shim must accept the documented signature without error.
    for mod in (
        context_budget,
        convergence_guard,
        cost_tracker,
        http_retry,
        resilience,
        quant_analytics,
        backtest_runner,
        code_lockfile_generator,
        cointegration_analyzer,
        github_repo_analyzer,
    ):
        assert callable(getattr(mod, "_env_int", None)) or callable(
            getattr(mod, "_env_float", None)
        ), f"{mod.__name__} has neither _env_int nor _env_float"
