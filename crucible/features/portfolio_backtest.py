"""
features/portfolio_backtest.py
================================
Portfolio-level backtesting: combine multiple strategy runs into a weighted portfolio.

Reads individual backtest results (``backtest_report.json`` or
``sample_out/ledger.csv``) from each run directory, aligns equity curves on
a common timeline, and computes portfolio-level risk metrics using only the
Python standard library (no numpy, no pandas).

Usage::

    from crucible.features.portfolio_backtest import run_portfolio_backtest

    result = run_portfolio_backtest(
        run_dirs=["saved_projects/run_a", "saved_projects/run_b"],
        weights=[0.6, 0.4],   # must sum to 1.0
    )
    print(result.portfolio_sharpe, result.portfolio_max_drawdown)

Environment variables::

    PORTFOLIO_REBALANCE_PERIOD   "daily"|"weekly"|"monthly" (default "monthly")
    PORTFOLIO_RISK_FREE_RATE     Annualised risk-free rate as a decimal (default 0.04)
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)


# ── Environment helpers ───────────────────────────────────────────────────────


try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_str(name: str, default: str) -> str:
    return _env.env_str(name, default)


def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default, finite_only=True)


_DEFAULT_REBALANCE_PERIOD: str = _env_str("PORTFOLIO_REBALANCE_PERIOD", "monthly")
_DEFAULT_RISK_FREE_RATE: float = _env_float("PORTFOLIO_RISK_FREE_RATE", 0.04)


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class PortfolioConfig:
    """Configuration for a portfolio backtest run."""

    run_dirs: List[str]
    """Absolute or relative paths to individual strategy run directories."""

    weights: List[float]
    """Portfolio weight for each strategy.  Must sum to 1.0 (within 1e-6)."""

    rebalance_period: str = _DEFAULT_REBALANCE_PERIOD
    """Rebalance frequency: ``"daily"``, ``"weekly"``, or ``"monthly"``."""

    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE
    """Annualised risk-free rate (e.g. 0.04 = 4 %)."""


@dataclass
class PortfolioResult:
    """Output of a portfolio backtest run."""

    portfolio_sharpe: Optional[float]
    """Annualised Sharpe ratio of the combined portfolio."""

    portfolio_sortino: Optional[float]
    """Annualised Sortino ratio (uses downside deviation)."""

    portfolio_calmar: Optional[float]
    """Calmar ratio: annualised return / max drawdown magnitude."""

    portfolio_max_drawdown: Optional[float]
    """Maximum peak-to-trough drawdown (negative fraction, e.g. -0.15 = -15 %)."""

    portfolio_total_return: Optional[float]
    """Total portfolio return as a fraction (e.g. 0.25 = +25 %)."""

    correlation_matrix: Dict[str, Any] = field(default_factory=dict)
    """Correlation matrix between individual strategy daily returns.
    Keys are strategy labels (run directory basenames); values are nested dicts."""

    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    """Portfolio equity curve as a list of ``{"ts": <ISO-str>, "equity": <float>}``."""

    strategy_contributions: List[Dict[str, Any]] = field(default_factory=list)
    """Per-strategy contribution metadata: run_dir, weight, total_return, sharpe."""

    report_path: str = ""
    """Absolute path to the saved ``portfolio_report.json``."""


# ── Equity curve loading ──────────────────────────────────────────────────────


def _load_equity_curve_from_backtest_report(run_dir: str) -> List[Tuple[str, float]]:
    """
    Load an equity curve from ``backtest_report.json``.

    Returns a list of ``(timestamp_iso, equity)`` tuples.
    Falls back to an empty list if no usable data is found.
    """
    path = os.path.join(run_dir, "backtest_report.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []

    # Look for an embedded equity_curve array
    curve = data.get("equity_curve") or []
    if curve and isinstance(curve, list):
        result: List[Tuple[str, float]] = []
        for point in curve:
            if not isinstance(point, dict):
                continue
            ts = str(point.get("ts") or point.get("timestamp") or point.get("date") or "")
            # Use explicit None-check so that a legitimate equity value of 0.0
            # is not treated as missing and silently replaced by the next key.
            eq = next(
                (point[k] for k in ("equity", "value", "close") if point.get(k) is not None),
                None,
            )
            try:
                result.append((ts, float(eq)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        if result:
            return result

    # Fallback: synthesise a 2-point curve from total return
    metrics = data.get("baseline_metrics") or data.get("metrics") or {}
    total_return = metrics.get("total_return_pct")
    if total_return is not None:
        try:
            ret = float(total_return) / 100.0
            now_iso = datetime.now(timezone.utc).isoformat()
            return [("2000-01-01T00:00:00+00:00", 1.0), (now_iso, 1.0 + ret)]
        except (TypeError, ValueError):
            pass
    return []


def _load_equity_curve_from_ledger_csv(run_dir: str) -> List[Tuple[str, float]]:
    """
    Load an equity curve from ``sample_out/ledger.csv`` or ``ledger.csv``.

    Expects columns: ``date`` (or ``timestamp``), ``equity`` (or ``portfolio_value``).
    """
    candidates = [
        os.path.join(run_dir, "sample_out", "ledger.csv"),
        os.path.join(run_dir, "ledger.csv"),
        os.path.join(run_dir, "data", "ledger.csv"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            result: List[Tuple[str, float]] = []
            with open(path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    # Detect timestamp column
                    ts = (
                        row.get("date") or row.get("timestamp") or
                        row.get("Date") or row.get("Timestamp") or ""
                    )
                    # Detect equity column — explicit None/empty check avoids
                    # the `or` falsy-zero bug where a legitimate equity=0 row
                    # would fall through to the next candidate column.
                    _eq_val = next(
                        (
                            row[k]
                            for k in ("equity", "portfolio_value", "Equity",
                                      "PortfolioValue", "value", "close")
                            if row.get(k) is not None and str(row[k]).strip() != ""
                        ),
                        None,
                    )
                    eq_str = str(_eq_val).strip() if _eq_val is not None else ""
                    try:
                        result.append((str(ts).strip(), float(eq_str)))
                    except (TypeError, ValueError):
                        continue
            if result:
                return result
        except OSError:
            continue
    return []


def _load_equity_curve(run_dir: str) -> List[Tuple[str, float]]:
    """Load equity curve for *run_dir*, trying all known sources."""
    curve = _load_equity_curve_from_backtest_report(run_dir)
    if not curve:
        curve = _load_equity_curve_from_ledger_csv(run_dir)
    return curve


# ── Timeline alignment ────────────────────────────────────────────────────────


def _ts_sort_key(ts: str) -> str:
    """Return a sort-comparable representation of the timestamp string."""
    return ts.strip()


def _align_curves(
    curves: List[List[Tuple[str, float]]],
) -> Tuple[List[str], List[List[float]]]:
    """
    Align multiple equity curves onto a common sorted timestamp sequence.

    Missing values are forward-filled (last-observation-carried-forward).
    All curves are normalised to start at 1.0 so they can be combined with
    weights independent of absolute capital levels.

    Parameters
    ----------
    curves:
        List of ``[(ts, equity), ...]`` lists — one per strategy.

    Returns
    -------
    timestamps:
        Sorted list of all unique timestamps appearing in any curve.
    aligned:
        One list of floats per strategy, same length as *timestamps*.
        Values are normalised equity levels (start = 1.0).
    """
    # Collect all timestamps
    all_ts: List[str] = []
    seen_ts: set = set()
    for curve in curves:
        for ts, _ in curve:
            if ts not in seen_ts:
                seen_ts.add(ts)
                all_ts.append(ts)
    all_ts.sort(key=_ts_sort_key)

    aligned: List[List[float]] = []
    for curve in curves:
        curve_dict: Dict[str, float] = {ts: eq for ts, eq in curve}
        # Normalise to start at 1.0
        first_eq: Optional[float] = None
        for ts in all_ts:
            if ts in curve_dict:
                first_eq = curve_dict[ts]
                break
        # Guard against zero, negative, and missing first_eq values.
        # A zero/negative first_eq would either divide-by-zero or invert the
        # normalised curve, corrupting portfolio metrics.  When the starting
        # equity is invalid we treat the curve as having no usable data and
        # forward-fill with 1.0, exactly as we would for a completely empty
        # curve.  This preserves the contract "all curves start at 1.0".
        if first_eq is None or first_eq <= 0.0:
            curve_dict.clear()  # suppress all data points; forward-fill 1.0
            first_eq = 1.0  # safe sentinel: curve_dict is now empty so the division
            # below is never reached, but setting first_eq avoids a latent
            # TypeError if curve_dict is not empty for any unforeseen reason.

        series: List[float] = []
        last_val: float = 1.0
        for ts in all_ts:
            if ts in curve_dict:
                last_val = curve_dict[ts] / first_eq
            series.append(last_val)
        aligned.append(series)

    return all_ts, aligned


# ── Statistics (stdlib only) ──────────────────────────────────────────────────


def _daily_returns(equity: List[float]) -> List[float]:
    """Compute simple period-over-period returns from an equity series."""
    if len(equity) < 2:
        return []
    # v1.1.2 (sixth-pass H-1): tighten the denominator floor from ``> 0.0`` to
    # ``> 1e-14`` per CLAUDE.md § 9.3.  Previous threshold admitted IEEE 754
    # subnormals; division then explodes to ~1e+300 and silently corrupts the
    # downstream Sharpe / Sortino / Calmar statistics.
    return [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        if equity[i - 1] > 1e-14
        and math.isfinite(equity[i - 1])
        and math.isfinite(equity[i])
        else 0.0
        for i in range(1, len(equity))
    ]


def _mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _std(values: List[float], ddof: int = 1) -> float:
    """Sample standard deviation (ddof=1) or population (ddof=0)."""
    n = len(values)
    if n <= ddof:
        return 0.0
    m = _mean(values)
    variance = sum((v - m) ** 2 for v in values) / (n - ddof)
    return math.sqrt(variance)


def _periods_per_year(n_periods: int) -> float:
    """Return the estimated number of trading periods per year.

    Approximates trading days (~252) if n_periods >= 100, weekly (~52) if
    >= 50, otherwise monthly (~12).

    The threshold is 100 (not 200) because this function receives the number of
    *returns* (len(equity_curve) - 1).  A 252-bar daily equity curve produces
    251 returns; a 126-bar (6-month) daily curve produces 125 returns.
    With the old threshold of 200, any daily series shorter than ~10 months
    was misclassified as weekly, understating the annualised Sharpe/Sortino/
    Calmar by ~sqrt(252/52) ≈ 2.2×.  100 is safe because typical weekly
    lookbacks rarely exceed 52*2 = 104 bars.
    """
    if n_periods >= 100:
        return 252.0
    if n_periods >= 50:
        return 52.0  # weekly
    return 12.0  # monthly


def _annualise_factor(n_periods: int) -> float:
    """Return sqrt(trading_periods_per_year) for Sharpe/Sortino annualisation."""
    return math.sqrt(_periods_per_year(n_periods))


def _sharpe(returns: List[float], risk_free_rate: float = 0.0) -> Optional[float]:
    if not returns:
        return None
    ppy = _periods_per_year(len(returns))
    rf_per_period = risk_free_rate / ppy
    excess = [r - rf_per_period for r in returns]
    s = _std(excess, ddof=1)
    if not (s > 1e-14):
        return None
    ann = math.sqrt(ppy)
    return (_mean(excess) / s) * ann


def _sortino(returns: List[float], risk_free_rate: float = 0.0) -> Optional[float]:
    if not returns:
        return None
    ppy = _periods_per_year(len(returns))
    rf_per_period = risk_free_rate / ppy
    excess = [r - rf_per_period for r in returns]
    downside = [e for e in excess if e < 0.0]
    if not downside:
        return None
    downside_dev = math.sqrt(sum(d ** 2 for d in downside) / len(downside))
    if not (downside_dev > 1e-14):
        return None
    ann = math.sqrt(ppy)
    return (_mean(excess) / downside_dev) * ann


def _max_drawdown(equity: List[float]) -> float:
    """Return the maximum peak-to-trough drawdown as a negative fraction."""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        if peak > 0.0:
            dd = (val - peak) / peak
            if dd < max_dd:
                max_dd = dd
    return max_dd


def _calmar(equity: List[float], returns: List[float]) -> Optional[float]:
    """Calmar ratio: annualised return / |max_drawdown|."""
    if not returns:
        return None
    # Calmar uses arithmetic annualisation (periods_per_year), NOT sqrt-based
    # volatility annualisation used by Sharpe/Sortino.
    ppy = _periods_per_year(len(returns))
    annualised_return = _mean(returns) * ppy
    mdd = abs(_max_drawdown(equity))
    # Subnormal-safe guard: ``mdd <= 0.0`` admits IEEE 754 subnormals
    # like 5e-324 which yield Calmar ratios on the order of 1e+300.
    if not (mdd > 1e-14):
        return None
    return annualised_return / mdd


def _total_return(equity: List[float]) -> Optional[float]:
    # Guard against zero AND negative first equity: zero causes division-by-zero;
    # negative causes an inverted (sign-flipped) return figure that is nonsensical
    # for portfolio equity curves.  Mirrors the guard in _align_curves().
    if len(equity) < 2 or equity[0] <= 0.0:
        return None
    return (equity[-1] - equity[0]) / equity[0]


def _pearson_correlation(x: List[float], y: List[float]) -> Optional[float]:
    """Compute Pearson correlation between two equal-length lists."""
    if len(x) != len(y):
        return None
    n = len(x)
    if n < 2:
        return None
    mx, my = _mean(x[:n]), _mean(y[:n])
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    denom_x = math.sqrt(sum((v - mx) ** 2 for v in x[:n]))
    denom_y = math.sqrt(sum((v - my) ** 2 for v in y[:n]))
    denom = denom_x * denom_y
    # Guard against zero/subnormal denominator (including IEEE 754 subnormals
    # that pass denom > 0.0 but produce numerically meaningless results).
    if not (denom > 1e-14):
        return None
    # Clamp to [-1, 1] to absorb floating-point rounding past exact boundaries.
    return max(-1.0, min(1.0, num / denom))


def _build_correlation_matrix(
    labels: List[str],
    returns_list: List[List[float]],
) -> Dict[str, Dict[str, Optional[float]]]:
    """Build a full N×N Pearson correlation matrix."""
    matrix: Dict[str, Dict[str, Optional[float]]] = {}
    n = len(labels)
    for i in range(n):
        matrix[labels[i]] = {}
        for j in range(n):
            if i == j:
                matrix[labels[i]][labels[j]] = 1.0
            elif j < i:
                # Symmetric — reuse already-computed value
                matrix[labels[i]][labels[j]] = matrix[labels[j]][labels[i]]
            else:
                matrix[labels[i]][labels[j]] = _pearson_correlation(
                    returns_list[i], returns_list[j]
                )
    return matrix


# ── Main entry point ──────────────────────────────────────────────────────────


def run_portfolio_backtest(
    run_dirs: List[str],
    weights: List[float],
    *,
    output_dir: Optional[str] = None,
    rebalance_period: Optional[str] = None,
    risk_free_rate: Optional[float] = None,
) -> PortfolioResult:
    """
    Run a portfolio-level backtest by combining individual strategy equity curves.

    Parameters
    ----------
    run_dirs:
        List of run directory paths, each produced by a Crucible pipeline run.
    weights:
        Portfolio weight for each strategy (must sum to 1.0).
    output_dir:
        Directory where ``portfolio_report.json`` is saved.  Defaults to
        ``run_dirs[0]``.
    rebalance_period:
        Override for ``PORTFOLIO_REBALANCE_PERIOD`` env var.
    risk_free_rate:
        Override for ``PORTFOLIO_RISK_FREE_RATE`` env var.

    Returns
    -------
    PortfolioResult
        Computed portfolio metrics, equity curve, and path to the saved report.

    Raises
    ------
    ValueError
        If ``run_dirs`` and ``weights`` have different lengths, if any
        individual weight is negative, or if ``weights`` do not sum to
        1.0 (within 1e-6 tolerance).
    """
    if len(run_dirs) != len(weights):
        raise ValueError(
            f"run_dirs and weights must have the same length "
            f"({len(run_dirs)} vs {len(weights)})."
        )
    if any(w < 0.0 for w in weights):
        raise ValueError(
            f"All weights must be non-negative; got {[round(w, 6) for w in weights]}."
        )
    if abs(sum(weights) - 1.0) > 1e-6:
        raise ValueError(
            f"Weights must sum to 1.0; got {sum(weights):.6f}."
        )

    resolved_rebalance = rebalance_period or _DEFAULT_REBALANCE_PERIOD
    resolved_rfr = risk_free_rate if risk_free_rate is not None else _DEFAULT_RISK_FREE_RATE
    resolved_output_dir = output_dir or (run_dirs[0] if run_dirs else ".")

    # ── Load individual equity curves ──────────────────────────────────────
    # Build deduplicated labels so correlation-matrix dict keys never collide.
    # Algorithm: first occurrence keeps the raw basename; each subsequent
    # occurrence gets a numeric suffix (_1, _2, …) that is guaranteed unique
    # against *all already-assigned labels* (including singletons and previously
    # generated suffixes).  This handles edge cases such as
    # run_dirs=["/a/run", "/b/run", "/c/run_1"] where naïvely assigning "run_1"
    # as a suffix for the second "run" would collide with the existing "run_1".
    _used_labels: set = set()
    _suffix_counter: Dict[str, int] = {}
    labels: List[str] = []
    for _rd in run_dirs:
        _base = os.path.basename(os.path.abspath(_rd))
        if _base not in _used_labels:
            labels.append(_base)
            _used_labels.add(_base)
        else:
            # Find the smallest N ≥ 1 such that "{base}_{N}" is not yet used.
            _n = _suffix_counter.get(_base, 1)
            _candidate = f"{_base}_{_n}"
            while _candidate in _used_labels:
                _n += 1
                _candidate = f"{_base}_{_n}"
            _suffix_counter[_base] = _n + 1
            labels.append(_candidate)
            _used_labels.add(_candidate)
    raw_curves: List[List[Tuple[str, float]]] = []
    strategy_contributions: List[Dict[str, Any]] = []

    for i, run_dir in enumerate(run_dirs):
        curve = _load_equity_curve(run_dir)
        raw_curves.append(curve)

    # ── Align curves on common timestamps ──────────────────────────────────
    # Only include strategies that have at least 2 data points.
    valid_indices = [i for i, c in enumerate(raw_curves) if len(c) >= 2]
    if not valid_indices:
        # No usable equity data — return a minimal result with None metrics.
        result = PortfolioResult(
            portfolio_sharpe=None,
            portfolio_sortino=None,
            portfolio_calmar=None,
            portfolio_max_drawdown=None,
            portfolio_total_return=None,
        )
        _save_portfolio_report(result, resolved_output_dir)
        return result

    valid_curves = [raw_curves[i] for i in valid_indices]
    valid_weights_raw = [weights[i] for i in valid_indices]
    valid_labels = [labels[i] for i in valid_indices]
    valid_run_dirs = [run_dirs[i] for i in valid_indices]

    # Re-normalise weights if some strategies had no data.
    # Log a warning so callers are not silently surprised by changed allocation.
    w_sum = sum(valid_weights_raw)
    if len(valid_indices) < len(weights) and w_sum > 0:
        excluded_labels = [labels[i] for i in range(len(weights)) if i not in valid_indices]
        renorm = [round(w / w_sum, 6) for w in valid_weights_raw]
        _LOGGER.warning(
            "run_portfolio_backtest: %d strateg%s excluded due to insufficient equity data "
            "(<2 data points): %s. Weights re-normalised from %s → %s.",
            len(excluded_labels),
            "ies" if len(excluded_labels) != 1 else "y",
            excluded_labels,
            [weights[i] for i in valid_indices],
            renorm,
        )
    valid_weights = [w / w_sum for w in valid_weights_raw] if w_sum > 0 else valid_weights_raw

    timestamps, aligned = _align_curves(valid_curves)

    # ── Build weighted portfolio equity curve ───────────────────────────────
    portfolio_equity: List[float] = []
    for t_idx in range(len(timestamps)):
        portfolio_val = sum(
            valid_weights[s] * aligned[s][t_idx]
            for s in range(len(valid_indices))
        )
        portfolio_equity.append(portfolio_val)

    # ── Compute portfolio metrics ───────────────────────────────────────────
    port_returns = _daily_returns(portfolio_equity)

    sharpe = _sharpe(port_returns, resolved_rfr)
    sortino = _sortino(port_returns, resolved_rfr)
    calmar = _calmar(portfolio_equity, port_returns)
    max_dd = _max_drawdown(portfolio_equity) if portfolio_equity else None
    total_ret = _total_return(portfolio_equity)

    # ── Correlation matrix ──────────────────────────────────────────────────
    individual_returns: List[List[float]] = [_daily_returns(aligned[s]) for s in range(len(valid_indices))]
    correlation_matrix = _build_correlation_matrix(valid_labels, individual_returns)

    # ── Per-strategy contributions ──────────────────────────────────────────
    for s_idx, (lbl, rd, w) in enumerate(zip(valid_labels, valid_run_dirs, valid_weights)):
        s_returns = individual_returns[s_idx]
        s_equity = aligned[s_idx]
        strategy_contributions.append({
            "run_dir": rd,
            "label": lbl,
            "weight": round(w, 6),
            "total_return": _total_return(s_equity),
            "sharpe": _sharpe(s_returns, resolved_rfr),
            "max_drawdown": _max_drawdown(s_equity),
            "data_points": len(s_equity),
        })

    # ── Equity curve output ─────────────────────────────────────────────────
    equity_curve_out = [
        {"ts": timestamps[i], "equity": round(portfolio_equity[i], 8)}
        for i in range(len(timestamps))
    ]

    result = PortfolioResult(
        portfolio_sharpe=round(sharpe, 6) if sharpe is not None else None,
        portfolio_sortino=round(sortino, 6) if sortino is not None else None,
        portfolio_calmar=round(calmar, 6) if calmar is not None else None,
        portfolio_max_drawdown=round(max_dd, 6) if max_dd is not None else None,
        portfolio_total_return=round(total_ret, 6) if total_ret is not None else None,
        correlation_matrix=correlation_matrix,
        equity_curve=equity_curve_out,
        strategy_contributions=strategy_contributions,
    )

    result.report_path = _save_portfolio_report(result, resolved_output_dir)
    return result


# ── Report persistence ────────────────────────────────────────────────────────


def _save_portfolio_report(result: PortfolioResult, output_dir: str) -> str:
    """Serialise *result* to ``portfolio_report.json`` and return the path.

    Returns the absolute path of the written file, or an empty string on any
    I/O or serialisation failure (so callers never need to handle exceptions).
    """
    report_path = os.path.join(output_dir, "portfolio_report.json")

    payload: Dict[str, Any] = {
        "portfolio_sharpe": result.portfolio_sharpe,
        "portfolio_sortino": result.portfolio_sortino,
        "portfolio_calmar": result.portfolio_calmar,
        "portfolio_max_drawdown": result.portfolio_max_drawdown,
        "portfolio_total_return": result.portfolio_total_return,
        "correlation_matrix": result.correlation_matrix,
        "strategy_contributions": result.strategy_contributions,
        # Truncate equity curve to 2000 points for readability
        "equity_curve": result.equity_curve[:2000],
        "equity_curve_points": len(result.equity_curve),
    }

    try:
        # os.makedirs is inside the try block so that OSError (e.g. when
        # output_dir is an existing file path, not a directory) is caught and
        # converted into an empty-string return instead of an unhandled raise.
        os.makedirs(output_dir, exist_ok=True)
        _tmp_report = report_path + ".tmp"
        with open(_tmp_report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(_tmp_report, report_path)
    except (OSError, ValueError):
        try:
            os.unlink(report_path + ".tmp")
        except OSError:
            pass
        return ""

    return report_path
