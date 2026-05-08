"""
features/transaction_cost_model.py
====================================
Realistic Transaction Cost Analysis for quantitative strategy runs.

Models commission, slippage, bid-ask spread, and optional Kyle-lambda market
impact.  Provides sensitivity analysis across commission/slippage scenarios and
computes breakeven transaction cost levels.

Environment variables
---------------------
TC_COMMISSION_PCT      Commission per trade as fraction (default 0.001 = 10 bps).
TC_SLIPPAGE_PCT        Slippage per trade as fraction (default 0.0005 = 5 bps).
TC_SPREAD_BPS          Bid-ask spread in basis points (default 2.0).
TC_USE_KYLE_IMPACT     1/true to model Kyle-lambda market impact (default 0).
TC_KYLE_LAMBDA         Kyle lambda coefficient (default 0.1).
TC_AVG_DAILY_VOLUME    Average daily volume for impact scaling (default None).
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


def _env_bool(name: str, default: bool) -> bool:
    return _env.env_bool(name, default)


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


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class TransactionCostConfig:
    commission_pct: float = field(default_factory=lambda: _env_float("TC_COMMISSION_PCT", 0.001))
    slippage_pct: float = field(default_factory=lambda: _env_float("TC_SLIPPAGE_PCT", 0.0005))
    spread_bps: float = field(default_factory=lambda: _env_float("TC_SPREAD_BPS", 2.0))
    use_kyle_impact: bool = field(default_factory=lambda: _env_bool("TC_USE_KYLE_IMPACT", False))
    kyle_lambda: float = field(default_factory=lambda: _env_float("TC_KYLE_LAMBDA", 0.1))
    avg_daily_volume: Optional[float] = field(
        default_factory=lambda: (
            # Explicit positive-only check: _env_float returns 0.0 when not set.
            # `or None` was wrong: it discards 0.0 via falsy-zero, but 0.0 and
            # "not set" are semantically identical here (both mean "no volume data").
            # Use `> 0` so absent/zero both map to None (Kyle impact disabled),
            # while any real positive volume is preserved.
            lambda v: v if v > 0 else None
        )(_env_float("TC_AVG_DAILY_VOLUME", 0.0))
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commission_pct": self.commission_pct,
            "slippage_pct": self.slippage_pct,
            "spread_bps": self.spread_bps,
            "use_kyle_impact": self.use_kyle_impact,
            "kyle_lambda": self.kyle_lambda,
            "avg_daily_volume": self.avg_daily_volume,
        }


@dataclass
class TransactionCostBreakdown:
    commission_cost: float = 0.0       # total commission as fraction of notional
    slippage_cost: float = 0.0
    spread_cost: float = 0.0
    market_impact_cost: float = 0.0
    total_cost_pct: float = 0.0
    cost_per_trade: float = 0.0
    trades_to_break_even: Optional[int] = None  # trades before costs exceed alpha

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commission_cost": _sanitise_float(self.commission_cost),
            "slippage_cost": _sanitise_float(self.slippage_cost),
            "spread_cost": _sanitise_float(self.spread_cost),
            "market_impact_cost": _sanitise_float(self.market_impact_cost),
            "total_cost_pct": _sanitise_float(self.total_cost_pct),
            "cost_per_trade": _sanitise_float(self.cost_per_trade),
            "trades_to_break_even": self.trades_to_break_even,
        }


@dataclass
class CostAdjustedMetrics:
    gross_sharpe: Optional[float] = None
    net_sharpe: Optional[float] = None
    gross_return_pct: Optional[float] = None
    net_return_pct: Optional[float] = None
    gross_max_dd: Optional[float] = None
    net_max_dd: Optional[float] = None
    total_cost_drag_pct: Optional[float] = None
    cost_breakdown: Optional[TransactionCostBreakdown] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gross_sharpe": _sanitise_float(self.gross_sharpe),
            "net_sharpe": _sanitise_float(self.net_sharpe),
            "gross_return_pct": _sanitise_float(self.gross_return_pct),
            "net_return_pct": _sanitise_float(self.net_return_pct),
            "gross_max_dd": _sanitise_float(self.gross_max_dd),
            "net_max_dd": _sanitise_float(self.net_max_dd),
            "total_cost_drag_pct": _sanitise_float(self.total_cost_drag_pct),
            "cost_breakdown": self.cost_breakdown.to_dict() if self.cost_breakdown else None,
            "errors": self.errors,
        }


@dataclass
class CostSensitivityResult:
    base_config: Optional[TransactionCostConfig] = None
    base_metrics: Optional[CostAdjustedMetrics] = None
    scenarios: List[Dict[str, Any]] = field(default_factory=list)
    breakeven_commission_pct: Optional[float] = None
    breakeven_slippage_pct: Optional[float] = None
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_config": self.base_config.to_dict() if self.base_config else None,
            "base_metrics": self.base_metrics.to_dict() if self.base_metrics else None,
            "scenarios": self.scenarios,
            "breakeven_commission_pct": _sanitise_float(self.breakeven_commission_pct),
            "breakeven_slippage_pct": _sanitise_float(self.breakeven_slippage_pct),
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Statistics helpers ────────────────────────────────────────────────────────

def _sharpe_from_returns(returns: List[float], rf_daily: float = 0.0) -> Optional[float]:
    n = len(returns)
    if n < 2:
        return None
    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    # Subnormal-safe guard: ``<= 0.0`` admits values like 5e-324 which
    # produce ratios on the order of 1e+300 after the division.
    if not (std > 1e-14):
        return None
    sharpe = (mean_r - rf_daily) / std * math.sqrt(252.0)
    return sharpe if math.isfinite(sharpe) else None


def _total_return_pct(returns: List[float]) -> Optional[float]:
    if not returns:
        return None
    total = 1.0
    for r in returns:
        total *= (1.0 + r)
    val = (total - 1.0) * 100.0
    return val if math.isfinite(val) else None


def _max_drawdown_pct(returns: List[float]) -> Optional[float]:
    if not returns:
        return None
    peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= (1.0 + r)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    val = max_dd * 100.0
    return val if math.isfinite(val) else None


# ── Cost computation ───────────────────────────────────────────────────────────

def _compute_per_trade_cost(config: TransactionCostConfig, trade_size: float = 1.0) -> float:
    """
    Compute the total per-trade cost as a fraction of notional.

    Includes: commission + slippage + half-spread + optional Kyle impact.
    """
    commission = config.commission_pct
    slippage = config.slippage_pct
    spread_fraction = config.spread_bps / 20000.0  # half-spread: bps / 2 / 10000

    market_impact = 0.0
    if config.use_kyle_impact and config.avg_daily_volume and config.avg_daily_volume > 0:
        # Kyle impact: lambda * (trade_size / ADV)
        participation = trade_size / config.avg_daily_volume
        market_impact = config.kyle_lambda * participation
    elif config.use_kyle_impact:
        # No ADV available — use a fixed 1% participation assumption
        market_impact = config.kyle_lambda * 0.01

    return commission + slippage + spread_fraction + market_impact


def compute_cost_adjusted_metrics(
    returns: List[float],
    trade_signals: List[int],
    config: TransactionCostConfig,
) -> CostAdjustedMetrics:
    """
    Compute gross and net metrics after deducting transaction costs.

    Parameters
    ----------
    returns : List[float]
        Period return series.
    trade_signals : List[int]
        Signal series aligned with returns. +1 = buy, -1 = sell, 0 = hold.
        Must have same length as returns.
    config : TransactionCostConfig
        Cost model configuration.

    Returns
    -------
    CostAdjustedMetrics
    """
    result = CostAdjustedMetrics()

    if not returns:
        result.errors.append("Empty returns series")
        return result

    n = len(returns)
    if len(trade_signals) != n:
        # Pad or trim trade_signals to match returns length
        if len(trade_signals) < n:
            trade_signals = list(trade_signals) + [0] * (n - len(trade_signals))
        else:
            trade_signals = list(trade_signals[:n])

    # Gross metrics
    result.gross_sharpe = _sharpe_from_returns(returns)
    result.gross_return_pct = _total_return_pct(returns)
    result.gross_max_dd = _max_drawdown_pct(returns)

    per_trade_cost = _compute_per_trade_cost(config)
    trade_count = sum(1 for s in trade_signals if s != 0)

    # Deduct cost on each trade bar
    net_returns: List[float] = []
    total_commission = 0.0
    total_slippage = 0.0
    total_spread = 0.0
    total_impact = 0.0

    for i in range(n):
        r = returns[i]
        if trade_signals[i] != 0:
            cost = per_trade_cost
            net_r = r - cost
            total_commission += config.commission_pct
            total_slippage += config.slippage_pct
            total_spread += config.spread_bps / 20000.0
            kyle_part = cost - config.commission_pct - config.slippage_pct - config.spread_bps / 20000.0
            total_impact += max(0.0, kyle_part)
        else:
            net_r = r
        net_returns.append(net_r)

    result.net_sharpe = _sharpe_from_returns(net_returns)
    result.net_return_pct = _total_return_pct(net_returns)
    result.net_max_dd = _max_drawdown_pct(net_returns)

    # Cost drag: use compound-return ratio rather than simple linear subtraction.
    # For multi-period series, (gross% - net%) in percentage points overstates
    # the true fraction of wealth consumed by costs.  The correct measure is
    # how much larger the gross final wealth is relative to the net final wealth:
    #   drag = (1 + gross/100) / (1 + net/100) - 1, converted to pct.
    # Guard against net_factor <= 0 (catastrophic loss) by falling back to
    # the simple difference so the field always has a meaningful finite value.
    if result.gross_return_pct is not None and result.net_return_pct is not None:
        gross_factor = 1.0 + result.gross_return_pct / 100.0
        net_factor = 1.0 + result.net_return_pct / 100.0
        if net_factor > 0:
            result.total_cost_drag_pct = (gross_factor / net_factor - 1.0) * 100.0
        else:
            # Fallback: if net return is -100% or worse, ratio is undefined;
            # report simple point difference as a conservative approximation.
            result.total_cost_drag_pct = result.gross_return_pct - result.net_return_pct

    total_cost = total_commission + total_slippage + total_spread + total_impact
    cost_per_trade = per_trade_cost

    # Trades to break even: gross_return_total / cost_per_trade
    trades_to_be: Optional[int] = None
    if result.gross_return_pct is not None and cost_per_trade > 0:
        gross_total_frac = result.gross_return_pct / 100.0
        if gross_total_frac > 0:
            # Each trade costs cost_per_trade; we need alpha > total costs
            estimated_alpha_per_trade = gross_total_frac / max(1, trade_count)
            if estimated_alpha_per_trade > cost_per_trade:
                trades_to_be = max(1, int(math.ceil(gross_total_frac / cost_per_trade)))

    result.cost_breakdown = TransactionCostBreakdown(
        commission_cost=total_commission,
        slippage_cost=total_slippage,
        spread_cost=total_spread,
        market_impact_cost=total_impact,
        total_cost_pct=total_cost * 100.0,
        cost_per_trade=cost_per_trade * 100.0,
        trades_to_break_even=trades_to_be,
    )

    return result


# ── Sensitivity analysis ───────────────────────────────────────────────────────

def run_cost_sensitivity(
    returns: List[float],
    trade_signals: List[int],
    n_scenarios: int = 10,
    base_config: Optional[TransactionCostConfig] = None,
) -> CostSensitivityResult:
    """
    Sweep commission and slippage across n_scenarios steps and find breakeven.

    Parameters
    ----------
    returns : List[float]
        Period return series.
    trade_signals : List[int]
        Trade signal series.
    n_scenarios : int
        Number of steps in the sweep.
    base_config : TransactionCostConfig, optional
        Base configuration. Uses env-defaults if None.

    Returns
    -------
    CostSensitivityResult
    """
    sens = CostSensitivityResult()

    if base_config is None:
        base_config = TransactionCostConfig()
    sens.base_config = base_config

    if not returns:
        sens.errors.append("Empty returns — cannot run sensitivity analysis")
        return sens

    # Guard: n_scenarios=1 produces only a zero-cost scenario (frac=0/0=0),
    # making breakeven analysis meaningless.  Clamp to minimum 2.
    if n_scenarios < 2:
        sens.errors.append(
            f"n_scenarios must be ≥ 2 for a meaningful sweep (got {n_scenarios}); "
            "using n_scenarios=2."
        )
        n_scenarios = 2

    # Base metrics
    sens.base_metrics = compute_cost_adjusted_metrics(returns, trade_signals, base_config)

    # Commission sweep: 0 to 0.003 in n_scenarios steps
    scenarios: List[Dict[str, Any]] = []
    comm_max = 0.003
    slip_max = 0.002

    for i in range(n_scenarios):
        frac = i / max(1, n_scenarios - 1)
        comm = frac * comm_max
        slip = frac * slip_max

        cfg_c = TransactionCostConfig(
            commission_pct=comm,
            slippage_pct=base_config.slippage_pct,
            spread_bps=base_config.spread_bps,
            use_kyle_impact=base_config.use_kyle_impact,
            kyle_lambda=base_config.kyle_lambda,
            avg_daily_volume=base_config.avg_daily_volume,
        )
        cfg_s = TransactionCostConfig(
            commission_pct=base_config.commission_pct,
            slippage_pct=slip,
            spread_bps=base_config.spread_bps,
            use_kyle_impact=base_config.use_kyle_impact,
            kyle_lambda=base_config.kyle_lambda,
            avg_daily_volume=base_config.avg_daily_volume,
        )

        m_c = compute_cost_adjusted_metrics(returns, trade_signals, cfg_c)
        m_s = compute_cost_adjusted_metrics(returns, trade_signals, cfg_s)

        scenarios.append({
            "step": i,
            "commission_sweep": {
                "commission_pct": comm,
                "net_sharpe": _sanitise_float(m_c.net_sharpe),
                "net_return_pct": _sanitise_float(m_c.net_return_pct),
                "cost_drag_pct": _sanitise_float(m_c.total_cost_drag_pct),
            },
            "slippage_sweep": {
                "slippage_pct": slip,
                "net_sharpe": _sanitise_float(m_s.net_sharpe),
                "net_return_pct": _sanitise_float(m_s.net_return_pct),
                "cost_drag_pct": _sanitise_float(m_s.total_cost_drag_pct),
            },
        })

    sens.scenarios = scenarios

    # Find breakeven commission (where net_sharpe first crosses 0).
    # Seed with None rather than gross_sharpe: when gross_sharpe is None
    # (insufficient data), seeding with it causes the `prev_c_sharpe is not None`
    # guard to fail for every iteration, silently suppressing all breakeven detection.
    be_comm: Optional[float] = None
    be_slip: Optional[float] = None
    prev_c_sharpe: Optional[float] = None
    prev_s_sharpe: Optional[float] = None

    for sc in scenarios:
        c_sharpe = sc["commission_sweep"]["net_sharpe"]
        s_sharpe = sc["slippage_sweep"]["net_sharpe"]
        comm = sc["commission_sweep"]["commission_pct"]
        slip = sc["slippage_sweep"]["slippage_pct"]

        if (
            be_comm is None
            and c_sharpe is not None
            and prev_c_sharpe is not None
            and prev_c_sharpe > 0
            and c_sharpe <= 0
        ):
            be_comm = comm
        if (
            be_slip is None
            and s_sharpe is not None
            and prev_s_sharpe is not None
            and prev_s_sharpe > 0
            and s_sharpe <= 0
        ):
            be_slip = slip

        if c_sharpe is not None:
            prev_c_sharpe = c_sharpe
        if s_sharpe is not None:
            prev_s_sharpe = s_sharpe

    sens.breakeven_commission_pct = be_comm
    sens.breakeven_slippage_pct = be_slip

    return sens


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_backtest_data(run_dir: str) -> Tuple[List[float], List[int], Optional[int]]:
    """
    Load returns, trade_signals, and trade_count from run_dir.

    Returns (returns, trade_signals, trade_count_from_report).
    trade_signals is inferred from trade_count if not directly available.
    """
    report_path = os.path.join(run_dir, "backtest_report.json")
    equity: List[float] = []
    trade_count: Optional[int] = None

    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            eq = data.get("equity_curve", [])
            if isinstance(eq, list):
                for item in eq:
                    if isinstance(item, (int, float)):
                        equity.append(float(item))
                    elif isinstance(item, dict):
                        v = item.get("equity", item.get("value", item.get("close")))
                        if v is not None:
                            try:
                                equity.append(float(v))
                            except (ValueError, TypeError):
                                pass

            # Try to get trade_count from various report locations.
            # break after first successful read so baseline_metrics wins over
            # best_metrics — without the break the last found value would win,
            # silently replacing a valid baseline count with an optimised one.
            for section in ("baseline_metrics", "best_metrics"):
                sec = data.get(section, {})
                if isinstance(sec, dict) and "trade_count" in sec:
                    try:
                        trade_count = int(sec["trade_count"])
                        break
                    except (ValueError, TypeError):
                        pass
        except (OSError, json.JSONDecodeError):
            pass

    # CSV fallback for equity
    if len(equity) < 2:
        data_dir = os.path.join(run_dir, "code", "data")
        if os.path.isdir(data_dir):
            for fname in sorted(os.listdir(data_dir)):
                if not fname.lower().endswith(".csv"):
                    continue
                try:
                    with open(os.path.join(data_dir, fname), "r", encoding="utf-8", newline="") as f:
                        reader = _csv_module.DictReader(f)
                        for row in reader:
                            for col in ("equity", "close", "Close", "price", "Price"):
                                if col in row:
                                    try:
                                        equity.append(float(row[col]))
                                        break
                                    except (ValueError, TypeError):
                                        continue
                    if len(equity) >= 2:
                        break
                except (OSError, _csv_module.Error):
                    pass

    if len(equity) < 2:
        return [], [], trade_count

    import math as _math
    # Preserve series length with else 0.0 so that trade-signal indices
    # remain temporally aligned with the equity timeline.  Filtering out
    # bars (as the previous comprehension did) would shrink the returns
    # array and shift all subsequent signal placements by the number of
    # skipped bars.
    returns = [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        if equity[i - 1] > 0
        and _math.isfinite(equity[i - 1])
        and _math.isfinite(equity[i])
        else 0.0
        for i in range(1, len(equity))
    ]

    # Infer trade signals: distribute trade_count uniformly across bars
    n = len(returns)
    if trade_count is not None and trade_count > 0:
        # Place signals at evenly spaced intervals
        signals = [0] * n
        if trade_count <= n:
            step = n // trade_count
            for k in range(trade_count):
                idx = min(k * step, n - 1)
                signals[idx] = 1 if k % 2 == 0 else -1
        else:
            signals = [1 if i % 2 == 0 else -1 for i in range(n)]
    else:
        # Assume one trade per bar (conservative)
        signals = [1 if i % 2 == 0 else -1 for i in range(n)]

    return returns, signals, trade_count


# ── Public runner ──────────────────────────────────────────────────────────────

def run_transaction_cost_analysis(
    run_dir: str,
    config: Optional[TransactionCostConfig] = None,
) -> CostSensitivityResult:
    """
    Load backtest data from run_dir, run full cost analysis, save report.

    Parameters
    ----------
    run_dir : str
        Path to run directory.
    config : TransactionCostConfig, optional
        Cost model configuration. Defaults to env-var driven TransactionCostConfig().

    Returns
    -------
    CostSensitivityResult
    """
    result = CostSensitivityResult()

    if not _is_quant_run(run_dir):
        msg = f"run_transaction_cost_analysis: skipping non-quant run_dir={run_dir}"
        logger.warning(msg)
        result.warnings.append(msg)
        return result

    if config is None:
        config = TransactionCostConfig()

    returns, trade_signals, trade_count = _load_backtest_data(run_dir)
    if not returns:
        msg = "Insufficient backtest data for transaction cost analysis"
        result.errors.append(msg)
        logger.warning(msg)
        return result

    logger.info(
        "Running transaction cost analysis: %d bars, %d trades",
        len(returns),
        sum(1 for s in trade_signals if s != 0),
    )

    n_scenarios = _env_int("TC_N_SCENARIOS", 10)
    result = run_cost_sensitivity(returns, trade_signals, n_scenarios=n_scenarios, base_config=config)

    # Persist report (atomic write via tmp + os.replace to prevent partial files)
    report_path = os.path.join(run_dir, "transaction_cost_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        result.report_path = report_path
        logger.info("Transaction cost report saved to %s", report_path)
    except Exception as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        result.errors.append(f"Could not save report: {exc}")

    return result
