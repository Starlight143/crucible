"""
Pin the ``BACKTEST_REQUIRE_REAL_DATA`` env-bool default behaviour.

v1.1.0 third-pass: CLAUDE.md §8 mandates that the synthetic-fallback
guard is ON by default (``.env.example`` ships ``=1``) and that typos
(``yse``, ``ture``, empty string) fall through to the default ON rather
than silently coercing to truthy.  These cases were not covered by any
test before this round; a regression in ``env_bool`` would have flipped
silent synthetic-data injection back on in production.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from crucible.features.backtest_runner import _require_real_data_active


@pytest.mark.parametrize(
    "env_value,expected",
    [
        # Whitelist truth values → guard ON
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("yes", True),
        ("YES", True),
        ("on", True),
        ("ON", True),
        # Whitelist falsy values → guard OFF
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
        # Empty string → falls through to default
        ("", True),
        # Typos → must NOT silent-truthy; fall through to default (ON)
        ("yse", True),
        ("ture", True),
        ("nope", True),
        ("garbage", True),
        ("2", True),  # not in the whitelist
    ],
)
def test_backtest_require_real_data_active_env_bool_whitelist(
    env_value: str, expected: bool,
):
    """The default is ON and only the documented whitelist tokens flip
    it off.  Any other value (typo, empty, unexpected) must keep the
    guard ON — silent coercion to True or False would be a security
    regression for the data-integrity contract.
    """
    with patch.dict(
        os.environ,
        {"BACKTEST_REQUIRE_REAL_DATA": env_value},
        clear=False,
    ):
        assert _require_real_data_active() is expected


def test_backtest_require_real_data_active_unset_defaults_on():
    """When the variable is entirely absent from os.environ, the guard
    must default to ON.  ``.env.example`` ships ``=1`` so production
    operators see this default; tests must pin it independently.
    """
    env_copy = {k: v for k, v in os.environ.items()
                if k != "BACKTEST_REQUIRE_REAL_DATA"}
    with patch.dict(os.environ, env_copy, clear=True):
        assert _require_real_data_active() is True
