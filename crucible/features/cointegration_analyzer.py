"""
features/cointegration_analyzer.py
====================================
Pairs Trading + Cointegration Analysis for the Crucible pipeline.

Implements the Augmented Dickey-Fuller (ADF) unit-root test, OLS-based hedge
ratio estimation, spread half-life computation, and a simple ±2σ strategy
Sharpe estimator — all in pure Python stdlib with no NumPy/Pandas dependency.

Usage::

    from crucible.features.cointegration_analyzer import (
        CointegrationConfig,
        run_cointegration_analysis,
    )

    result = run_cointegration_analysis("/path/to/saved_projects/my_run")
    if result.n_cointegrated > 0:
        bp = result.best_pair
        print(f"Best: {bp.asset_a}/{bp.asset_b}  z={bp.current_z_score:.2f}  signal={bp.signal}")
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ── Env helpers ───────────────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_bool(name: str, default: bool) -> bool:
    return _env.env_bool(name, default)

def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)

def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default, finite_only=True)

def _env_str(name: str, default: str) -> str:
    return _env.env_str_passthrough(name, default)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class CointegrationConfig:
    min_observations: int = 60
    significance_level: float = 0.05
    adf_max_lag: int = 5
    half_life_min: float = 1.0
    half_life_max: float = 60.0
    spread_z_threshold: float = 2.0


@dataclass
class ADFResult:
    test_stat: float
    p_value: float
    critical_values: Dict[str, float]   # "1%", "5%", "10%"
    is_stationary: bool
    lags_used: int


@dataclass
class PairResult:
    asset_a: str
    asset_b: str
    hedge_ratio: float
    spread_mean: float
    spread_std: float
    half_life_days: Optional[float]
    adf_result: ADFResult
    correlation: float
    is_cointegrated: bool
    current_z_score: float
    signal: str                # "BUY_SPREAD" | "SELL_SPREAD" | "HOLD"
    sharpe_estimate: float


@dataclass
class CointegrationResult:
    pairs: List[PairResult] = field(default_factory=list)
    n_assets_tested: int = 0
    n_pairs_tested: int = 0
    n_cointegrated: int = 0
    best_pair: Optional[PairResult] = None
    report_path: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── MacKinnon (1994) critical-value constants ─────────────────────────────────
# Table from MacKinnon 1994, "Numerical Distribution Functions…"
# nc (no constant) variant; we use the with-constant variant (c).
# Format: (cv_base, cv_inf, cv_inf2) for each significance level.
_MACKINNON_C: Dict[str, Tuple[float, float, float]] = {
    "1%":  (-3.43035, -6.5393, -16.786),
    "5%":  (-2.86154, -2.8903,  -4.234),
    "10%": (-2.56677, -1.5495,  -1.361),
}

# Logistic coefficients for approximate p-value mapping (fitted to MacKinnon table).
# p ≈ 1 / (1 + exp(a + b * t_stat))
# Fitted via two-point solution on the 1% and 5% MacKinnon critical values:
#   t=-3.43 → p=0.01,  t=-2.86 → p=0.05
_ADF_PVAL_A: float = -5.3382
_ADF_PVAL_B: float = -2.8960


# ── Math helpers ──────────────────────────────────────────────────────────────

def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _mean(series: List[float]) -> float:
    if not series:
        return 0.0
    return sum(series) / len(series)


def _variance(series: List[float], ddof: int = 0) -> float:
    n = len(series)
    if n <= ddof:
        return 0.0
    mu = _mean(series)
    return sum((x - mu) ** 2 for x in series) / (n - ddof)


def _std(series: List[float], ddof: int = 0) -> float:
    v = _variance(series, ddof=ddof)
    # v1.1.0 fourth-pass: align subnormal guard with CLAUDE.md rule.
    return math.sqrt(v) if v > 1e-14 else 0.0


def _covariance(x: List[float], y: List[float], ddof: int = 0) -> float:
    n = min(len(x), len(y))
    if n <= ddof:
        return 0.0
    mx, my = _mean(x[:n]), _mean(y[:n])
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (n - ddof)


def _pearson_r(x: List[float], y: List[float]) -> float:
    cov = _covariance(x, y, ddof=0)
    sx = _std(x, ddof=0)
    sy = _std(y, ddof=0)
    if not (sx > 1e-14) or not (sy > 1e-14):
        return 0.0
    raw = cov / (sx * sy)
    # NaN-aware clamp — same pattern as dynamic_correlation.py.
    # The naive ``max(-1, min(1, nan))`` is order-sensitive in Python
    # and can silently leak NaN into the correlation result when an
    # intermediate ``_covariance`` / ``_mean`` propagates NaN from a
    # series that contained NaN values.
    if not math.isfinite(raw):
        return 0.0
    return max(-1.0, min(1.0, raw))


# ── OLS via normal equations ──────────────────────────────────────────────────

def _compute_ols_beta(x: List[float], y: List[float]) -> Tuple[float, float]:
    """
    Ordinary least squares: y = slope * x + intercept.

    Returns (slope, intercept).
    Raises ValueError if x has zero variance.
    """
    n = min(len(x), len(y))
    if n < 2:
        raise ValueError("OLS requires at least 2 observations.")
    sx = _std(x[:n], ddof=0)
    if not (sx > 1e-14):
        raise ValueError("OLS: x has zero variance.")
    slope = _covariance(x[:n], y[:n], ddof=0) / _variance(x[:n], ddof=0)
    intercept = _mean(y[:n]) - slope * _mean(x[:n])
    return slope, intercept


# ── OLS with design matrix (for ADF regression) ──────────────────────────────

def _ols_full(
    X: List[List[float]],
    y: List[float],
) -> Tuple[List[float], float]:
    """
    OLS regression: y = X @ beta.
    X is a list of row vectors (including the constant column if needed).

    Returns (beta_list, residual_std_error).
    Uses Gaussian elimination with partial pivoting.
    """
    n = len(y)
    k = len(X[0])

    # Build X^T X  (k x k)  and  X^T y  (k,)
    XTX: List[List[float]] = [[0.0] * k for _ in range(k)]
    XTy: List[float] = [0.0] * k
    for i in range(n):
        row = X[i]
        for r in range(k):
            XTy[r] += row[r] * y[i]
            for c in range(k):
                XTX[r][c] += row[r] * row[c]

    # Augment [XTX | XTy]
    aug: List[List[float]] = [XTX[r] + [XTy[r]] for r in range(k)]

    # Gaussian elimination with partial pivoting
    for col in range(k):
        # Find pivot
        pivot_row = max(range(col, k), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        if abs(aug[col][col]) < 1e-14:
            raise ValueError(f"OLS: singular matrix at column {col}.")
        pivot = aug[col][col]
        aug[col] = [v / pivot for v in aug[col]]
        for r in range(k):
            if r != col:
                factor = aug[r][col]
                aug[r] = [aug[r][c] - factor * aug[col][c] for c in range(k + 1)]

    beta: List[float] = [aug[r][k] for r in range(k)]

    # Residual std error
    ss_res = 0.0
    for i in range(n):
        y_hat = sum(X[i][j] * beta[j] for j in range(k))
        ss_res += (y[i] - y_hat) ** 2
    dof = n - k
    rse = math.sqrt(ss_res / dof) if dof > 0 else 0.0

    return beta, rse


def _ols_se(X: List[List[float]], rse: float) -> List[float]:
    """
    Return standard errors of OLS coefficients given the design matrix and RSE.
    SE = sqrt(diag((X^T X)^{-1})) * rse
    """
    k = len(X[0])
    n = len(X)

    XTX: List[List[float]] = [[0.0] * k for _ in range(k)]
    for row in X:
        for r in range(k):
            for c in range(k):
                XTX[r][c] += row[r] * row[c]

    # Invert via augmented row reduction
    aug: List[List[float]] = [
        XTX[r] + [1.0 if r == c else 0.0 for c in range(k)]
        for r in range(k)
    ]
    for col in range(k):
        pivot_row = max(range(col, k), key=lambda r: abs(aug[r][col]))
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        if abs(aug[col][col]) < 1e-14:
            return [float("nan")] * k
        pivot = aug[col][col]
        aug[col] = [v / pivot for v in aug[col]]
        for r in range(k):
            if r != col:
                factor = aug[r][col]
                aug[r] = [aug[r][c] - factor * aug[col][c] for c in range(2 * k)]

    inv_diag = [aug[r][k + r] for r in range(k)]
    return [math.sqrt(max(0.0, d)) * rse for d in inv_diag]


# ── ADF critical values (MacKinnon 1994) ──────────────────────────────────────

def _mackinnon_cv(n: int) -> Dict[str, float]:
    """
    Return sample-size-adjusted critical values using MacKinnon (1994) formula:
    cv_adj = cv_base + cv_inf/n + cv_inf2/n²
    """
    result: Dict[str, float] = {}
    for pct, (cv_base, cv_inf, cv_inf2) in _MACKINNON_C.items():
        if n >= 500:
            result[pct] = cv_base
        else:
            result[pct] = cv_base + cv_inf / n + cv_inf2 / (n * n)
    return result


def _adf_pvalue(t_stat: float) -> float:
    """
    Approximate p-value via logistic function fit to MacKinnon critical values.
    p ≈ 1 / (1 + exp(a + b * t_stat))
    Clamped to [0.001, 0.999].
    """
    try:
        p = 1.0 / (1.0 + math.exp(_ADF_PVAL_A + _ADF_PVAL_B * t_stat))
    except OverflowError:
        p = 0.0
    return max(0.001, min(0.999, p))


# ── ADF test ──────────────────────────────────────────────────────────────────

def _aic(rss: float, n: int, k: int) -> float:
    """Akaike Information Criterion for lag selection."""
    # Subnormal-safe guard: ``rss <= 0.0`` admits IEEE 754 subnormals
    # such as 5e-324, for which ``log(rss/n)`` underflows to -inf and
    # the lag selector then prefers the *most* degenerate fit.
    if n <= 0 or not (rss > 1e-14):
        return float("inf")
    return n * math.log(rss / n) + 2 * k


def _adf_test(series: List[float], max_lag: int = 5) -> ADFResult:
    """
    Augmented Dickey-Fuller unit-root test (pure Python).

    Regression: Δy_t = α + β·y_{t-1} + Σ_{i=1}^{p} γ_i·Δy_{t-i} + ε

    The lag order p is chosen by AIC minimisation over [0, max_lag].
    The null hypothesis is that the series has a unit root (β = 0).
    A significantly negative t-stat for β rejects the null → series is stationary.
    """
    n = len(series)
    if n < 10:
        cv = _mackinnon_cv(n)
        return ADFResult(
            test_stat=0.0,
            p_value=1.0,
            critical_values=cv,
            is_stationary=False,
            lags_used=0,
        )

    # First differences
    dy: List[float] = [series[t] - series[t - 1] for t in range(1, n)]

    best_lag = 0
    best_aic = float("inf")

    for lag in range(0, min(max_lag, n // 5) + 1):
        # Build regression matrices for this lag
        T = len(dy) - lag          # number of observations in regression
        if T < lag + 3:
            break

        X_rows: List[List[float]] = []
        y_rows: List[float] = []

        for t in range(lag, len(dy)):
            # level at t (= series[t] since dy has index shifted by 1)
            y_lag1 = series[t]     # y_{t-1} in original series (0-indexed: series[t])
            row = [1.0, y_lag1]    # constant, y_{t-1}
            for i in range(1, lag + 1):
                row.append(dy[t - i])   # Δy_{t-i}
            X_rows.append(row)
            y_rows.append(dy[t])

        try:
            beta, rse = _ols_full(X_rows, y_rows)
        except ValueError:
            continue

        # RSS for AIC
        ss_res = sum(
            (y_rows[i] - sum(X_rows[i][j] * beta[j] for j in range(len(beta)))) ** 2
            for i in range(len(y_rows))
        )
        k_params = len(beta)
        aic_val = _aic(ss_res, len(y_rows), k_params)
        if aic_val < best_aic:
            best_aic = aic_val
            best_lag = lag

    # Re-estimate with best_lag
    X_best: List[List[float]] = []
    y_best: List[float] = []
    for t in range(best_lag, len(dy)):
        y_lag1 = series[t]
        row = [1.0, y_lag1]
        for i in range(1, best_lag + 1):
            row.append(dy[t - i])
        X_best.append(row)
        y_best.append(dy[t])

    cv = _mackinnon_cv(len(y_best))

    try:
        beta_best, rse_best = _ols_full(X_best, y_best)
        se_list = _ols_se(X_best, rse_best)
        # beta[1] is the coefficient on y_{t-1}; its SE is se_list[1]
        se_beta = se_list[1] if len(se_list) > 1 else 1.0
        # `se_beta <= 0.0` would let IEEE 754 subnormal floats (~5e-324) through
        # and produce ~1e+300 t-statistics → false stationarity conclusions.
        # `not (se_beta > 1e-14)` rejects subnormals and zero in one check.
        if not math.isfinite(se_beta) or not (se_beta > 1e-14):
            se_beta = 1.0
        t_stat = beta_best[1] / se_beta
    except (ValueError, ZeroDivisionError, IndexError):
        return ADFResult(
            test_stat=0.0,
            p_value=1.0,
            critical_values=cv,
            is_stationary=False,
            lags_used=best_lag,
        )

    p_val = _adf_pvalue(t_stat)
    is_stat = t_stat < cv["5%"]   # reject H0 at 5% level → stationary

    return ADFResult(
        test_stat=t_stat,
        p_value=p_val,
        critical_values=cv,
        is_stationary=is_stat,
        lags_used=best_lag,
    )


# ── Half-life of mean reversion ───────────────────────────────────────────────

def _compute_half_life(spread: List[float]) -> Optional[float]:
    """
    Estimate half-life of mean reversion via OLS:
    Δspread_t = α + β·spread_{t-1}

    half_life = -ln(2) / β  (β must be negative for mean reversion).
    Returns None if β ≥ 0 (no mean reversion) or on numerical failure.
    """
    n = len(spread)
    if n < 5:
        return None
    x = spread[:-1]   # spread_{t-1}
    y = [spread[t] - spread[t - 1] for t in range(1, n)]  # Δspread_t
    try:
        beta, intercept = _compute_ols_beta(x, y)
    except ValueError:
        return None
    if beta >= 0.0 or math.isnan(beta):
        return None
    hl = -math.log(2.0) / beta
    if hl <= 0.0:
        return None
    return hl


# ── Z-score ───────────────────────────────────────────────────────────────────

def _compute_zscore(series: List[float], window: int = 60) -> float:
    """
    Compute rolling Z-score of the last element of *series* using the most
    recent *window* observations.

    Returns 0.0 if std is zero or window is too small.
    """
    if len(series) < 2:
        return 0.0
    w = min(window, len(series))
    recent = series[-w:]
    mu = _mean(recent)
    sigma = _std(recent, ddof=1) if w > 1 else 0.0
    if not (sigma > 1e-12):
        return 0.0
    return (series[-1] - mu) / sigma


# ── Sharpe estimator via ±2σ strategy ────────────────────────────────────────

def _estimate_sharpe(
    spread: List[float],
    z_threshold: float = 2.0,
    window: int = 60,
) -> float:
    """
    Simulate a simple pairs mean-reversion strategy on the spread series and
    return an annualised Sharpe estimate.

    Entry rules:
    - Buy spread when z < -z_threshold
    - Sell spread when z > +z_threshold
    Exit rule: close when |z| < 0.5

    Returns Sharpe = mean(daily_ret) / std(daily_ret) * sqrt(252).
    """
    if len(spread) < window + 10:
        return 0.0

    daily_returns: List[float] = []
    position = 0    # +1 long spread, -1 short spread, 0 flat
    entry_sigma = 1.0  # rolling std at entry; used as stable denominator for P&L

    for t in range(window, len(spread)):
        sub = spread[max(0, t - window): t]
        mu = _mean(sub)
        sigma = _std(sub, ddof=1) if len(sub) > 1 else 0.0
        if not (sigma > 1e-12):
            daily_returns.append(0.0)
            continue
        z = (spread[t] - mu) / sigma

        if position != 0:
            # P&L for holding position: normalise by the spread's rolling std at
            # entry rather than by entry_price.  entry_price is the *spread value*
            # (not a capital amount) and can be near zero for mean-reverting spreads,
            # causing astronomical returns when used as a divisor.  entry_sigma is
            # always > 0 because we capture it only when sigma > 0 at entry time.
            if position == 1:
                ret = (spread[t] - spread[t - 1]) / (entry_sigma + 1e-10)
            else:
                ret = -(spread[t] - spread[t - 1]) / (entry_sigma + 1e-10)
            daily_returns.append(ret)
            # Exit signal
            if abs(z) < 0.5:
                position = 0
                entry_sigma = 1.0
        else:
            daily_returns.append(0.0)
            # Entry signal
            if z < -z_threshold:
                position = 1
                entry_sigma = sigma  # capture current volatility as notional scale
            elif z > z_threshold:
                position = -1
                entry_sigma = sigma  # capture current volatility as notional scale

    if len(daily_returns) < 5:
        return 0.0
    mu_r = _mean(daily_returns)
    sigma_r = _std(daily_returns, ddof=1) if len(daily_returns) > 1 else 0.0
    if not (sigma_r > 1e-12):
        return 0.0
    return mu_r / sigma_r * math.sqrt(252)


# ── Pair analysis ─────────────────────────────────────────────────────────────

def analyze_pairs(
    price_series: Dict[str, List[float]],
    timestamps: List[str],
    config: CointegrationConfig,
) -> CointegrationResult:
    """
    Test all N*(N-1)/2 pairs for cointegration (capped at 100 pairs).

    For each pair (A, B):
    - Estimate hedge ratio β = cov(A,B)/var(B) via OLS
    - Compute spread = A - β·B
    - Run ADF test on spread
    - Compute half-life, Pearson correlation, Z-score, signal, Sharpe

    Returns a sorted CointegrationResult (cointegrated pairs first, then
    highest |correlation|).
    """
    result = CointegrationResult(
        n_assets_tested=len(price_series),
    )

    symbols = sorted(price_series.keys())
    pairs_tested = 0
    pair_results: List[PairResult] = []

    _MAX_PAIRS = 100

    for i in range(len(symbols)):
        if pairs_tested >= _MAX_PAIRS:
            result.warnings.append(
                f"Capped at {_MAX_PAIRS} pairs — not all combinations tested."
            )
            break
        for j in range(i + 1, len(symbols)):
            if pairs_tested >= _MAX_PAIRS:
                break
            sym_a = symbols[i]
            sym_b = symbols[j]
            a = price_series[sym_a]
            b = price_series[sym_b]

            # Align lengths
            n = min(len(a), len(b))
            if n < config.min_observations:
                result.warnings.append(
                    f"Skipping {sym_a}/{sym_b}: only {n} observations "
                    f"(need {config.min_observations})."
                )
                continue

            a = a[:n]
            b = b[:n]
            pairs_tested += 1

            try:
                beta, _ = _compute_ols_beta(b, a)   # A = β·B + α + ε
            except ValueError as exc:
                result.errors.append(f"{sym_a}/{sym_b} OLS failed: {exc}")
                continue

            spread: List[float] = [a[t] - beta * b[t] for t in range(n)]

            s_mean = _mean(spread)
            s_std = _std(spread, ddof=1) if n > 1 else 0.0

            adf = _adf_test(spread, max_lag=config.adf_max_lag)
            hl = _compute_half_life(spread)
            corr = _pearson_r(a, b)
            z = _compute_zscore(spread, window=min(config.min_observations, n))

            # Cointegration decision
            hl_ok = (
                hl is not None
                and config.half_life_min <= hl <= config.half_life_max
            )
            is_coint = adf.is_stationary and hl_ok

            # Signal
            if abs(z) < config.spread_z_threshold:
                signal = "HOLD"
            elif z <= -config.spread_z_threshold:
                # Use <= so that z == -threshold correctly generates BUY_SPREAD.
                # With strict <, a z exactly equal to -threshold falls through to
                # SELL_SPREAD (the else branch) — an inverted signal at the boundary.
                signal = "BUY_SPREAD"
            else:
                signal = "SELL_SPREAD"

            sharpe = _estimate_sharpe(
                spread,
                z_threshold=config.spread_z_threshold,
                window=min(config.min_observations, n),
            )

            pair_results.append(PairResult(
                asset_a=sym_a,
                asset_b=sym_b,
                hedge_ratio=beta,
                spread_mean=s_mean,
                spread_std=s_std,
                half_life_days=hl,
                adf_result=adf,
                correlation=corr,
                is_cointegrated=is_coint,
                current_z_score=z,
                signal=signal,
                sharpe_estimate=sharpe,
            ))

    # Sort: cointegrated first, then by |correlation| descending
    pair_results.sort(key=lambda p: (not p.is_cointegrated, -abs(p.correlation)))

    n_coint = sum(1 for p in pair_results if p.is_cointegrated)
    best: Optional[PairResult] = None
    if n_coint > 0:
        # Among cointegrated: prefer highest Sharpe estimate
        coint_pairs = [p for p in pair_results if p.is_cointegrated]
        best = max(coint_pairs, key=lambda p: p.sharpe_estimate)

    result.pairs = pair_results
    result.n_pairs_tested = pairs_tested
    result.n_cointegrated = n_coint
    result.best_pair = best
    return result


# ── JSON serialisation helper ─────────────────────────────────────────────────

def _sanitize_float(v: object) -> object:
    """Replace NaN/Inf with None for JSON compatibility."""
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
    return v


def _pair_to_dict(p: PairResult) -> Dict:
    return {
        "asset_a": p.asset_a,
        "asset_b": p.asset_b,
        "hedge_ratio": _sanitize_float(p.hedge_ratio),
        "spread_mean": _sanitize_float(p.spread_mean),
        "spread_std": _sanitize_float(p.spread_std),
        "half_life_days": _sanitize_float(p.half_life_days),
        "correlation": _sanitize_float(p.correlation),
        "is_cointegrated": p.is_cointegrated,
        "current_z_score": _sanitize_float(p.current_z_score),
        "signal": p.signal,
        "sharpe_estimate": _sanitize_float(p.sharpe_estimate),
        "adf": {
            "test_stat": _sanitize_float(p.adf_result.test_stat),
            "p_value": _sanitize_float(p.adf_result.p_value),
            "critical_values": {
                k: _sanitize_float(v)
                for k, v in p.adf_result.critical_values.items()
            },
            "is_stationary": p.adf_result.is_stationary,
            "lags_used": p.adf_result.lags_used,
        },
    }


# ── CSV loading ───────────────────────────────────────────────────────────────

def _load_csv_ohlcv(path: str) -> Tuple[List[str], List[float]]:
    """
    Load a CSV file and return (timestamps, close_prices).

    Accepts any of the common column orderings:
    - date, open, high, low, close, volume
    - timestamp, close
    - date, close
    Falls back to the first numeric column if "close" is not found.
    """
    timestamps: List[str] = []
    prices: List[float] = []

    # Determine CSV dialect from the first 4 kB, then re-open for full read.
    # Both open() calls use context managers to prevent file-handle leaks if
    # csv.DictReader construction raises after the open succeeds.
    dialect = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            sample = fh.read(4096)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except Exception:
        pass  # fall back to default dialect below

    # fieldnames and orig_fields are captured inside the with-block while the
    # DictReader is still live (after list(reader) the header is already
    # populated, but we must read it before the file handle closes).
    fieldnames: list = []
    orig_fields: list = []
    try:
        if dialect is not None:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh_obj:
                reader = csv.DictReader(fh_obj, dialect=dialect)
                rows = list(reader)
                fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
                orig_fields = list(reader.fieldnames or [])
        else:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh_obj:
                reader = csv.DictReader(fh_obj)
                rows = list(reader)
                fieldnames = [f.strip().lower() for f in (reader.fieldnames or [])]
                orig_fields = list(reader.fieldnames or [])
    except Exception:
        return [], []

    if not rows:
        return [], []

    # Identify date column
    date_candidates = ["date", "timestamp", "time", "datetime", "index"]
    date_col: Optional[str] = None
    for cand in date_candidates:
        if cand in fieldnames:
            date_col = cand
            break
    if date_col is None and fieldnames:
        date_col = fieldnames[0]

    # Identify price column
    price_candidates = ["close", "adj close", "adjusted_close", "price", "last"]
    price_col: Optional[str] = None
    for cand in price_candidates:
        if cand in fieldnames:
            price_col = cand
            break
    if price_col is None:
        # Fallback: first numeric column that is not the date column
        for fn in fieldnames:
            if fn == date_col:
                continue
            try:
                float(rows[0].get(fn, "nan"))
                price_col = fn
                break
            except (ValueError, TypeError):
                continue

    if price_col is None:
        return [], []

    # Rebuild original-case mapping for actual DictReader keys
    # (orig_fields was captured inside the with-block above)
    lower_to_orig = {f.strip().lower(): f for f in orig_fields}
    real_date_col = lower_to_orig.get(date_col, date_col) if date_col else None
    real_price_col = lower_to_orig.get(price_col, price_col) if price_col else None

    for row in rows:
        # date
        ts = str(row.get(real_date_col, "")).strip() if real_date_col else ""
        # price
        try:
            price = float(str(row.get(real_price_col, "nan")).strip().replace(",", ""))
        except (ValueError, TypeError):
            continue
        if math.isnan(price) or math.isinf(price) or price <= 0.0:
            continue
        timestamps.append(ts)
        prices.append(price)

    return timestamps, prices


# ── Timestamp alignment (forward-fill) ───────────────────────────────────────

def _align_series(
    series_dict: Dict[str, Tuple[List[str], List[float]]],
) -> Tuple[List[str], Dict[str, List[float]]]:
    """
    Align all price series on a common sorted timestamp union.
    Missing values are forward-filled (last-observation-carried-forward).
    Returns (common_timestamps, aligned_price_dict).
    """
    # Collect all timestamps
    all_ts: set = set()
    for ts_list, _ in series_dict.values():
        all_ts.update(ts_list)

    if not all_ts:
        return [], {}

    sorted_ts = sorted(all_ts)

    # Phase 1: build forward-filled arrays and record each symbol's first valid
    # index relative to sorted_ts.
    filled_arrays: Dict[str, List[float]] = {}
    first_valid_indices: Dict[str, int] = {}
    for sym, (ts_list, prices) in series_dict.items():
        ts_map: Dict[str, float] = {}
        for t, p in zip(ts_list, prices):
            ts_map[t] = p

        filled: List[float] = []
        last_price = float("nan")
        for ts in sorted_ts:
            if ts in ts_map:
                last_price = ts_map[ts]
            filled.append(last_price)

        first_valid = next((i for i, v in enumerate(filled) if not math.isnan(v)), None)
        if first_valid is None:
            continue  # symbol has no valid prices — skip entirely
        filled_arrays[sym] = filled
        first_valid_indices[sym] = first_valid

    if not filled_arrays:
        return [], {}

    # Phase 2: use a global cut-point so every aligned array and sorted_ts are
    # the same length.  Each symbol's individual first_valid may differ; taking
    # the maximum ensures no series starts with a NaN after trimming, and all
    # arrays are identically sized so callers can zip them with sorted_ts safely.
    global_first = max(first_valid_indices.values())
    aligned: Dict[str, List[float]] = {
        sym: filled_arrays[sym][global_first:]
        for sym in filled_arrays
    }

    return sorted_ts[global_first:], aligned


# ── Run-mode guard ────────────────────────────────────────────────────────────

def _is_quant_run(run_dir: str) -> bool:
    """Return True if this is a quant-mode run (or mode unknown)."""
    result_path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(result_path):
        return True
    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode = str(data.get("mode", "")).lower()
        return mode in ("quant", "")
    except (OSError, json.JSONDecodeError):
        return True


# ── Public API ────────────────────────────────────────────────────────────────

def run_cointegration_analysis(
    run_dir: str,
    config: Optional[CointegrationConfig] = None,
) -> CointegrationResult:
    """
    Load OHLCV data from ``{run_dir}/code/data/*.csv`` and run cointegration
    analysis on all discovered asset pairs.

    - Saves ``{run_dir}/cointegration_report.json``
    - Returns a CointegrationResult (never raises)
    """
    if config is None:
        config = CointegrationConfig()

    result = CointegrationResult()

    if not _is_quant_run(run_dir):
        result.warnings.append("Cointegration analysis skipped: not a quant-mode run.")
        return result

    data_dir = os.path.join(run_dir, "code", "data")
    if not os.path.isdir(data_dir):
        result.warnings.append(
            f"Data directory not found: {data_dir}. "
            "No CSV files to load."
        )
        return result

    raw_series: Dict[str, Tuple[List[str], List[float]]] = {}
    for fname in sorted(os.listdir(data_dir)):
        if not fname.lower().endswith(".csv"):
            continue
        symbol = re.sub(r"\.(csv|CSV)$", "", fname)
        fpath = os.path.join(data_dir, fname)
        try:
            ts_list, prices = _load_csv_ohlcv(fpath)
        except Exception as exc:
            result.errors.append(f"Failed to load {fname}: {exc}")
            continue
        if len(prices) < config.min_observations:
            result.warnings.append(
                f"Skipped {fname}: only {len(prices)} rows "
                f"(need ≥ {config.min_observations})."
            )
            continue
        raw_series[symbol] = (ts_list, prices)

    if len(raw_series) < 2:
        result.warnings.append(
            f"Need ≥ 2 assets for pairs analysis (found {len(raw_series)})."
        )
        _save_report(run_dir, result)
        return result

    # Align on common timestamps
    common_ts, aligned = _align_series(raw_series)

    # Drop assets with too many NaN after alignment
    clean: Dict[str, List[float]] = {}
    for sym, prices in aligned.items():
        valid_count = sum(1 for p in prices if not math.isnan(p))
        if valid_count >= config.min_observations:
            clean[sym] = prices
        else:
            result.warnings.append(
                f"Asset {sym} dropped after alignment: "
                f"only {valid_count} valid observations."
            )

    if len(clean) < 2:
        result.warnings.append("Need ≥ 2 assets with sufficient data after alignment.")
        _save_report(run_dir, result)
        return result

    _log.info(
        "Cointegration: %d assets, %d timestamps", len(clean), len(common_ts)
    )

    # Preserve warnings accumulated during alignment/filtering before replacing
    # `result` — analyze_pairs() returns a fresh CointegrationResult() with its
    # own empty .warnings list, which would silently discard any warnings about
    # assets dropped due to insufficient post-alignment observations.
    _pre_warnings = result.warnings[:]
    result = analyze_pairs(clean, common_ts, config)
    result.warnings[:0] = _pre_warnings  # prepend; keeps analyze_pairs' warnings at end

    _save_report(run_dir, result)
    return result


def _save_report(run_dir: str, result: CointegrationResult) -> None:
    """Persist result to cointegration_report.json."""
    report_path = os.path.join(run_dir, "cointegration_report.json")
    payload: Dict = {
        "n_assets_tested": result.n_assets_tested,
        "n_pairs_tested": result.n_pairs_tested,
        "n_cointegrated": result.n_cointegrated,
        "best_pair": _pair_to_dict(result.best_pair) if result.best_pair else None,
        "pairs": [_pair_to_dict(p) for p in result.pairs],
        "errors": result.errors,
        "warnings": result.warnings,
    }
    _tmp_path = report_path + ".tmp"
    try:
        os.makedirs(run_dir, exist_ok=True)
        with open(_tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        result.report_path = report_path
    except OSError as exc:
        result.errors.append(f"Failed to save report: {exc}")
