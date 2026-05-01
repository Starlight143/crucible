"""
features/options_analyzer.py
==============================
Black-Scholes options pricing and Greeks analysis feature.

Computes call/put prices plus the full set of first-order Greeks (delta,
gamma, theta, vega, rho) for a configurable grid of strikes and maturities.
The normal CDF uses scipy when available, falling back to the classic
Abramowitz & Stegun polynomial approximation (max error < 7.5e-8).

If an ``analysis_result.json`` file is present in the run directory and its
text content matches options-related keywords, the report is flagged as
``options_relevant``.

Environment variables
---------------------
OPTIONS_SPOT_PRICE          Underlying spot price (default: '100').
OPTIONS_RISK_FREE_RATE      Continuous risk-free rate, annualised (default: '0.05').
OPTIONS_VOLATILITY          Implied / historical volatility, annualised (default: '0.20').
OPTIONS_STRIKES_RANGE       Comma-separated strike/spot ratios (default: '0.8,0.9,1.0,1.1,1.2').
OPTIONS_MATURITIES_DAYS     Comma-separated maturities in calendar days
                            (default: '7,30,60,90,180,365').
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from crucible.feature_registry import BaseFeature, FeatureConfig, FeatureResult, register


# ---------------------------------------------------------------------------
# Normal distribution helpers
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Cumulative distribution function of the standard normal distribution.

    Uses scipy.stats when available; otherwise falls back to the Abramowitz
    and Stegun (1964) rational approximation (max absolute error < 7.5e-8).
    """
    try:
        from scipy.stats import norm  # type: ignore[import]
        return float(norm.cdf(x))
    except ImportError:
        pass
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (
        0.319381530
        + t * (
            -0.356563782
            + t * (
                1.781477937
                + t * (-1.821255978 + t * 1.330274429)
            )
        )
    )
    pdf = math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
    cdf = 1.0 - pdf * poly
    return cdf if x >= 0 else 1.0 - cdf


def _norm_pdf(x: float) -> float:
    """Probability density function of the standard normal distribution."""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


# ---------------------------------------------------------------------------
# Black-Scholes pricing and Greeks
# ---------------------------------------------------------------------------

@dataclass
class BSResult:
    """Container for Black-Scholes prices and Greeks."""

    S: float
    K: float
    T: float
    r: float
    sigma: float
    call_price: float
    put_price: float
    delta_call: float
    delta_put: float
    gamma: float
    theta_call: float
    theta_put: float
    vega: float
    rho_call: float
    rho_put: float
    d1: float
    d2: float

    def to_dict(self) -> Dict[str, Any]:
        """Return all fields rounded to 6 decimal places."""
        return {k: round(v, 6) for k, v in self.__dict__.items()}


def black_scholes(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
) -> BSResult:
    """Compute Black-Scholes prices and Greeks.

    Parameters
    ----------
    S:     Current underlying price.
    K:     Strike price.
    T:     Time to expiry in years (> 0).
    r:     Continuously compounded risk-free rate.
    sigma: Annualised volatility (> 0).

    Returns
    -------
    BSResult with prices and Greeks. All Greeks use market conventions:
    * theta   — per calendar day (divided by 365).
    * vega    — per 1 % move in vol (divided by 100).
    * rho     — per 1 % move in rates (divided by 100).
    * delta_put  — negative (Nd1 - 1).

    When T <= 0, sigma <= 0, S <= 0, or K <= 0, all outputs are zero to
    avoid domain errors.
    """
    # Use `not (x > 1e-14)` instead of `x <= 0` to also reject IEEE 754
    # subnormals (~5e-324).  A subnormal sigma or T passes `sigma <= 0` and
    # produces ~1e+300 results in the d1 / gamma denominators.
    if (
        not (T > 1e-14)
        or not (sigma > 1e-14)
        or not (S > 1e-14)
        or not (K > 1e-14)
    ):
        z = 0.0
        return BSResult(S, K, T, r, sigma, z, z, z, z, z, z, z, z, z, z, z, z)

    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    Nd1 = _norm_cdf(d1)
    Nd2 = _norm_cdf(d2)
    Nm_d1 = _norm_cdf(-d1)
    Nm_d2 = _norm_cdf(-d2)

    discount = math.exp(-r * T)
    call = S * Nd1 - K * discount * Nd2
    put = K * discount * Nm_d2 - S * Nm_d1

    gamma = _norm_pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * _norm_pdf(d1) * math.sqrt(T) / 100.0
    theta_call = (
        -S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
        - r * K * discount * Nd2
    ) / 365.0
    theta_put = (
        -S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
        + r * K * discount * Nm_d2
    ) / 365.0
    rho_call = K * T * discount * Nd2 / 100.0
    rho_put = -K * T * discount * Nm_d2 / 100.0

    return BSResult(
        S, K, T, r, sigma,
        call, put,
        Nd1, Nd1 - 1,       # delta_call, delta_put
        gamma,
        theta_call, theta_put,
        vega,
        rho_call, rho_put,
        d1, d2,
    )


# ---------------------------------------------------------------------------
# Keyword detection
# ---------------------------------------------------------------------------

_OPTIONS_KEYWORDS = re.compile(
    r'\b(option|call|put|derivative|hedge|delta.?hedge|gamma|vega|theta'
    r'|implied.?vol|IV.surface|straddle|strangle|butterfly|condor)\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Core analysis function
# ---------------------------------------------------------------------------

def analyze_options(run_dir: str) -> Dict[str, Any]:
    """Build a full options pricing grid and detect strategy relevance.

    Reads environment variables for pricing parameters, scans
    ``analysis_result.json`` for options keywords, computes a
    strike × maturity pricing grid, writes ``options_analysis.json``,
    and returns the report dict.

    Parameters
    ----------
    run_dir:
        Path to the current pipeline run directory.

    Returns
    -------
    Dict with keys: options_relevant, parameters, pricing_grid_size,
    pricing_grid.
    """
    try:
        spot = float(os.environ.get('OPTIONS_SPOT_PRICE', '100'))
        if not math.isfinite(spot) or spot <= 0.0:
            spot = 100.0
    except (TypeError, ValueError):
        spot = 100.0
    try:
        r = float(os.environ.get('OPTIONS_RISK_FREE_RATE', '0.05'))
        if not math.isfinite(r):
            r = 0.05
    except (TypeError, ValueError):
        r = 0.05
    try:
        sigma = float(os.environ.get('OPTIONS_VOLATILITY', '0.20'))
        if not math.isfinite(sigma) or sigma <= 0.0:
            sigma = 0.20
    except (TypeError, ValueError):
        sigma = 0.20
    try:
        strike_pcts: List[float] = [
            float(x.strip())
            for x in os.environ.get('OPTIONS_STRIKES_RANGE', '0.8,0.9,1.0,1.1,1.2').split(',')
            if x.strip()
        ]
        if not strike_pcts:
            strike_pcts = [0.8, 0.9, 1.0, 1.1, 1.2]
    except (TypeError, ValueError):
        strike_pcts = [0.8, 0.9, 1.0, 1.1, 1.2]
    try:
        mat_days: List[int] = [
            int(x.strip())
            for x in os.environ.get('OPTIONS_MATURITIES_DAYS', '7,30,60,90,180,365').split(',')
            if x.strip()
        ]
        if not mat_days:
            mat_days = [7, 30, 60, 90, 180, 365]
    except (TypeError, ValueError):
        mat_days = [7, 30, 60, 90, 180, 365]

    # Detect whether the strategy analysis mentions options concepts
    options_relevant = False
    analysis_path = os.path.join(run_dir, 'analysis_result.json')
    if os.path.isfile(analysis_path):
        try:
            with open(analysis_path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            options_relevant = bool(_OPTIONS_KEYWORDS.search(json.dumps(data)))
        except (OSError, json.JSONDecodeError):
            pass

    # Build strike × maturity pricing grid
    grid: List[Dict[str, Any]] = []
    for pct in strike_pcts:
        K = spot * pct
        for days in mat_days:
            T = days / 365.0
            res = black_scholes(spot, K, T, r, sigma)
            grid.append({
                'strike_pct': pct,
                'strike': round(K, 4),
                'maturity_days': days,
                **res.to_dict(),
            })

    report: Dict[str, Any] = {
        'options_relevant': options_relevant,
        'parameters': {'spot': spot, 'r': r, 'sigma': sigma},
        'pricing_grid_size': len(grid),
        'pricing_grid': grid,
    }

    out_path = os.path.join(run_dir, 'options_analysis.json')
    _tmp_path = out_path + ".tmp"
    try:
        with open(_tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(report, fh, indent=2)
        os.replace(_tmp_path, out_path)
    except (OSError, ValueError):
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass

    return report


# ---------------------------------------------------------------------------
# Feature registration
# ---------------------------------------------------------------------------

@register('options_analyzer')
class OptionsAnalyzerFeature(BaseFeature):
    """Black-Scholes options pricing and Greeks analysis."""

    name = 'options_analyzer'
    label = 'Options Pricing Analyzer'
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        t0 = time.monotonic()
        try:
            report = analyze_options(run_dir)
            relevant = report.get('options_relevant', False)
            grid_size = report.get('pricing_grid_size', 0)
            suffix = (
                ' (strategy is options-relevant)'
                if relevant
                else ' (strategy may not use options)'
            )
            summary = f'Options analysis complete: {grid_size} grid points{suffix}'
            return FeatureResult(
                feature=self.name,
                success=True,
                summary=summary,
                details={'options_relevant': relevant, 'grid_size': grid_size},
                duration_seconds=time.monotonic() - t0,
            )
        except Exception as exc:
            return FeatureResult(
                feature=self.name,
                success=False,
                summary=str(exc),
                error=str(exc),
                duration_seconds=time.monotonic() - t0,
            )
