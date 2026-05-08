"""
features/signal_analyzer.py
=============================
Signal Decay Analysis for Quant mode runs.

Measures how predictive power (information coefficient) decays over multiple
holding horizons, estimates signal half-life, and identifies the effective
horizon beyond which the signal is no longer statistically significant.

All computation is pure Python (stdlib only).  scipy is used for t-CDF if
available.

Environment variables
---------------------
SIGNAL_HORIZONS            Comma-separated horizon list in days (default "1,2,3,5,10,20,40").
SIGNAL_MIN_OBSERVATIONS    Min data points required per horizon (default 30).
SIGNAL_SIGNIFICANCE_THRESH p-value threshold for significance (default 0.05).
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


def _env_list_int(name: str, default: List[int]) -> List[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return [int(v.strip()) for v in raw.split(",") if v.strip()]
    except ValueError:
        return default


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


# ── Optional scipy ────────────────────────────────────────────────────────────

try:
    from scipy import stats as _scipy_stats  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _regularised_incomplete_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    if x > (a + 1.0) / (a + b + 2.0):
        return 1.0 - _regularised_incomplete_beta(b, a, 1.0 - x)
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta) / a
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
                num = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
            else:
                num = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
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
                return front * f
    return front * f


def _t_pvalue_two_sided(t_val: float, df: float) -> float:
    if _HAS_SCIPY:
        return float(2.0 * _scipy_stats.t.sf(abs(t_val), df))
    if df < 1:
        return 1.0
    x = df / (df + t_val * t_val)
    ibeta = _regularised_incomplete_beta(df / 2.0, 0.5, x)
    return ibeta  # already = 2 * tail prob


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class SignalDecayConfig:
    horizons: List[int] = field(
        default_factory=lambda: _env_list_int("SIGNAL_HORIZONS", [1, 2, 3, 5, 10, 20, 40])
    )
    min_observations: int = field(
        default_factory=lambda: _env_int("SIGNAL_MIN_OBSERVATIONS", 30)
    )
    significance_threshold: float = field(
        default_factory=lambda: _env_float("SIGNAL_SIGNIFICANCE_THRESH", 0.05)
    )


@dataclass
class HorizonStats:
    horizon_days: int
    avg_return: Optional[float]             # net: long - short average
    t_stat: Optional[float]
    p_value: Optional[float]
    is_significant: bool
    avg_return_long: Optional[float]        # when signal == +1
    avg_return_short: Optional[float]       # when signal == -1
    hit_rate: Optional[float]              # fraction in correct direction

    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon_days": self.horizon_days,
            "avg_return": _sanitise_float(self.avg_return),
            "t_stat": _sanitise_float(self.t_stat),
            "p_value": _sanitise_float(self.p_value),
            "is_significant": self.is_significant,
            "avg_return_long": _sanitise_float(self.avg_return_long),
            "avg_return_short": _sanitise_float(self.avg_return_short),
            "hit_rate": _sanitise_float(self.hit_rate),
        }


@dataclass
class SignalDecayResult:
    horizon_stats: List[HorizonStats] = field(default_factory=list)
    signal_half_life_days: Optional[float] = None
    effective_horizon_days: Optional[int] = None  # last horizon with p < threshold
    decay_curve_text: str = ""
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "horizon_stats": [h.to_dict() for h in self.horizon_stats],
            "signal_half_life_days": _sanitise_float(self.signal_half_life_days),
            "effective_horizon_days": self.effective_horizon_days,
            "decay_curve_text": self.decay_curve_text,
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Core signal decay computation ──────────────────────────────────────────────

def _compute_forward_return(returns: List[float], start: int, horizon: int) -> Optional[float]:
    """Compute h-day compounded forward return starting at index start."""
    end = start + horizon
    if end > len(returns):
        return None
    total = 1.0
    for i in range(start, end):
        total *= (1.0 + returns[i])
    return total - 1.0


def _t_stat_from_sample(values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """One-sample t-test: H0: mean == 0. Returns (t_stat, p_value)."""
    n = len(values)
    if n < 2:
        return None, None
    mean_v = sum(values) / n
    var = sum((v - mean_v) ** 2 for v in values) / (n - 1)
    std = math.sqrt(var)
    # not (> 1e-14): catches both exact zero and IEEE 754 subnormals (> 0.0 but
    # < ~5e-324) that would produce an astronomically large t-statistic.
    if not (std > 1e-14):
        return None, None
    t = mean_v / (std / math.sqrt(n))
    p = _t_pvalue_two_sided(t, float(n - 1))
    return (
        t if math.isfinite(t) else None,
        p if math.isfinite(p) else None,
    )


def compute_signal_decay(
    signals: List[int],
    returns: List[float],
    timestamps: List[str],
    config: SignalDecayConfig,
) -> SignalDecayResult:
    """
    Compute signal decay statistics for each horizon.

    Parameters
    ----------
    signals : List[int]
        Signal series: +1 (long), -1 (short), 0 (flat). Same length as returns.
    returns : List[float]
        Period return series.
    timestamps : List[str]
        Timestamp strings aligned with returns.
    config : SignalDecayConfig
        Decay analysis configuration.

    Returns
    -------
    SignalDecayResult
    """
    result = SignalDecayResult()

    if not returns or not signals:
        result.errors.append("Empty returns or signals")
        return result

    n = len(returns)
    if len(signals) != n:
        if len(signals) < n:
            signals = list(signals) + [0] * (n - len(signals))
        else:
            signals = list(signals[:n])

    horizon_stats: List[HorizonStats] = []

    for h in sorted(config.horizons):
        # Collect forward returns for long and short signals
        long_fwd: List[float] = []
        short_fwd: List[float] = []
        net_fwd: List[float] = []
        hit_correct = 0
        hit_total = 0

        for i in range(n):
            sig = signals[i]
            if sig == 0:
                continue
            fwd = _compute_forward_return(returns, i, h)
            if fwd is None:
                continue

            if sig == 1:
                long_fwd.append(fwd)
                net_fwd.append(fwd)
                hit_total += 1
                if fwd > 0:
                    hit_correct += 1
            elif sig == -1:
                short_fwd.append(fwd)
                # For short signals, expected return is negative fwd
                net_fwd.append(-fwd)
                hit_total += 1
                if fwd < 0:
                    hit_correct += 1

        if len(net_fwd) < config.min_observations:
            hs = HorizonStats(
                horizon_days=h,
                avg_return=None,
                t_stat=None,
                p_value=None,
                is_significant=False,
                avg_return_long=None,
                avg_return_short=None,
                hit_rate=None,
            )
            horizon_stats.append(hs)
            continue

        avg_net = sum(net_fwd) / len(net_fwd)
        t_stat, p_val = _t_stat_from_sample(net_fwd)
        is_sig = p_val is not None and p_val < config.significance_threshold

        avg_long = (sum(long_fwd) / len(long_fwd)) if long_fwd else None
        avg_short = (sum(short_fwd) / len(short_fwd)) if short_fwd else None
        hit_rate = (hit_correct / hit_total) if hit_total > 0 else None

        hs = HorizonStats(
            horizon_days=h,
            avg_return=_sanitise_float(avg_net),
            t_stat=_sanitise_float(t_stat),
            p_value=_sanitise_float(p_val),
            is_significant=is_sig,
            avg_return_long=_sanitise_float(avg_long),
            avg_return_short=_sanitise_float(avg_short),
            hit_rate=_sanitise_float(hit_rate),
        )
        horizon_stats.append(hs)

    result.horizon_stats = horizon_stats

    # Effective horizon: last horizon with is_significant == True
    sig_horizons = [hs.horizon_days for hs in horizon_stats if hs.is_significant]
    if sig_horizons:
        result.effective_horizon_days = max(sig_horizons)

    # Signal half-life: fit exponential decay A * exp(-b * h) to avg_return vs h
    # Using least-squares on log scale: log(avg_ret) = log(A) - b*h
    fit_data: List[Tuple[int, float]] = []
    for hs in horizon_stats:
        # Use abs() so short-biased strategies (negative avg_return) are
        # not silently excluded from half-life fitting.  The log-scale regression
        # only requires the magnitude; we fit on |avg_return| for all strategies.
        if hs.avg_return is not None and abs(hs.avg_return) > 1e-12:
            fit_data.append((hs.horizon_days, abs(hs.avg_return)))

    if len(fit_data) >= 2:
        # Linear regression on (h, log(avg_ret))
        xs = [float(fd[0]) for fd in fit_data]
        ys = [math.log(fd[1]) for fd in fit_data]
        n_fit = len(xs)
        mx = sum(xs) / n_fit
        my = sum(ys) / n_fit
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n_fit))
        denom = sum((xi - mx) ** 2 for xi in xs)
        # ``> 0`` lets IEEE 754 subnormals through, producing a wildly
        # inflated slope ``b`` and a half-life that *passes* ``isfinite``
        # but is meaningless (e.g. 1e-310).  Use the project subnormal
        # threshold to reject any divisor close to zero.
        if denom > 1e-14:
            b = -num / denom  # negative slope ≡ decay rate
            if b > 1e-14:
                _half_life = math.log(2.0) / b
                # Very small b (near-zero decay) produces Inf half-life; only
                # assign finite values so the JSON report stays serialisable.
                if math.isfinite(_half_life):
                    result.signal_half_life_days = _half_life

    # ASCII decay curve
    result.decay_curve_text = _render_decay_curve(horizon_stats)

    return result


def _render_decay_curve(horizon_stats: List[HorizonStats]) -> str:
    """Render a simple ASCII chart of avg_return vs horizon."""
    valid = [(hs.horizon_days, hs.avg_return)
             for hs in horizon_stats if hs.avg_return is not None]
    if not valid:
        return "No data for decay curve."

    max_val = max(abs(v) for _, v in valid) or 1.0
    width = 50
    lines = ["Signal Decay Curve (avg net return per horizon)"]
    lines.append("Horizon | Return            | Sig")
    lines.append("--------|-------------------|----")

    for hs in horizon_stats:
        sig_marker = "* " if hs.is_significant else "  "
        if hs.avg_return is None:
            bar = " " * (width // 2) + "(insufficient data)"
        else:
            bar_len = int(abs(hs.avg_return) / max_val * (width // 2))
            bar_len = max(0, min(width // 2, bar_len))
            if hs.avg_return >= 0:
                bar = " " * (width // 2) + "+" * bar_len
            else:
                bar = " " * (width // 2 - bar_len) + "-" * bar_len + " " * (width // 2)

        ret_str = f"{hs.avg_return * 100:.2f}%" if hs.avg_return is not None else "N/A"
        lines.append(f"  {hs.horizon_days:4d}d  | {ret_str:>8} {bar[:20]} | {sig_marker}")

    return "\n".join(lines)


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_equity_and_signals(
    run_dir: str,
) -> Tuple[List[float], List[str], List[int]]:
    """
    Load (returns, timestamps, signals) from run_dir.

    Signal extraction order:
      1. backtest_report.json → trade_log entries
      2. stdout / stderr files in run_dir (scan for trade log lines)
      3. Fallback: derive signals from equity direction changes

    Returns (returns, timestamps, signals).
    """
    equity: List[float] = []
    timestamps: List[str] = []
    trade_signals_explicit: List[Dict[str, Any]] = []

    report_path = os.path.join(run_dir, "backtest_report.json")
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            eq = data.get("equity_curve", [])
            if isinstance(eq, list) and len(eq) >= 2:
                for item in eq:
                    if isinstance(item, (int, float)):
                        equity.append(float(item))
                        timestamps.append("")
                    elif isinstance(item, dict):
                        v = item.get("equity", item.get("value", item.get("close")))
                        t = item.get("timestamp", item.get("date", ""))
                        if v is not None:
                            try:
                                equity.append(float(v))
                                timestamps.append(str(t))
                            except (ValueError, TypeError):
                                pass

            # Check for trade_log
            trade_log = data.get("trade_log", [])
            if isinstance(trade_log, list):
                trade_signals_explicit = trade_log
        except (OSError, json.JSONDecodeError):
            pass

    # CSV fallback
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
                                        ts_val = row.get("date", row.get("Date", ""))
                                        timestamps.append(str(ts_val))
                                        break
                                    except (ValueError, TypeError):
                                        continue
                    if len(equity) >= 2:
                        break
                except (OSError, _csv_module.Error):
                    pass

    if len(equity) < 2:
        return [], [], []

    # Compute returns and aligned timestamps together so that zero-equity
    # bars are dropped from both lists consistently (avoids misalignment).
    returns: List[float] = []
    ts_aligned: List[str] = []
    _ts_src = timestamps[1:] if len(timestamps) == len(equity) else timestamps
    for i in range(1, len(equity)):
        # Guard non-positive and non-finite values: NaN == 0 is False so NaN
        # would pass an `== 0` check and produce NaN returns; negative equity
        # (leveraged blowup) produces mathematically valid but semantically
        # wrong signal IC values that corrupt all downstream statistics.
        if not math.isfinite(equity[i - 1]) or not math.isfinite(equity[i]) or equity[i - 1] <= 0:
            continue
        returns.append((equity[i] - equity[i - 1]) / equity[i - 1])
        ts_aligned.append(_ts_src[i - 1] if i - 1 < len(_ts_src) else "")

    # Derive signals if explicit trade log not available
    if trade_signals_explicit:
        # Map trade_log entries to bar indices by timestamp
        ts_map: Dict[str, int] = {ts: i for i, ts in enumerate(ts_aligned) if ts}
        sig_list = [0] * len(returns)
        for trade in trade_signals_explicit:
            ts = trade.get("timestamp", trade.get("date", ""))
            side = str(trade.get("side", trade.get("direction", ""))).lower()
            if ts in ts_map:
                idx = ts_map[ts]
                if side in ("buy", "long", "enter_long"):
                    sig_list[idx] = 1
                elif side in ("sell", "short", "enter_short"):
                    sig_list[idx] = -1
        signals = sig_list
    else:
        # Fallback: signal from return sign changes (momentum proxy)
        signals = [0] * len(returns)
        if returns:
            signals[0] = 1 if returns[0] > 0 else (-1 if returns[0] < 0 else 0)
        for i in range(1, len(returns)):
            if returns[i - 1] > 0:
                signals[i] = 1
            elif returns[i - 1] < 0:
                signals[i] = -1
            else:
                signals[i] = 0

    return returns, ts_aligned, signals


# ── Public runner ──────────────────────────────────────────────────────────────

def run_signal_analysis(
    run_dir: str,
    config: Optional[SignalDecayConfig] = None,
) -> SignalDecayResult:
    """
    Load trade data from run_dir, compute signal decay, save signal_decay_report.json.

    Parameters
    ----------
    run_dir : str
        Path to run directory.
    config : SignalDecayConfig, optional
        Signal decay configuration. Defaults to env-var driven SignalDecayConfig().

    Returns
    -------
    SignalDecayResult — with None metrics and a warning if no signal data found.
    """
    result = SignalDecayResult()

    if not _is_quant_run(run_dir):
        msg = f"run_signal_analysis: skipping non-quant run_dir={run_dir}"
        logger.warning(msg)
        result.warnings.append(msg)
        return result

    if config is None:
        config = SignalDecayConfig()

    returns, timestamps, signals = _load_equity_and_signals(run_dir)

    if not returns:
        msg = "No equity/return data found — signal analysis skipped"
        result.warnings.append(msg)
        logger.warning(msg)
        return result

    if all(s == 0 for s in signals):
        msg = "All signals are zero — no actionable signal data found"
        result.warnings.append(msg)
        logger.warning(msg)
        # Still save empty report
    else:
        logger.info(
            "Running signal decay analysis: %d bars, %d non-zero signals",
            len(returns),
            sum(1 for s in signals if s != 0),
        )
        result = compute_signal_decay(signals, returns, timestamps, config)

    report_path = os.path.join(run_dir, "signal_decay_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        result.report_path = report_path
        logger.info("Signal decay report saved to %s", report_path)
    except Exception as exc:
        result.errors.append(f"Could not save report: {exc}")

    return result
