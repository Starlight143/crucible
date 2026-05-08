"""
features/tearsheet.py
======================
Strategy Tearsheet Generator — produces a rich Markdown (and basic HTML) report
integrating all available analytics reports from a run directory.

No matplotlib required: all charts are rendered as Unicode/ASCII.

Sections included (when data available):
  1. Header — strategy name, date range, data source
  2. Summary stats table (Sharpe, Sortino, Calmar, Max DD, Win Rate, etc.)
  3. Monthly returns heatmap (year × month ASCII table)
  4. Cumulative return chart (80-char ASCII line chart)
  5. Top N drawdown periods
  6. Walk-forward summary  (if walk_forward_report.json exists)
  7. Regime performance    (if regime_report.json exists)
  8. Transaction cost      (if transaction_cost_report.json exists)
  9. Statistical significance (if quant_analytics_report.json exists)

Environment variables
---------------------
TEARSHEET_MAX_DRAWDOWN_PERIODS   Number of drawdown periods to list (default 5).
"""
from __future__ import annotations

import json
import logging
import math
import os
import csv as _csv_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Env helpers ───────────────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


def _env_str(name: str, default: str) -> str:
    return _env.env_str(name, default)


def _env_bool(name: str, default: bool) -> bool:
    return _env.env_bool(name, default)


# ── Mode isolation (tearsheet works for all modes) ────────────────────────────

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
class TearsheetConfig:
    include_monthly_returns: bool = field(
        default_factory=lambda: _env_bool("TEARSHEET_MONTHLY_RETURNS", True)
    )
    include_drawdown_periods: bool = field(
        default_factory=lambda: _env_bool("TEARSHEET_DRAWDOWN_PERIODS", True)
    )
    include_trade_analysis: bool = field(
        default_factory=lambda: _env_bool("TEARSHEET_TRADE_ANALYSIS", True)
    )
    max_drawdown_periods: int = field(
        default_factory=lambda: _env_int("TEARSHEET_MAX_DRAWDOWN_PERIODS", 5)
    )


@dataclass
class MonthlyReturn:
    year: int
    month: int       # 1-12
    return_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {"year": self.year, "month": self.month, "return_pct": self.return_pct}


@dataclass
class DrawdownPeriod:
    start_ts: str
    end_ts: str
    drawdown_pct: float
    duration_days: int
    recovery_days: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "drawdown_pct": _sanitise_float(self.drawdown_pct),
            "duration_days": self.duration_days,
            "recovery_days": self.recovery_days,
        }


@dataclass
class TearsheetResult:
    markdown_text: str = ""
    html_text: str = ""
    monthly_returns: List[MonthlyReturn] = field(default_factory=list)
    drawdown_periods: List[DrawdownPeriod] = field(default_factory=list)
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monthly_returns": [m.to_dict() for m in self.monthly_returns],
            "drawdown_periods": [d.to_dict() for d in self.drawdown_periods],
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_equity_and_timestamps(run_dir: str) -> Tuple[List[float], List[str]]:
    """Load equity curve and timestamps from run_dir."""
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
                        fv = float(item)
                        if math.isfinite(fv):
                            values.append(fv)
                            timestamps.append("")
                    elif isinstance(item, dict):
                        v = item.get("equity", item.get("value", item.get("close")))
                        t = item.get("timestamp", item.get("date", item.get("ts", "")))
                        if v is not None:
                            try:
                                fv = float(v)
                                if not math.isfinite(fv):
                                    continue
                                values.append(fv)
                                timestamps.append(str(t))
                            except (ValueError, TypeError):
                                pass
                if len(values) >= 2:
                    return values, timestamps
        except (OSError, json.JSONDecodeError):
            pass

    data_dir = os.path.join(run_dir, "code", "data")
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if not fname.lower().endswith(".csv"):
                continue
            try:
                values = []
                timestamps = []
                with open(os.path.join(data_dir, fname), "r", encoding="utf-8", newline="") as f:
                    reader = _csv_module.DictReader(f)
                    for row in reader:
                        for col in ("equity", "close", "Close", "price", "Price"):
                            if col in row:
                                try:
                                    fv = float(row[col])
                                    if not math.isfinite(fv):
                                        continue
                                    values.append(fv)
                                    ts_val = row.get("date", row.get("Date",
                                                     row.get("timestamp", "")))
                                    timestamps.append(str(ts_val))
                                    break
                                except (ValueError, TypeError):
                                    continue
                if len(values) >= 2:
                    return values, timestamps
            except (OSError, _csv_module.Error):
                pass

    return [], []


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_backtest_report(run_dir: str) -> Optional[Dict[str, Any]]:
    return _load_json(os.path.join(run_dir, "backtest_report.json"))


# ── Metrics computation ────────────────────────────────────────────────────────

def _equity_to_returns(equity: List[float]) -> List[float]:
    if len(equity) < 2:
        return []
    # Guard positive and finite: negative equity produces mathematically valid
    # but semantically wrong returns (leveraged strategies should not have
    # account equity go below zero); NaN/Inf propagate into Sharpe/Calmar.
    return [
        (equity[i] - equity[i - 1]) / equity[i - 1]
        if equity[i - 1] > 0
        and math.isfinite(equity[i - 1])
        and math.isfinite(equity[i])
        else 0.0
        for i in range(1, len(equity))
    ]


def _sharpe(returns: List[float], risk_free_rate: float = 0.0) -> Optional[float]:
    n = len(returns)
    if n < 2:
        return None
    rf_per_period = risk_free_rate / 252.0
    excess = [r - rf_per_period for r in returns]
    mean_ex = sum(excess) / n
    var = sum((r - mean_ex) ** 2 for r in excess) / (n - 1)
    std = math.sqrt(var)
    # ``<= 0.0`` lets IEEE 754 subnormals (e.g. 5e-324) through; the
    # subsequent division then explodes to ~1e+300 and yields a fictitious
    # Sharpe.  Guard with the project's standard subnormal threshold.
    if not (std > 1e-14):
        return None
    return (mean_ex / std) * math.sqrt(252.0)


def _sortino(returns: List[float], risk_free_rate: float = 0.0) -> Optional[float]:
    n = len(returns)
    if n < 2:
        return None
    rf_per_period = risk_free_rate / 252.0
    excess = [r - rf_per_period for r in returns]
    mean_ex = sum(excess) / n
    neg_rets = [r for r in excess if r < 0]
    if not neg_rets:
        return None
    # Downside deviation uses total N per the original Sortino (1991) definition
    # (not Bessel-corrected N-1 used in _sharpe). This is intentional.
    downside_var = sum(r ** 2 for r in neg_rets) / n
    downside_std = math.sqrt(downside_var)
    # Subnormal-safe guard: see ``_sharpe`` above.
    if not (downside_std > 1e-14):
        return None
    return (mean_ex / downside_std) * math.sqrt(252.0)


def _max_drawdown_and_periods(
    equity: List[float],
    timestamps: List[str],
    n_top: int = 5,
) -> Tuple[float, List[DrawdownPeriod]]:
    """Return (max_drawdown_pct, list of top DrawdownPeriod)."""
    if len(equity) < 2:
        return 0.0, []

    # Track all drawdown periods
    peak_val = equity[0]
    peak_idx = 0
    in_dd = False
    dd_start_idx = 0
    all_periods: List[DrawdownPeriod] = []

    for i in range(1, len(equity)):
        if equity[i] >= peak_val:
            if in_dd:
                # Record recovery
                trough_idx = _find_trough_idx(equity, dd_start_idx, i)
                dd_pct = ((peak_val - min(equity[dd_start_idx:i])) / peak_val * 100.0) if peak_val > 0 else 0.0
                all_periods.append(DrawdownPeriod(
                    start_ts=timestamps[dd_start_idx] if timestamps and dd_start_idx < len(timestamps) else str(dd_start_idx),
                    end_ts=timestamps[i] if timestamps and i < len(timestamps) else str(i),
                    drawdown_pct=dd_pct,
                    # duration_days = peak → trough (industry standard)
                    duration_days=trough_idx - dd_start_idx,
                    # recovery_days = trough → recovery-to-prior-peak
                    recovery_days=i - trough_idx,
                ))
                in_dd = False
            peak_val = equity[i]
            peak_idx = i
        else:
            if not in_dd:
                in_dd = True
                dd_start_idx = peak_idx

    # If still in drawdown at end
    if in_dd:
        dd_pct = ((peak_val - min(equity[dd_start_idx:])) / peak_val * 100.0) if peak_val > 0 else 0.0
        all_periods.append(DrawdownPeriod(
            start_ts=timestamps[dd_start_idx] if timestamps and dd_start_idx < len(timestamps) else str(dd_start_idx),
            end_ts=timestamps[-1] if timestamps else str(len(equity) - 1),
            drawdown_pct=dd_pct,
            duration_days=len(equity) - 1 - dd_start_idx,
            recovery_days=None,
        ))

    all_periods.sort(key=lambda p: p.drawdown_pct, reverse=True)
    max_dd = all_periods[0].drawdown_pct if all_periods else 0.0

    # Also compute overall max drawdown directly
    peak = equity[0]
    global_max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > global_max_dd:
            global_max_dd = dd

    return global_max_dd * 100.0, all_periods[:n_top]


def _find_trough_idx(equity: List[float], start: int, end: int) -> int:
    """Find index of minimum equity between start and end."""
    if start >= end:
        return start
    min_val = equity[start]
    min_idx = start
    for i in range(start, end):
        if equity[i] < min_val:
            min_val = equity[i]
            min_idx = i
    return min_idx


def _calmar(returns: List[float], max_dd_pct: float) -> Optional[float]:
    if not returns or max_dd_pct <= 0:
        return None
    n = len(returns)
    # Geometric (compound) annualisation: CAGR = (total_growth)^(252/n) - 1.
    # Arithmetic mean × 252 overstates the true compound return by roughly
    # σ²/2 × 252 (Jensen's inequality), biasing Calmar high for volatile
    # strategies.  The max_dd denominator is already computed on compound equity,
    # so the numerator must use the same compounding methodology.
    total = 1.0
    for r in returns:
        total *= (1.0 + r)
    if total <= 0:
        return None
    ann_return = (total ** (252.0 / n) - 1.0) * 100.0
    # Guard against Inf/NaN in ann_return before dividing: total**(252/n)
    # overflows for very large total with tiny n (e.g. a single 100× bar).
    if not math.isfinite(ann_return):
        return None
    calmar = ann_return / max_dd_pct
    return calmar if math.isfinite(calmar) else None


def _win_rate_profit_factor(returns: List[float]) -> Tuple[Optional[float], Optional[float]]:
    if not returns:
        return None, None
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    win_rate = len(wins) / len(returns)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else None
    return win_rate, pf


def _compute_monthly_returns(
    equity: List[float],
    timestamps: List[str],
) -> List[MonthlyReturn]:
    """Aggregate equity curve into monthly return objects."""
    if len(equity) < 2 or not any(timestamps):
        # No timestamps — return empty
        return []

    monthly: Dict[Tuple[int, int], Tuple[float, float]] = {}  # (year, month) → (start_eq, end_eq)

    for i, ts in enumerate(timestamps):
        if not ts:
            continue
        try:
            # Try to parse date from timestamp string
            ts_clean = ts[:10]  # YYYY-MM-DD
            dt = datetime.strptime(ts_clean, "%Y-%m-%d")
            key = (dt.year, dt.month)
            if key not in monthly:
                monthly[key] = (equity[i], equity[i])
            else:
                monthly[key] = (monthly[key][0], equity[i])
        except (ValueError, IndexError):
            continue

    result: List[MonthlyReturn] = []
    for (year, month), (start, end) in sorted(monthly.items()):
        if start > 0 and math.isfinite(start) and math.isfinite(end):
            ret_pct = (end - start) / start * 100.0
            result.append(MonthlyReturn(year=year, month=month, return_pct=ret_pct))

    return result


# ── ASCII chart functions ──────────────────────────────────────────────────────

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _monthly_heatmap(monthly_returns: List[MonthlyReturn]) -> str:
    """Render monthly returns as a year × month ASCII table."""
    if not monthly_returns:
        return "_No monthly return data available._\n"

    years = sorted(set(m.year for m in monthly_returns))
    # Build lookup
    lookup: Dict[Tuple[int, int], float] = {
        (m.year, m.month): m.return_pct for m in monthly_returns
    }

    header = "| Year |" + "".join(f" {ab:>6} |" for ab in _MONTH_ABBR) + " Annual |"
    sep = "|------|" + "--------|" * 12 + "---------|"
    lines = [header, sep]

    for year in years:
        annual = 1.0
        cells: List[str] = []
        for month in range(1, 13):
            val = lookup.get((year, month))
            if val is not None:
                annual *= (1.0 + val / 100.0)
                sign = "+" if val >= 0 else ""
                cell = f"{sign}{val:5.1f}%"
            else:
                cell = "      "
            cells.append(cell)
        annual_pct = (annual - 1.0) * 100.0
        # If any month returned NaN, annual_pct propagates NaN which breaks
        # f-string formatting and renders as "   nan%" in the Markdown table.
        if not math.isfinite(annual_pct):
            annual_pct = 0.0
        sign = "+" if annual_pct >= 0 else ""
        annual_str = f"{sign}{annual_pct:6.1f}%"
        row = f"| {year} |" + "".join(f" {c:>7}|" for c in cells) + f" {annual_str:>8}|"
        lines.append(row)

    return "\n".join(lines) + "\n"


def _ascii_line_chart(
    values: List[float],
    width: int = 78,
    height: int = 12,
    title: str = "",
) -> str:
    """Render a time series as an 80-char-wide ASCII line chart."""
    if not values:
        return "_No data._\n"

    # height - 1 appears in denominators below; guard against height < 2
    # so that a single-row chart does not raise ZeroDivisionError.
    if height < 2:
        height = 2

    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        max_v = min_v + 1.0

    # Downsample to width
    n = len(values)
    if n > width:
        step = n / width
        sampled = [values[min(n - 1, int(round(i * step)))] for i in range(width)]
    else:
        sampled = list(values)
        width = len(sampled)

    # Build grid (height rows × width cols)
    grid = [[" "] * width for _ in range(height)]

    for col, val in enumerate(sampled):
        row = int(round((max_v - val) / (max_v - min_v) * (height - 1)))
        row = max(0, min(height - 1, row))
        grid[row][col] = "●"

    lines: List[str] = []
    if title:
        lines.append(title)

    for row_idx, row_data in enumerate(grid):
        # Y-axis label at leftmost column
        label_val = max_v - (max_v - min_v) * row_idx / (height - 1)
        label = f"{label_val:8.2f} |"
        lines.append(label + "".join(row_data))

    x_axis = " " * 10 + "+" + "-" * width
    lines.append(x_axis)

    return "\n".join(lines) + "\n"


# ── HTML conversion ────────────────────────────────────────────────────────────

def _markdown_to_basic_html(md: str) -> str:
    """Very basic Markdown → HTML conversion for tearsheet."""
    import html as _html_mod
    lines = md.split("\n")
    html_lines: List[str] = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<style>body{font-family:monospace;max-width:960px;margin:0 auto;padding:1em;}",
        "table{border-collapse:collapse;width:100%;}",
        "th,td{border:1px solid #ccc;padding:4px 8px;text-align:right;}",
        "th{background:#f0f0f0;}",
        "pre{background:#f8f8f8;padding:1em;overflow-x:auto;}",
        "h1{border-bottom:2px solid #333;} h2{border-bottom:1px solid #ccc;}",
        "</style></head><body><pre>",
    ]

    for line in lines:
        escaped = _html_mod.escape(line)
        html_lines.append(escaped)

    html_lines.append("</pre></body></html>")
    return "\n".join(html_lines)


# ── Tearsheet generation ───────────────────────────────────────────────────────

def generate_tearsheet(
    run_dir: str,
    config: Optional[TearsheetConfig] = None,
) -> TearsheetResult:
    """
    Generate a full strategy tearsheet from all available reports in run_dir.

    Parameters
    ----------
    run_dir : str
        Path to run directory containing backtest_report.json and optional
        analytical sub-reports.
    config : TearsheetConfig, optional
        Tearsheet configuration.

    Returns
    -------
    TearsheetResult
    """
    result = TearsheetResult()
    is_quant = _is_quant_run(run_dir)

    if config is None:
        config = TearsheetConfig()

    equity, timestamps = _load_equity_and_timestamps(run_dir)
    returns = _equity_to_returns(equity)

    br = _load_backtest_report(run_dir)
    wf_report = _load_json(os.path.join(run_dir, "walk_forward_report.json"))
    regime_report = _load_json(os.path.join(run_dir, "regime_report.json"))
    tc_report = _load_json(os.path.join(run_dir, "transaction_cost_report.json"))
    qa_report = _load_json(os.path.join(run_dir, "quant_analytics_report.json"))
    mc_report = _load_json(os.path.join(run_dir, "monte_carlo_report.json"))

    md_parts: List[str] = []

    def _md_cell(value: str) -> str:
        """Sanitize a string for use inside a Markdown table cell.

        Pipe characters break the table structure; newlines terminate the row.
        Both are replaced with safe equivalents so user-controlled JSON strings
        (strategy names, scenario labels, etc.) cannot corrupt the tearsheet.
        """
        return (
            str(value)
            .replace("|", "&#124;")
            .replace("\r", "")
            .replace("\n", " ")
        )

    # ── 1. Header ─────────────────────────────────────────────────────────────
    run_name = os.path.basename(os.path.abspath(run_dir))
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    data_source = ""
    data_symbol = ""
    if br:
        data_source = br.get("data_source", "")
        data_symbol = br.get("data_symbol", "")

    ts_range = ""
    if timestamps:
        ts_clean = [t for t in timestamps if t]
        if ts_clean:
            ts_range = f"{ts_clean[0]} → {ts_clean[-1]}"

    md_parts.append(f"# Strategy Tearsheet — {run_name}\n")
    md_parts.append(f"**Generated:** {now_str}  ")
    if data_symbol:
        md_parts.append(f"**Symbol:** {data_symbol}  ")
    if data_source:
        md_parts.append(f"**Data Source:** {data_source}  ")
    if ts_range:
        md_parts.append(f"**Date Range:** {ts_range}  ")
    md_parts.append("\n---\n")

    # ── 2. Summary stats ───────────────────────────────────────────────────────
    md_parts.append("## Summary Statistics\n")

    if returns:
        sharpe_val = _sharpe(returns)
        sortino_val = _sortino(returns)
        max_dd_pct, dd_periods = _max_drawdown_and_periods(
            equity, timestamps, config.max_drawdown_periods
        )
        result.drawdown_periods = dd_periods
        calmar_val = _calmar(returns, max_dd_pct)
        win_rate, pf = _win_rate_profit_factor(returns)

        n = len(equity)
        total_return_pct: Optional[float] = None
        if n >= 2 and equity[0] > 0:
            total_return_pct = (equity[-1] - equity[0]) / equity[0] * 100.0

        ann_return: Optional[float] = None
        if returns:
            # Geometric (CAGR) annualisation: consistent with Calmar's CAGR.
            # Arithmetic mean × 252 over-estimates returns when volatility is
            # high; CAGR correctly compounds per-bar returns to an annual rate.
            _n_r = len(returns)
            _total_r = 1.0
            for _r in returns:
                _total_r *= (1.0 + _r)
            if _total_r > 0:
                ann_return = (_total_r ** (252.0 / _n_r) - 1.0) * 100.0

        trade_count: Optional[int] = None
        if br:
            for sec in ("baseline_metrics", "best_metrics"):
                tc_sec = br.get(sec, {})
                if isinstance(tc_sec, dict) and "trade_count" in tc_sec:
                    try:
                        trade_count = int(tc_sec["trade_count"])
                        break
                    except (ValueError, TypeError):
                        pass

        def _fmt(v: Optional[float], fmt: str = ".4f", suffix: str = "") -> str:
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                return "N/A"
            return f"{v:{fmt}}{suffix}"

        rows = [
            ("Metric", "Value"),
            ("---", "---"),
            ("Total Return", _fmt(total_return_pct, ".2f", "%")),
            ("Annual Return (est.)", _fmt(ann_return, ".2f", "%")),
            ("Sharpe Ratio", _fmt(sharpe_val, ".4f")),
            ("Sortino Ratio", _fmt(sortino_val, ".4f")),
            ("Calmar Ratio", _fmt(calmar_val, ".4f")),
            ("Max Drawdown", _fmt(max_dd_pct, ".2f", "%")),
            ("Win Rate", _fmt(win_rate * 100 if win_rate is not None else None, ".2f", "%")),
            ("Profit Factor", _fmt(pf, ".4f")),
            ("Trade Count", str(trade_count) if trade_count is not None else "N/A"),
            ("Data Bars", str(len(equity))),
        ]

        md_parts.append("| Metric | Value |\n|--------|-------|\n")
        for k, v in rows[2:]:
            md_parts.append(f"| {k} | {v} |\n")
        md_parts.append("\n")
    else:
        md_parts.append("_No equity data available._\n\n")
        max_dd_pct = 0.0
        dd_periods = []

    # ── 3. Monthly returns heatmap ─────────────────────────────────────────────
    if config.include_monthly_returns:
        md_parts.append("## Monthly Returns Heatmap\n\n")
        if equity and any(timestamps):
            monthly_rets = _compute_monthly_returns(equity, timestamps)
            result.monthly_returns = monthly_rets
            md_parts.append(_monthly_heatmap(monthly_rets))
        else:
            monthly_rets_from_returns: List[MonthlyReturn] = []
            if returns:
                # No timestamps — assign synthetic month indices
                bars_per_month = 21
                for chunk_idx in range(0, len(returns), bars_per_month):
                    chunk = returns[chunk_idx: chunk_idx + bars_per_month]
                    if not chunk:
                        continue
                    total = 1.0
                    for r in chunk:
                        total *= (1.0 + r)
                    synthetic_year = 2020 + chunk_idx // (bars_per_month * 12)
                    synthetic_month = (chunk_idx // bars_per_month) % 12 + 1
                    monthly_rets_from_returns.append(MonthlyReturn(
                        year=synthetic_year,
                        month=synthetic_month,
                        return_pct=(total - 1.0) * 100.0,
                    ))
                result.monthly_returns = monthly_rets_from_returns
                md_parts.append(
                    "_Note: timestamps not available; months are synthetic._\n\n"
                )
                md_parts.append(_monthly_heatmap(monthly_rets_from_returns))
            else:
                md_parts.append("_No data for monthly heatmap._\n\n")

    # ── 4. Cumulative return chart ─────────────────────────────────────────────
    md_parts.append("## Cumulative Equity Curve\n\n")
    if equity:
        # Normalise to 1.0; guard against zero, NaN, and Inf as base.
        base = equity[0] if equity[0] > 0 and math.isfinite(equity[0]) else 1.0
        normalised = [v / base for v in equity]
        md_parts.append("```\n")
        md_parts.append(_ascii_line_chart(normalised, width=78, height=12,
                                          title="Equity (normalised to 1.0)"))
        md_parts.append("```\n\n")
    else:
        md_parts.append("_No equity data._\n\n")

    # ── 5. Drawdown periods ────────────────────────────────────────────────────
    if config.include_drawdown_periods:
        md_parts.append(f"## Top {config.max_drawdown_periods} Drawdown Periods\n\n")
        if dd_periods:
            md_parts.append(
                "| # | Start | End | Drawdown | Duration (bars) | Recovery (bars) |\n"
                "|---|-------|-----|----------|-----------------|----------------|\n"
            )
            for i, ddp in enumerate(dd_periods, 1):
                rec = str(ddp.recovery_days) if ddp.recovery_days is not None else "Ongoing"
                md_parts.append(
                    f"| {i} | {ddp.start_ts} | {ddp.end_ts} | "
                    f"-{ddp.drawdown_pct:.2f}% | {ddp.duration_days} | {rec} |\n"
                )
            md_parts.append("\n")
        else:
            md_parts.append("_No significant drawdown periods identified._\n\n")

    # ── 6. Walk-forward summary ────────────────────────────────────────────────
    if is_quant and wf_report:
        md_parts.append("## Walk-Forward Validation\n\n")
        avg_is = wf_report.get("avg_is_sharpe")
        avg_oos = wf_report.get("avg_oos_sharpe")
        decay = wf_report.get("sharpe_decay_ratio")
        consistency = wf_report.get("consistency_score")

        def _fv(v: Any, fmt: str = ".4f") -> str:
            if v is None:
                return "N/A"
            try:
                _f = float(v)
            except (ValueError, TypeError):
                return "N/A"
            if not math.isfinite(_f):
                return "N/A"
            return f"{_f:{fmt}}"

        md_parts.append(
            f"| Metric | Value |\n|--------|-------|\n"
            f"| Avg IS Sharpe | {_fv(avg_is)} |\n"
            f"| Avg OOS Sharpe | {_fv(avg_oos)} |\n"
            f"| Sharpe Decay Ratio (OOS/IS) | {_fv(decay)} |\n"
            f"| Consistency Score | {_fv(consistency, '.2%') if consistency is not None else 'N/A'} |\n"
        )

        folds = wf_report.get("folds", [])
        if folds:
            md_parts.append("\n**Fold Details:**\n\n")
            md_parts.append("| Fold | IS Sharpe | OOS Sharpe | OOS Return | OOS MaxDD |\n")
            md_parts.append("|------|-----------|------------|------------|-----------|\n")
            for fold in folds:
                is_m = fold.get("is_metrics", {}) or {}
                oos_m = fold.get("oos_metrics", {}) or {}
                md_parts.append(
                    f"| {_md_cell(fold.get('fold_idx', '?'))} "
                    f"| {_fv(is_m.get('sharpe_ratio'))} "
                    f"| {_fv(oos_m.get('sharpe_ratio'))} "
                    f"| {_fv(oos_m.get('total_return_pct'), '.2f')}% "
                    f"| {_fv(oos_m.get('max_drawdown_pct'), '.2f')}% |\n"
                )
        md_parts.append("\n")

    # ── 7. Regime performance ──────────────────────────────────────────────────
    if regime_report:
        md_parts.append("## Regime Performance\n\n")
        current = regime_report.get("current_regime", "N/A")
        md_parts.append(f"**Current Regime:** {_md_cell(current)}\n\n")
        perf = regime_report.get("regime_performance", {})
        if perf:
            md_parts.append(
                "| Regime | Avg Return | Avg Volatility | Sharpe Est. | # Bars |\n"
                "|--------|------------|----------------|-------------|--------|\n"
            )
            for label, stats in sorted(perf.items()):
                avg_r = stats.get("avg_return")
                avg_v = stats.get("avg_volatility")
                shp = stats.get("sharpe_estimate")
                n_b = stats.get("n_bars", "N/A")

                def _pf(v: Any) -> str:
                    if v is None:
                        return "N/A"
                    try:
                        _f = float(v)
                    except (ValueError, TypeError):
                        return "N/A"
                    if not math.isfinite(_f):
                        return "N/A"
                    return f"{_f:.4f}"

                md_parts.append(
                    f"| {_md_cell(label)} | {_pf(avg_r)} | {_pf(avg_v)} | {_pf(shp)} | {_md_cell(n_b)} |\n"
                )
        md_parts.append("\n")

    # ── 8. Transaction cost analysis ──────────────────────────────────────────
    if tc_report:
        md_parts.append("## Transaction Cost Analysis\n\n")
        base = tc_report.get("base_metrics", {}) or {}

        def _fv2(v: Any, fmt: str = ".4f") -> str:
            if v is None:
                return "N/A"
            try:
                _f = float(v)
            except (ValueError, TypeError):
                return "N/A"
            if not math.isfinite(_f):
                return "N/A"
            return f"{_f:{fmt}}"

        gross_sh = base.get("gross_sharpe")
        net_sh = base.get("net_sharpe")
        gross_ret = base.get("gross_return_pct")
        net_ret = base.get("net_return_pct")
        drag = base.get("total_cost_drag_pct")

        md_parts.append(
            f"| Metric | Value |\n|--------|-------|\n"
            f"| Gross Sharpe | {_fv2(gross_sh)} |\n"
            f"| Net Sharpe (after costs) | {_fv2(net_sh)} |\n"
            f"| Gross Return | {_fv2(gross_ret, '.2f')}% |\n"
            f"| Net Return (after costs) | {_fv2(net_ret, '.2f')}% |\n"
            f"| Cost Drag | {_fv2(drag, '.2f')}% |\n"
        )

        be_comm = tc_report.get("breakeven_commission_pct")
        be_slip = tc_report.get("breakeven_slippage_pct")
        if be_comm is not None:
            md_parts.append(f"| Breakeven Commission | {_fv2(be_comm, '.4f')} |\n")
        if be_slip is not None:
            md_parts.append(f"| Breakeven Slippage | {_fv2(be_slip, '.4f')} |\n")

        breakdown = base.get("cost_breakdown", {}) or {}
        if breakdown:
            cpt = breakdown.get("cost_per_trade")
            md_parts.append(f"| Cost per Trade | {_fv2(cpt, '.4f')}% |\n")

        md_parts.append("\n")

    # ── 9. Statistical significance ────────────────────────────────────────────
    if is_quant and qa_report:
        sig = qa_report.get("significance", {}) or {}
        if sig:
            md_parts.append("## Statistical Significance\n\n")

            def _fv3(v: Any, fmt: str = ".4f") -> str:
                if v is None:
                    return "N/A"
                try:
                    _f = float(v)
                except (ValueError, TypeError):
                    return "N/A"
                if not math.isfinite(_f):
                    return "N/A"
                return f"{_f:{fmt}}"

            observed = sig.get("observed_sharpe")
            p_val = sig.get("p_value")
            is_sig = sig.get("is_significant", False)
            ci_lo = sig.get("sharpe_ci_lower")
            ci_hi = sig.get("sharpe_ci_upper")
            dsr = sig.get("deflated_sharpe_ratio")
            dsr_p = sig.get("dsr_p_value")

            sig_badge = "**SIGNIFICANT** ✓" if is_sig else "Not significant"

            md_parts.append(
                f"| Metric | Value |\n|--------|-------|\n"
                f"| Observed Sharpe | {_fv3(observed)} |\n"
                f"| Permutation p-value | {_fv3(p_val)} |\n"
                f"| Significance (α=0.05) | {sig_badge} |\n"
                f"| 95% Sharpe CI | [{_fv3(ci_lo)}, {_fv3(ci_hi)}] |\n"
                f"| Deflated Sharpe Ratio | {_fv3(dsr)} |\n"
                f"| DSR p-value | {_fv3(dsr_p)} |\n"
            )
            md_parts.append("\n")

    # ── Monte Carlo snippet ────────────────────────────────────────────────────
    if mc_report:
        stats = mc_report.get("simulation_stats", {}) or {}
        if stats:
            md_parts.append("## Monte Carlo Simulation Summary\n\n")

            def _fv4(v: Any, fmt: str = ".4f") -> str:
                if v is None:
                    return "N/A"
                try:
                    _f = float(v)
                except (ValueError, TypeError):
                    return "N/A"
                if not math.isfinite(_f):
                    return "N/A"
                return f"{_f:{fmt}}"

            md_parts.append(
                f"| Metric | Value |\n|--------|-------|\n"
                f"| Mean Final Equity | {_fv4(stats.get('mean_final_equity'))} |\n"
                f"| Median Final Equity | {_fv4(stats.get('median_final_equity'))} |\n"
                f"| VaR (5%) | {_fv4(stats.get('var_5pct'), '.4f')} |\n"
                f"| CVaR (5%) | {_fv4(stats.get('cvar_5pct'), '.4f')} |\n"
                f"| Max Simulated Drawdown | {_fv4(stats.get('max_simulated_drawdown_pct'), '.2f')}% |\n"
                f"| Prob(Loss) | {_fv4(stats.get('prob_loss'), '.2%')} |\n"
                f"| Prob(DD > 20%) | {_fv4(stats.get('prob_drawdown_gt_20pct'), '.2%')} |\n"
            )

            stress_results = mc_report.get("stress_results", [])
            if stress_results:
                md_parts.append("\n**Stress Scenarios:**\n\n")
                md_parts.append(
                    "| Scenario | Portfolio Return | Max Drawdown |\n"
                    "|----------|------------------|--------------|\n"
                )
                for sc in stress_results:
                    md_parts.append(
                        f"| {_md_cell(sc.get('name', '?'))} "
                        f"| {_fv4(sc.get('portfolio_return_pct'), '.2f')}% "
                        f"| {_fv4(sc.get('max_drawdown_pct'), '.2f')}% |\n"
                    )
            md_parts.append("\n")

    # ── Footer ─────────────────────────────────────────────────────────────────
    md_parts.append("---\n")
    md_parts.append(f"_Tearsheet generated by Crucible on {now_str}_\n")

    markdown_text = "".join(md_parts)
    html_text = _markdown_to_basic_html(markdown_text)

    result.markdown_text = markdown_text
    result.html_text = html_text

    # ── Save files ─────────────────────────────────────────────────────────────
    md_path = os.path.join(run_dir, "strategy_tearsheet.md")
    html_path = os.path.join(run_dir, "strategy_tearsheet.html")

    _md_tmp = md_path + ".tmp"
    try:
        with open(_md_tmp, "w", encoding="utf-8") as f:
            f.write(markdown_text)
        os.replace(_md_tmp, md_path)
        logger.info("Tearsheet Markdown saved to %s", md_path)
    except Exception as exc:
        # Catch all exceptions (not just OSError) so that UnicodeEncodeError or
        # other write failures are recorded and the .tmp file is cleaned up.
        result.errors.append(f"Could not save Markdown: {exc}")
        try:
            os.unlink(_md_tmp)
        except OSError:
            pass

    _html_tmp = html_path + ".tmp"
    try:
        with open(_html_tmp, "w", encoding="utf-8") as f:
            f.write(html_text)
        os.replace(_html_tmp, html_path)
        logger.info("Tearsheet HTML saved to %s", html_path)
    except Exception as exc:
        result.errors.append(f"Could not save HTML: {exc}")
        try:
            os.unlink(_html_tmp)
        except OSError:
            pass

    result.report_path = md_path
    return result
