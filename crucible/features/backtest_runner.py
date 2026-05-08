"""
features/backtest_runner.py
============================
Post-processing feature for **Quant mode** runs.

Responsibilities
----------------
1. **Auto-prepare backtest data** — fetch real historical OHLCV data from web
   APIs (yfinance for stocks/ETFs, ccxt/Binance public API for crypto).  Falls
   back to the project's own ``data_provider.py`` if present, then to synthetic
   GBM data as a last resort.
2. **Execute the backtest** in an isolated subprocess with a configurable
   timeout.
3. **Parse results** (Sharpe ratio, max drawdown, total return, win rate, trade
   count, etc.) and produce a structured JSON report.
4. **Parameter optimisation** — grid-search or random-search over a
   user-defined (or auto-detected) parameter space, rank by a target metric
   (default: Sharpe ratio), and report the best set.
5. **Closed-loop remediation** — if the backtest crashes or produces invalid
   data, optionally invoke the LLM codegen agent to fix the code, then re-run.

Data source resolution order
-----------------------------
1. Project already has data files (``code/data/*.csv`` etc.) → use them.
2. Project has ``data_provider.py`` → call it as subprocess to fetch data.
3. ``yfinance`` is installed → download real OHLCV from Yahoo Finance.
4. ``ccxt`` is installed *or* Binance public API available → download crypto
   OHLCV via REST.
5. Last resort → generate synthetic GBM data (marked as ``synthetic`` in the
   report).

Usage::

    from crucible.features.backtest_runner import run_backtest_pipeline
    report = run_backtest_pipeline(run_dir, llm=llm)

Or via the enhanced runner::

    python run_crucible_enhanced.py run --backtest-runner

Environment variables
---------------------
BACKTEST_TIMEOUT           Max seconds per backtest subprocess (default 120).
BACKTEST_SYMBOL            Ticker / symbol to download (default "SPY").
BACKTEST_DATA_SOURCE       Force data source: "yfinance", "binance", "project",
                           "synthetic", or "auto" (default "auto").
BACKTEST_PERIOD            Download period for yfinance (default "auto" — derived
                           from detected timeframe).  For Binance, converted to
                           approximate candle count via ``_period_to_candles()``.
BACKTEST_INTERVAL          Candle interval / granularity (default "auto" — auto-
                           detected from strategy code).  Valid values: "1m",
                           "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M".
BACKTEST_DATA_ROWS         Rows of synthetic OHLCV data if web fetch fails
                           (default 500).
BACKTEST_PARAM_SEARCH      Search strategy: "grid", "random", or "bayesian"
                           (default "grid").
BACKTEST_MAX_COMBOS        Max parameter combinations to evaluate (default 50).
BACKTEST_TARGET_METRIC     Metric to optimise for (default "sharpe_ratio").
BACKTEST_BAYESIAN_N_TRIALS Number of Optuna trials for Bayesian search (default 30).
BACKTEST_FIX_MAX_ROUNDS    Max LLM fix iterations if backtest fails (default 3).
BACKTEST_INITIAL_CAPITAL   Starting capital for synthetic runs (default 100000).
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import math
import os
import random
import subprocess
import threading
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import product as iter_product
from typing import Any, Dict, List, Optional, Set, Tuple

_UTC = timezone.utc

# Module-level RNG used for non-reproducible sampling (e.g. random parameter
# search).  Kept separate from the local RNG in generate_synthetic_ohlcv so
# that seeded synthetic data generation never shares state with param search.
_PARAM_RNG: random.Random = random.Random()
_PARAM_RNG_LOCK: threading.Lock = threading.Lock()


# ── Configuration ────────────────────────────────────────────────────────────


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


BACKTEST_TIMEOUT = _env_int("BACKTEST_TIMEOUT", 120)
BACKTEST_SYMBOL = _env_str("BACKTEST_SYMBOL", "SPY")
BACKTEST_DATA_SOURCE = _env_str("BACKTEST_DATA_SOURCE", "auto")
BACKTEST_PERIOD = _env_str("BACKTEST_PERIOD", "auto")
BACKTEST_INTERVAL = _env_str("BACKTEST_INTERVAL", "auto")
BACKTEST_DATA_ROWS = _env_int("BACKTEST_DATA_ROWS", 500)
BACKTEST_PARAM_SEARCH = _env_str("BACKTEST_PARAM_SEARCH", "grid")
BACKTEST_MAX_COMBOS = _env_int("BACKTEST_MAX_COMBOS", 50)
BACKTEST_TARGET_METRIC = _env_str("BACKTEST_TARGET_METRIC", "sharpe_ratio")
BACKTEST_BAYESIAN_N_TRIALS = _env_int("BACKTEST_BAYESIAN_N_TRIALS", 30)
BACKTEST_FIX_MAX_ROUNDS = _env_int("BACKTEST_FIX_MAX_ROUNDS", 3)
BACKTEST_INITIAL_CAPITAL = _env_float("BACKTEST_INITIAL_CAPITAL", 100_000.0)


# ── Timeframe profiles ──────────────────────────────────────────────────────
#
# Maps a canonical interval to its recommended (period, yf_interval, limit).
# - period: yfinance-style period string
# - yf_interval: yfinance ``interval`` parameter
# - binance_interval: Binance klines interval string
# - limit: default candle count for Binance / ccxt
# - synthetic_rows: rows for synthetic fallback data
# - is_intraday: True if sub-daily → timestamps include HH:MM:SS

_TIMEFRAME_PROFILES: Dict[str, Dict[str, Any]] = {
    "1m":  {"period": "7d",   "yf_interval": "1m",  "binance_interval": "1m",  "limit": 1440 * 7, "synthetic_rows": 5000,  "is_intraday": True},
    "3m":  {"period": "7d",   "yf_interval": "5m",  "binance_interval": "3m",  "limit": 480 * 7,  "synthetic_rows": 3000,  "is_intraday": True},
    "5m":  {"period": "30d",  "yf_interval": "5m",  "binance_interval": "5m",  "limit": 288 * 30, "synthetic_rows": 5000,  "is_intraday": True},
    "15m": {"period": "60d",  "yf_interval": "15m", "binance_interval": "15m", "limit": 96 * 60,  "synthetic_rows": 4000,  "is_intraday": True},
    "30m": {"period": "60d",  "yf_interval": "30m", "binance_interval": "30m", "limit": 48 * 60,  "synthetic_rows": 3000,  "is_intraday": True},
    "1h":  {"period": "6mo",  "yf_interval": "1h",  "binance_interval": "1h",  "limit": 24 * 180, "synthetic_rows": 4000,  "is_intraday": True},
    "2h":  {"period": "6mo",  "yf_interval": "1h",  "binance_interval": "2h",  "limit": 12 * 180, "synthetic_rows": 2000,  "is_intraday": True},
    "4h":  {"period": "1y",   "yf_interval": "1h",  "binance_interval": "4h",  "limit": 6 * 365,  "synthetic_rows": 2000,  "is_intraday": True},
    "6h":  {"period": "1y",   "yf_interval": "1h",  "binance_interval": "6h",  "limit": 4 * 365,  "synthetic_rows": 1500,  "is_intraday": True},
    "8h":  {"period": "1y",   "yf_interval": "1h",  "binance_interval": "8h",  "limit": 3 * 365,  "synthetic_rows": 1000,  "is_intraday": True},
    "12h": {"period": "1y",   "yf_interval": "1h",  "binance_interval": "12h", "limit": 2 * 365,  "synthetic_rows": 730,   "is_intraday": True},
    "1d":  {"period": "2y",   "yf_interval": "1d",  "binance_interval": "1d",  "limit": 730,      "synthetic_rows": 500,   "is_intraday": False},
    "3d":  {"period": "5y",   "yf_interval": "1d",  "binance_interval": "3d",  "limit": 600,      "synthetic_rows": 600,   "is_intraday": False},
    "1w":  {"period": "5y",   "yf_interval": "1wk", "binance_interval": "1w",  "limit": 260,      "synthetic_rows": 260,   "is_intraday": False},
    "1M":  {"period": "10y",  "yf_interval": "1mo", "binance_interval": "1M",  "limit": 120,      "synthetic_rows": 120,   "is_intraday": False},
}

# Default profile when detection fails or interval is "auto" with no match
_DEFAULT_PROFILE: Dict[str, Any] = _TIMEFRAME_PROFILES["1d"]


def resolve_timeframe_profile(
    interval: str,
    period: str = "auto",
) -> Dict[str, Any]:
    """
    Resolve a canonical interval + optional period override into a full profile.

    Parameters
    ----------
    interval : str
        Canonical interval key (e.g. "1h", "5m", "1d") or "auto".
        When "auto", returns the default "1d" profile.
    period : str
        If not "auto", overrides the profile's default period.

    Returns
    -------
    dict
        Profile dict with keys: period, yf_interval, binance_interval, limit,
        synthetic_rows, is_intraday.
    """
    key = interval.strip()
    # Preserve "1M" (monthly) case — lowercase everything else
    if key != "1M":
        key = key.lower()
    # Normalise common aliases
    alias_map = {
        "daily": "1d", "day": "1d",
        "hourly": "1h", "hour": "1h",
        "weekly": "1w", "week": "1w",
        "monthly": "1M", "month": "1M",
        "minute": "1m", "min": "1m",
        "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
        "60m": "1h", "60min": "1h",
        "240m": "4h", "240min": "4h",
        "1wk": "1w", "1mo": "1M",
    }
    key = alias_map.get(key, key)

    profile = dict(_TIMEFRAME_PROFILES.get(key, _DEFAULT_PROFILE))

    # Allow user to override period while keeping interval-specific defaults
    if period != "auto":
        profile["period"] = period

    return profile


# ── Data models ──────────────────────────────────────────────────────────────


@dataclass
class BacktestMetrics:
    """Parsed metrics from a single backtest execution."""

    sharpe_ratio: Optional[float] = None
    total_return_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    win_rate: Optional[float] = None
    trade_count: Optional[int] = None
    profit_factor: Optional[float] = None
    annualised_volatility: Optional[float] = None
    calmar_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None
    alpha: Optional[float] = None
    beta: Optional[float] = None

    # Raw output capture
    raw_stdout: str = ""
    raw_stderr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        for k in (
            "sharpe_ratio", "total_return_pct", "max_drawdown_pct",
            "win_rate", "trade_count", "profit_factor",
            "annualised_volatility", "calmar_ratio", "sortino_ratio",
            "alpha", "beta",
        ):
            v = getattr(self, k, None)
            if v is None:
                continue
            # Exclude NaN / Inf which are not valid JSON values
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                continue
            d[k] = v
        return d

    def metric_value(self, name: str) -> Optional[float]:
        """Return the value of a named metric, or None.

        Recognises common aliases so that callers using the shorthand name
        (e.g. ``"max_drawdown"``) work the same as the canonical field name
        (``"max_drawdown_pct"``).  This mirrors the JSON-parsing alias table
        in ``_parse_backtest_output`` at line ~1693.
        """
        # Keep this in sync with _fill_metrics_from_dict() so that any
        # shorthand metric name accepted at parse-time also works here.
        _ALIASES: Dict[str, str] = {
            # drawdown
            "max_drawdown":          "max_drawdown_pct",
            "drawdown":              "max_drawdown_pct",
            # return
            "return":                "total_return_pct",
            "total_return":          "total_return_pct",
            "return_pct":            "total_return_pct",
            "returns":               "total_return_pct",
            # sharpe
            "sharpe":                "sharpe_ratio",
            "sr":                    "sharpe_ratio",
            # win rate
            "winrate":               "win_rate",
            "win_pct":               "win_rate",
            # profit factor
            "pf":                    "profit_factor",
            # volatility (note: field name uses British spelling)
            "annualized_volatility": "annualised_volatility",
            "volatility":            "annualised_volatility",
            "vol":                   "annualised_volatility",
            # calmar / sortino shorthands
            "calmar":                "calmar_ratio",
            "sortino":               "sortino_ratio",
            # trade count shorthands (keep in sync with _fill_metrics_from_dict)
            "trades":                "trade_count",
            "num_trades":            "trade_count",
            "total_trades":          "trade_count",
        }
        resolved = _ALIASES.get(name, name)
        v = getattr(self, resolved, None)
        if v is None:
            return None
        try:
            result = float(v)
            # Treat NaN/Inf as missing — they break sort comparisons and
            # are already filtered in to_dict(); keep behaviour consistent.
            if math.isnan(result) or math.isinf(result):
                return None
            return result
        except (TypeError, ValueError):
            return None


@dataclass
class ParameterCombo:
    """One parameter combination and its backtest result."""

    params: Dict[str, Any]
    metrics: Optional[BacktestMetrics] = None
    error: Optional[str] = None
    success: bool = False

    def to_dict(self) -> Dict[str, Any]:
        # Sanitise params: NaN/Inf floats are not valid JSON and would cause
        # json.dump() to raise ValueError, silently dropping the entire report.
        sanitised_params: Dict[str, Any] = {
            k: (None if (isinstance(v, float) and not math.isfinite(v)) else v)
            for k, v in self.params.items()
        }
        d: Dict[str, Any] = {"params": sanitised_params, "success": self.success}
        if self.metrics:
            d["metrics"] = self.metrics.to_dict()
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class BacktestReport:
    """Full report produced by the backtest runner pipeline."""

    success: bool = False
    run_dir: str = ""
    data_source: str = ""           # "existing" | "yfinance" | "binance" | "project_provider" | "synthetic"
    data_symbol: str = ""           # ticker/symbol used for download
    data_interval: str = ""         # candle interval used (e.g. "1d", "1h", "5m")
    data_rows: int = 0
    baseline_metrics: Optional[BacktestMetrics] = None
    parameter_search: str = "none"  # "grid" | "random" | "none"
    combos_evaluated: int = 0
    best_params: Optional[Dict[str, Any]] = None
    best_metrics: Optional[BacktestMetrics] = None
    all_combos: List[ParameterCombo] = field(default_factory=list)
    fix_rounds_used: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    _data_file: Optional[str] = field(default=None, repr=False)
    # Stores the runtime target_metric so that report generation uses the same
    # metric that was actually optimised, not the module-level default.
    target_metric: str = BACKTEST_TARGET_METRIC

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "success": self.success,
            "data_source": self.data_source,
            "data_symbol": self.data_symbol,
            "data_interval": self.data_interval,
            "data_rows": self.data_rows,
            "parameter_search": self.parameter_search,
            "combos_evaluated": self.combos_evaluated,
            "fix_rounds_used": self.fix_rounds_used,
            # Persist the runtime target_metric so that deserialized reports
            # continue to use the same metric that was actually optimised.
            "target_metric": self.target_metric,
        }
        if self.baseline_metrics:
            d["baseline_metrics"] = self.baseline_metrics.to_dict()
        if self.best_params:
            # Sanitise NaN/Inf — consistent with ParameterCombo.to_dict().
            d["best_params"] = {
                k: (None if (isinstance(v, float) and not math.isfinite(v)) else v)
                for k, v in self.best_params.items()
            }
        if self.best_metrics:
            d["best_metrics"] = self.best_metrics.to_dict()
        if self.all_combos:
            d["all_combos"] = [c.to_dict() for c in self.all_combos]
        if self.errors:
            d["errors"] = self.errors
        if self.warnings:
            d["warnings"] = self.warnings
        return d

    def summary_text(self) -> str:
        lines = ["═══ Backtest Runner Report ═══"]
        lines.append(f"Status: {'SUCCESS' if self.success else 'FAILED'}")
        symbol_info = f" [{self.data_symbol}]" if self.data_symbol else ""
        interval_info = f" @{self.data_interval}" if self.data_interval else ""
        lines.append(f"Data source: {self.data_source}{symbol_info}{interval_info} ({self.data_rows} rows)")
        if self.baseline_metrics:
            bm = self.baseline_metrics
            lines.append("── Baseline metrics ──")
            if bm.sharpe_ratio is not None and math.isfinite(bm.sharpe_ratio):
                lines.append(f"  Sharpe Ratio:    {bm.sharpe_ratio:.4f}")
            if bm.total_return_pct is not None and math.isfinite(bm.total_return_pct):
                lines.append(f"  Total Return:    {bm.total_return_pct:.2f}%")
            if bm.max_drawdown_pct is not None and math.isfinite(bm.max_drawdown_pct):
                lines.append(f"  Max Drawdown:    {bm.max_drawdown_pct:.2f}%")
            if bm.win_rate is not None and math.isfinite(bm.win_rate):
                lines.append(f"  Win Rate:        {bm.win_rate * 100:.2f}%")
            if bm.trade_count is not None:
                lines.append(f"  Trade Count:     {bm.trade_count}")
        if self.combos_evaluated > 0:
            lines.append(f"── Parameter search ({self.parameter_search}) ──")
            lines.append(f"  Combinations:    {self.combos_evaluated}")
            if self.best_params:
                _safe_bp = {
                    k: (None if (isinstance(v, float) and not math.isfinite(v)) else v)
                    for k, v in self.best_params.items()
                }
                lines.append(f"  Best params:     {_safe_bp}")
            if self.best_metrics:
                mv = self.best_metrics.metric_value(self.target_metric)
                if mv is not None:
                    lines.append(f"  Best {self.target_metric}: {mv:.4f}")
        if self.fix_rounds_used > 0:
            lines.append(f"Fix rounds used: {self.fix_rounds_used}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        for e in self.errors:
            lines.append(f"  ✗ {e}")
        return "\n".join(lines)


# ── Strategy comparison ───────────────────────────────────────────────────────

_COMPARISON_METRICS: List[str] = [
    "sharpe_ratio",
    "total_return_pct",
    "max_drawdown_pct",
    "win_rate",
    "trade_count",
    "profit_factor",
    "annualised_volatility",
    "calmar_ratio",
    "sortino_ratio",
]

# Metrics where a *lower* value is better (all others: higher = better).
# Used by best-combo selection, Optuna study direction, and leaderboard ranking.
_LOWER_IS_BETTER: Set[str] = {"max_drawdown", "max_drawdown_pct", "annualised_volatility"}


@dataclass
class BacktestComparison:
    """
    Side-by-side comparison of multiple :class:`BacktestReport` objects.

    Each entry in *reports* corresponds to one strategy variant.
    *labels* provides a human-readable name for each report (auto-generated
    from ``data_symbol`` / index if omitted).

    ``metric_table`` maps metric name → {label: value} for every metric
    present in at least one report.  ``best_by_metric`` maps each metric to
    the label of the best-performing strategy for that metric.
    """

    reports: List[BacktestReport] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    metric_table: Dict[str, Dict[str, Optional[float]]] = field(default_factory=dict)
    best_by_metric: Dict[str, str] = field(default_factory=dict)

    def summary_text(self) -> str:
        """Return a formatted comparison table as a multi-line string.

        Layout::

            ═══ Backtest Strategy Comparison ═══
                                          BTC           ETH
                                          ------------  ------------
              sharpe ratio               *1.2345        0.8765
              total return pct            0.3456       *0.9876
        """
        if not self.reports or not self.labels:
            return "BacktestComparison: no reports or labels to compare."
        lines = ["═══ Backtest Strategy Comparison ═══"]

        # ── Column widths ──────────────────────────────────────────────────
        # Value columns: wide enough for any label plus a 2-char margin.
        col_w = max(12, max(len(lbl) for lbl in self.labels) + 2)

        # Metric-name column: wide enough for all metric display names; the
        # data rows use this as the left-side label field so the value
        # columns must be offset by the same amount in the header and
        # separator rows.
        metric_label_w = max(
            28,
            max(
                (len(m.replace("_", " ")) for m in self.metric_table),
                default=28,
            ),
        )
        # Prefix that aligns the header/separator with the value columns.
        row_prefix = "  " + " " * metric_label_w + "  "

        # ── Header ─────────────────────────────────────────────────────────
        label_row = row_prefix + "  ".join(lbl.ljust(col_w) for lbl in self.labels)
        lines.append(label_row)
        separator = row_prefix + "-" * (
            col_w * len(self.labels) + 2 * (len(self.labels) - 1)
        )
        lines.append(separator)

        # ── Data rows ──────────────────────────────────────────────────────
        for metric, label_vals in self.metric_table.items():
            best_label = self.best_by_metric.get(metric)
            row_parts = []
            for lbl in self.labels:
                val = label_vals.get(lbl)
                if val is None:
                    cell = "N/A"
                elif isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    cell = "N/A"
                elif metric in ("win_rate",):
                    cell = f"{val * 100:.2f}%"
                elif metric in ("trade_count",):
                    cell = str(int(val))
                else:
                    cell = f"{val:.4f}"
                # Use ASCII "*" not fullwidth "★": Python's ljust() counts
                # characters, not display columns, so fullwidth chars shift
                # alignment by one column in monospace terminal output.
                marker = "*" if lbl == best_label else " "
                row_parts.append(f"{marker}{cell}".ljust(col_w))
            lines.append(
                f"  {metric.replace('_', ' '):{metric_label_w}s}  "
                + "  ".join(row_parts)
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "labels": self.labels,
            "metric_table": {
                metric: {lbl: val for lbl, val in label_vals.items()}
                for metric, label_vals in self.metric_table.items()
            },
            "best_by_metric": self.best_by_metric,
        }


def compare_backtest_reports(
    reports: List[BacktestReport],
    *,
    labels: Optional[List[str]] = None,
    metrics: Optional[List[str]] = None,
) -> BacktestComparison:
    """
    Build a :class:`BacktestComparison` from a list of :class:`BacktestReport`.

    Parameters
    ----------
    reports:
        One or more completed backtest reports.
    labels:
        Optional display names.  Must match *len(reports)* if provided.
        Auto-generated from ``data_symbol`` (or sequential index) when omitted.
    metrics:
        Subset of metric names to include.  Defaults to all metrics in
        ``_COMPARISON_METRICS``.

    Returns
    -------
    BacktestComparison
        Populated comparison object including ``metric_table`` and
        ``best_by_metric``.
    """
    if not reports:
        return BacktestComparison()

    # Resolve labels
    if labels is None:
        labels = []
        for i, r in enumerate(reports):
            if r.data_symbol:
                lbl = r.data_symbol
            else:
                lbl = f"Strategy-{i + 1}"
            # Deduplicate: increment suffix counter until the label is unique
            if lbl in labels:
                suffix = 1
                while f"{lbl}_{suffix}" in labels:
                    suffix += 1
                lbl = f"{lbl}_{suffix}"
            labels.append(lbl)
    else:
        if len(labels) != len(reports):
            raise ValueError(
                f"compare_backtest_reports: labels length ({len(labels)}) "
                f"must match reports length ({len(reports)})"
            )
        # Duplicate labels cause silent data corruption: metric_table rows are
        # keyed by label, so two reports sharing a label result in the first
        # report's values being overwritten by the second.  Detect and reject
        # early so callers get an explicit error instead of wrong data.
        if len(set(labels)) != len(labels):
            # Count occurrences without external imports; collect only the
            # unique names that appear more than once so the error message
            # lists each duplicate once, even when it appears 3+ times.
            _lbl_counts: Dict[str, int] = {}
            for _l in labels:
                _lbl_counts[_l] = _lbl_counts.get(_l, 0) + 1
            dupes = sorted(_l for _l, cnt in _lbl_counts.items() if cnt > 1)
            raise ValueError(
                f"compare_backtest_reports: labels must be unique — "
                f"duplicate labels found: {dupes}"
            )
        # Defensive copy so that the caller mutating their list after calling
        # this function does not corrupt BacktestComparison.labels.
        labels = list(labels)

    # Use `is not None` so that an explicitly-passed empty list [] is honoured
    # and not silently replaced by the full default set.
    active_metrics = list(metrics) if metrics is not None else list(_COMPARISON_METRICS)

    # Build metric_table: metric → {label: value}
    metric_table: Dict[str, Dict[str, Optional[float]]] = {}
    for metric in active_metrics:
        row: Dict[str, Optional[float]] = {}
        for lbl, report in zip(labels, reports):
            # Prefer best_metrics (post-optimisation) over baseline_metrics
            metrics_obj = report.best_metrics or report.baseline_metrics
            if metrics_obj is not None:
                val = metrics_obj.metric_value(metric)
            else:
                val = None
            row[lbl] = val
        # Only include metrics where at least one report has data
        if any(v is not None for v in row.values()):
            metric_table[metric] = row

    # Compute best_by_metric
    best_by_metric: Dict[str, str] = {}
    for metric, row in metric_table.items():
        lower_better = metric in _LOWER_IS_BETTER
        best_lbl: Optional[str] = None
        best_val: Optional[float] = None
        for lbl, val in row.items():
            if val is None:
                continue
            if math.isnan(val) or math.isinf(val):
                continue
            if best_val is None:
                best_val = val
                best_lbl = lbl
            elif lower_better and val < best_val:
                best_val = val
                best_lbl = lbl
            elif not lower_better and val > best_val:
                best_val = val
                best_lbl = lbl
        if best_lbl is not None:
            best_by_metric[metric] = best_lbl

    return BacktestComparison(
        reports=reports,
        labels=labels,
        metric_table=metric_table,
        best_by_metric=best_by_metric,
    )


# ── Synthetic data generator ─────────────────────────────────────────────────


def _interval_to_timedelta(interval: str) -> timedelta:
    """Convert a candle interval string to a timedelta for synthetic data."""
    raw = interval.strip()
    # Preserve "1M" (monthly) case distinction from "1m" (minute)
    key = raw if raw == "1M" else raw.lower()
    _map = {
        "1m": timedelta(minutes=1), "3m": timedelta(minutes=3),
        "5m": timedelta(minutes=5), "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30), "1h": timedelta(hours=1),
        "2h": timedelta(hours=2), "4h": timedelta(hours=4),
        "6h": timedelta(hours=6), "8h": timedelta(hours=8),
        "12h": timedelta(hours=12), "1d": timedelta(days=1),
        "3d": timedelta(days=3), "1w": timedelta(weeks=1),
        "1M": timedelta(days=30),
    }
    return _map.get(key, timedelta(days=1))


def _is_intraday_interval(interval: str) -> bool:
    """Return True if the interval is sub-daily."""
    key = interval.strip()
    # Preserve "1M" (monthly) — don't lowercase it
    lookup_key = key if key == "1M" else key.lower()
    profile = _TIMEFRAME_PROFILES.get(lookup_key, None)
    if profile is not None:
        return profile["is_intraday"]
    # Fallback heuristic: "m" (minute) or "h" (hour) suffix but NOT "1M" (month)
    lower = key.lower()
    if lower.endswith("h"):
        return True
    if lower.endswith("m") and key != "1M":
        return True
    return False


def generate_synthetic_ohlcv(
    rows: int = 500,
    *,
    start_date: Optional[str] = None,
    initial_price: float = 100.0,
    volatility: float = 0.02,
    drift: float = 0.0001,
    seed: Optional[int] = None,
    interval: str = "1d",
) -> str:
    """
    Generate synthetic OHLCV CSV data with realistic price dynamics.

    Uses geometric Brownian motion with mean-reverting volume.  The output is a
    standard CSV with columns: date,open,high,low,close,volume.

    Parameters
    ----------
    interval : str
        Candle interval (e.g. "1d", "1h", "5m").  Sub-daily intervals produce
        datetime timestamps (``%Y-%m-%d %H:%M:%S``) instead of date-only.

    Returns the CSV text as a string.
    """
    # Use a local RNG instance to avoid mutating global random state.
    rng = random.Random(seed)

    if start_date:
        try:
            dt = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            dt = datetime(2020, 1, 1)
    else:
        dt = datetime(2020, 1, 1)

    step = _interval_to_timedelta(interval)
    intraday = _is_intraday_interval(interval)
    date_fmt = "%Y-%m-%d %H:%M:%S" if intraday else "%Y-%m-%d"

    # Scale volatility/drift to the interval duration (vs. daily baseline)
    interval_hours = max(step.total_seconds() / 3600, 1 / 60)
    daily_hours = 24.0
    scale = math.sqrt(interval_hours / daily_hours)
    scaled_vol = volatility * scale
    scaled_drift = drift * (interval_hours / daily_hours)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "open", "high", "low", "close", "volume"])

    price = initial_price
    base_volume = 1_000_000

    for _ in range(rows):
        # Skip weekends for daily+ intervals (stocks); crypto trades 24/7
        if not intraday:
            while dt.weekday() >= 5:
                dt += timedelta(days=1)

        open_price = price
        # Geometric Brownian motion (scaled to interval)
        ret = scaled_drift + scaled_vol * rng.gauss(0, 1)
        close_price = open_price * math.exp(ret)

        # Intraday high/low
        intraday_range = abs(close_price - open_price) + open_price * scaled_vol * abs(rng.gauss(0, 0.5))
        high_price = max(open_price, close_price) + intraday_range * rng.uniform(0.1, 0.5)
        low_price = min(open_price, close_price) - intraday_range * rng.uniform(0.1, 0.5)
        low_price = max(low_price, 0.01)  # Prevent negative prices

        # Volume with mean reversion (scale down for smaller intervals)
        vol_scale = interval_hours / daily_hours
        vol_noise = rng.gauss(0, 0.3)
        volume = int(base_volume * vol_scale * math.exp(vol_noise))
        volume = max(volume, 100)

        writer.writerow([
            dt.strftime(date_fmt),
            f"{open_price:.4f}",
            f"{high_price:.4f}",
            f"{low_price:.4f}",
            f"{close_price:.4f}",
            str(volume),
        ])

        price = close_price
        dt += step

    return buf.getvalue()


# ── Real data fetching ────────────────────────────────────────────────────────


def _yfinance_available() -> bool:
    """Check whether the ``yfinance`` package is importable."""
    try:
        import yfinance  # noqa: F401
        return True
    except ImportError:
        return False


def _ccxt_available() -> bool:
    """
    Check whether the ``ccxt`` package is installed without importing it.

    Some Windows Python environments can crash or emit fatal native-extension
    teardown errors when ``ccxt`` eagerly imports optional numeric backends in
    the main process. Probe via importlib metadata only.
    """
    return importlib.util.find_spec("ccxt") is not None


def fetch_yfinance_ohlcv(
    symbol: str = "SPY",
    period: str = "2y",
    interval: str = "1d",
) -> Optional[str]:
    """
    Download historical OHLCV data from Yahoo Finance via ``yfinance``.

    For intraday intervals, timestamps include HH:MM:SS.

    Returns CSV text on success, None on failure.
    """
    if not _yfinance_available():
        import warnings
        warnings.warn(
            "yfinance is not installed; falling back to synthetic data. "
            "Install with: pip install yfinance",
            ImportWarning,
            stacklevel=2,
        )
        return None
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df is None or df.empty:
            return None

        # Normalise column names to lowercase
        df = df.reset_index()
        rename_map = {}
        for col in df.columns:
            lower = col.lower().strip()
            if lower in ("date", "datetime"):
                rename_map[col] = "date"
            elif lower == "open":
                rename_map[col] = "open"
            elif lower == "high":
                rename_map[col] = "high"
            elif lower == "low":
                rename_map[col] = "low"
            elif lower == "close":
                rename_map[col] = "close"
            elif lower == "volume":
                rename_map[col] = "volume"
        df = df.rename(columns=rename_map)

        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(set(df.columns)):
            return None

        df = df[["date", "open", "high", "low", "close", "volume"]]
        # Convert date to string — include time for intraday
        intraday = _is_intraday_interval(interval)
        if intraday:
            df["date"] = df["date"].astype(str).str[:19]
        else:
            df["date"] = df["date"].astype(str).str[:10]
        # Drop rows with NaN
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        if df.empty:
            return None

        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return buf.getvalue()
    except Exception:
        return None


def fetch_binance_ohlcv(
    symbol: str = "BTCUSDT",
    interval: str = "1d",
    limit: int = 500,
) -> Optional[str]:
    """
    Download historical OHLCV from the Binance public REST API.

    No API key required — uses the public /api/v3/klines endpoint.
    Falls back to ``ccxt`` if installed.

    Returns CSV text on success, None on failure.
    """
    # Strategy 1: direct REST call (no dependencies)
    csv_text = _fetch_binance_rest(symbol, interval, limit)
    if csv_text:
        return csv_text

    # Strategy 2: ccxt library
    if _ccxt_available():
        return _fetch_ccxt_ohlcv(symbol, interval, limit)

    return None


def _fetch_binance_rest(
    symbol: str,
    interval: str = "1d",
    limit: int = 500,
) -> Optional[str]:
    """Fetch from Binance public klines endpoint using only stdlib."""
    import urllib.error
    import urllib.request

    # Sanitise symbol for Binance (uppercase, no slash)
    clean_symbol = symbol.upper().replace("/", "").replace("-", "")
    # Binance caps at 1000 per request
    effective_limit = min(limit, 1000)
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={clean_symbol}&interval={interval}&limit={effective_limit}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Crucible-BacktestRunner/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None

    if not isinstance(data, list) or len(data) == 0:
        return None

    intraday = _is_intraday_interval(interval)
    date_fmt = "%Y-%m-%d %H:%M:%S" if intraday else "%Y-%m-%d"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "open", "high", "low", "close", "volume"])

    for candle in data:
        # Binance klines: [open_time, open, high, low, close, volume, ...]
        if not isinstance(candle, list) or len(candle) < 6:
            continue
        try:
            ts_ms = int(candle[0])
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=_UTC)
            writer.writerow([
                dt.strftime(date_fmt),
                candle[1],  # open
                candle[2],  # high
                candle[3],  # low
                candle[4],  # close
                candle[5],  # volume
            ])
        except (ValueError, TypeError, OSError):
            continue

    result = buf.getvalue()
    # Validate we got at least some rows
    if result.count("\n") < 3:
        return None
    return result


def _fetch_ccxt_ohlcv(
    symbol: str,
    interval: str = "1d",
    limit: int = 500,
) -> Optional[str]:
    """
    Fetch OHLCV via the ``ccxt`` library (Binance exchange).

    This runs in a subprocess so optional native dependencies loaded by ccxt do
    not destabilise the main process on locked-down Windows environments.
    """
    if not _ccxt_available():
        import warnings
        warnings.warn(
            "ccxt is not installed; falling back to Binance public API or synthetic data. "
            "Install with: pip install ccxt",
            ImportWarning,
            stacklevel=2,
        )
        return None
    try:
        script = textwrap.dedent(
            """
            import csv
            import io
            import sys
            from datetime import datetime, timezone

            import ccxt

            symbol = sys.argv[1]
            interval = sys.argv[2]
            limit = int(sys.argv[3])
            utc = timezone.utc

            exchange = ccxt.binance({"enableRateLimit": True})
            ccxt_symbol = symbol.upper()
            if "/" not in ccxt_symbol and len(ccxt_symbol) > 3:
                for quote in ("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB"):
                    if ccxt_symbol.endswith(quote):
                        base = ccxt_symbol[: -len(quote)]
                        ccxt_symbol = f"{base}/{quote}"
                        break

            tf_map = {
                "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
                "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h",
                "12h": "12h", "1d": "1d", "3d": "3d", "1w": "1w", "1M": "1M",
            }
            timeframe = tf_map.get(interval, "1d")
            intraday = interval.endswith("m") or interval.endswith("h")
            date_fmt = "%Y-%m-%d %H:%M:%S" if intraday else "%Y-%m-%d"

            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe=timeframe, limit=limit)
            if not ohlcv:
                raise SystemExit(2)

            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\\n")
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for candle in ohlcv:
                dt = datetime.fromtimestamp(candle[0] / 1000, tz=utc)
                writer.writerow([
                    dt.strftime(date_fmt),
                    candle[1],
                    candle[2],
                    candle[3],
                    candle[4],
                    candle[5],
                ])
            sys.stdout.write(buf.getvalue())
            """
        ).strip()
        result = subprocess.run(
            [sys.executable, "-c", script, symbol, interval, str(limit)],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if result.returncode != 0:
            return None
        csv_text = result.stdout.strip()
        if not csv_text:
            return None
        return csv_text + ("\n" if not csv_text.endswith("\n") else "")
    except Exception:
        return None


def _run_project_data_provider(code_dir: str, timeout: int = 60) -> Optional[str]:
    """
    Call the project's own ``data_provider.py`` to fetch/prepare data.

    The data_provider is expected to write a CSV file to ``data/`` and print
    the output path to stdout.  Returns the path to the data file on success,
    None on failure.
    """
    provider_path = os.path.join(code_dir, "data_provider.py")
    if not os.path.isfile(provider_path):
        return None

    try:
        result = subprocess.run(
            [sys.executable, provider_path],
            cwd=code_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_make_safe_env(code_dir),
        )
        if result.returncode != 0:
            return None

        # Check if data was created
        data_dir = os.path.join(code_dir, "data")
        if os.path.isdir(data_dir):
            for f in os.listdir(data_dir):
                if f.lower().endswith(".csv"):
                    return os.path.join(data_dir, f)

        # Also check stdout for file path
        stdout_path = result.stdout.strip()
        if stdout_path and os.path.isfile(stdout_path):
            return stdout_path

        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _detect_symbol_from_code(code_dir: str) -> Optional[str]:
    """
    Attempt to detect the trading symbol from the project's source code.

    Scans for patterns like:
      - SYMBOL = "BTCUSDT"
      - ticker = "AAPL"
      - pair = "ETH/USDT"
    """
    import re

    symbol_re = re.compile(
        r"""(?:SYMBOL|TICKER|PAIR|ASSET|INSTRUMENT)\s*=\s*['"]([\w/.-]+)['"]""",
        re.IGNORECASE,
    )
    try:
        for f in os.listdir(code_dir):
            if not f.endswith(".py"):
                continue
            fpath = os.path.join(code_dir, f)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read(5000)
                m = symbol_re.search(content)
                if m:
                    return m.group(1)
            except OSError:
                continue
    except OSError:
        pass
    return None


def _detect_timeframe_from_code(code_dir: str) -> Optional[str]:
    """
    Attempt to detect the candle interval / timeframe from the project's source code.

    Scans for patterns like:
      - TIMEFRAME = "1h"
      - INTERVAL = "5m"
      - CANDLE_INTERVAL = "15m"
      - interval = "4h"
      - PERIOD = "1d"  (when clearly a candle interval, not a date range)

    Returns a canonical interval string (e.g. "1h", "5m", "1d") or None.
    """
    import re

    # Patterns that strongly indicate a candle interval
    tf_re = re.compile(
        r"""(?:TIMEFRAME|TIME_FRAME|INTERVAL|CANDLE_INTERVAL|CANDLE_SIZE|KLINE_INTERVAL|GRANULARITY|RESOLUTION|BAR_SIZE)\s*=\s*['"]([\w]+)['"]""",
        re.IGNORECASE,
    )
    # Secondary pattern: period-like variable but with interval values
    period_re = re.compile(
        r"""(?:PERIOD|TF)\s*=\s*['"](\d+[mhdwM](?:in)?(?:ute)?(?:our)?(?:ay)?(?:eek)?)['"]""",
        re.IGNORECASE,
    )

    # Canonical normalisation map
    normalise = {
        "1min": "1m", "1minute": "1m", "1m": "1m",
        "3min": "3m", "3minute": "3m", "3m": "3m",
        "5min": "5m", "5minute": "5m", "5m": "5m",
        "15min": "15m", "15minute": "15m", "15m": "15m",
        "30min": "30m", "30minute": "30m", "30m": "30m",
        "60min": "1h", "60m": "1h",
        "1hour": "1h", "1h": "1h",
        "2hour": "2h", "2h": "2h",
        "4hour": "4h", "4h": "4h",
        "6hour": "6h", "6h": "6h",
        "8hour": "8h", "8h": "8h",
        "12hour": "12h", "12h": "12h",
        "1day": "1d", "1d": "1d", "daily": "1d",
        "3day": "3d", "3d": "3d",
        "1week": "1w", "1w": "1w", "1wk": "1w", "weekly": "1w",
        "1month": "1M", "1mo": "1M", "1M": "1M", "monthly": "1M",
    }

    try:
        for f in os.listdir(code_dir):
            if not f.endswith(".py"):
                continue
            fpath = os.path.join(code_dir, f)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read(8000)
                # Try strong pattern first
                m = tf_re.search(content)
                if m:
                    raw = m.group(1)
                    # Try exact match first (preserves "1M" vs "1m")
                    canonical = normalise.get(raw) or normalise.get(raw.lower())
                    if canonical:
                        return canonical
                # Try secondary pattern
                m = period_re.search(content)
                if m:
                    raw = m.group(1)
                    canonical = normalise.get(raw) or normalise.get(raw.lower())
                    if canonical:
                        return canonical
            except OSError:
                continue
    except OSError:
        pass
    return None


def _is_crypto_symbol(symbol: str) -> bool:
    """Heuristic: check if a symbol looks like a crypto pair."""
    upper = symbol.upper().replace("/", "").replace("-", "")
    crypto_quotes = ("USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "TUSD", "DAI")
    crypto_bases = (
        "BTC", "ETH", "BNB", "SOL", "ADA", "DOT", "AVAX", "MATIC", "LINK",
        "UNI", "DOGE", "SHIB", "XRP", "LTC", "ATOM",
    )
    for quote in crypto_quotes:
        if upper.endswith(quote):
            base = upper[: -len(quote)]
            if base and len(base) >= 2:
                return True
    for base in crypto_bases:
        if upper.startswith(base):
            return True
    return False


def _period_to_candles(period: str, interval: str) -> int:
    """
    Convert a yfinance-style period + interval to approximate candle count.

    Used when fetching from Binance, which expects a ``limit`` parameter
    (number of candles) instead of a date-based period.
    """
    total_days = _period_to_days(period)
    step = _interval_to_timedelta(interval)
    step_days = max(step.total_seconds() / 86400, 1 / 1440)  # at least 1 minute
    candles = int(total_days / step_days)
    # Binance allows max 1000 per request; ccxt may paginate
    return max(candles, 10)


def prepare_data(
    code_dir: str,
    *,
    symbol: str = BACKTEST_SYMBOL,
    data_source: str = BACKTEST_DATA_SOURCE,
    period: str = BACKTEST_PERIOD,
    interval: str = BACKTEST_INTERVAL,
    fallback_rows: int = BACKTEST_DATA_ROWS,
) -> Tuple[str, str, int]:
    """
    Prepare backtest data using the resolution cascade.

    Returns (data_source_label, data_file_path, row_count).

    The ``interval`` and ``period`` parameters support "auto", in which case
    the function auto-detects the strategy's timeframe from source code and
    selects appropriate defaults via ``resolve_timeframe_profile()``.

    Resolution order (when data_source="auto"):
    1. Project's own data_provider.py
    2. yfinance (for stocks/ETFs)
    3. Binance REST API / ccxt (for crypto)
    4. Synthetic GBM data (last resort)
    """
    data_dir = os.path.join(code_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    output_file = os.path.join(data_dir, "sample_data.csv")

    # ── Auto-detect symbol from code if not explicitly set ──────────────────
    detected_symbol = _detect_symbol_from_code(code_dir)
    effective_symbol = symbol
    if detected_symbol and symbol == "SPY":
        effective_symbol = detected_symbol

    # ── Auto-detect timeframe from code ─────────────────────────────────────
    effective_interval = interval
    if effective_interval == "auto":
        detected_tf = _detect_timeframe_from_code(code_dir)
        effective_interval = detected_tf or "1d"

    # Resolve the full profile (period, yf_interval, binance_interval, etc.)
    profile = resolve_timeframe_profile(effective_interval, period)
    eff_period: str = profile["period"]
    yf_interval: str = profile["yf_interval"]
    bn_interval: str = profile["binance_interval"]
    syn_rows: int = profile["synthetic_rows"] if fallback_rows == BACKTEST_DATA_ROWS else fallback_rows

    # ── Forced source ────────────────────────────────────────────────────────
    if data_source == "project":
        path = _run_project_data_provider(code_dir)
        if path:
            return "project_provider", path, _count_csv_rows_file(path)

    if data_source == "yfinance":
        csv_text = fetch_yfinance_ohlcv(
            effective_symbol, period=eff_period, interval=yf_interval,
        )
        if csv_text:
            _write_csv(output_file, csv_text)
            return "yfinance", output_file, _count_lines(csv_text)

    if data_source == "binance":
        limit = _period_to_candles(eff_period, bn_interval)
        csv_text = fetch_binance_ohlcv(
            effective_symbol, interval=bn_interval, limit=limit,
        )
        if csv_text:
            _write_csv(output_file, csv_text)
            return "binance", output_file, _count_lines(csv_text)

    if data_source == "synthetic":
        csv_text = generate_synthetic_ohlcv(
            rows=syn_rows, seed=42, interval=effective_interval,
        )
        _write_csv(output_file, csv_text)
        return "synthetic", output_file, syn_rows

    # ── Auto resolution ──────────────────────────────────────────────────────

    # 1. Try project's own data_provider
    path = _run_project_data_provider(code_dir)
    if path:
        return "project_provider", path, _count_csv_rows_file(path)

    # 2. Decide yfinance vs Binance based on symbol
    is_crypto = _is_crypto_symbol(effective_symbol)

    if not is_crypto:
        # 2a. Try yfinance first (stocks/ETFs)
        csv_text = fetch_yfinance_ohlcv(
            effective_symbol, period=eff_period, interval=yf_interval,
        )
        if csv_text:
            _write_csv(output_file, csv_text)
            return "yfinance", output_file, _count_lines(csv_text)

    # 2b. Try Binance (crypto or yfinance failed)
    limit = _period_to_candles(eff_period, bn_interval)
    binance_symbol = effective_symbol if is_crypto else "BTCUSDT"
    csv_text = fetch_binance_ohlcv(
        binance_symbol, interval=bn_interval, limit=limit,
    )
    if csv_text:
        _write_csv(output_file, csv_text)
        return "binance", output_file, _count_lines(csv_text)

    # 2c. If crypto failed with Binance, still try yfinance (some crypto tickers work)
    if is_crypto:
        yf_symbol = effective_symbol.replace("/", "-").upper()
        csv_text = fetch_yfinance_ohlcv(
            yf_symbol, period=eff_period, interval=yf_interval,
        )
        if csv_text:
            _write_csv(output_file, csv_text)
            return "yfinance", output_file, _count_lines(csv_text)

    # 3. Last resort: synthetic data
    csv_text = generate_synthetic_ohlcv(
        rows=syn_rows, seed=42, interval=effective_interval,
    )
    _write_csv(output_file, csv_text)
    return "synthetic", output_file, syn_rows


def _write_csv(path: str, text: str) -> None:
    """Write CSV text to *path* atomically via a sibling .tmp file."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _count_lines(csv_text: str) -> int:
    """Count data rows in CSV text (excluding header)."""
    lines = [line for line in csv_text.strip().split("\n") if line.strip()]
    return max(len(lines) - 1, 0)


def _count_csv_rows_file(path: str) -> int:
    """Count data rows in a CSV file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            count = sum(1 for line in fh if line.strip()) - 1
            return max(count, 0)
    except OSError:
        return 0


def _period_to_days(period: str) -> int:
    """Convert a yfinance-style period string to approximate day count."""
    p = period.strip()
    try:
        # Handle literal uppercase "M" (months) BEFORE lowercasing.
        # p.upper().endswith("M") matches BOTH "1M" (monthly) AND "1m" (minute),
        # so use p[-1:] == "M" to require a LITERAL uppercase trailing M.
        # This avoids treating "1m" as 1 month (30 days).
        if p[-1:] == "M" and not p.lower().endswith("mo"):
            return int(float(p[:-1]) * 30)
    except (ValueError, TypeError):
        pass
    p = p.lower()
    try:
        if p.endswith("y"):
            return int(float(p[:-1]) * 365)
        if p.endswith("mo"):
            return int(float(p[:-2]) * 30)
        if p.endswith("w"):
            return int(float(p[:-1]) * 7)
        if p.endswith("d"):
            return int(p[:-1])
    except (ValueError, TypeError):
        pass
    return 500


# ── Code-dir inspection ─────────────────────────────────────────────────────


def _find_code_dir(run_dir: str) -> Optional[str]:
    """Locate the ``code/`` subdirectory of a run."""
    code_dir = os.path.join(run_dir, "code")
    if os.path.isdir(code_dir):
        return code_dir
    return None


def _find_backtest_entry(code_dir: str) -> Optional[str]:
    """Find the backtest entrypoint file inside the code directory."""
    candidates = ["backtest.py", "run_backtest.py", "main.py"]
    for name in candidates:
        path = os.path.join(code_dir, name)
        if os.path.isfile(path):
            return path
    # Fallback: any .py file containing 'backtest' in its name
    try:
        for f in os.listdir(code_dir):
            if f.endswith(".py") and "backtest" in f.lower():
                return os.path.join(code_dir, f)
    except OSError:
        pass
    return None


def _has_data_file(code_dir: str) -> bool:
    """Check whether the code directory already contains data files."""
    data_extensions = {".csv", ".json", ".parquet", ".xlsx", ".h5", ".hdf5"}
    data_dirs = ["data", "datasets", "sample_data"]
    # Check root
    try:
        for f in os.listdir(code_dir):
            _, ext = os.path.splitext(f)
            if ext.lower() in data_extensions:
                return True
    except OSError:
        pass
    # Check known subdirectories
    for subdir in data_dirs:
        data_path = os.path.join(code_dir, subdir)
        if os.path.isdir(data_path):
            try:
                for f in os.listdir(data_path):
                    _, ext = os.path.splitext(f)
                    if ext.lower() in data_extensions:
                        return True
            except OSError:
                pass
    return False


def _detect_param_space(code_dir: str) -> Dict[str, List[Any]]:
    """
    Attempt to detect tunable parameters from the strategy/backtest code.

    Scans for patterns like:
      - ``PARAM_NAME = value``  (module-level constants)
      - ``parser.add_argument("--param-name", default=...)``
      - Comments: ``# tunable: param_name = [val1, val2, ...]``

    Returns a dict mapping parameter names to lists of candidate values.
    If nothing is detected, returns a sensible default space.
    """
    import re

    param_space: Dict[str, List[Any]] = {}

    # Scan all .py files
    try:
        py_files = [
            os.path.join(code_dir, f)
            for f in os.listdir(code_dir)
            if f.endswith(".py")
        ]
    except OSError:
        py_files = []

    # Pattern for explicit tunable comments: # tunable: name = [v1, v2, v3]
    tunable_re = re.compile(
        r"#\s*tunable:\s*(\w+)\s*=\s*\[([^\]]+)\]",
        re.IGNORECASE,
    )
    # Pattern for module-level numeric constants
    const_re = re.compile(
        r"^([A-Z_][A-Z0-9_]*)\s*=\s*([\d.]+)\s*(?:#.*)?$",
        re.MULTILINE,
    )

    for fpath in py_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue

        # Explicit tunable annotations
        for m in tunable_re.finditer(content):
            name = m.group(1)
            vals_str = m.group(2)
            try:
                vals = [_parse_numeric(v.strip()) for v in vals_str.split(",")]
                vals = [v for v in vals if v is not None]
                # Deduplicate while preserving user-specified order.
                # Optuna raises ValueError if suggest_categorical receives
                # duplicate choices (e.g. "# tunable: X = [10, 20, 10]").
                seen: set = set()
                vals = [v for v in vals if not (v in seen or seen.add(v))]  # type: ignore[func-returns-value]
                if vals:
                    param_space[name] = vals
            except Exception:
                pass

        # Auto-detect numeric constants (conservative)
        for m in const_re.finditer(content):
            name = m.group(1)
            val = _parse_numeric(m.group(2))
            if val is not None and name not in param_space:
                # Skip obviously non-tunable names
                skip_names = {
                    "VERSION", "MAX_RETRIES", "TIMEOUT", "DEBUG",
                    "LOG_LEVEL", "PORT", "SEED",
                }
                if name in skip_names:
                    continue
                # Generate a small search space around the detected value
                if isinstance(val, int):
                    candidates = sorted({
                        max(1, int(val * 0.5)),
                        val,
                        int(val * 1.5),
                        int(val * 2.0),
                    })
                else:
                    # When val == 0.0, multiplying produces degenerate [0,0,0,0,0].
                    # Use an absolute step so the search space is meaningful.
                    if val == 0.0:
                        step_abs = 0.01
                    else:
                        step_abs = abs(val) * 0.25
                    candidates = sorted({
                        round(val - step_abs * 2, 6),
                        round(val - step_abs, 6),
                        val,
                        round(val + step_abs, 6),
                        round(val + step_abs * 2, 6),
                    })
                param_space[name] = candidates

    # Fallback defaults if nothing detected
    if not param_space:
        param_space = {
            "LOOKBACK_PERIOD": [10, 20, 50, 100],
            "STOP_LOSS_PCT": [0.02, 0.05, 0.10],
            "TAKE_PROFIT_PCT": [0.05, 0.10, 0.20],
        }

    return param_space


def _parse_numeric(s: str) -> Any:
    """Parse a string as int or float, return None on failure."""
    try:
        if "." in s:
            return float(s)
        return int(s)
    except (ValueError, TypeError):
        return None


# ── Backtest execution ───────────────────────────────────────────────────────


# Keys that must never be forwarded to LLM-generated subprocess code.
_SENSITIVE_ENV_KEY_PATTERNS = (
    "API_KEY", "API_SECRET", "SECRET_KEY", "TOKEN", "PASSWORD",
    "CREDENTIAL", "OPENROUTER", "OPENAI_API", "ANTHROPIC_API",
    "ALIBABA_", "AWS_SECRET", "TELEGRAM_",
)


def _make_safe_env(
    code_dir: str,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """
    Build a sanitised environment for an LLM-generated subprocess.

    Strips keys matching ``_SENSITIVE_ENV_KEY_PATTERNS`` from the inherited
    environment to prevent credential leakage.  ``BACKTEST_*`` keys and the
    explicit *env_overrides* are always kept.
    """
    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        upper = k.upper()
        # Always keep BACKTEST_* overrides and essential runtime keys
        if upper.startswith("BACKTEST_"):
            env[k] = v
            continue
        # Strip anything that looks like a credential
        if any(pat in upper for pat in _SENSITIVE_ENV_KEY_PATTERNS):
            continue
        # Drop inherited PYTHONPATH — set a clean value below.  Inheriting it
        # allows other projects on the user's PYTHONPATH that contain same-named
        # packages (e.g. "src") to shadow the generated code's own package
        # directory via Python's namespace-package scan.
        if upper == "PYTHONPATH":
            continue
        env[k] = v

    # Only the generated code directory should be on Python's path.
    env["PYTHONPATH"] = code_dir

    if env_overrides:
        env.update(env_overrides)
    return env


def _read_metrics_from_result_file(
    code_dir: str,
    *,
    written_after_wall: Optional[float] = None,
) -> Optional["BacktestMetrics"]:
    """Return parsed metrics from a JSON result file written by *this* run.

    Companion to ``_purge_stale_result_files`` for the failure branch.
    Returns ``None`` (NOT a default-zero ``BacktestMetrics``) when:

    - No result file exists.
    - The result file's mtime is older than ``written_after_wall`` (wall-
      clock seconds since epoch) — i.e. it is a leftover from a previous
      run that the purge step did not catch (rare; possible if the user
      manually placed a JSON in code_dir before invoking the pipeline).
    - The JSON parses but yields no recognised metric fields.

    The strict gating ensures the failure path never carries metric values
    forward unless they were *demonstrably* produced by the failing run.
    """
    # ``BacktestMetrics`` is defined in this module — no import needed.
    for fname in ("backtest_results.json", "results.json", "output.json"):
        fpath = os.path.join(code_dir, fname)
        if not os.path.isfile(fpath):
            continue
        # Stale-file guard.  Subtract a small epsilon so a sub-second-fast
        # subprocess that wrote its file at the same wall-second as our
        # anchor is still accepted (file mtimes on Windows have ~10 ms
        # resolution; subtracting 0.5 s avoids flaky exclusions).
        if written_after_wall is not None:
            try:
                mtime = os.path.getmtime(fpath)
            except OSError:
                continue
            if mtime < (written_after_wall - 0.5):
                continue
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        metrics = BacktestMetrics()
        _fill_metrics_from_dict(metrics, data)
        # Only return the metrics object if at least one field was filled —
        # an empty BacktestMetrics is indistinguishable from "no metrics".
        if any(
            getattr(metrics, attr, None) is not None
            for attr in (
                "sharpe_ratio", "total_return_pct", "max_drawdown_pct",
                "win_rate", "trade_count", "profit_factor",
                "annualised_volatility", "calmar_ratio", "sortino_ratio",
                "alpha", "beta",
            )
        ):
            return metrics
    return None


def _purge_stale_result_files(code_dir: str) -> None:
    """Remove any leftover result JSONs from previous subprocess runs.

    Without this purge, when a backtest succeeded once and then failed on a
    later combo / fix-round, ``_parse_backtest_output()`` would still find the
    successful run's ``backtest_results.json`` on disk and report its metrics
    as if the failed run had produced them.  That would make ``success=False``
    reports carry ghost metrics from a sibling run, which downstream
    analytics (the analyst crew, the tearsheet, the cost model) would treat
    as legitimate.  Deleting these files before each subprocess execution
    guarantees the parser only sees what *this* run actually wrote.
    """
    for fname in ("backtest_results.json", "results.json", "output.json"):
        fpath = os.path.join(code_dir, fname)
        try:
            if os.path.isfile(fpath):
                os.unlink(fpath)
        except OSError:
            # Best-effort cleanup — never raise from a pre-flight helper;
            # the parser's ``json.load`` will surface any genuine read
            # failure that arises later.
            pass


def _run_backtest_subprocess(
    code_dir: str,
    entrypoint: str,
    *,
    timeout: int = BACKTEST_TIMEOUT,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """
    Execute the backtest script in an isolated subprocess.

    Sensitive environment variables (API keys, tokens, etc.) are stripped
    from the subprocess environment to prevent credential leakage from
    LLM-generated code.

    Pre-flight protections:
    - Syntax check: before launching ``python entrypoint``, run ``compile()``
      over the file.  When the LLM-generated entrypoint has a syntax error
      (e.g. unterminated string literal), reporting the error directly
      skips the ~1 s subprocess startup cost and yields a stderr message
      in the exact format ``_try_llm_fix`` expects, instead of a Python
      traceback header that can mislead the fix prompt.
    - Stale result-file purge: see ``_purge_stale_result_files`` doc.

    Returns (returncode, stdout, stderr).
    """
    # Pre-flight syntax check — short-circuits the subprocess when we
    # already know it cannot compile.  Returncode 1 mirrors the exit code
    # CPython itself uses for SyntaxError at module import time.
    try:
        with open(entrypoint, "r", encoding="utf-8", errors="replace") as fh:
            entry_src = fh.read()
    except OSError as exc:
        return -2, "", f"Cannot read entrypoint: {exc}"
    syntax_err = _validate_python_syntax(entry_src, entrypoint)
    if syntax_err is not None:
        return 1, "", f"SyntaxError in {os.path.basename(entrypoint)}: {syntax_err}"

    _purge_stale_result_files(code_dir)
    env = _make_safe_env(code_dir, env_overrides)

    try:
        result = subprocess.run(
            [sys.executable, entrypoint],
            cwd=code_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Backtest timed out after {timeout}s"
    except OSError as exc:
        return -2, "", f"Subprocess error: {exc}"


def _parse_backtest_output(stdout: str, stderr: str, code_dir: str) -> BacktestMetrics:
    """
    Parse backtest results from stdout/stderr and/or result files.

    Supports multiple output conventions:
    1. JSON object printed to stdout with metric keys
    2. ``backtest_results.json`` file written in the code directory
    3. Key-value lines in stdout (e.g. ``Sharpe Ratio: 1.23``)
    """
    import re

    metrics = BacktestMetrics(raw_stdout=stdout[:5000], raw_stderr=stderr[:2000])

    # Strategy 1: Try to find JSON in stdout
    json_data = _try_parse_json_from_text(stdout)

    # Strategy 2: Try result file
    if json_data is None:
        for fname in ("backtest_results.json", "results.json", "output.json"):
            fpath = os.path.join(code_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as fh:
                        json_data = json.load(fh)
                    break
                except (json.JSONDecodeError, OSError):
                    pass

    if isinstance(json_data, dict):
        _fill_metrics_from_dict(metrics, json_data)
        return metrics

    # Strategy 3: Parse key-value lines from stdout
    kv_patterns = {
        "sharpe_ratio": re.compile(r"sharpe\s*(?:ratio)?[\s:=]+([+-]?\d+\.?\d*)", re.I),
        "total_return_pct": re.compile(r"(?:total\s*)?return[\s:=]+([+-]?\d+\.?\d*)%?", re.I),
        "max_drawdown_pct": re.compile(r"max\s*draw\s*down[\s:=]+([+-]?\d+\.?\d*)%?", re.I),
        "win_rate": re.compile(r"win\s*rate[\s:=]+([+-]?\d+\.?\d*)%?", re.I),
        "trade_count": re.compile(r"(?:total\s*)?trades?[\s:=]+(\d+)", re.I),
        "profit_factor": re.compile(r"profit\s*factor[\s:=]+([+-]?\d+\.?\d*)", re.I),
    }
    combined = stdout + "\n" + stderr
    for attr, pattern in kv_patterns.items():
        m = pattern.search(combined)
        if m:
            try:
                val = float(m.group(1))
                if attr == "trade_count":
                    setattr(metrics, attr, int(val))
                else:
                    setattr(metrics, attr, val)
            except (ValueError, IndexError):
                pass

    return metrics


def _try_parse_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Try to extract a JSON object from text, even if mixed with other output."""
    # First try the full text
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find JSON block delimited by { ... }.
    # Uses a while-loop so that when a candidate text[start:i+1] fails to parse
    # we can retry from start+1, allowing detection of inner JSON that was
    # "absorbed" by an outer unbalanced brace in surrounding log text.
    depth = 0
    start = -1
    i = 0
    text_len = len(text)
    while i < text_len:
        ch = text[i]
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    data = json.loads(text[start : i + 1])
                    if isinstance(data, dict):
                        return data
                except (json.JSONDecodeError, ValueError):
                    # Outer span failed — retry from the character after 'start'
                    # so inner JSON blocks can still be discovered.
                    i = start
                    depth = 0
                    start = -1
        i += 1
    return None


def _fill_metrics_from_dict(metrics: BacktestMetrics, data: Dict[str, Any]) -> None:
    """Fill BacktestMetrics fields from a dict with flexible key matching."""
    key_map = {
        "sharpe_ratio": ["sharpe_ratio", "sharpe", "sr"],
        "total_return_pct": ["total_return_pct", "total_return", "return", "return_pct", "returns"],
        "max_drawdown_pct": ["max_drawdown_pct", "max_drawdown", "drawdown"],
        "win_rate": ["win_rate", "winrate", "win_pct"],
        "trade_count": ["trade_count", "trades", "num_trades", "total_trades"],
        "profit_factor": ["profit_factor", "pf"],
        "annualised_volatility": ["annualised_volatility", "annualized_volatility", "volatility", "vol"],
        "calmar_ratio": ["calmar_ratio", "calmar"],
        "sortino_ratio": ["sortino_ratio", "sortino"],
        "alpha": ["alpha"],
        "beta": ["beta"],
    }
    # Flatten nested dicts (e.g. {"metrics": {"sharpe": 1.2}})
    flat = {}
    for k, v in data.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                flat[k2.lower().strip()] = v2
        else:
            flat[k.lower().strip()] = v

    for attr, aliases in key_map.items():
        for alias in aliases:
            if alias in flat:
                val = flat[alias]
                try:
                    if attr == "trade_count":
                        fv = float(val)
                        # round() avoids silent truncation (e.g. "3.7" → 4).
                        # Guard against inf/nan: round(inf) raises OverflowError
                        # and round(nan) raises ValueError in Python ≥ 3.11.
                        if math.isfinite(fv):
                            setattr(metrics, attr, round(fv))
                    else:
                        fv = float(val)
                        if math.isfinite(fv):
                            setattr(metrics, attr, fv)
                except (ValueError, TypeError, OverflowError):
                    pass
                break


# ── Parameter optimisation ───────────────────────────────────────────────────

import logging as _logging  # noqa: E402,I001  (needed here; avoid top-level re-ordering)
_BACKTEST_LOGGER = _logging.getLogger(__name__)


def _run_optuna_optimization(
    param_space: Dict[str, List[Any]],
    evaluate_fn: Any,  # Callable[[Dict[str, Any]], Optional[float]]
    n_trials: int = BACKTEST_BAYESIAN_N_TRIALS,
    target_metric: str = BACKTEST_TARGET_METRIC,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Run Bayesian hyperparameter optimisation via Optuna.

    Uses ``optuna.samplers.TPESampler`` (Tree-structured Parzen Estimator) to
    intelligently explore the discrete parameter space.  Falls back to random
    search if Optuna is not installed.

    Parameters
    ----------
    param_space:
        Mapping of parameter name → list of discrete candidate values.
    evaluate_fn:
        Callable that accepts a ``Dict[str, Any]`` of parameters and returns an
        ``Optional[float]`` metric value (higher is better, ``None`` on failure).
    n_trials:
        Number of Optuna trials to run.
    target_metric:
        Name of the metric being optimised (used only for logging).

    Returns
    -------
    Tuple of:
        - best_params: The parameter dict that yielded the highest metric value,
          or ``None`` if every trial failed.
        - all_results: List of ``{"params": ..., "value": ..., "success": ...}``
          dicts for every trial.
    """
    if not param_space:
        return None, []

    all_results: List[Dict[str, Any]] = []

    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        def _objective(trial: Any) -> float:
            params: Dict[str, Any] = {
                name: trial.suggest_categorical(name, choices)
                for name, choices in param_space.items()
            }
            value = evaluate_fn(params)
            all_results.append({
                "params": dict(params),
                "value": value,
                "success": value is not None,
            })
            # Optuna minimises by default; we maximise, so return the value
            # directly (study direction is set to "maximize" below).
            if value is None:
                raise optuna.exceptions.TrialPruned()
            fv = float(value)
            if not math.isfinite(fv):
                raise optuna.exceptions.TrialPruned()
            return fv

        # Minimise lower-is-better metrics (drawdown, volatility); maximise everything else.
        direction = "minimize" if target_metric in _LOWER_IS_BETTER else "maximize"

        study = optuna.create_study(
            direction=direction,
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(_objective, n_trials=n_trials, catch=(Exception,))

        best_params: Optional[Dict[str, Any]] = None
        if study.best_trial is not None and study.best_trial.state.name == "COMPLETE":
            best_params = dict(study.best_trial.params)

        return best_params, all_results

    except ImportError:
        _BACKTEST_LOGGER.warning(
            "optuna is not installed — Bayesian search falling back to random search. "
            "Install with: pip install optuna"
        )

    # ── Random-search fallback ────────────────────────────────────────────────
    names = list(param_space.keys())
    value_lists = [param_space[n] for n in names]
    seen_combos: List[Dict[str, Any]] = []
    for _ in range(n_trials * 2):
        with _PARAM_RNG_LOCK:
            combo = {n: _PARAM_RNG.choice(vs) for n, vs in zip(names, value_lists)}
        if combo in seen_combos:
            continue
        seen_combos.append(combo)
        value = evaluate_fn(combo)
        all_results.append({"params": dict(combo), "value": value, "success": value is not None})
        if len(seen_combos) >= n_trials:
            break

    # Apply the same direction logic as the Optuna path above so that the
    # fallback correctly minimises lower-is-better metrics instead of always maximising.
    _minimize_fb = target_metric in _LOWER_IS_BETTER

    best_params = None
    best_value: Optional[float] = None
    for r in all_results:
        v = r.get("value")
        if v is not None:
            fv = float(v)
            if not math.isfinite(fv):
                continue  # NaN/Inf cannot be meaningfully compared; skip
            if best_value is None or (_minimize_fb and fv < best_value) or (not _minimize_fb and fv > best_value):
                best_value = fv
                best_params = r["params"]

    return best_params, all_results


def _build_param_combos(
    param_space: Dict[str, List[Any]],
    strategy: str = "grid",
    max_combos: int = BACKTEST_MAX_COMBOS,
) -> List[Dict[str, Any]]:
    """Generate parameter combinations from the search space."""
    # iter_product(*[]) yields one empty tuple — guard to return no combos
    # when the space is empty rather than running a single combo with no params.
    if not param_space:
        return []
    names = list(param_space.keys())
    value_lists = [param_space[n] for n in names]

    if strategy == "grid":
        all_combos = [
            dict(zip(names, vals))
            for vals in iter_product(*value_lists)
        ]
    else:
        # Random search
        all_combos = []
        for _ in range(max_combos * 2):
            with _PARAM_RNG_LOCK:
                combo = {n: _PARAM_RNG.choice(vs) for n, vs in zip(names, value_lists)}
            if combo not in all_combos:
                all_combos.append(combo)
            if len(all_combos) >= max_combos:
                break

    # Truncate to max_combos
    if len(all_combos) > max_combos:
        if strategy == "grid":
            # Midpoint-based uniform sampling: place a virtual sample point at
            # the centre of each equal-width bucket so the first and last
            # elements of the combo list have equal probability of selection.
            # Using int(i * step) (floor-based) would systematically exclude
            # the last few elements when len is not an exact multiple of max.
            n = len(all_combos)
            step = n / max(1, max_combos)
            indices = [min(int((i + 0.5) * step), n - 1) for i in range(max_combos)]
            all_combos = [all_combos[i] for i in indices]
        else:
            all_combos = all_combos[:max_combos]

    return all_combos


def _params_to_env(params: Dict[str, Any]) -> Dict[str, str]:
    """Convert parameter dict to environment variable overrides."""
    env: Dict[str, str] = {}
    for k, v in params.items():
        if isinstance(v, float) and not math.isfinite(v):
            continue  # NaN/Inf cannot be represented as env var strings
        env_key = f"BACKTEST_PARAM_{k.upper()}"
        env[env_key] = str(v)
    return env


# ── LLM fix loop ─────────────────────────────────────────────────────────────


def _build_fix_prompt(
    error_output: str,
    code_content: str,
    original_problem: str,
) -> str:
    """Build a prompt for the LLM to fix the backtest code."""
    return textwrap.dedent(f"""\
    The following backtest code has failed to execute correctly.

    ## Error Output
    ```
    {error_output[:3000]}
    ```

    ## Current Code
    ```python
    {code_content[:8000]}
    ```

    ## Original Problem
    {original_problem[:2000]}

    ## Instructions
    Fix the code so the backtest runs successfully. The code must:
    1. Read OHLCV data from a CSV file. Check the BACKTEST_DATA_FILE env var first,
       then fall back to data/sample_data.csv. The CSV has columns:
       date,open,high,low,close,volume
    2. Execute the strategy backtest
    3. Print results as a JSON object to stdout with at least these keys:
       - sharpe_ratio (float)
       - total_return_pct (float)
       - max_drawdown_pct (float)
       - win_rate (float)
       - trade_count (int)

    Output ONLY the fixed Python code, no explanations.
    """)


def _validate_python_syntax(code: str, filename: str = "<llm_fix>") -> Optional[str]:
    """Compile *code* in syntax-only mode to detect SyntaxError.

    Returns ``None`` when the source is syntactically valid, otherwise a
    short human-readable error string ("line N: <msg>") suitable for
    appending to the report.  ``compile()`` raises ``SyntaxError`` for
    unterminated strings, mismatched brackets, invalid characters, etc. —
    exactly the family of LLM-introduced bugs the fix loop is meant to
    catch.  ``ValueError`` covers null-byte injection from corrupted UTF-8
    decoding ("source code string cannot contain null bytes").
    """
    if not code or not isinstance(code, str):
        return "empty code"
    try:
        compile(code, filename, "exec")
        return None
    except (SyntaxError, ValueError) as exc:
        lineno = getattr(exc, "lineno", None)
        msg = getattr(exc, "msg", str(exc))
        return f"line {lineno}: {msg}" if lineno else str(msg)


# Pure-Python repair pass for LLM-fix responses that arrive with JSON-style
# escape sequences instead of real control characters.  Some providers
# (notably reasoning-class models running under STRICT_JSON) emit ``\\n`` as a
# literal backslash + n inside the code body, which causes ``compile()`` to
# fail with ``SyntaxError: unexpected character after line continuation
# character``.  Section_01 already handles this for the codegen bundle
# pipeline; this function applies the same repair to the backtest fix loop —
# without it, a single mis-escape in the LLM-suggested fix would burn a whole
# round and eventually fall through with ``LLM fix round N produced no valid
# code.``
def _deterministic_repair_llm_code(code: str) -> str:
    if not code:
        return code
    # Strip BOM and stray triple-backtick prefixes / suffixes the response
    # extractor might have missed (e.g. a stray ```` ``` ```` on its own line
    # at the end of the body).
    repaired = code.lstrip("﻿").strip()
    # Drop a trailing fence-only line if present.
    if repaired.endswith("```"):
        repaired = repaired[: -3].rstrip()
    # Drop a leading fence-only line if present (e.g. ``` on its own line).
    if repaired.startswith("```"):
        # Find the end of the first line; preserve everything after it.
        nl = repaired.find("\n")
        repaired = repaired[nl + 1 :] if nl >= 0 else ""
    if not repaired:
        return code
    # If it already compiles, do nothing — never alter known-good code.
    if _validate_python_syntax(repaired) is None:
        return repaired
    # Iterate at most 5 unescape passes mirroring section_01's strategy: keep
    # reducing literal escapes only while each pass yields at least one
    # replacement AND leaves the result still uncompilable (so we don't
    # corrupt code that legitimately contains backslash sequences inside
    # strings).  Bail out at the first version that compiles.
    current = repaired
    for _ in range(5):
        nxt = (
            current.replace("\\\\n", "\\n")
                   .replace("\\n", "\n")
                   .replace("\\t", "\t")
                   .replace("\\r", "\r")
                   .replace("\\\"", '"')
                   .replace("\\'", "'")
        )
        if nxt == current:
            break
        current = nxt
        if _validate_python_syntax(current) is None:
            return current
    # Most-reduced form is what gets returned even if it still doesn't
    # compile — the caller validates again and treats failure as
    # ``produced no valid code``, exactly the contract we want.
    return current


def _try_llm_fix(
    llm: Any,
    code_dir: str,
    entrypoint: str,
    error_output: str,
    original_problem: str,
) -> bool:
    """
    Attempt to fix the backtest code using the LLM.

    Returns True if the file was updated **and the new code passes a syntax
    check**.  Previously the function only checked that the extracted block
    was non-empty (``len(fixed_code.strip()) < 20``); when the LLM's "fix"
    contained the same SyntaxError class as the broken input (e.g. the LLM
    re-emitted an unterminated string literal), the file would be
    overwritten with equally-broken code, the next subprocess call would
    crash again, and the loop would burn a whole round per iteration with
    no progress.  ``compile()`` is run on the proposed fix before writing —
    any SyntaxError counts as ``produced no valid code`` so the loop falls
    through to the failure path immediately instead of dragging out three
    rounds of broken code.
    """
    try:
        with open(entrypoint, "r", encoding="utf-8") as fh:
            current_code = fh.read()
    except OSError:
        return False

    prompt = _build_fix_prompt(error_output, current_code, original_problem)

    # Duck-typed LLM call (supports .invoke(), .complete(), or callable)
    response_text = ""
    try:
        if hasattr(llm, "invoke"):
            result = llm.invoke(prompt)
            response_text = getattr(result, "content", str(result))
        elif hasattr(llm, "complete"):
            result = llm.complete(prompt)
            response_text = str(result)
        elif callable(llm):
            result = llm(prompt)
            response_text = str(result)
        else:
            return False
    except Exception:
        return False

    # Extract code from response (strip markdown fences if present)
    fixed_code = _extract_code_block(response_text)
    if not fixed_code or len(fixed_code.strip()) < 20:
        return False

    # Deterministic escape / fence repair before validation.  Some providers
    # re-emit the suggested code with JSON-style ``\n`` literals rather than
    # real newlines; the section_01 unescape helper handles the codegen
    # bundle and the same repair is applied here for the backtest fix path.
    fixed_code = _deterministic_repair_llm_code(fixed_code)

    # Hard syntax gate.  Any SyntaxError in the proposed fix short-circuits
    # to False so the round counts as "no valid code".
    syntax_err = _validate_python_syntax(fixed_code, entrypoint)
    if syntax_err is not None:
        return False

    # Refuse to write a "fix" that is identical to the current
    # broken code — that just burns the next round without changing
    # anything.  The original code may itself fail compile (the very
    # reason we are in the fix loop), so the comparison is done against
    # the verbatim file content.
    if fixed_code.strip() == (current_code or "").strip():
        return False

    # Atomic write: write to .tmp then rename so that a crash mid-write
    # does not leave the entrypoint file in a corrupted/truncated state.
    tmp = entrypoint + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(fixed_code)
        os.replace(tmp, entrypoint)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _extract_code_block(text: str) -> str:
    """Extract Python code from markdown-fenced or raw response.

    Handles the two common LLM "almost-correct" response shapes:
    1. Forgotten closing ``` ``` ``` fence — match anything from the opener
       until end-of-string and strip the trailing fence if it is present
       on the very last line.
    2. Multiple fenced blocks — pick the longest Python-looking block
       rather than always the first (LLMs sometimes emit a tiny example
       fence followed by the real fix).
    """
    import re

    # Strategy 1: paired fenced block — prefer the longest match across
    # multiple fences so a small ``# example`` block before the actual fix
    # never wins.
    fenced_blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced_blocks:
        # Pick the longest block (length-by-character is a reasonable
        # proxy for "the real code").  Tie-broken by first-match order,
        # which is ``max()``'s default behaviour for equal keys.
        best = max(fenced_blocks, key=lambda b: len(b))
        return best.strip()

    # Strategy 2: opening fence with no closing fence (LLM truncated).
    open_fence = re.search(r"```(?:python|py)?\s*\n(.*)$", text, re.DOTALL)
    if open_fence:
        body = open_fence.group(1).strip()
        # Drop any trailing partial fence remnant.
        if body.endswith("```"):
            body = body[:-3].rstrip()
        if body and len(body) >= 20:
            return body

    # Strategy 3: if the entire response looks like code, return it.
    # Use a proportional threshold: require ≥40% of the first 10 non-empty
    # lines to start with a code indicator.  This accepts short snippets like
    # "import os / def main(): / pass" (2 of 3 lines = 67%) while rejecting
    # pure prose ("The fix should …" = 0 of N lines = 0%).
    lines = text.strip().split("\n")
    code_indicators = ("import ", "def ", "class ", "from ", "#", "if ", "for ")
    nonempty = [ln for ln in lines[:10] if ln.strip()]
    code_lines = sum(1 for line in nonempty if any(line.strip().startswith(ind) for ind in code_indicators))
    if nonempty and code_lines / len(nonempty) >= 0.4:
        return text.strip()
    # Response looks like prose — return empty so the caller knows extraction
    # failed rather than writing English sentences as a Python entrypoint.
    return ""


# ── Analysis report generation ───────────────────────────────────────────────


def _generate_analysis_markdown(report: BacktestReport) -> str:
    """Generate a human-readable Markdown analysis report."""
    lines = ["# Backtest Analysis Report", ""]
    lines.append(f"**Status:** {'✅ SUCCESS' if report.success else '❌ FAILED'}")
    symbol_info = f" [{report.data_symbol}]" if report.data_symbol else ""
    lines.append(f"**Data Source:** {report.data_source}{symbol_info} ({report.data_rows} rows)")
    lines.append("")

    if report.baseline_metrics:
        bm = report.baseline_metrics
        lines.append("## Baseline Performance")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        if bm.sharpe_ratio is not None and math.isfinite(bm.sharpe_ratio):
            lines.append(f"| Sharpe Ratio | {bm.sharpe_ratio:.4f} |")
        if bm.total_return_pct is not None and math.isfinite(bm.total_return_pct):
            lines.append(f"| Total Return | {bm.total_return_pct:.2f}% |")
        if bm.max_drawdown_pct is not None and math.isfinite(bm.max_drawdown_pct):
            lines.append(f"| Max Drawdown | {bm.max_drawdown_pct:.2f}% |")
        if bm.win_rate is not None and math.isfinite(bm.win_rate):
            lines.append(f"| Win Rate | {bm.win_rate * 100:.2f}% |")
        if bm.trade_count is not None:
            lines.append(f"| Trade Count | {bm.trade_count} |")
        if bm.profit_factor is not None and math.isfinite(bm.profit_factor):
            lines.append(f"| Profit Factor | {bm.profit_factor:.4f} |")
        if bm.annualised_volatility is not None and math.isfinite(bm.annualised_volatility):
            lines.append(f"| Annualised Volatility | {bm.annualised_volatility:.4f} |")
        if bm.sortino_ratio is not None and math.isfinite(bm.sortino_ratio):
            lines.append(f"| Sortino Ratio | {bm.sortino_ratio:.4f} |")
        if bm.calmar_ratio is not None and math.isfinite(bm.calmar_ratio):
            lines.append(f"| Calmar Ratio | {bm.calmar_ratio:.4f} |")
        if bm.alpha is not None and math.isfinite(bm.alpha):
            lines.append(f"| Alpha | {bm.alpha:.4f} |")
        if bm.beta is not None and math.isfinite(bm.beta):
            lines.append(f"| Beta | {bm.beta:.4f} |")
        lines.append("")

    if report.combos_evaluated > 0:
        lines.append("## Parameter Optimisation")
        lines.append("")
        lines.append(f"**Search Strategy:** {report.parameter_search}")
        lines.append(f"**Combinations Evaluated:** {report.combos_evaluated}")
        lines.append("")
        if report.best_params:
            lines.append("### Best Parameters")
            lines.append("")
            lines.append("| Parameter | Value |")
            lines.append("|-----------|-------|")
            for k, v in report.best_params.items():
                # Treat NaN/Inf the same as None — to_dict() replaces them
                # with None for JSON, but the in-memory object may still hold
                # the original float.
                _bad = isinstance(v, float) and not math.isfinite(v)
                v_str = "N/A" if (v is None or _bad) else str(v)
                lines.append(f"| `{k}` | {v_str} |")
            lines.append("")
        if report.best_metrics:
            bm = report.best_metrics
            lines.append("### Best Performance")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|--------|-------|")
            if bm.sharpe_ratio is not None and math.isfinite(bm.sharpe_ratio):
                lines.append(f"| Sharpe Ratio | {bm.sharpe_ratio:.4f} |")
            if bm.total_return_pct is not None and math.isfinite(bm.total_return_pct):
                lines.append(f"| Total Return | {bm.total_return_pct:.2f}% |")
            if bm.max_drawdown_pct is not None and math.isfinite(bm.max_drawdown_pct):
                lines.append(f"| Max Drawdown | {bm.max_drawdown_pct:.2f}% |")
            if bm.win_rate is not None and math.isfinite(bm.win_rate):
                lines.append(f"| Win Rate | {bm.win_rate * 100:.2f}% |")
            if bm.trade_count is not None:
                lines.append(f"| Trade Count | {bm.trade_count} |")
            if bm.profit_factor is not None and math.isfinite(bm.profit_factor):
                lines.append(f"| Profit Factor | {bm.profit_factor:.4f} |")
            if bm.annualised_volatility is not None and math.isfinite(bm.annualised_volatility):
                lines.append(f"| Annualised Volatility | {bm.annualised_volatility:.4f} |")
            if bm.sortino_ratio is not None and math.isfinite(bm.sortino_ratio):
                lines.append(f"| Sortino Ratio | {bm.sortino_ratio:.4f} |")
            if bm.calmar_ratio is not None and math.isfinite(bm.calmar_ratio):
                lines.append(f"| Calmar Ratio | {bm.calmar_ratio:.4f} |")
            if bm.alpha is not None and math.isfinite(bm.alpha):
                lines.append(f"| Alpha | {bm.alpha:.4f} |")
            if bm.beta is not None and math.isfinite(bm.beta):
                lines.append(f"| Beta | {bm.beta:.4f} |")
            lines.append("")

        # Top 5 combos table
        successful_combos = [c for c in report.all_combos if c.success and c.metrics]
        if successful_combos:
            target = report.target_metric
            _minimize_md = target in _LOWER_IS_BETTER
            # For lower-is-better metrics (drawdown, volatility) sort ascending
            # (best = smallest value first); for all others sort descending.
            # The fallback sentinel must also match the direction so that combos
            # with missing metric values sink to the bottom of the ranking.
            _sentinel = float("inf") if _minimize_md else float("-inf")
            def _combo_metric_value(combo: ParameterCombo) -> float:
                if combo.metrics is None:
                    return _sentinel
                value = combo.metrics.metric_value(target)
                return value if value is not None else _sentinel

            successful_combos.sort(key=_combo_metric_value, reverse=not _minimize_md)
            top_n = successful_combos[:5]
            lines.append("### Top 5 Parameter Sets")
            lines.append("")
            param_names = list(top_n[0].params.keys()) if top_n else []
            # Annotate the metric column header with a unit hint when the values
            # are displayed in a transformed form (e.g. win_rate shown as %).
            _target_label = {
                "win_rate":         "win_rate (%)",
                "total_return_pct": "total_return (%)",
                "max_drawdown_pct": "max_drawdown (%)",
            }.get(target, target)
            header = "| Rank | " + " | ".join(f"`{n}`" for n in param_names) + f" | {_target_label} |"
            sep = "|------|" + "|".join("------" for _ in param_names) + "|--------|"
            lines.append(header)
            lines.append(sep)
            for i, combo in enumerate(top_n, 1):
                vals = " | ".join(
                    "N/A" if (pv := combo.params.get(n)) is None or (isinstance(pv, float) and not math.isfinite(pv)) else str(pv)
                    for n in param_names
                )
                mv = combo.metrics.metric_value(target) if combo.metrics else None
                if mv is None:
                    mv_str = "N/A"
                elif target == "win_rate":
                    # win_rate is stored as a fraction (0–1); display as percentage.
                    mv_str = f"{mv * 100:.2f}%"
                elif target in ("total_return_pct", "max_drawdown_pct"):
                    # Already stored in pct form (e.g. 15.0 = 15%); add % suffix.
                    mv_str = f"{mv:.2f}%"
                else:
                    mv_str = f"{mv:.4f}"
                lines.append(f"| {i} | {vals} | {mv_str} |")
            lines.append("")

    if report.fix_rounds_used > 0:
        lines.append(f"**Code fix rounds used:** {report.fix_rounds_used}")
        lines.append("")

    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- {w}")
        lines.append("")

    if report.errors:
        lines.append("## Errors")
        lines.append("")
        for e in report.errors:
            lines.append(f"- {e}")
        lines.append("")

    return "\n".join(lines)


# ── Main pipeline ────────────────────────────────────────────────────────────


def run_backtest_pipeline(
    run_dir: str,
    *,
    llm: Any = None,
    timeout: int = BACKTEST_TIMEOUT,
    data_rows: int = BACKTEST_DATA_ROWS,
    param_search: str = BACKTEST_PARAM_SEARCH,
    max_combos: int = BACKTEST_MAX_COMBOS,
    target_metric: str = BACKTEST_TARGET_METRIC,
    fix_max_rounds: int = BACKTEST_FIX_MAX_ROUNDS,
    initial_capital: float = BACKTEST_INITIAL_CAPITAL,
) -> BacktestReport:
    """
    Run the full backtest pipeline for a Quant mode run directory.

    Steps:
    1. Locate code directory and backtest entrypoint
    2. Auto-prepare data if missing
    3. Execute baseline backtest
    4. If baseline fails and LLM is available, attempt code fix loop
    5. Run parameter optimisation
    6. Produce analysis report
    """
    # Normalise target_metric to its canonical field name so that all
    # downstream logic (_LOWER_IS_BETTER membership, metric_value() lookups,
    # report.target_metric, Optuna direction) operates on the same string.
    # This mirrors the alias table in metric_value() — kept in sync manually.
    _METRIC_CANONICAL: Dict[str, str] = {
        "max_drawdown":          "max_drawdown_pct",
        "drawdown":              "max_drawdown_pct",
        "return":                "total_return_pct",
        "total_return":          "total_return_pct",
        "return_pct":            "total_return_pct",
        "returns":               "total_return_pct",
        "sharpe":                "sharpe_ratio",
        "sr":                    "sharpe_ratio",
        "winrate":               "win_rate",
        "win_pct":               "win_rate",
        "pf":                    "profit_factor",
        "annualized_volatility": "annualised_volatility",
        "volatility":            "annualised_volatility",
        "vol":                   "annualised_volatility",
        "calmar":                "calmar_ratio",
        "sortino":               "sortino_ratio",
        "trades":                "trade_count",
        "num_trades":            "trade_count",
        "total_trades":          "trade_count",
    }
    # Guard against non-string input (e.g. None) before dict.get() and all
    # downstream string operations (getattr, _LOWER_IS_BETTER membership, etc.).
    if not isinstance(target_metric, str) or not target_metric.strip():
        target_metric = BACKTEST_TARGET_METRIC
    else:
        # Strip leading/trailing whitespace so " sharpe_ratio" matches the
        # same as "sharpe_ratio" in _METRIC_CANONICAL and _LOWER_IS_BETTER.
        target_metric = target_metric.strip()
    target_metric = _METRIC_CANONICAL.get(target_metric, target_metric)

    report = BacktestReport(run_dir=run_dir, target_metric=target_metric)

    # ── Step 0: Validate mode ────────────────────────────────────────────────
    analysis_path = os.path.join(run_dir, "analysis_result.json")
    analysis_data: Dict[str, Any] = {}
    if os.path.isfile(analysis_path):
        try:
            with open(analysis_path, "r", encoding="utf-8") as fh:
                analysis_data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            pass

    mode = str(analysis_data.get("mode_used", "")).lower()
    if mode and mode != "quant":
        report.warnings.append(
            f"Backtest runner is designed for Quant mode but run uses '{mode}'. "
            "Results may not be meaningful."
        )

    # Monotonic timestamp captured at the start of this pipeline invocation.
    # Used by ``_read_metrics_from_result_file`` in the failure
    # branch to discriminate between a fresh result file written by *this*
    # subprocess (mtime newer than this anchor) and a stale file left over
    # from a previous successful run inside the same code_dir (mtime older).
    # ``time.monotonic()`` is preferred over ``time.time()`` because it
    # cannot run backwards under NTP corrections.
    import time as _time
    run_started_monotonic = _time.monotonic()
    run_started_wall = _time.time()

    # ── Step 1: Locate code ──────────────────────────────────────────────────
    code_dir = _find_code_dir(run_dir)
    if code_dir is None:
        report.errors.append("No code/ directory found in run output.")
        return report

    entrypoint = _find_backtest_entry(code_dir)
    if entrypoint is None:
        report.errors.append(
            "No backtest entrypoint found. Expected backtest.py, run_backtest.py, or main.py."
        )
        return report

    # ── Step 2: Auto-prepare data ────────────────────────────────────────────
    symbol = _env_str("BACKTEST_SYMBOL", "SPY")
    data_source_pref = _env_str("BACKTEST_DATA_SOURCE", "auto")
    period = _env_str("BACKTEST_PERIOD", "auto")
    interval = _env_str("BACKTEST_INTERVAL", "auto")

    # Resolve effective interval for report metadata
    effective_interval = interval
    if effective_interval == "auto":
        detected_tf = _detect_timeframe_from_code(code_dir)
        effective_interval = detected_tf or "1d"
    report.data_interval = effective_interval

    if _has_data_file(code_dir):
        report.data_source = "existing"
        report.data_rows = _count_csv_rows(code_dir)
        # Mirror the symbol-detection logic from the prepare_data branch so
        # data_symbol is populated even when the strategy ships its own data file.
        _detected_sym_ex = _detect_symbol_from_code(code_dir)
        report.data_symbol = _detected_sym_ex if (_detected_sym_ex and symbol == "SPY") else symbol
    else:
        try:
            source_label, data_file, row_count = prepare_data(
                code_dir,
                symbol=symbol,
                data_source=data_source_pref,
                period=period,
                interval=interval,
                fallback_rows=data_rows,
            )
            report.data_source = source_label
            # Replicate prepare_data's auto-detection logic so data_symbol
            # records the EFFECTIVE ticker (auto-detected from code) rather
            # than the raw env-default "SPY".  prepare_data does not return
            # effective_symbol, so we re-derive it here with the same rule.
            _detected_sym = _detect_symbol_from_code(code_dir)
            _effective_sym = _detected_sym if (_detected_sym and symbol == "SPY") else symbol
            report.data_symbol = _effective_sym if source_label != "project_provider" else ""
            report.data_rows = row_count
            report._data_file = data_file  # passed to env_overrides, not os.environ
        except Exception as exc:
            report.errors.append(f"Failed to prepare backtest data: {exc}")
            return report

    # ── Step 3: Baseline backtest ────────────────────────────────────────────
    # Guard initial_capital against NaN/Inf before converting to string:
    # str(float('nan')) → "nan" which silently propagates to the subprocess.
    _safe_capital = (
        initial_capital
        if isinstance(initial_capital, (int, float)) and math.isfinite(initial_capital)
        else BACKTEST_INITIAL_CAPITAL
    )
    env_overrides = {
        "BACKTEST_INITIAL_CAPITAL": str(_safe_capital),
    }
    if report.data_source != "existing":
        # Prefer the file path returned by prepare_data; fall back to convention
        data_file_path = getattr(report, "_data_file", None) or os.path.join(
            code_dir, "data", "sample_data.csv",
        )
        if os.path.isfile(data_file_path):
            env_overrides["BACKTEST_DATA_FILE"] = data_file_path

    original_problem = str(analysis_data.get("summary", ""))

    returncode, stdout, stderr = _run_backtest_subprocess(
        code_dir, entrypoint, timeout=timeout, env_overrides=env_overrides,
    )

    # ── Step 4: Fix loop if baseline fails ───────────────────────────────────
    fix_round = 0
    while returncode != 0 and llm is not None and fix_round < fix_max_rounds:
        fix_round += 1
        report.warnings.append(f"Backtest failed (exit={returncode}), fix round {fix_round}")
        error_text = (stderr or stdout or "Unknown error")[:3000]
        if _try_llm_fix(llm, code_dir, entrypoint, error_text, original_problem):
            returncode, stdout, stderr = _run_backtest_subprocess(
                code_dir, entrypoint, timeout=timeout, env_overrides=env_overrides,
            )
        else:
            report.errors.append(f"LLM fix round {fix_round} produced no valid code.")
            break

    report.fix_rounds_used = fix_round

    if returncode != 0:
        report.errors.append(
            f"Backtest failed with exit code {returncode}. "
            f"stderr: {(stderr or '')[:500]}"
        )
        # Fail-loud: do NOT parse partial output via
        # ``_parse_backtest_output(stdout, stderr, code_dir)`` from a crashed
        # run, because the regex would extract "metrics" from stack-trace
        # strings ("Sharpe ratio: 0.0", etc.) and downstream LLM agents
        # would consume those phantom numbers as if they were real backtest
        # data.  When the subprocess returncode is non-zero, the most
        # honest answer is "no metrics".  Result-file lookups still happen
        # via the JSON-only path below, gated on a fresh-write timestamp,
        # so legitimate runs that wrote a JSON before raising are honoured.
        baseline_from_json = _read_metrics_from_result_file(
            code_dir, written_after_wall=run_started_wall
        )
        if baseline_from_json is not None:
            report.baseline_metrics = baseline_from_json
        _persist_report(run_dir, report)
        return report

    baseline_metrics = _parse_backtest_output(stdout, stderr, code_dir)
    report.baseline_metrics = baseline_metrics
    report.success = True

    # ── Step 5: Parameter optimisation ───────────────────────────────────────
    param_space = _detect_param_space(code_dir)
    if param_space:
        report.parameter_search = param_search

        # Shared evaluation function: run subprocess and return metric value.
        def _evaluate_params(combo_params: Dict[str, Any]) -> Optional[float]:
            param_env = _params_to_env(combo_params)
            param_env.update(env_overrides)
            rc, out, err = _run_backtest_subprocess(
                code_dir, entrypoint, timeout=timeout, env_overrides=param_env,
            )
            combo = ParameterCombo(params=combo_params)
            if rc == 0:
                combo.metrics = _parse_backtest_output(out, err, code_dir)
                combo.success = True
                mv = combo.metrics.metric_value(target_metric)
            else:
                combo.error = (err or out or "unknown error")[:200]
                combo.success = False
                mv = None
            report.all_combos.append(combo)
            return mv

        best_value: Optional[float] = None
        best_combo: Optional[ParameterCombo] = None

        # Direction-aware comparison: lower-is-better metrics (drawdown,
        # volatility) are minimised; all other metrics (Sharpe, Sortino,
        # total_return, …) are maximised.  Both the Bayesian re-scan and the
        # grid/random loops below must use the same logic — previously they
        # always maximised, which would silently select the *worst* combo when
        # optimising for drawdown or volatility.
        _minimize = target_metric in _LOWER_IS_BETTER

        if param_search == "bayesian":
            # Bayesian optimisation via Optuna (falls back to random search
            # internally if optuna is not installed).
            n_trials = _env_int("BACKTEST_BAYESIAN_N_TRIALS", BACKTEST_BAYESIAN_N_TRIALS)
            best_params_dict, _ = _run_optuna_optimization(
                param_space,
                _evaluate_params,
                n_trials=n_trials,
                target_metric=target_metric,
            )
            # Determine best_combo from collected all_combos (populated by the
            # _evaluate_params callback during the Optuna study).  We re-scan
            # here instead of using best_params_dict directly so that we can
            # retrieve the full ParameterCombo object (including metrics).
            for combo in report.all_combos:
                if not combo.success:
                    continue
                mv = combo.metrics.metric_value(target_metric) if combo.metrics else None
                if mv is not None:
                    if best_value is None or (_minimize and mv < best_value) or (not _minimize and mv > best_value):
                        best_value = mv
                        best_combo = combo
        else:
            combos = _build_param_combos(param_space, strategy=param_search, max_combos=max_combos)

            for combo_params in combos:
                mv = _evaluate_params(combo_params)
                # Find the combo we just appended
                evaluated_combo = report.all_combos[-1] if report.all_combos else None
                if mv is not None and evaluated_combo is not None:
                    if best_value is None or (_minimize and mv < best_value) or (not _minimize and mv > best_value):
                        best_value = mv
                        best_combo = evaluated_combo

        report.combos_evaluated = len(report.all_combos)
        if best_combo:
            report.best_params = best_combo.params
            report.best_metrics = best_combo.metrics

    # ── Step 6: Persist report ───────────────────────────────────────────────
    _persist_report(run_dir, report)
    return report


def _count_csv_rows(code_dir: str) -> int:
    """Count rows of the first CSV file found in code_dir or data/ subdirectory."""
    search_dirs = [code_dir, os.path.join(code_dir, "data")]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for f in os.listdir(d):
                if f.lower().endswith(".csv"):
                    fpath = os.path.join(d, f)
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                            # Count non-empty lines minus header
                            count = sum(1 for line in fh if line.strip()) - 1
                            return max(count, 0)
                    except OSError:
                        pass
        except OSError:
            pass
    return 0


def _persist_report(run_dir: str, report: BacktestReport) -> None:
    """Write the backtest report as JSON and Markdown to the run directory."""
    # JSON report — write atomically via tmp + os.replace so that a ValueError
    # from json.dump (e.g. residual NaN/Inf not caught by to_dict()) does NOT
    # truncate the existing file to 0 bytes and silently corrupt all downstream
    # analytics (tearsheet, transaction_cost_model, signal_analyzer).
    json_path = os.path.join(run_dir, "backtest_report.json")
    json_tmp = json_path + ".tmp"
    try:
        with open(json_tmp, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(json_tmp, json_path)
    except (OSError, ValueError):
        try:
            os.remove(json_tmp)
        except OSError:
            pass

    # Markdown analysis (atomic write via tmp + os.replace)
    md_path = os.path.join(run_dir, "backtest_analysis.md")
    _md_tmp = md_path + ".tmp"
    try:
        md_content = _generate_analysis_markdown(report)
        with open(_md_tmp, "w", encoding="utf-8") as fh:
            fh.write(md_content)
        os.replace(_md_tmp, md_path)
    except Exception:
        # Catch all exceptions (not just OSError): _generate_analysis_markdown can
        # raise AttributeError/KeyError; f.write() can raise UnicodeEncodeError.
        # Either leaves the .tmp file on disk without cleanup.
        try:
            os.unlink(_md_tmp)
        except OSError:
            pass

    # Best params as .env snippet (atomic write via tmp + os.replace)
    if report.best_params:
        env_path = os.path.join(run_dir, "best_params.env")
        _env_tmp = env_path + ".tmp"
        try:
            with open(_env_tmp, "w", encoding="utf-8") as fh:
                fh.write("# Best parameters found by backtest runner\n")
                for k, v in report.best_params.items():
                    if isinstance(v, float) and not math.isfinite(v):
                        continue  # skip NaN/Inf — not representable as env values
                    fh.write(f"BACKTEST_PARAM_{k.upper()}={v}\n")
            os.replace(_env_tmp, env_path)
        except Exception:
            try:
                os.unlink(_env_tmp)
            except OSError:
                pass
