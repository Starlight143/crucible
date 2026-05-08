"""
features/risk_attribution.py
=============================
Component VaR and Risk Attribution for multi-strategy portfolios.

Computes portfolio VaR/CVaR (historical simulation) and decomposes it into
per-strategy component VaR contributions using the covariance method.
All matrix operations are pure Python (no numpy required).

Environment variables
---------------------
RISK_CONFIDENCE_LEVEL   VaR confidence level (default 0.95).
RISK_METHOD             Computation method: "historical" (default).
RISK_LOOKBACK_WINDOW    Lookback window in bars (default 252).
"""
from __future__ import annotations

import json
import logging
import math
import os
import csv as _csv_module
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Env helpers ───────────────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default, finite_only=True)


def _env_str(name: str, default: str) -> str:
    return _env.env_str(name, default)


# ── Mode isolation ─────────────────────────────────────────────────────────────

def _is_quant_run(run_dir: str) -> bool:
    """Return True if this is a quant-mode run (or mode unknown)."""
    result_path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(result_path):
        return True
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mode = str(data.get("mode", "")).lower()
        return mode in ("quant", "")
    except (OSError, json.JSONDecodeError):
        return True


def _sanitise_float(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class RiskAttributionConfig:
    confidence_level: float = field(
        default_factory=lambda: _env_float("RISK_CONFIDENCE_LEVEL", 0.95)
    )
    method: str = field(
        default_factory=lambda: _env_str("RISK_METHOD", "historical")
    )
    lookback_window: int = field(
        default_factory=lambda: _env_int("RISK_LOOKBACK_WINDOW", 252)
    )


@dataclass
class ComponentVaR:
    strategy_label: str
    weight: float
    standalone_var_pct: Optional[float]       # VaR of this strategy in isolation
    component_var_pct: Optional[float]        # This strategy's contribution to portfolio VaR
    marginal_var_pct: Optional[float]         # Impact of adding 1% more of this strategy
    diversification_benefit_pct: Optional[float]  # standalone - component
    contribution_pct: Optional[float]         # % of total portfolio VaR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_label": self.strategy_label,
            "weight": _sanitise_float(self.weight),
            "standalone_var_pct": _sanitise_float(self.standalone_var_pct),
            "component_var_pct": _sanitise_float(self.component_var_pct),
            "marginal_var_pct": _sanitise_float(self.marginal_var_pct),
            "diversification_benefit_pct": _sanitise_float(self.diversification_benefit_pct),
            "contribution_pct": _sanitise_float(self.contribution_pct),
        }


@dataclass
class RiskAttributionResult:
    portfolio_var_pct: Optional[float] = None
    portfolio_cvar_pct: Optional[float] = None
    component_vars: List[ComponentVaR] = field(default_factory=list)
    diversification_ratio: Optional[float] = None  # portfolio VaR / weighted avg standalone VaR
    concentration_score: Optional[float] = None    # HHI of risk contributions
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portfolio_var_pct": _sanitise_float(self.portfolio_var_pct),
            "portfolio_cvar_pct": _sanitise_float(self.portfolio_cvar_pct),
            "component_vars": [cv.to_dict() for cv in self.component_vars],
            "diversification_ratio": _sanitise_float(self.diversification_ratio),
            "concentration_score": _sanitise_float(self.concentration_score),
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Pure-Python matrix operations ────────────────────────────────────────────

def _dot(v1: List[float], v2: List[float]) -> float:
    """Dot product of two equal-length vectors."""
    return sum(a * b for a, b in zip(v1, v2))


def _mat_vec(M: List[List[float]], v: List[float]) -> List[float]:
    """Matrix-vector product M @ v."""
    return [_dot(row, v) for row in M]


def _covariance_matrix(
    returns_matrix: List[List[float]],
) -> List[List[float]]:
    """
    Compute k×k sample covariance matrix from k strategy return series.

    Each inner list is one strategy's return series.
    All series are trimmed to the shortest length.
    """
    k = len(returns_matrix)
    if k == 0:
        return []
    n = min(len(r) for r in returns_matrix)
    if n < 2:
        return [[0.0] * k for _ in range(k)]

    means = [sum(r[:n]) / n for r in returns_matrix]
    cov = [[0.0] * k for _ in range(k)]

    for i in range(k):
        for j in range(i, k):
            c = sum(
                (returns_matrix[i][t] - means[i]) * (returns_matrix[j][t] - means[j])
                for t in range(n)
            ) / (n - 1)
            cov[i][j] = c
            cov[j][i] = c

    return cov


# ── VaR / CVaR computation ────────────────────────────────────────────────────

def compute_portfolio_var(
    portfolio_returns: List[float],
    confidence_level: float = 0.95,
) -> Tuple[float, float]:
    """
    Historical simulation VaR and CVaR.

    Parameters
    ----------
    portfolio_returns : List[float]
        Portfolio return series.
    confidence_level : float
        Confidence level, e.g. 0.95.

    Returns
    -------
    Tuple[float, float]
        (VaR, CVaR) as positive fractions representing losses.
        E.g. (0.02, 0.03) means 2% VaR and 3% CVaR.
    """
    if not portfolio_returns:
        return 0.0, 0.0

    sorted_rets = sorted(portfolio_returns)
    n = len(sorted_rets)

    # CVaR/VaR: worst (1-confidence_level) fraction
    # cutoff = number of tail observations (e.g. 50 for n=1000, 95% CL)
    # CVaR tail = sorted_rets[:cutoff] (indices 0 … cutoff-1)
    # VaR threshold = sorted_rets[cutoff-1] (last/least-bad element of the tail)
    # Using cutoff instead of cutoff-1 for VaR would place it one step *outside*
    # the tail, understating the loss estimate.
    cutoff = max(1, int(math.floor((1.0 - confidence_level) * n)))
    var_val = -sorted_rets[cutoff - 1]  # boundary of the (1-CL) loss tail
    tail = sorted_rets[:cutoff]
    cvar_val = -sum(tail) / len(tail) if tail else var_val

    return max(0.0, var_val), max(0.0, cvar_val)


def compute_component_var(
    strategy_returns_list: List[List[float]],
    weights: List[float],
    labels: List[str],
    confidence_level: float = 0.95,
) -> RiskAttributionResult:
    """
    Compute component VaR for a portfolio of strategies.

    Parameters
    ----------
    strategy_returns_list : List[List[float]]
        Return series for each strategy (may have different lengths).
    weights : List[float]
        Portfolio weights summing to 1.0.
    labels : List[str]
        Strategy labels corresponding to each return series.
    confidence_level : float
        VaR confidence level.

    Returns
    -------
    RiskAttributionResult
    """
    result = RiskAttributionResult()

    k = len(strategy_returns_list)
    if k == 0:
        result.errors.append("No strategy returns provided")
        return result

    if len(weights) != k or len(labels) != k:
        result.errors.append("Mismatched lengths: returns, weights, labels must all have same length")
        return result

    # Normalise weights.
    # Subnormal-safe guard: ``<= 0`` admits IEEE 754 subnormals (e.g.
    # weights=[5e-325, ...]), which divided into each weight at line 257
    # produces normalised weights on the order of 1e+300 and propagates into
    # the portfolio-variance quadratic form below, breaking every
    # component-VaR / contribution_pct downstream.  Project-standard threshold:
    # require ``> 1e-14``.
    total_w = sum(weights)
    if not (total_w > 1e-14):
        result.errors.append("Weights sum to <= 0 (or below subnormal threshold 1e-14)")
        return result
    w = [wi / total_w for wi in weights]

    # Trim to minimum common length
    n = min(len(r) for r in strategy_returns_list)
    if n < 5:
        result.errors.append(f"Insufficient data: minimum series length = {n} (need >= 5)")
        return result

    trimmed = [r[-n:] for r in strategy_returns_list]

    # Build portfolio return series
    portfolio_returns: List[float] = []
    for t in range(n):
        pr = sum(w[i] * trimmed[i][t] for i in range(k))
        portfolio_returns.append(pr)

    # Portfolio VaR / CVaR
    port_var, port_cvar = compute_portfolio_var(portfolio_returns, confidence_level)
    result.portfolio_var_pct = port_var * 100.0
    result.portfolio_cvar_pct = port_cvar * 100.0

    # Covariance matrix Σ
    sigma = _covariance_matrix(trimmed)

    # Portfolio variance: σ²_p = w' Σ w
    sigma_w = _mat_vec(sigma, w)
    port_variance = _dot(w, sigma_w)
    port_std = math.sqrt(max(0.0, port_variance))

    # Component VaR via covariance method:
    # ComponentVaR_i = w_i * (Σw)_i / σ_p * VaR_p
    # where (Σw)_i is the i-th element of Σw (marginal covariance)
    component_vars: List[ComponentVaR] = []
    total_component_var_abs = 0.0
    raw_components: List[float] = []

    for i in range(k):
        # Standalone VaR of strategy i
        standalone_var, _ = compute_portfolio_var(trimmed[i], confidence_level)
        standalone_var_pct = standalone_var * 100.0

        # Component VaR: weight × marginal covariance / portfolio std × portfolio VaR
        # Subnormal-safe guard: ``> 0`` admits IEEE 754 subnormals which
        # would explode the marginal-cov division and contaminate every
        # downstream contribution_pct / marginal_var_pct.
        if port_std > 1e-14 and port_var > 1e-14:
            marginal_cov = sigma_w[i]  # ∂σ_p/∂w_i = (Σw)_i / σ_p
            component_var_frac = w[i] * marginal_cov / port_std * port_var
            component_var_pct = component_var_frac * 100.0

            # Marginal VaR: effect of adding 1% more of strategy i
            # ΔVaR_p / Δw_i ≈ (Σw)_i / σ_p * VaR_p (per unit weight change)
            marginal_var_pct = (marginal_cov / port_std * port_var) * 100.0
        else:
            component_var_pct = 0.0
            marginal_var_pct = 0.0

        diversification_benefit_pct = standalone_var_pct - component_var_pct
        raw_components.append(component_var_pct)
        total_component_var_abs += abs(component_var_pct)

        cv = ComponentVaR(
            strategy_label=labels[i],
            weight=w[i],
            standalone_var_pct=_sanitise_float(standalone_var_pct),
            component_var_pct=_sanitise_float(component_var_pct),
            marginal_var_pct=_sanitise_float(marginal_var_pct),
            diversification_benefit_pct=_sanitise_float(diversification_benefit_pct),
            contribution_pct=None,  # filled below
        )
        component_vars.append(cv)

    # Fill contribution_pct
    for i, cv in enumerate(component_vars):
        if total_component_var_abs > 1e-14 and cv.component_var_pct is not None:
            cv.contribution_pct = _sanitise_float(
                abs(raw_components[i]) / total_component_var_abs * 100.0
            )

    result.component_vars = component_vars

    # Diversification ratio = weighted avg standalone VaR / portfolio VaR
    if port_var > 1e-14:
        weighted_standalone = sum(
            w[i] * (cv.standalone_var_pct or 0.0) / 100.0
            for i, cv in enumerate(component_vars)
        )
        result.diversification_ratio = _sanitise_float(weighted_standalone / port_var)

    # HHI concentration score (sum of squared contribution fractions)
    if total_component_var_abs > 1e-14:
        contribs = [abs(r) / total_component_var_abs for r in raw_components]
        hhi = sum(c ** 2 for c in contribs)
        result.concentration_score = _sanitise_float(hhi)

    return result


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_returns_from_run_dir(run_dir: str, lookback: int) -> List[float]:
    """Load return series from a single run_dir."""
    report_path = os.path.join(run_dir, "backtest_report.json")
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            eq = data.get("equity_curve", [])
            if isinstance(eq, list) and len(eq) >= 2:
                values: List[float] = []
                for item in eq:
                    if isinstance(item, (int, float)):
                        values.append(float(item))
                    elif isinstance(item, dict):
                        v = item.get("equity", item.get("value", item.get("close")))
                        if v is not None:
                            try:
                                values.append(float(v))
                            except (ValueError, TypeError):
                                pass
                if len(values) >= 2:
                    returns = [
                        (values[i] - values[i - 1]) / values[i - 1]
                        if values[i - 1] > 0
                        and math.isfinite(values[i - 1])
                        and math.isfinite(values[i])
                        else 0.0
                        for i in range(1, len(values))
                    ]
                    return returns[-lookback:]
        except (OSError, json.JSONDecodeError):
            pass

    data_dir = os.path.join(run_dir, "code", "data")
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if not fname.lower().endswith(".csv"):
                continue
            try:
                values = []
                with open(os.path.join(data_dir, fname), "r", encoding="utf-8", newline="") as f:
                    reader = _csv_module.DictReader(f)
                    for row in reader:
                        for col in ("equity", "close", "Close", "price", "Price"):
                            if col in row:
                                try:
                                    values.append(float(row[col]))
                                    break
                                except (ValueError, TypeError):
                                    continue
                if len(values) >= 2:
                    returns = [
                        (values[i] - values[i - 1]) / values[i - 1]
                        if values[i - 1] > 0
                        and math.isfinite(values[i - 1])
                        and math.isfinite(values[i])
                        else 0.0
                        for i in range(1, len(values))
                    ]
                    return returns[-lookback:]
            except (OSError, _csv_module.Error):
                pass

    return []


# ── Public runner ──────────────────────────────────────────────────────────────

def run_risk_attribution(
    run_dirs: List[str],
    weights: List[float],
    output_dir: str,
    config: Optional[RiskAttributionConfig] = None,
) -> RiskAttributionResult:
    """
    Load returns from each run_dir, compute risk attribution, save report.

    Parameters
    ----------
    run_dirs : List[str]
        List of run directory paths. Each must contain backtest data.
    weights : List[float]
        Portfolio weights for each run_dir. Will be normalised to sum to 1.
    output_dir : str
        Directory where risk_attribution_report.json will be saved.
    config : RiskAttributionConfig, optional
        Risk attribution configuration.

    Returns
    -------
    RiskAttributionResult
    """
    result = RiskAttributionResult()

    if not run_dirs:
        result.errors.append("No run_dirs provided")
        return result

    if len(weights) != len(run_dirs):
        result.errors.append(
            f"Length mismatch: {len(run_dirs)} run_dirs but {len(weights)} weights"
        )
        return result

    if config is None:
        config = RiskAttributionConfig()

    # Check mode for each run_dir (log warnings for non-quant, proceed anyway)
    for rd in run_dirs:
        if not _is_quant_run(rd):
            msg = f"run_risk_attribution: run_dir={rd} is not a quant run; including anyway"
            logger.info(msg)
            result.warnings.append(msg)

    # Load returns
    strategy_returns_list: List[List[float]] = []
    labels: List[str] = []
    valid_weights: List[float] = []

    for rd, w in zip(run_dirs, weights):
        rets = _load_returns_from_run_dir(rd, config.lookback_window)
        if len(rets) < 5:
            msg = f"Skipping run_dir={rd}: insufficient data ({len(rets)} bars)"
            result.warnings.append(msg)
            logger.warning(msg)
            continue
        strategy_returns_list.append(rets)
        labels.append(os.path.basename(os.path.abspath(rd)))
        valid_weights.append(w)

    if not strategy_returns_list:
        result.errors.append("No valid strategy return data found in any run_dir")
        return result

    logger.info(
        "Computing risk attribution for %d strategies, confidence=%.2f",
        len(strategy_returns_list),
        config.confidence_level,
    )

    # Preserve warnings accumulated above (mode-check, insufficient-data) before
    # result is overwritten by compute_component_var which returns a fresh object.
    _pre_warnings = result.warnings[:]
    result = compute_component_var(
        strategy_returns_list,
        valid_weights,
        labels,
        confidence_level=config.confidence_level,
    )
    result.warnings = _pre_warnings + result.warnings

    # Persist report
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "risk_attribution_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        result.report_path = report_path
        logger.info("Risk attribution report saved to %s", report_path)
    except (OSError, ValueError) as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        result.errors.append(f"Could not save report: {exc}")

    return result
