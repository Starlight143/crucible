"""
features/quant_analytics.py
============================
Walk-Forward Validation and Statistical Significance Testing for Quant mode runs.

Responsibilities
----------------
1. Walk-forward validation: splits the equity curve / returns into IS/OOS folds,
   runs sub-backtests for each fold, and aggregates metrics.
2. Statistical significance testing: permutation test, bootstrap CI, and
   Deflated Sharpe Ratio (DSR) to detect overfitting from multiple testing.
3. Integration function: orchestrates both analyses and saves a JSON report.

Environment variables
---------------------
WALK_FORWARD_N_SPLITS         Number of IS/OOS folds (default 5).
WALK_FORWARD_IS_PCT           Use percentage-based splits (default 1 → True).
WALK_FORWARD_OOS_PCT          OOS fraction of each fold (default 0.3).
WALK_FORWARD_MIN_TRAIN_BARS   Minimum in-sample bars per fold (default 100).
SIG_N_PERMUTATIONS            Permutations for significance test (default 1000).
SIG_N_BOOTSTRAP               Bootstrap resamples for CI (default 1000).
SIG_CONFIDENCE_LEVEL          Confidence level for bootstrap CI (default 0.95).
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import random
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


# ── JSON serialisation helpers ────────────────────────────────────────────────

def _sanitise_float(v: Any) -> Any:
    """Replace NaN/Inf with None for JSON safety."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def _sanitise_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _sanitise_float(v) for k, v in d.items()}


# ── Optional scipy ────────────────────────────────────────────────────────────

try:
    from scipy import stats as _scipy_stats  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _normal_cdf(x: float) -> float:
    """Standard normal CDF using math.erfc for stdlib-only path."""
    if _HAS_SCIPY:
        return float(_scipy_stats.norm.cdf(x))
    # Abramowitz & Stegun approximation via erfc
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _t_cdf(t: float, df: float) -> float:
    """Student-t CDF. Falls back to normal CDF when df >= 30."""
    if _HAS_SCIPY:
        return float(_scipy_stats.t.cdf(t, df))
    if df >= 30:
        return _normal_cdf(t)
    # Approximation via incomplete beta function
    x = df / (df + t * t)
    try:
        ibeta = _regularised_incomplete_beta(df / 2.0, 0.5, x)
    except Exception:
        return _normal_cdf(t)
    p = 0.5 * ibeta
    return p if t < 0 else 1.0 - p


def _regularised_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b) via continued fraction."""
    if x < 0.0 or x > 1.0:
        return 0.0
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    # Use symmetry relation for numerical stability
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _regularised_incomplete_beta(b, a, 1.0 - x)
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta) / a
    # Lentz's continued fraction
    def cf() -> float:
        TINY = 1e-30
        f = TINY
        c = TINY
        d = 1.0 - (a + b) * x / (a + 1.0)
        if abs(d) < TINY:
            d = TINY
        d = 1.0 / d
        f = d
        for m in range(1, 200):
            for sign in (1, -1):
                if sign == 1:
                    n = m
                    num = n * (b - n) * x / ((a + 2 * n - 1) * (a + 2 * n))
                else:
                    n = m
                    num = -(a + n) * (a + b + n) * x / ((a + 2 * n) * (a + 2 * n + 1))
                d = 1.0 + num * d
                if abs(d) < TINY:
                    d = TINY
                c = 1.0 + num / c
                if abs(c) < TINY:
                    c = TINY
                d = 1.0 / d
                delta = c * d
                f *= delta
                if abs(delta - 1.0) < 1e-10:
                    return f
        return f
    return front * cf()


# ── Sharpe helper ─────────────────────────────────────────────────────────────

def _finite_returns(returns: List[float]) -> List[float]:
    """Filter NaN / Inf sentinels out of a returns list.

    v1.1.0: ``_equity_to_returns`` now emits ``float('nan')`` for slots where
    the previous equity was zero / negative / non-finite (previously it
    silently substituted ``0.0``, contaminating Sharpe and the bootstrap
    pool with synthetic flat days).  Every consumer that aggregates the
    list (mean / std / pct / max-dd) must filter NaN first.
    """
    return [r for r in returns if math.isfinite(r)]


def _sharpe_from_returns(
    returns: List[float],
    risk_free_rate: float = 0.0,
) -> Optional[float]:
    """Annualised Sharpe ratio from a list of period returns.

    Non-finite entries (NaN / Inf sentinels emitted by ``_equity_to_returns``
    when the prior-step equity was invalid) are filtered before the mean /
    variance computation so a strategy with the occasional invalid bar
    does not have its Sharpe deflated by synthetic zero returns.
    """
    finite = _finite_returns(returns)
    if len(finite) < 2:
        return None
    n = len(finite)
    mean_r = sum(finite) / n
    variance = sum((r - mean_r) ** 2 for r in finite) / (n - 1)
    std_r = math.sqrt(variance)
    if not (std_r > 1e-14):
        return None
    excess = mean_r - risk_free_rate / 252.0
    sharpe = (excess / std_r) * math.sqrt(252.0)
    if not math.isfinite(sharpe):
        return None
    return sharpe


# ── BacktestMetrics (lightweight import-free version) ─────────────────────────

@dataclass
class BacktestMetrics:
    sharpe_ratio: Optional[float] = None
    total_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    win_rate: Optional[float] = None
    trade_count: Optional[int] = None
    profit_factor: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: _sanitise_float(v)
            for k, v in {
                "sharpe_ratio": self.sharpe_ratio,
                "total_return_pct": self.total_return_pct,
                "max_drawdown_pct": self.max_drawdown_pct,
                "win_rate": self.win_rate,
                "trade_count": self.trade_count,
                "profit_factor": self.profit_factor,
            }.items()
            if v is not None
        }


# ── Walk-Forward dataclasses ──────────────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    n_splits: int = field(default_factory=lambda: _env_int("WALK_FORWARD_N_SPLITS", 5))
    is_pct: bool = field(default_factory=lambda: _env_bool("WALK_FORWARD_IS_PCT", True))
    oos_pct: float = field(default_factory=lambda: _env_float("WALK_FORWARD_OOS_PCT", 0.3))
    min_train_bars: int = field(default_factory=lambda: _env_int("WALK_FORWARD_MIN_TRAIN_BARS", 100))


@dataclass
class WalkForwardFold:
    fold_idx: int
    is_start: int
    is_end: int
    oos_start: int
    oos_end: int
    is_metrics: Optional[BacktestMetrics] = None
    oos_metrics: Optional[BacktestMetrics] = None
    is_success: bool = False
    oos_success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "fold_idx": self.fold_idx,
            "is_start": self.is_start,
            "is_end": self.is_end,
            "oos_start": self.oos_start,
            "oos_end": self.oos_end,
            "is_success": self.is_success,
            "oos_success": self.oos_success,
        }
        if self.is_metrics:
            d["is_metrics"] = self.is_metrics.to_dict()
        if self.oos_metrics:
            d["oos_metrics"] = self.oos_metrics.to_dict()
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class WalkForwardResult:
    folds: List[WalkForwardFold] = field(default_factory=list)
    avg_is_sharpe: Optional[float] = None
    avg_oos_sharpe: Optional[float] = None
    sharpe_decay_ratio: Optional[float] = None  # OOS / IS
    avg_oos_max_dd: Optional[float] = None
    consistency_score: Optional[float] = None   # fraction of folds with positive OOS return
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "folds": [f.to_dict() for f in self.folds],
            "avg_is_sharpe": _sanitise_float(self.avg_is_sharpe),
            "avg_oos_sharpe": _sanitise_float(self.avg_oos_sharpe),
            "sharpe_decay_ratio": _sanitise_float(self.sharpe_decay_ratio),
            "avg_oos_max_dd": _sanitise_float(self.avg_oos_max_dd),
            "consistency_score": _sanitise_float(self.consistency_score),
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Statistical Significance dataclasses ──────────────────────────────────────

@dataclass
class SignificanceTestResult:
    n_permutations: int = 0
    observed_sharpe: Optional[float] = None
    p_value: Optional[float] = None
    # v1.1.0 third-pass: split one-sided / two-sided p-values explicitly.
    # ``p_value`` continues to track the test that drives
    # ``is_significant`` (default two-sided so short-only strategies with
    # observed_sharpe < 0 are correctly evaluated against |SR| ≥ |obs|
    # rather than always p≈1).  The other two fields are populated for
    # transparency so downstream consumers can pick whichever direction
    # matches their hypothesis.
    p_value_one_sided: Optional[float] = None
    p_value_two_sided: Optional[float] = None
    # v1.1.0 fourth-pass: expose BOTH one-sided tails explicitly so
    # consumers can pick whichever matches their pre-registered
    # hypothesis without relying on the auto-direction in
    # ``p_value_one_sided`` (which picks the tail the OBSERVATION
    # falls into — formally double-dipping for hypothesis testing).
    p_value_greater: Optional[float] = None  # H1: observed > permuted
    p_value_less: Optional[float] = None     # H1: observed < permuted
    alternative: str = "two-sided"  # "two-sided" | "greater" | "less"
    is_significant: bool = False
    sharpe_ci_lower: Optional[float] = None
    sharpe_ci_upper: Optional[float] = None
    deflated_sharpe_ratio: Optional[float] = None
    dsr_p_value: Optional[float] = None
    bootstrap_n: int = 0
    sharpe_distribution: List[float] = field(default_factory=list)  # truncated to 100
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_permutations": self.n_permutations,
            "observed_sharpe": _sanitise_float(self.observed_sharpe),
            "p_value": _sanitise_float(self.p_value),
            "p_value_one_sided": _sanitise_float(self.p_value_one_sided),
            "p_value_two_sided": _sanitise_float(self.p_value_two_sided),
            "p_value_greater": _sanitise_float(self.p_value_greater),
            "p_value_less": _sanitise_float(self.p_value_less),
            "alternative": self.alternative,
            "is_significant": self.is_significant,
            "sharpe_ci_lower": _sanitise_float(self.sharpe_ci_lower),
            "sharpe_ci_upper": _sanitise_float(self.sharpe_ci_upper),
            "deflated_sharpe_ratio": _sanitise_float(self.deflated_sharpe_ratio),
            "dsr_p_value": _sanitise_float(self.dsr_p_value),
            "bootstrap_n": self.bootstrap_n,
            "sharpe_distribution": [_sanitise_float(v) for v in self.sharpe_distribution],
            "errors": self.errors,
        }


# ── Walk-Forward helpers ───────────────────────────────────────────────────────

def _load_equity_curve(run_dir: str) -> Tuple[List[float], List[str]]:
    """
    Load equity curve values and timestamps from run_dir.

    Returns (equity_values, timestamps). On failure, returns ([], []).
    """
    # Try backtest_report.json first
    report_path = os.path.join(run_dir, "backtest_report.json")
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            eq = data.get("equity_curve", [])
            if isinstance(eq, list) and len(eq) >= 2:
                values: List[float] = []
                timestamps: List[str] = []
                for item in eq:
                    if isinstance(item, (int, float)):
                        values.append(float(item))
                        timestamps.append("")
                    elif isinstance(item, dict):
                        v = item.get("equity", item.get("value", item.get("close", None)))
                        t = item.get("timestamp", item.get("date", item.get("ts", "")))
                        if v is not None:
                            try:
                                values.append(float(v))
                                timestamps.append(str(t))
                            except (ValueError, TypeError):
                                pass
                if len(values) >= 2:
                    return values, timestamps
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    # Try CSV files in code/data/
    data_dir = os.path.join(run_dir, "code", "data")
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if not fname.lower().endswith(".csv"):
                continue
            csv_path = os.path.join(data_dir, fname)
            try:
                values = []
                timestamps = []
                with open(csv_path, "r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        for col in ("equity", "close", "Close", "price", "Price"):
                            if col in row:
                                try:
                                    values.append(float(row[col]))
                                    ts_col = row.get("date", row.get("Date",
                                                     row.get("timestamp", row.get("ts", ""))))
                                    timestamps.append(str(ts_col))
                                    break
                                except (ValueError, TypeError):
                                    continue
                if len(values) >= 2:
                    return values, timestamps
            except (OSError, csv.Error):
                pass

    return [], []


def _equity_to_returns(equity: List[float]) -> List[float]:
    """Convert equity curve to period return series.

    v1.1.0: invalid slots (prev<=0, prev/curr non-finite) emit
    ``float('nan')`` rather than a silent ``0.0`` substitution.  The
    output list keeps the same length as the input minus one, preserving
    positional alignment with any external timestamp list; downstream
    consumers in this module call :func:`_finite_returns` to drop NaN
    before aggregation, so Sharpe / bootstrap / DSR / walk-forward stats
    no longer mistake a bad-data sentinel for a flat-return day.
    """
    if len(equity) < 2:
        return []
    returns: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        curr = equity[i]
        # v1.1.0 fifth-pass (G-16): tighten denominator floor to 1e-14
        # per CLAUDE.md § 9.3.  Previous ``prev > 0`` admitted IEEE 754
        # subnormals (5e-324) which produced 1e+300-magnitude returns
        # poisoning every downstream stat.
        if prev > 1e-14 and math.isfinite(prev) and math.isfinite(curr):
            returns.append((curr - prev) / prev)
        else:
            returns.append(float("nan"))
    return returns


def _compute_metrics_from_returns(returns: List[float]) -> BacktestMetrics:
    """Compute BacktestMetrics from a return series.

    Non-finite entries (NaN sentinels from ``_equity_to_returns``) are
    filtered before aggregation so an invalid bar does not collapse the
    cumulative product, deflate Sharpe, or invent a synthetic flat day.
    """
    finite = _finite_returns(returns)
    if not finite:
        return BacktestMetrics()
    n = len(finite)
    total_return = 1.0
    for r in finite:
        total_return *= (1.0 + r)
    total_return_pct = (total_return - 1.0) * 100.0

    sharpe = _sharpe_from_returns(finite)

    # Max drawdown
    peak = 1.0
    equity = 1.0
    max_dd = 0.0
    for r in finite:
        equity *= (1.0 + r)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    wins = sum(1 for r in finite if r > 0)
    win_rate: Optional[float] = wins / n if n > 0 else None

    return BacktestMetrics(
        sharpe_ratio=sharpe,
        total_return_pct=total_return_pct if math.isfinite(total_return_pct) else None,
        max_drawdown_pct=max_dd * 100.0,
        win_rate=win_rate,
        trade_count=n,
    )


def _build_walk_forward_folds(
    n_total: int,
    config: WalkForwardConfig,
) -> List[Tuple[int, int, int, int]]:
    """
    Compute (is_start, is_end, oos_start, oos_end) index tuples.

    Uses a rolling-window approach: each fold advances by oos_size bars.
    """
    if config.n_splits <= 0:
        return []
    oos_size = max(1, int(round(n_total * config.oos_pct / config.n_splits)))
    is_size = max(config.min_train_bars, n_total - config.n_splits * oos_size)

    folds = []
    for i in range(config.n_splits):
        oos_start = is_size + i * oos_size
        oos_end = min(oos_start + oos_size, n_total)
        is_start = max(0, oos_start - is_size)
        is_end = oos_start
        if is_end - is_start < config.min_train_bars:
            continue
        if oos_end <= oos_start:
            continue
        folds.append((is_start, is_end, oos_start, oos_end))

    # If rolling yields nothing (small dataset), fall back to single split
    if not folds and n_total >= config.min_train_bars + 2:
        split = int(n_total * (1.0 - config.oos_pct))
        folds.append((0, split, split, n_total))

    return folds


def run_walk_forward(
    run_dir: str,
    config: Optional[WalkForwardConfig] = None,
    llm: Any = None,
    verbose: bool = True,
) -> WalkForwardResult:
    """
    Run walk-forward validation on the equity curve found in run_dir.

    For each IS/OOS fold, compute BacktestMetrics directly from the equity
    sub-series (no subprocess launched — the strategy code is already evaluated;
    we slice the existing equity curve).

    Parameters
    ----------
    run_dir : str
        Directory containing backtest_report.json (and optionally code/).
    config : WalkForwardConfig, optional
        Walk-forward parameters. Defaults to env-var driven WalkForwardConfig().
    llm : Any
        Unused; accepted for API compatibility.
    verbose : bool
        If True, log progress at INFO level.

    Returns
    -------
    WalkForwardResult
    """
    result = WalkForwardResult()

    if not _is_quant_run(run_dir):
        msg = f"run_walk_forward: skipping non-quant run_dir={run_dir}"
        logger.warning(msg)
        result.warnings.append(msg)
        return result

    if config is None:
        config = WalkForwardConfig()

    equity, timestamps = _load_equity_curve(run_dir)
    if len(equity) < config.min_train_bars + 2:
        msg = (
            f"run_walk_forward: insufficient equity curve data "
            f"(got {len(equity)}, need {config.min_train_bars + 2})"
        )
        logger.warning(msg)
        result.warnings.append(msg)
        return result

    fold_specs = _build_walk_forward_folds(len(equity), config)
    if not fold_specs:
        msg = "run_walk_forward: could not build any valid folds"
        logger.warning(msg)
        result.warnings.append(msg)
        return result

    folds: List[WalkForwardFold] = []
    for idx, (is_start, is_end, oos_start, oos_end) in enumerate(fold_specs):
        if verbose:
            logger.info(
                "Walk-forward fold %d/%d  IS=[%d,%d) OOS=[%d,%d)",
                idx + 1, len(fold_specs), is_start, is_end, oos_start, oos_end,
            )

        fold = WalkForwardFold(
            fold_idx=idx,
            is_start=is_start,
            is_end=is_end,
            oos_start=oos_start,
            oos_end=oos_end,
        )

        try:
            is_eq = equity[is_start:is_end]
            is_returns = _equity_to_returns(is_eq)
            if is_returns:
                fold.is_metrics = _compute_metrics_from_returns(is_returns)
                fold.is_success = True
        except Exception as exc:
            fold.error = f"IS error: {exc}"
            logger.exception("Walk-forward IS fold %d failed", idx)

        try:
            oos_eq = equity[oos_start:oos_end]
            oos_returns = _equity_to_returns(oos_eq)
            if oos_returns:
                fold.oos_metrics = _compute_metrics_from_returns(oos_returns)
                fold.oos_success = True
        except Exception as exc:
            fold.error = (fold.error or "") + f" OOS error: {exc}"
            logger.exception("Walk-forward OOS fold %d failed", idx)

        folds.append(fold)

    result.folds = folds

    # Aggregate
    is_sharpes = [
        f.is_metrics.sharpe_ratio
        for f in folds
        if f.is_success and f.is_metrics and f.is_metrics.sharpe_ratio is not None
    ]
    oos_sharpes = [
        f.oos_metrics.sharpe_ratio
        for f in folds
        if f.oos_success and f.oos_metrics and f.oos_metrics.sharpe_ratio is not None
    ]
    oos_returns = [
        f.oos_metrics.total_return_pct
        for f in folds
        if f.oos_success and f.oos_metrics and f.oos_metrics.total_return_pct is not None
    ]
    oos_dds = [
        f.oos_metrics.max_drawdown_pct
        for f in folds
        if f.oos_success and f.oos_metrics and f.oos_metrics.max_drawdown_pct is not None
    ]

    if is_sharpes:
        result.avg_is_sharpe = sum(is_sharpes) / len(is_sharpes)
    if oos_sharpes:
        result.avg_oos_sharpe = sum(oos_sharpes) / len(oos_sharpes)
    if (
        result.avg_is_sharpe is not None
        and result.avg_oos_sharpe is not None
        and math.isfinite(result.avg_is_sharpe)
        and math.isfinite(result.avg_oos_sharpe)
        # v1.1.0 fifth-pass (G-15): tighten denominator floor to
        # 1e-8 per CLAUDE.md § 9.3 quant rule.  Previous 1e-10 admits
        # IS Sharpe in the 1e-10..1e-8 band, after which
        # ``oos_sharpe / is_sharpe`` can balloon to ~1e+8 (same
        # failure pattern that motivated the DSR denom_sq fix).
        and abs(result.avg_is_sharpe) > 1e-8
    ):
        ratio = result.avg_oos_sharpe / result.avg_is_sharpe
        result.sharpe_decay_ratio = ratio if math.isfinite(ratio) else None

    if oos_dds:
        result.avg_oos_max_dd = sum(oos_dds) / len(oos_dds)

    if oos_returns:
        positive_oos = sum(1 for r in oos_returns if r > 0)
        result.consistency_score = positive_oos / len(oos_returns)

    # Persist report (atomic write via tmp + os.replace)
    report_path = os.path.join(run_dir, "walk_forward_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        result.report_path = report_path
        if verbose:
            logger.info("Walk-forward report saved to %s", report_path)
    except Exception as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        result.errors.append(f"Could not save report: {exc}")

    return result


# ── Statistical Significance ───────────────────────────────────────────────────

def run_significance_test(
    returns: List[float],
    n_permutations: int = 1000,
    n_bootstrap: int = 1000,
    confidence_level: float = 0.95,
    n_trials_tested: int = 1,
) -> SignificanceTestResult:
    """
    Run permutation test, bootstrap CI, and Deflated Sharpe Ratio (DSR).

    Parameters
    ----------
    returns : List[float]
        Period return series (daily or otherwise).
    n_permutations : int
        Number of random permutations for p-value estimation.
    n_bootstrap : int
        Number of bootstrap resamples for CI.
    confidence_level : float
        Confidence level for bootstrap CI (e.g. 0.95).
    n_trials_tested : int
        Number of strategies/parameter sets tested (used for DSR adjustment).

    Returns
    -------
    SignificanceTestResult
    """
    result = SignificanceTestResult(
        n_permutations=n_permutations,
        bootstrap_n=n_bootstrap,
    )

    if len(returns) < 5:
        result.errors.append("Insufficient returns for significance test (need >= 5)")
        return result

    # v1.1.0: filter NaN sentinels before computing significance.  Allowing
    # NaN entries into the permutation pool would silently corrupt the
    # shuffle (any permutation that placed NaN in the early window would
    # propagate NaN through std/mean and the entire permutation would be
    # discarded by _sharpe_from_returns' None return).  Filtering up front
    # makes the test consume a strictly clean signal.
    returns = _finite_returns(returns)
    observed_sharpe = _sharpe_from_returns(returns)
    result.observed_sharpe = observed_sharpe

    if observed_sharpe is None:
        result.errors.append("Could not compute observed Sharpe ratio (zero std)")
        return result

    # v1.1.0: independent RNGs for permutation vs bootstrap so reordering
    # the two blocks during future maintenance does not silently change
    # the bootstrap CI.  Both seed off the same base for reproducibility.
    # v1.1.0 fourth-pass: env-overridable seeds so operators running
    # multi-fold validation (where every fold uses the same observed
    # series) can opt into different permutations per fold without
    # touching code.  Defaults match the prior hard-coded values.
    perm_rng = random.Random(_env_int("SIG_PERMUTATION_SEED", 42))
    boot_rng = random.Random(_env_int("SIG_BOOTSTRAP_SEED", 43))

    # ── Permutation test ──────────────────────────────────────────────────────
    perm_sharpes: List[float] = []
    returns_copy = list(returns)
    for _ in range(n_permutations):
        perm_rng.shuffle(returns_copy)
        s = _sharpe_from_returns(returns_copy)
        if s is not None:
            perm_sharpes.append(s)

    if perm_sharpes:
        # Phipson & Smyth (2010) +1 correction: the observed value is itself
        # one realisation under H0, so reporting count_ge / N would imply
        # ``p = 0`` whenever no permutation matched — overstating the
        # evidence against H0.  ``(count_ge + 1) / (N + 1)`` is the exact
        # unbiased estimator for a permutation p-value.
        #
        # v1.1.0 third-pass: also compute the two-sided variant.  The
        # original one-sided test gave p ≈ 1 for any strategy with
        # observed_sharpe < 0 (e.g. a short-only strategy whose
        # genuine signal lives in the lower tail).  The two-sided
        # version compares |observed| against |permuted|, so both
        # tails are credited — and becomes the default
        # ``p_value``/``is_significant`` driver.
        n_perm = len(perm_sharpes)
        count_ge = sum(1 for s in perm_sharpes if s >= observed_sharpe)
        count_le = sum(1 for s in perm_sharpes if s <= observed_sharpe)
        abs_obs = abs(observed_sharpe)
        count_abs_ge = sum(1 for s in perm_sharpes if abs(s) >= abs_obs)

        p_one_sided_greater = (count_ge + 1) / (n_perm + 1)
        p_one_sided_less = (count_le + 1) / (n_perm + 1)
        # Auto-direction one-sided: pick the tail the observation
        # actually points into.  Useful for the "summarise in one
        # number" UI lane; consumers running a pre-registered
        # directional test should read ``p_value_greater`` /
        # ``p_value_less`` directly.
        p_one_sided = (
            p_one_sided_greater if observed_sharpe >= 0 else p_one_sided_less
        )
        # ``min(1.0, ...)`` is mathematically unreachable because
        # ``count_abs_ge ≤ n_perm`` by construction; retained as an
        # assertion-style backstop in case the input invariant ever
        # changes (e.g. someone weights the count).
        p_two_sided = (count_abs_ge + 1) / (n_perm + 1)

        result.p_value_one_sided = p_one_sided
        result.p_value_two_sided = p_two_sided
        result.p_value_greater = p_one_sided_greater
        result.p_value_less = p_one_sided_less
        result.alternative = "two-sided"
        # Default ``p_value`` tracks the two-sided test so a sign-flipped
        # strategy (e.g. short-only) is no longer silently dismissed.
        result.p_value = p_two_sided
        result.is_significant = p_two_sided < 0.05
        result.sharpe_distribution = perm_sharpes[:100]  # truncate for storage
    else:
        result.p_value = 1.0
        result.p_value_one_sided = 1.0
        result.p_value_two_sided = 1.0
        result.p_value_greater = 1.0
        result.p_value_less = 1.0
        result.is_significant = False
        result.errors.append(
            "Permutation test produced no valid Sharpe samples — p_value not meaningful"
        )

    # ── Bootstrap CI ─────────────────────────────────────────────────────────
    n = len(returns)
    boot_sharpes: List[float] = []
    for _ in range(n_bootstrap):
        sample = [boot_rng.choice(returns) for _ in range(n)]
        s = _sharpe_from_returns(sample)
        if s is not None:
            boot_sharpes.append(s)

    if boot_sharpes:
        boot_sharpes.sort()
        alpha = 1.0 - confidence_level
        # Use (n-1) as the scaling factor for percentile index computation so
        # that lo/hi indices correctly span [0, n-1].  Using n instead would
        # systematically over-shoot the upper bound by one slot.
        n_boot = len(boot_sharpes)
        lo_idx = max(0, int(math.floor((alpha / 2.0) * (n_boot - 1))))
        hi_idx = min(n_boot - 1, int(math.ceil((1.0 - alpha / 2.0) * (n_boot - 1))))
        result.sharpe_ci_lower = boot_sharpes[lo_idx]
        result.sharpe_ci_upper = boot_sharpes[hi_idx]

    # ── Deflated Sharpe Ratio (DSR) ────────────────────────────────────────────
    # Bailey & Lopez de Prado (2014) formula
    # DSR = Φ((SR_hat - SR_0) * sqrt(T) / sqrt(1 - skew*SR_hat + (kurt-1)/4 * SR_hat^2))
    try:
        T = float(n)
        mean_r = sum(returns) / n
        m2 = sum((r - mean_r) ** 2 for r in returns) / n
        m3 = sum((r - mean_r) ** 3 for r in returns) / n
        m4 = sum((r - mean_r) ** 4 for r in returns) / n

        # v1.1.0 fourth-pass: tighten subnormal guard (m2 > 1e-28 ↔
        # std_r > 1e-14, matching the project-wide floor in CLAUDE.md).
        std_r = math.sqrt(m2) if m2 > 1e-28 else None

        if std_r is not None and std_r > 1e-14:
            skew = m3 / (std_r ** 3)
            kurt = m4 / (std_r ** 4)  # raw kurtosis (Bailey & Lopez de Prado DSR uses (kurt-1)/4 which requires
            # raw kurtosis so that a normal distribution contributes (3-1)/4=0.5 > 0.
            # Using excess kurtosis here would make (kurt-1)/4 negative for platykurtic
            # distributions, collapsing the denominator and inverting the DSR.)

            # SR_0 benchmark: maximum expected Sharpe under null of multiple testing
            # Formula: SR_0 = ((1 - γ) * Z(1 - 1/n_trials) + γ * Z(1 - 1/(n_trials*e))) / sqrt(T)
            # where γ ≈ 0.5772 (Euler-Mascheroni constant) and Z is the inverse standard normal
            euler_gamma = 0.5772156649015328

            # Approximate inverse normal CDF via rational approximation
            # (Beasley-Springer-Moro / Abramowitz & Stegun 26.2.23).
            #
            # v1.1.0: the previous formulation evaluated the rational at
            # ``t = sqrt(-2 ln p_use)`` and returned ``sign * (t - num/den)``.
            # At p=0.5 (= ln 0.5 = -0.693, t ≈ 1.1774) the polynomial does
            # NOT cancel exactly — ``t - num/den`` evaluates to ≈ -1.5e-5
            # instead of 0.  As p crossed 0.5 the sign flipped, producing a
            # ~3e-5 discontinuity that mattered for DSR computations with
            # ``n_trials=2`` where ``sr_0`` already lives near zero.  We now
            # special-case p == 0.5 exactly (the analytic answer is 0) so
            # the function is continuous and matches the scipy fallback at
            # the median.
            def _inv_normal(p: float) -> float:
                if _HAS_SCIPY:
                    return float(_scipy_stats.norm.ppf(p))
                # Rational approximation (good for p in (0,1)).
                p = max(1e-12, min(1.0 - 1e-12, p))
                if abs(p - 0.5) < 1e-12:
                    # Analytic value at the median; avoids the tiny
                    # discontinuity in the rational approximation around
                    # t ≈ 1.1774 (sqrt(-2 ln 0.5)).
                    return 0.0
                if p < 0.5:
                    sign = -1.0
                    p_use = p
                else:
                    sign = 1.0
                    p_use = 1.0 - p
                t_v = math.sqrt(-2.0 * math.log(p_use))
                c0, c1, c2 = 2.515517, 0.802853, 0.010328
                d1, d2, d3 = 1.432788, 0.189269, 0.001308
                num = c0 + c1 * t_v + c2 * t_v ** 2
                den = 1.0 + d1 * t_v + d2 * t_v ** 2 + d3 * t_v ** 3
                return sign * (t_v - num / den)

            n_t = max(1, n_trials_tested)
            if n_t <= 1:
                # With a single trial there is no multiple-testing adjustment;
                # the benchmark Sharpe sr_0 is simply 0.
                sr_0 = 0.0
            else:
                e = math.e
                z1 = _inv_normal(1.0 - 1.0 / n_t)
                z2 = _inv_normal(1.0 - 1.0 / (n_t * e))
                sr_0 = ((1.0 - euler_gamma) * z1 + euler_gamma * z2) / math.sqrt(T)

            # DSR numerator / denominator
            sr_hat = observed_sharpe / math.sqrt(252.0)  # de-annualise to per-period
            denom_sq = 1.0 - skew * sr_hat + (kurt - 1.0) / 4.0 * sr_hat ** 2
            # v1.1.0 third-pass: previous floor of 1e-14 still admitted
            # numerically unstable denominators — when ``denom_sq`` was
            # e.g. 1.5e-14, ``sqrt`` produced ~1.2e-7 and ``dsr_z``
            # ballooned to 1e+8, saturating ``_normal_cdf`` at 1.0 and
            # reporting a false-positive DSR p-value of 0.  We now (a)
            # raise the floor to 1e-8 (matching the practical scale of
            # ``sr_hat`` × ``sqrt(T)``) and (b) clip the resulting
            # ``dsr_z`` to ``±10`` so even a borderline denom_sq cannot
            # produce a saturated probability — ``Φ(10) = 1 − 7.6e-24``
            # is already indistinguishable from saturation but
            # preserves a numerically sane score.
            if denom_sq > 1e-8:
                raw_dsr_z = (sr_hat - sr_0) * math.sqrt(T) / math.sqrt(denom_sq)
                # v1.1.0 fourth-pass: clamp lowered from ±10 to ±6.
                # ``Φ(10) ≈ 1 - 7.6e-24`` round-trips through json.dumps
                # as the float ``1.0`` exactly — operators couldn't
                # tell "genuinely huge dsr_z" from "borderline denom_sq
                # clip" because both produced the same visible score.
                # ``Φ(6) ≈ 1 - 9.9e-10`` is still saturated for any
                # practical purpose but stays finite-distinguishable
                # from 1.0 in JSON.  Additionally append a warning to
                # ``result.errors`` when the raw value WAS clipped so
                # consumers know the boundary was hit.
                if raw_dsr_z > 6.0 or raw_dsr_z < -6.0:
                    result.errors.append(
                        f"DSR z-score clipped from {raw_dsr_z:.2f} to ±6 "
                        "(denominator near numerical floor — score is saturated, "
                        "interpret with caution)"
                    )
                dsr_z = max(-6.0, min(6.0, raw_dsr_z))
                result.deflated_sharpe_ratio = _normal_cdf(dsr_z)
                result.dsr_p_value = 1.0 - result.deflated_sharpe_ratio
            else:
                result.errors.append(
                    "DSR denominator below numerical floor "
                    "(distribution too degenerate for a stable estimate)"
                )
        else:
            result.errors.append("Cannot compute DSR: zero standard deviation")
    except Exception as exc:
        result.errors.append(f"DSR computation failed: {exc}")

    return result


# ── Integration function ───────────────────────────────────────────────────────

def run_quant_analytics(
    run_dir: str,
    llm: Any = None,
    walk_forward: bool = True,
    significance_test: bool = True,
    wf_config: Optional[WalkForwardConfig] = None,
) -> Dict[str, Any]:
    """
    Orchestrate walk-forward validation and significance testing for run_dir.

    Parameters
    ----------
    run_dir : str
        Path to the run directory (must contain backtest_report.json or equity data).
    llm : Any
        Optional LLM handle (passed through to walk-forward; currently unused).
    walk_forward : bool
        Whether to run walk-forward validation.
    significance_test : bool
        Whether to run significance testing.
    wf_config : WalkForwardConfig, optional
        Custom walk-forward configuration.

    Returns
    -------
    Dict with keys: "walk_forward", "significance", "success".
    """
    output: Dict[str, Any] = {
        "walk_forward": None,
        "significance": None,
        "success": False,
        "errors": [],
        "warnings": [],
    }

    if not _is_quant_run(run_dir):
        msg = f"run_quant_analytics: skipping non-quant run_dir={run_dir}"
        logger.warning(msg)
        output["warnings"].append(msg)
        return output

    equity, _ = _load_equity_curve(run_dir)
    returns = _equity_to_returns(equity)

    # Walk-forward
    if walk_forward and len(equity) >= 10:
        try:
            wf_result = run_walk_forward(run_dir, config=wf_config, llm=llm)
            output["walk_forward"] = wf_result.to_dict()
        except Exception as exc:
            msg = f"Walk-forward failed: {exc}"
            logger.exception(msg)
            output["errors"].append(msg)
    elif walk_forward:
        output["warnings"].append(
            f"Walk-forward skipped: insufficient data ({len(equity)} equity points)"
        )

    # Significance test
    if significance_test and len(returns) >= 5:
        try:
            n_perm = _env_int("SIG_N_PERMUTATIONS", 1000)
            n_boot = _env_int("SIG_N_BOOTSTRAP", 1000)
            conf = _env_float("SIG_CONFIDENCE_LEVEL", 0.95)
            sig_result = run_significance_test(
                returns,
                n_permutations=n_perm,
                n_bootstrap=n_boot,
                confidence_level=conf,
            )
            output["significance"] = sig_result.to_dict()
        except Exception as exc:
            msg = f"Significance test failed: {exc}"
            logger.exception(msg)
            output["errors"].append(msg)
    elif significance_test:
        output["warnings"].append(
            f"Significance test skipped: insufficient returns ({len(returns)})"
        )

    output["success"] = not output["errors"]

    # Persist report (atomic write via tmp + os.replace)
    report_path = os.path.join(run_dir, "quant_analytics_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        output["report_path"] = report_path
        logger.info("Quant analytics report saved to %s", report_path)
    except Exception as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        output["errors"].append(f"Could not save report: {exc}")

    return output
