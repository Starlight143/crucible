"""
features/monte_carlo.py
========================
Monte Carlo Simulation and Stress Testing for quantitative strategy runs.

Generates N simulated equity paths by bootstrapping historical returns over a
configurable horizon, computes VaR/CVaR at multiple confidence levels, and
applies hardcoded historical stress scenarios (2008, 2020, 2022).

All computation is pure Python (stdlib only).

Environment variables
---------------------
MC_N_SIMULATIONS      Number of simulation paths (default 5000).
MC_HORIZON_DAYS       Simulation horizon in trading days (default 252).
MC_METHOD             Simulation method: "bootstrap" (default).
MC_SEED               Random seed for reproducibility (default 42).
"""
from __future__ import annotations

import json
import logging
import math
import os
import csv as _csv_module
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Env helpers ───────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, ""))
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        val = float(os.environ.get(name, ""))
        return val if math.isfinite(val) else default
    except (ValueError, TypeError):
        return default


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


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


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _sanitise_float(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _sanitise_list(lst: List[Any]) -> List[Any]:
    return [_sanitise_float(v) for v in lst]


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class MonteCarloConfig:
    n_simulations: int = field(default_factory=lambda: _env_int("MC_N_SIMULATIONS", 5000))
    horizon_days: int = field(default_factory=lambda: _env_int("MC_HORIZON_DAYS", 252))
    confidence_levels: List[float] = field(
        default_factory=lambda: [0.05, 0.10, 0.25, 0.75, 0.90, 0.95]
    )
    method: str = field(default_factory=lambda: _env_str("MC_METHOD", "bootstrap"))
    stress_scenarios: List[str] = field(
        default_factory=lambda: ["2008_crisis", "2020_covid", "2022_rates"]
    )
    seed: int = field(default_factory=lambda: _env_int("MC_SEED", 42))


@dataclass
class SimulationStats:
    mean_final_equity: Optional[float] = None
    median_final_equity: Optional[float] = None
    std_final_equity: Optional[float] = None
    var_5pct: Optional[float] = None
    var_10pct: Optional[float] = None
    cvar_5pct: Optional[float] = None
    max_simulated_drawdown_pct: Optional[float] = None
    prob_loss: Optional[float] = None
    prob_drawdown_gt_20pct: Optional[float] = None
    prob_drawdown_gt_30pct: Optional[float] = None
    percentile_distribution: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mean_final_equity": _sanitise_float(self.mean_final_equity),
            "median_final_equity": _sanitise_float(self.median_final_equity),
            "std_final_equity": _sanitise_float(self.std_final_equity),
            "var_5pct": _sanitise_float(self.var_5pct),
            "var_10pct": _sanitise_float(self.var_10pct),
            "cvar_5pct": _sanitise_float(self.cvar_5pct),
            "max_simulated_drawdown_pct": _sanitise_float(self.max_simulated_drawdown_pct),
            "prob_loss": _sanitise_float(self.prob_loss),
            "prob_drawdown_gt_20pct": _sanitise_float(self.prob_drawdown_gt_20pct),
            "prob_drawdown_gt_30pct": _sanitise_float(self.prob_drawdown_gt_30pct),
            "percentile_distribution": {
                k: _sanitise_float(v) for k, v in self.percentile_distribution.items()
            },
        }


@dataclass
class StressScenario:
    name: str
    description: str
    shock_returns: List[float]
    portfolio_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "shock_returns": _sanitise_list(self.shock_returns),
            "portfolio_return_pct": _sanitise_float(self.portfolio_return_pct),
            "max_drawdown_pct": _sanitise_float(self.max_drawdown_pct),
        }


@dataclass
class MonteCarloResult:
    simulation_stats: Optional[SimulationStats] = None
    stress_results: List[StressScenario] = field(default_factory=list)
    equity_paths_sample: List[List[float]] = field(default_factory=list)  # max 50 paths
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "simulation_stats": self.simulation_stats.to_dict() if self.simulation_stats else None,
            "stress_results": [s.to_dict() for s in self.stress_results],
            "equity_paths_sample": [
                _sanitise_list(path) for path in self.equity_paths_sample
            ],
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Stress scenario definitions ────────────────────────────────────────────────

# Monthly return sequences (approximate, expressed as daily-equivalent rates
# where needed). Values are monthly total returns as fractions.
_STRESS_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "2008_crisis": {
        "description": (
            "2008 Financial Crisis: Sep-Dec 2008 monthly shock sequence "
            "(-10%, -8%, -5%, -4%)"
        ),
        "monthly_returns": [-0.10, -0.08, -0.05, -0.04],
    },
    "2020_covid": {
        "description": (
            "2020 COVID-19 Crash: Feb-Apr 2020 monthly shock sequence "
            "(-8%, -12%, +12%)"
        ),
        "monthly_returns": [-0.08, -0.12, 0.12],
    },
    "2022_rates": {
        "description": (
            "2022 Interest Rate Shock: Jan-Jun 2022 average ~-6%/month "
            "(-6%, -3%, -5%, -8%, -1%, -7%)"
        ),
        "monthly_returns": [-0.06, -0.03, -0.05, -0.08, -0.01, -0.07],
    },
}


def _monthly_to_daily_returns(monthly_returns: List[float]) -> List[float]:
    """Convert monthly returns to daily equivalents (21 trading days/month)."""
    daily: List[float] = []
    for m in monthly_returns:
        daily_r = (1.0 + m) ** (1.0 / 21.0) - 1.0
        daily.extend([daily_r] * 21)
    return daily


def _apply_stress_scenario(
    name: str,
    defn: Dict[str, Any],
    strategy_returns: List[float],
    rng: random.Random,
) -> StressScenario:
    """
    Apply a stress scenario to the strategy by scaling its return distribution.

    The approach: replace strategy returns during the shock period with a
    blended version (50% actual strategy returns re-sampled, 50% pure shock).
    This preserves some strategy-specific character while applying the macro shock.
    """
    shock_daily = _monthly_to_daily_returns(defn["monthly_returns"])
    n_shock = len(shock_daily)

    # Build scenario returns: blend strategy sample with shock
    if strategy_returns:
        scenario_returns: List[float] = []
        for i, shock_r in enumerate(shock_daily):
            strat_r = rng.choice(strategy_returns)
            blended = 0.5 * shock_r + 0.5 * strat_r
            scenario_returns.append(blended)
    else:
        scenario_returns = list(shock_daily)

    # Portfolio return
    total = 1.0
    for r in scenario_returns:
        total *= (1.0 + r)
    portfolio_return_pct = (total - 1.0) * 100.0

    # Max drawdown during scenario
    peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for r in scenario_returns:
        equity *= (1.0 + r)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return StressScenario(
        name=name,
        description=defn["description"],
        shock_returns=scenario_returns[:50],  # truncate for storage
        portfolio_return_pct=portfolio_return_pct if math.isfinite(portfolio_return_pct) else None,
        max_drawdown_pct=max_dd * 100.0 if math.isfinite(max_dd) else None,
    )


# ── Simulation core ────────────────────────────────────────────────────────────

def _simulate_bootstrap(
    returns: List[float],
    n_simulations: int,
    horizon_days: int,
    rng: random.Random,
) -> List[List[float]]:
    """
    Bootstrap simulation: resample with replacement from historical returns.

    Returns list of n_simulations paths, each a list of horizon_days+1 equity values
    starting at 1.0.
    """
    paths: List[List[float]] = []

    for _ in range(n_simulations):
        path = [1.0]
        equity = 1.0
        for _ in range(horizon_days):
            r = rng.choice(returns)
            equity *= (1.0 + r)
            path.append(equity)
        paths.append(path)

    return paths


def _compute_simulation_stats(
    paths: List[List[float]],
    confidence_levels: List[float],
) -> SimulationStats:
    """Compute statistics across simulated paths."""
    if not paths:
        return SimulationStats()

    final_equities = [path[-1] for path in paths]
    final_equities.sort()
    n = len(final_equities)

    mean_fe = sum(final_equities) / n
    var_fe = sum((v - mean_fe) ** 2 for v in final_equities) / (n - 1) if n > 1 else 0.0
    std_fe = math.sqrt(var_fe)

    # Median
    med_fe = final_equities[n // 2] if n % 2 == 1 else (
        (final_equities[n // 2 - 1] + final_equities[n // 2]) / 2.0
    )

    # VaR: at 5% and 10% (loss relative to starting equity of 1.0)
    # CVaR uses final_equities[:cutoff] (indices 0 … cutoff-1), so VaR must
    # sit at cutoff-1 (last/least-bad element of the tail), not at cutoff which
    # would be one step outside the tail and understate the loss.
    def _var_at(level: float) -> Optional[float]:
        idx = max(0, min(n - 1, int(math.floor(level * n)) - 1))
        val = 1.0 - final_equities[idx]
        return val if math.isfinite(val) else None

    var_5 = _var_at(0.05)
    var_10 = _var_at(0.10)

    # CVaR at 5%: mean of the worst 5%
    cutoff_5 = max(1, int(math.floor(0.05 * n)))
    worst_5 = final_equities[:cutoff_5]
    cvar_5 = (1.0 - sum(worst_5) / len(worst_5)) if worst_5 else None

    # Max drawdown across ALL paths
    all_dd: List[float] = []
    for path in paths:
        peak = 1.0
        max_dd = 0.0
        for v in path:
            if v > peak:
                peak = v
            dd = (peak - v) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        all_dd.append(max_dd)
    max_sim_dd = max(all_dd) * 100.0 if all_dd else None

    prob_loss = sum(1 for fe in final_equities if fe < 1.0) / n
    prob_dd_20 = sum(1 for dd in all_dd if dd > 0.20) / n
    prob_dd_30 = sum(1 for dd in all_dd if dd > 0.30) / n

    # Percentile distribution
    percentiles: Dict[str, float] = {}
    for cl in confidence_levels:
        pct_idx = max(0, min(n - 1, int(round(cl * (n - 1)))))
        key = f"p{int(cl * 100)}"
        percentiles[key] = _sanitise_float(final_equities[pct_idx])

    return SimulationStats(
        mean_final_equity=_sanitise_float(mean_fe),
        median_final_equity=_sanitise_float(med_fe),
        std_final_equity=_sanitise_float(std_fe),
        var_5pct=_sanitise_float(var_5),
        var_10pct=_sanitise_float(var_10),
        cvar_5pct=_sanitise_float(cvar_5),
        max_simulated_drawdown_pct=_sanitise_float(max_sim_dd),
        prob_loss=_sanitise_float(prob_loss),
        prob_drawdown_gt_20pct=_sanitise_float(prob_dd_20),
        prob_drawdown_gt_30pct=_sanitise_float(prob_dd_30),
        percentile_distribution=percentiles,
    )


# ── Public simulation function ────────────────────────────────────────────────

def run_monte_carlo_simulation(
    returns: List[float],
    config: Optional[MonteCarloConfig] = None,
) -> MonteCarloResult:
    """
    Run Monte Carlo simulation and stress tests on a return series.

    Parameters
    ----------
    returns : List[float]
        Historical period return series.
    config : MonteCarloConfig, optional
        Simulation configuration.

    Returns
    -------
    MonteCarloResult
    """
    result = MonteCarloResult()

    if config is None:
        config = MonteCarloConfig()

    if len(returns) < 5:
        result.errors.append("Insufficient returns for Monte Carlo (need >= 5)")
        return result

    # Validate simulation dimensions; zero/negative values would produce an
    # empty path list or silent no-op loops that look like success.
    if config.n_simulations <= 0:
        result.errors.append(
            f"n_simulations must be > 0 (got {config.n_simulations})"
        )
        return result
    if config.horizon_days <= 0:
        result.errors.append(
            f"horizon_days must be > 0 (got {config.horizon_days})"
        )
        return result

    rng = random.Random(config.seed)

    # ── Bootstrap simulation ──────────────────────────────────────────────────
    # Filter guard-zero sentinels (inserted by _load_returns for invalid bars)
    # from the resampling pool to prevent artificial volatility suppression.
    # Defence-in-depth: also drop any non-finite values (NaN / inf) that could
    # poison the equity-curve product even though _load_returns already filters
    # them upstream — never rely on a single layer for numerical correctness.
    bootstrap_pool = [r for r in returns if r != 0.0 and math.isfinite(r)]
    if not bootstrap_pool:
        bootstrap_pool = returns
    else:
        _n_zeros = len(returns) - len(bootstrap_pool)
        if _n_zeros > len(returns) * 0.10:
            result.warnings.append(
                f"Bootstrap: {_n_zeros}/{len(returns)} ({_n_zeros * 100 // len(returns)}%) "
                "zero returns removed from sampling pool. If the strategy holds cash "
                "frequently, simulated VaR/CVaR may be overstated."
            )
    try:
        paths = _simulate_bootstrap(
            bootstrap_pool, config.n_simulations, config.horizon_days, rng
        )
        result.simulation_stats = _compute_simulation_stats(paths, config.confidence_levels)
        # Store up to 50 sample paths
        sample_paths = paths[: min(50, len(paths))]
        result.equity_paths_sample = [
            [_sanitise_float(v) for v in path] for path in sample_paths
        ]
    except Exception as exc:
        msg = f"Monte Carlo simulation failed: {exc}"
        logger.exception(msg)
        result.errors.append(msg)

    # ── Stress scenarios ──────────────────────────────────────────────────────
    stress_rng = random.Random(config.seed + 1)
    for scenario_name in config.stress_scenarios:
        defn = _STRESS_DEFINITIONS.get(scenario_name)
        if defn is None:
            result.warnings.append(f"Unknown stress scenario: '{scenario_name}'")
            continue
        try:
            sc = _apply_stress_scenario(scenario_name, defn, bootstrap_pool, stress_rng)
            result.stress_results.append(sc)
        except Exception as exc:
            msg = f"Stress scenario '{scenario_name}' failed: {exc}"
            logger.exception(msg)
            result.errors.append(msg)

    return result


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_returns(run_dir: str) -> List[float]:
    """Load return series from backtest_report.json or CSV."""
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
                    returns = []
                    for i in range(1, len(values)):
                        prev, curr = values[i - 1], values[i]
                        if prev > 0 and math.isfinite(prev) and math.isfinite(curr):
                            returns.append((curr - prev) / prev)
                        else:
                            returns.append(0.0)
                    return returns
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
                    returns = []
                    for i in range(1, len(values)):
                        prev, curr = values[i - 1], values[i]
                        if prev > 0 and math.isfinite(prev) and math.isfinite(curr):
                            returns.append((curr - prev) / prev)
                        else:
                            returns.append(0.0)
                    return returns
            except (OSError, _csv_module.Error):
                pass

    return []


# ── Public runner ──────────────────────────────────────────────────────────────

def run_monte_carlo(
    run_dir: str,
    config: Optional[MonteCarloConfig] = None,
) -> MonteCarloResult:
    """
    Load returns from run_dir, run Monte Carlo simulation, save monte_carlo_report.json.

    Parameters
    ----------
    run_dir : str
        Path to run directory.
    config : MonteCarloConfig, optional
        Simulation configuration. Defaults to env-var driven MonteCarloConfig().

    Returns
    -------
    MonteCarloResult
    """
    result = MonteCarloResult()

    # Mode check: Monte Carlo is useful for all modes
    if not _is_quant_run(run_dir):
        msg = f"run_monte_carlo: run_dir={run_dir} is not a quant run; proceeding anyway"
        logger.info(msg)
        result.warnings.append(msg)

    if config is None:
        config = MonteCarloConfig()

    returns = _load_returns(run_dir)
    if len(returns) < 5:
        msg = f"Insufficient returns for Monte Carlo ({len(returns)} bars)"
        result.errors.append(msg)
        logger.warning(msg)
        return result

    logger.info(
        "Running Monte Carlo: %d simulations × %d days on %d historical returns",
        config.n_simulations,
        config.horizon_days,
        len(returns),
    )

    _pre_warnings = result.warnings[:]
    result = run_monte_carlo_simulation(returns, config)
    result.warnings = _pre_warnings + result.warnings

    report_path = os.path.join(run_dir, "monte_carlo_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        result.report_path = report_path
        logger.info("Monte Carlo report saved to %s", report_path)
    except Exception as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        result.errors.append(f"Could not save report: {exc}")

    return result
