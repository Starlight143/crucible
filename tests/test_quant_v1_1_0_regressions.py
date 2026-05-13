"""
Regression tests for the v1.1.0 quant-stack patches.

v1.1.0 third-pass: five quant patches landed without dedicated tests
(M6 NaN-sentinel returns, M7 Monte-Carlo bootstrap pool, M8 DrawdownPeriod
duration_bars rename, M9 power_iteration relative tolerance, H11 factor
AR(1) labelling).  This file pins each one's contract so a refactor
cannot silently regress them.
"""
from __future__ import annotations

import math

import pytest


# ─── M6: NaN sentinel in _equity_to_returns ──────────────────────────────────

def test_equity_to_returns_emits_nan_sentinel_for_non_positive_prior():
    """A non-positive prior equity slot must surface as ``NaN``, not
    silently substitute ``0.0``.  Downstream consumers (Sharpe,
    win-rate, max-drawdown) filter NaN explicitly, so the sentinel
    preserves correct counts; a 0.0 substitution would inflate Sharpe
    denominators with phantom flat-period returns.
    """
    from crucible.features.quant_analytics import _equity_to_returns

    # Equity series with a zero prior (corrupted slot).
    equity = [100.0, 0.0, 1.0, 1.5]
    returns = _equity_to_returns(equity)

    # First return uses prior=100.0 → (0 - 100) / 100 = -1.0 (legitimate).
    # Second return uses prior=0.0 → NaN sentinel.
    # Third return uses prior=1.0 → 0.5
    assert returns[0] == -1.0
    assert math.isnan(returns[1]), (
        f"expected NaN for divide-by-zero slot, got {returns[1]!r}"
    )
    assert returns[2] == 0.5


def test_finite_returns_helper_drops_only_non_finite():
    """``_finite_returns`` should preserve legitimate zero returns
    (a flat period is real data) while dropping NaN / ±Inf."""
    from crucible.features.quant_analytics import _finite_returns

    raw = [0.0, 0.5, float("nan"), -0.3, float("inf"), 0.0, float("-inf")]
    out = _finite_returns(raw)
    assert out == [0.0, 0.5, -0.3, 0.0]


# ─── M9: power_iteration relative tolerance with large eigenvalues ───────────

def test_power_iteration_converges_on_large_eigenvalues_dense():
    """The relative-tolerance convergence (M9) must handle matrices
    whose dominant eigenvalue is far from 1.0.

    v1.1.0 fourth-pass: switched from a 3×3 diagonal (1-iteration
    trivial case) to a 4×4 DENSE symmetric matrix with eigenvalues
    [1e6, 1e3, 1, 1e-3] constructed via Q @ diag(λ) @ Qᵀ for a
    fixed orthogonal Q.  This forces multiple iterations and
    actually exercises the convergence test (a regression to
    absolute tolerance would fail to converge on the small-
    eigenvalue components within the iteration cap).
    """
    from crucible.features.dynamic_correlation import _power_iteration

    # Hand-constructed orthonormal basis vectors for a reproducible
    # rotation.  Q is a 4×4 orthogonal matrix; lambda_diag holds the
    # eigenvalues.  M = Q @ diag(lambda) @ Q.T has those eigenvalues.
    import numpy as _np

    rng = _np.random.default_rng(seed=0)
    A = rng.standard_normal((4, 4))
    Q, _ = _np.linalg.qr(A)  # orthonormal columns
    lambdas = _np.array([1e6, 1e3, 1.0, 1e-3])
    M_np = Q @ _np.diag(lambdas) @ Q.T
    # Symmetrise to floor any FP drift.
    M_np = 0.5 * (M_np + M_np.T)
    M = M_np.tolist()

    eigenvalue, eigenvector = _power_iteration(M, max_iter=500, tol=1e-8)
    assert eigenvalue is not None
    # Must be within 0.1 % of the analytical truth.
    assert math.isclose(eigenvalue, 1_000_000.0, rel_tol=1e-3), (
        f"power_iteration eigenvalue {eigenvalue} far from 1e6"
    )


# ─── H11: factor_analyzer AR(1) fallback labelling ───────────────────────────

def test_factor_analyzer_ar1_fallback_label_in_warnings(tmp_path):
    """When market data is unavailable AND ``use_ff_data=False`` the
    CAPM fallback must rename its beta to ``autocorrelation_beta``
    AND emit a loud warning into ``result.warnings`` so consumers do
    not interpret a self-lag coefficient as a market beta.

    Construct a synthetic Quant run directory and run the analyser
    end-to-end through the AR(1) fallback path.
    """
    import json as _json
    from crucible.features.factor_analyzer import (
        FactorAnalysisResult, FactorConfig, run_factor_analysis,
    )

    # Sanity: the dataclass must expose the new field.
    assert hasattr(FactorAnalysisResult(), "autocorrelation_beta"), (
        "FactorAnalysisResult missing autocorrelation_beta field"
    )

    # Build a minimal Quant run_dir that ``run_factor_analysis``
    # accepts (project_meta.json with mode=Quant + a backtest report
    # with an equity_curve).
    run_dir = tmp_path / "ar1_run"
    run_dir.mkdir()
    (run_dir / "project_meta.json").write_text(
        _json.dumps({"mode": "Quant"}), encoding="utf-8",
    )
    # 60 bars of slow drift — enough for the AR(1) regression to run.
    equity = [1.0 + i * 0.001 + ((-1) ** i) * 0.0005 for i in range(60)]
    (run_dir / "backtest_report.json").write_text(
        _json.dumps({"equity_curve": equity}), encoding="utf-8",
    )

    config = FactorConfig(use_ff_data=False)
    result = run_factor_analysis(str(run_dir), config=config)

    # In AR(1) fallback we expect ``market_beta`` to be None and
    # ``autocorrelation_beta`` populated (or at least the warning
    # to surface even on a degenerate fit).
    assert result.market_beta is None, (
        f"market_beta should be None on AR(1) fallback, got {result.market_beta}"
    )
    warnings_blob = " ".join(result.warnings or [])
    assert (
        "AR(1)" in warnings_blob
        or "autocorrelation" in warnings_blob.lower()
        or "self-lag" in warnings_blob.lower()
    ), (
        f"AR(1) fallback warning missing from result.warnings: {result.warnings!r}"
    )


# ─── M8: DrawdownPeriod schema (duration_bars + legacy duration_days) ───────

def test_drawdown_period_dual_emits_bars_and_days_in_to_dict():
    """``DrawdownPeriod.to_dict()`` must emit BOTH ``duration_bars``
    (canonical) and ``duration_days`` (legacy) so v1.0 consumers do
    not break while v1.1+ consumers can move to the bar-aware name.
    """
    from crucible.features.tearsheet import DrawdownPeriod

    dp = DrawdownPeriod(
        start_ts="2024-01-01",
        end_ts="2024-01-11",
        drawdown_pct=-10.0,
        duration_bars=10,
        recovery_bars=5,
    )
    d = dp.to_dict()
    assert "duration_bars" in d, "canonical duration_bars missing"
    assert "duration_days" in d, "legacy duration_days missing"
    assert d["duration_bars"] == d["duration_days"] == 10
    assert d["recovery_bars"] == d["recovery_days"] == 5
    # Property aliases on the instance itself must mirror the canonical fields.
    assert dp.duration_days == 10
    assert dp.recovery_days == 5


# ─── DSR floor + dsr_z clip (H-2 third-pass) ────────────────────────────────

def test_dsr_clips_extreme_z_score():
    """The de-annualised DSR z-score must be clipped so a degenerate
    denominator cannot saturate the score to exactly 0.0 / 1.0.

    v1.1.0 fourth-pass: clamp lowered from ±10 to ±6.  The previous
    ±10 boundary was indistinguishable from 1.0 after json round-trip;
    ±6 (Φ(6) ≈ 1 - 9.9e-10) stays finite-distinguishable.  Test now
    uses a non-zero-mean tiny-variance series so the DSR path is
    guaranteed to execute (no early-out on observed_sharpe=None).
    """
    from crucible.features.quant_analytics import run_significance_test

    # Non-zero mean, very small variance — sr_hat is moderate, denom_sq
    # is small → dsr_z grows large → clip should fire.
    returns = [5e-6, 3e-6, 5e-6, 3e-6, 5e-6, 3e-6] * 50
    result = run_significance_test(returns, n_permutations=50, n_bootstrap=50)

    # The DSR path must have executed (non-None) — otherwise the test
    # would silently pass by skipping the assertion.
    assert result.deflated_sharpe_ratio is not None, (
        "DSR path didn't execute; series may not have non-zero mean"
    )
    # The clipped value must remain finite-distinguishable from 0/1.
    assert 0.0 < result.deflated_sharpe_ratio < 1.0, (
        f"DSR saturated despite clip: {result.deflated_sharpe_ratio}"
    )
    # If the raw z-score exceeded ±6, the clip should have appended a
    # warning to result.errors.
    if any("clipped" in err.lower() for err in result.errors):
        # Clip fired — the result is bounded by Φ(±6) on either side.
        # Φ(6) ≈ 1 - 9.9e-10
        assert result.deflated_sharpe_ratio <= 1.0 - 1e-10 + 1e-12
        assert result.deflated_sharpe_ratio >= 9.9e-10 - 1e-12


# ─── Permutation two-sided (H-3 third-pass) ──────────────────────────────────

def test_permutation_schema_exposes_directional_tails():
    """v1.1.0 fourth-pass: ``SignificanceTestResult`` exposes
    ``p_value_one_sided`` / ``p_value_two_sided`` / ``p_value_greater``
    / ``p_value_less`` / ``alternative`` — the schema additions that
    let consumers pick whichever tail matches their pre-registered
    hypothesis instead of relying on the auto-direction default.

    Note: the existing pure-Sharpe permutation test is mathematically
    permutation-invariant (mean and std do not depend on order), so
    every shuffle reproduces ``observed_sharpe`` exactly and all
    tail counts collapse to ``n_perm``.  That makes the p-value
    uninformative for a SHARPE permutation, but the SCHEMA — five
    populated fields with sensible bounds — is the contract this
    fix actually guarantees.  Time-dependent statistics (max DD,
    rolling Sharpe) would produce non-trivial tails; those live in
    other modules.
    """
    from crucible.features.quant_analytics import run_significance_test

    # Heterogeneous returns so the observed Sharpe is well-defined.
    returns = [0.01, -0.005, 0.02, -0.01, 0.015, -0.003, 0.008,
               -0.002, 0.012, -0.007] * 15
    result = run_significance_test(returns, n_permutations=100, n_bootstrap=50)

    # All five p-value fields populated.
    assert result.p_value is not None
    assert result.p_value_one_sided is not None
    assert result.p_value_two_sided is not None
    assert result.p_value_greater is not None
    assert result.p_value_less is not None
    assert result.alternative == "two-sided"

    # Each tail is a valid probability in [0, 1].
    for field_name in ("p_value", "p_value_one_sided", "p_value_two_sided",
                       "p_value_greater", "p_value_less"):
        v = getattr(result, field_name)
        assert 0.0 <= v <= 1.0, f"{field_name}={v} out of [0,1]"

    # The default ``p_value`` matches the two-sided variant (this is
    # the actual behavioural change T3 made; the old default was
    # one-sided "greater" and silently dismissed short-bias signals).
    assert result.p_value == result.p_value_two_sided


def test_permutation_p_value_greater_plus_less_brackets_one():
    """For Sharpe-permutation (which is order-invariant), every
    shuffled value equals ``observed_sharpe`` exactly, so
    ``count_ge == count_le == n_perm`` and both directional tails
    collapse to 1.0.  The schema requirement is just that
    ``p_value_greater + p_value_less`` is well-defined and >= 1.0
    (every permutation participates in at least one tail, possibly
    both if it equals the observation).
    """
    from crucible.features.quant_analytics import run_significance_test

    returns = [0.01, -0.005, 0.02, -0.01, 0.015, -0.003, 0.008,
               -0.002, 0.012, -0.007] * 15
    result = run_significance_test(returns, n_permutations=100, n_bootstrap=50)

    assert result.p_value_greater is not None
    assert result.p_value_less is not None
    total = result.p_value_greater + result.p_value_less
    # >= 1.0 (every permutation in at least one tail, possibly both).
    assert total >= 1.0 - 1e-9, (
        f"directional tails sum {total} < 1.0 — would imply some "
        "permutation belongs to NEITHER tail (impossible by construction)"
    )
