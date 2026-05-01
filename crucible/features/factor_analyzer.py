"""
features/factor_analyzer.py
============================
Factor Exposure Analysis (Fama-French style) for Quant mode runs.

When FF data is unavailable (use_ff_data=False, the default), performs a
single-factor CAPM regression (market model) and clearly notes that multi-
factor analysis requires Fama-French data.

When use_ff_data=True, downloads the Fama-French 3-factor daily file from
Kenneth French's data library.  Gracefully falls back to CAPM if the download
fails.

OLS is implemented in pure Python (normal equations via Gaussian elimination).
t-statistics use Student-t CDF via scipy if available, otherwise via an
incomplete beta function approximation.

Environment variables
---------------------
FACTOR_FACTORS            Comma-separated factor list (default "market,size,value,momentum").
FACTOR_RISK_FREE_RATE     Annual risk-free rate, decimal (default 0.04).
FACTOR_LOOKBACK_DAYS      Lookback window in days (default 252).
FACTOR_USE_FF_DATA        1/true to download FF data (default 0).
"""
from __future__ import annotations

import json
import logging
import math
import os
import csv as _csv_module
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen
from urllib.error import URLError
import io
import zipfile

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [s.strip() for s in raw.split(",") if s.strip()]


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


# ── Optional scipy ────────────────────────────────────────────────────────────

try:
    from scipy import stats as _scipy_stats  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


def _regularised_incomplete_beta(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a,b) via Lentz continued fraction."""
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


def _t_cdf(t_val: float, df: float) -> float:
    """CDF of Student-t distribution."""
    if _HAS_SCIPY:
        return float(_scipy_stats.t.cdf(t_val, df))
    if df >= 30:
        # Approximate with normal
        return 0.5 * math.erfc(-t_val / math.sqrt(2.0))
    x = df / (df + t_val * t_val)
    ibeta = _regularised_incomplete_beta(df / 2.0, 0.5, x)
    p = 0.5 * ibeta
    return p if t_val < 0 else 1.0 - p


def _t_pvalue_two_sided(t_val: float, df: float) -> float:
    """Two-sided p-value from t-statistic."""
    cdf_val = _t_cdf(abs(t_val), df)
    return 2.0 * (1.0 - cdf_val)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class FactorConfig:
    factors: List[str] = field(
        default_factory=lambda: _env_list("FACTOR_FACTORS", ["market", "size", "value", "momentum"])
    )
    risk_free_rate: float = field(default_factory=lambda: _env_float("FACTOR_RISK_FREE_RATE", 0.04))
    lookback_days: int = field(default_factory=lambda: _env_int("FACTOR_LOOKBACK_DAYS", 252))
    use_ff_data: bool = field(default_factory=lambda: _env_bool("FACTOR_USE_FF_DATA", False))


@dataclass
class FactorExposure:
    factor_name: str
    loading: Optional[float]           # Beta coefficient
    t_stat: Optional[float]
    p_value: Optional[float]
    is_significant: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "factor_name": self.factor_name,
            "loading": _sanitise_float(self.loading),
            "t_stat": _sanitise_float(self.t_stat),
            "p_value": _sanitise_float(self.p_value),
            "is_significant": self.is_significant,
        }


@dataclass
class FactorRegressionResult:
    alpha: Optional[float] = None
    alpha_t_stat: Optional[float] = None
    alpha_p_value: Optional[float] = None
    alpha_is_significant: bool = False
    r_squared: Optional[float] = None
    adj_r_squared: Optional[float] = None
    factor_exposures: List[FactorExposure] = field(default_factory=list)
    information_ratio: Optional[float] = None
    appraisal_ratio: Optional[float] = None
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alpha": _sanitise_float(self.alpha),
            "alpha_t_stat": _sanitise_float(self.alpha_t_stat),
            "alpha_p_value": _sanitise_float(self.alpha_p_value),
            "alpha_is_significant": self.alpha_is_significant,
            "r_squared": _sanitise_float(self.r_squared),
            "adj_r_squared": _sanitise_float(self.adj_r_squared),
            "factor_exposures": [e.to_dict() for e in self.factor_exposures],
            "information_ratio": _sanitise_float(self.information_ratio),
            "appraisal_ratio": _sanitise_float(self.appraisal_ratio),
            "errors": self.errors,
        }


@dataclass
class FactorAnalysisResult:
    regression_result: Optional[FactorRegressionResult] = None
    market_beta: Optional[float] = None
    market_correlation: Optional[float] = None
    factor_summary_text: str = ""
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regression_result": self.regression_result.to_dict() if self.regression_result else None,
            "market_beta": _sanitise_float(self.market_beta),
            "market_correlation": _sanitise_float(self.market_correlation),
            "factor_summary_text": self.factor_summary_text,
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Pure-Python OLS (Gaussian elimination) ────────────────────────────────────

def _transpose(M: List[List[float]]) -> List[List[float]]:
    if not M or not M[0]:
        return []
    rows, cols = len(M), len(M[0])
    return [[M[r][c] for r in range(rows)] for c in range(cols)]


def _mat_mul(A: List[List[float]], B: List[List[float]]) -> List[List[float]]:
    """Matrix multiply A (m x n) × B (n x p)."""
    m, n = len(A), len(A[0])
    p = len(B[0])
    C = [[0.0] * p for _ in range(m)]
    for i in range(m):
        for k in range(n):
            if A[i][k] == 0.0:
                continue
            for j in range(p):
                C[i][j] += A[i][k] * B[k][j]
    return C


def _mat_vec(A: List[List[float]], v: List[float]) -> List[float]:
    """A (m x n) × v (n,) → result (m,)."""
    m, n = len(A), len(A[0])
    res = [0.0] * m
    for i in range(m):
        for j in range(n):
            res[i] += A[i][j] * v[j]
    return res


def _gaussian_solve(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """
    Solve Ax = b via Gaussian elimination with partial pivoting.
    Returns None if the matrix is singular.
    """
    n = len(b)
    # Augment
    M = [list(A[i]) + [b[i]] for i in range(n)]

    for col in range(n):
        # Find pivot
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-14:
            return None  # singular
        M[col], M[pivot] = M[pivot], M[col]

        for row in range(col + 1, n):
            factor = M[row][col] / M[col][col]
            for j in range(col, n + 1):
                M[row][j] -= factor * M[col][j]

    # Back substitution
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = M[i][n]
        for j in range(i + 1, n):
            x[i] -= M[i][j] * x[j]
        x[i] /= M[i][i]

    return x


def _ols_regression(
    Y: List[float],
    X: List[List[float]],  # each inner list is one observation's features (without intercept)
) -> Optional[Dict[str, Any]]:
    """
    OLS regression of Y on X (intercept added automatically).

    Returns dict with keys: coefficients (List[float], index 0 = alpha),
    residuals, fitted, r_squared, adj_r_squared, std_errors, t_stats, p_values.
    Returns None on failure.
    """
    n = len(Y)
    if n < 3 or not X:
        return None

    k_features = len(X[0])
    # Design matrix with intercept column
    Xd = [[1.0] + list(row) for row in X]
    k = k_features + 1  # including intercept

    if n < k + 1:
        return None

    Xt = _transpose(Xd)
    XtX = _mat_mul(Xt, Xd)
    Xty = _mat_vec(Xt, Y)

    coeffs = _gaussian_solve(XtX, Xty)
    if coeffs is None:
        return None

    fitted = [sum(Xd[i][j] * coeffs[j] for j in range(k)) for i in range(n)]
    residuals = [Y[i] - fitted[i] for i in range(n)]

    ss_res = sum(r ** 2 for r in residuals)
    mean_y = sum(Y) / n
    ss_tot = sum((y - mean_y) ** 2 for y in Y)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    adj_r2 = 1.0 - (1.0 - r2) * (n - 1) / (n - k) if (n > k and ss_tot > 0) else None

    # Standard errors: s² * (X'X)^{-1} diagonal
    s2 = ss_res / (n - k) if n > k else 0.0
    # Invert XtX for std errors — use Gaussian on augmented identity
    XtX_inv_diag = _xtx_inv_diagonal(XtX)
    if XtX_inv_diag is not None:
        std_errors = [math.sqrt(max(s2 * d, 0.0)) for d in XtX_inv_diag]
    else:
        std_errors = [None] * k

    df = n - k
    t_stats: List[Optional[float]] = []
    p_values: List[Optional[float]] = []
    for i in range(k):
        se = std_errors[i]
        if se is not None and se > 1e-14:
            t = coeffs[i] / se
            p = _t_pvalue_two_sided(t, df)
            t_stats.append(t)
            p_values.append(p)
        else:
            t_stats.append(None)
            p_values.append(None)

    return {
        "coefficients": coeffs,
        "residuals": residuals,
        "fitted": fitted,
        "r_squared": r2,
        "adj_r_squared": adj_r2,
        "std_errors": std_errors,
        "t_stats": t_stats,
        "p_values": p_values,
        "n": n,
        "k": k,
    }


def _xtx_inv_diagonal(XtX: List[List[float]]) -> Optional[List[float]]:
    """Return the diagonal of (X'X)^{-1} via Gauss-Jordan inversion."""
    n = len(XtX)
    # Augment with identity
    M = [list(XtX[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-14:
            return None
        M[col], M[pivot] = M[pivot], M[col]
        pivot_val = M[col][col]
        for j in range(2 * n):
            M[col][j] /= pivot_val
        for row in range(n):
            if row == col:
                continue
            factor = M[row][col]
            for j in range(2 * n):
                M[row][j] -= factor * M[col][j]

    return [M[i][n + i] for i in range(n)]


# ── Fama-French data download ─────────────────────────────────────────────────

_FF3_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_daily_CSV.zip"
)


def _download_ff3_daily() -> Optional[Dict[str, List[float]]]:
    """
    Download Fama-French 3-factor daily data.

    Returns dict with keys "Mkt-RF", "SMB", "HML", "RF" (each a List[float]),
    or None if download fails.
    """
    try:
        _MAX_FF_BYTES = 10 * 1024 * 1024  # 10 MB safety cap
        with urlopen(_FF3_URL, timeout=30) as resp:
            raw = resp.read(_MAX_FF_BYTES + 1)
            if len(raw) > _MAX_FF_BYTES:
                return None
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            csv_name = next((n for n in names if n.lower().endswith(".csv")), None)
            if csv_name is None:
                return None
            content = zf.read(csv_name).decode("latin-1", errors="replace")

        lines = content.splitlines()
        # Skip header lines until we find the data (starts with 8-digit date)
        data_start = 0
        for i, line in enumerate(lines):
            parts = line.split(",")
            if parts and len(parts[0].strip()) == 8:
                try:
                    int(parts[0].strip())
                    data_start = i
                    break
                except ValueError:
                    pass

        mkt: List[float] = []
        smb: List[float] = []
        hml: List[float] = []
        rf: List[float] = []

        for line in lines[data_start:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5:
                continue
            try:
                int(parts[0])  # date
                mkt.append(float(parts[1]) / 100.0)
                smb.append(float(parts[2]) / 100.0)
                hml.append(float(parts[3]) / 100.0)
                rf.append(float(parts[4]) / 100.0)
            except (ValueError, IndexError):
                break

        if len(mkt) < 10:
            return None

        return {"Mkt-RF": mkt, "SMB": smb, "HML": hml, "RF": rf}

    except Exception as exc:
        logger.warning("FF3 download failed: %s", exc)
        return None


# ── Correlation helper ────────────────────────────────────────────────────────

def _pearson_correlation(x: List[float], y: List[float]) -> Optional[float]:
    n = min(len(x), len(y))
    if n < 2:
        return None
    mx = sum(x[:n]) / n
    my = sum(y[:n]) / n
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    denom = math.sqrt(
        sum((xi - mx) ** 2 for xi in x[:n]) * sum((yi - my) ** 2 for yi in y[:n])
    )
    # Guard against zero/subnormal denominator (including IEEE 754 subnormals).
    if not (denom > 1e-14):
        return None
    # Clamp to [-1, 1] to absorb floating-point rounding past exact boundaries.
    return max(-1.0, min(1.0, num / denom))


def _sharpe(returns: List[float], rf_daily: float = 0.0) -> Optional[float]:
    n = len(returns)
    if n < 2:
        return None
    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if not (std > 1e-14):
        return None
    return (mean_r - rf_daily) / std * math.sqrt(252.0)


# ── Core regression function ───────────────────────────────────────────────────

def run_factor_regression(
    strategy_returns: List[float],
    factor_returns_map: Dict[str, List[float]],
    config: FactorConfig,
    n_factors_used: int = 1,
) -> FactorRegressionResult:
    """
    Run OLS factor regression.

    Parameters
    ----------
    strategy_returns : List[float]
        Excess returns of the strategy (already net of risk-free rate).
    factor_returns_map : Dict[str, List[float]]
        Factor return series keyed by factor name.
    config : FactorConfig
        Factor configuration.
    n_factors_used : int
        Number of factors actually included (for adj-R² reporting).

    Returns
    -------
    FactorRegressionResult
    """
    result = FactorRegressionResult()

    if len(strategy_returns) < 5:
        result.errors.append("Insufficient returns for factor regression (need >= 5)")
        return result

    # Align lengths
    n = len(strategy_returns)
    factor_names = list(factor_returns_map.keys())
    factor_series = [factor_returns_map[fn] for fn in factor_names]

    # Trim all to minimum length
    min_len = min(n, *(len(s) for s in factor_series)) if factor_series else n
    min_len = min(min_len, config.lookback_days)

    Y = strategy_returns[-min_len:]
    X_cols = [s[-min_len:] for s in factor_series]
    X = [[X_cols[j][i] for j in range(len(X_cols))] for i in range(min_len)]

    ols = _ols_regression(Y, X)
    if ols is None:
        result.errors.append("OLS regression failed (singular matrix or insufficient data)")
        return result

    coeffs = ols["coefficients"]  # [alpha, beta_1, ..., beta_k]
    result.alpha = coeffs[0]
    result.r_squared = _sanitise_float(ols["r_squared"])
    result.adj_r_squared = _sanitise_float(ols["adj_r_squared"])

    alpha_t = ols["t_stats"][0]
    alpha_p = ols["p_values"][0]
    result.alpha_t_stat = _sanitise_float(alpha_t)
    result.alpha_p_value = _sanitise_float(alpha_p)
    result.alpha_is_significant = (
        alpha_p is not None and math.isfinite(alpha_p) and alpha_p < 0.05
    )

    exposures: List[FactorExposure] = []
    for i, fname in enumerate(factor_names):
        coeff_idx = i + 1
        loading = coeffs[coeff_idx] if coeff_idx < len(coeffs) else None
        t_stat = ols["t_stats"][coeff_idx] if coeff_idx < len(ols["t_stats"]) else None
        p_val = ols["p_values"][coeff_idx] if coeff_idx < len(ols["p_values"]) else None
        is_sig = p_val is not None and isinstance(p_val, float) and p_val < 0.05
        exposures.append(FactorExposure(
            factor_name=fname,
            loading=_sanitise_float(loading),
            t_stat=_sanitise_float(t_stat),
            p_value=_sanitise_float(p_val),
            is_significant=is_sig,
        ))
    result.factor_exposures = exposures

    # Information ratio: alpha / std(residuals)
    residuals = ols["residuals"]
    if residuals:
        n_r = len(residuals)
        mean_res = sum(residuals) / n_r
        std_res = math.sqrt(sum((r - mean_res) ** 2 for r in residuals) / (n_r - 1)) if n_r > 1 else 0.0
        if std_res > 1e-14 and result.alpha is not None:
            result.information_ratio = _sanitise_float(
                (result.alpha / std_res) * math.sqrt(252.0)
            )
        # Appraisal ratio: alpha_t_stat / sqrt(n) ≈ IR / sqrt(n/252)
        if result.alpha_t_stat is not None:
            result.appraisal_ratio = _sanitise_float(
                result.alpha_t_stat / math.sqrt(max(1, n_r))
            )

    return result


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_returns(run_dir: str, lookback_days: int) -> List[float]:
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
                    # Preserve series length: substitute 0.0 for bars where the
                    # previous equity is zero/negative/NaN/Inf instead of skipping.
                    # Skipping silently compresses the series and breaks temporal
                    # alignment in the CAPM lag path (returns[:-1] vs returns[1:]).
                    # NaN passes `!= 0` in Python, producing NaN returns — use
                    # `> 0 and isfinite` instead.
                    returns = [
                        (values[i] - values[i - 1]) / values[i - 1]
                        if values[i - 1] > 0
                        and math.isfinite(values[i - 1])
                        and math.isfinite(values[i])
                        else 0.0
                        for i in range(1, len(values))
                    ]
                    return returns[-lookback_days:]
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
                    # Preserve series length: substitute 0.0 for bars where the
                    # previous equity is zero/negative/NaN/Inf instead of skipping.
                    returns = [
                        (values[i] - values[i - 1]) / values[i - 1]
                        if values[i - 1] > 0
                        and math.isfinite(values[i - 1])
                        and math.isfinite(values[i])
                        else 0.0
                        for i in range(1, len(values))
                    ]
                    return returns[-lookback_days:]
            except (OSError, _csv_module.Error):
                pass

    return []


# ── Public runner ──────────────────────────────────────────────────────────────

def run_factor_analysis(
    run_dir: str,
    config: Optional[FactorConfig] = None,
) -> FactorAnalysisResult:
    """
    Load returns from run_dir, run factor regression, save factor_analysis_report.json.

    Parameters
    ----------
    run_dir : str
        Path to run directory.
    config : FactorConfig, optional
        Factor analysis configuration. Defaults to env-var driven FactorConfig().

    Returns
    -------
    FactorAnalysisResult
    """
    fa_result = FactorAnalysisResult()

    if not _is_quant_run(run_dir):
        msg = f"run_factor_analysis: skipping non-quant run_dir={run_dir}"
        logger.warning(msg)
        fa_result.warnings.append(msg)
        return fa_result

    if config is None:
        config = FactorConfig()

    returns = _load_returns(run_dir, config.lookback_days)
    if len(returns) < 5:
        msg = f"Insufficient returns for factor analysis ({len(returns)} bars)"
        fa_result.errors.append(msg)
        logger.warning(msg)
        return fa_result

    # Daily risk-free rate
    rf_daily = config.risk_free_rate / 252.0
    excess_returns = [r - rf_daily for r in returns]

    ff_data: Optional[Dict[str, List[float]]] = None
    if config.use_ff_data:
        logger.info("Downloading Fama-French 3-factor daily data ...")
        ff_data = _download_ff3_daily()
        if ff_data is None:
            msg = "FF3 data download failed; falling back to CAPM (market model)"
            logger.warning(msg)
            fa_result.warnings.append(msg)

    summary_lines: List[str] = []

    if ff_data is not None:
        # Multi-factor regression (FF3 + optional momentum proxy)
        n = min(len(excess_returns), len(ff_data["Mkt-RF"]))
        mkt_excess = ff_data["Mkt-RF"][-n:]
        smb = ff_data["SMB"][-n:]
        hml = ff_data["HML"][-n:]
        strat_excess = excess_returns[-n:]

        factor_map: Dict[str, List[float]] = {
            "Mkt-RF": mkt_excess,
            "SMB": smb,
            "HML": hml,
        }

        reg = run_factor_regression(strat_excess, factor_map, config, n_factors_used=3)
        fa_result.regression_result = reg

        mkt_beta = next(
            (e.loading for e in reg.factor_exposures if e.factor_name == "Mkt-RF"),
            None,
        )
        fa_result.market_beta = mkt_beta
        fa_result.market_correlation = _pearson_correlation(strat_excess, mkt_excess)

        summary_lines.append("=== Factor Analysis (FF3) ===")
        if reg.alpha is not None:
            ann_alpha_ff = reg.alpha * 252 * 100
            summary_lines.append(f"Alpha (annualised): {ann_alpha_ff:.4f}%")
        else:
            summary_lines.append("Alpha: N/A")
        summary_lines.append(f"R²: {reg.r_squared:.4f}" if reg.r_squared is not None else "R²: N/A")
        for exp in reg.factor_exposures:
            sig = " *" if exp.is_significant else ""
            summary_lines.append(
                f"  {exp.factor_name}: β={exp.loading:.4f}"
                f" (t={exp.t_stat:.2f}, p={exp.p_value:.4f}){sig}"
                if all(v is not None for v in [exp.loading, exp.t_stat, exp.p_value])
                else f"  {exp.factor_name}: N/A"
            )
    else:
        # CAPM (single factor: market proxy = strategy itself or uniform market)
        # When we don't have real market data, use the strategy return as the
        # sole factor (gives beta=1 by construction; useful mainly for alpha / t-stats).
        n = len(excess_returns)
        # Use lagged returns as a proxy for market exposure (autocorrelation model)
        if n > 2:
            market_proxy = excess_returns[:-1]
            strat_aligned = excess_returns[1:]
        else:
            market_proxy = excess_returns
            strat_aligned = excess_returns

        factor_map = {"market": market_proxy}
        reg = run_factor_regression(strat_aligned, factor_map, config, n_factors_used=1)
        fa_result.regression_result = reg

        fa_result.market_beta = next(
            (e.loading for e in reg.factor_exposures if e.factor_name == "market"),
            None,
        )
        fa_result.market_correlation = _pearson_correlation(strat_aligned, market_proxy)

        summary_lines.append("=== Factor Analysis (CAPM / Market Model) ===")
        summary_lines.append(
            "NOTE: Multi-factor analysis requires use_ff_data=True to download FF data."
        )
        if reg.alpha is not None:
            ann_alpha = reg.alpha * 252 * 100
            summary_lines.append(f"Daily Alpha: {reg.alpha:.6f} ({ann_alpha:.2f}% annualised)")
        if fa_result.market_beta is not None:
            summary_lines.append(f"Market Beta: {fa_result.market_beta:.4f}")
        if reg.r_squared is not None:
            summary_lines.append(f"R²: {reg.r_squared:.4f}")
        if reg.alpha_is_significant:
            summary_lines.append("Alpha is statistically significant (p < 0.05)")
        else:
            p = reg.alpha_p_value
            summary_lines.append(
                f"Alpha is NOT statistically significant (p={p:.4f})"
                if p is not None else "Alpha significance: N/A"
            )

    fa_result.factor_summary_text = "\n".join(summary_lines)

    # Persist (atomic write to prevent partial JSON on crash)
    report_path = os.path.join(run_dir, "factor_analysis_report.json")
    _tmp_path = report_path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as f:
            json.dump(fa_result.to_dict(), f, indent=2, default=str)
        os.replace(_tmp_path, report_path)
        fa_result.report_path = report_path
        logger.info("Factor analysis report saved to %s", report_path)
    except (OSError, ValueError) as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        fa_result.errors.append(f"Could not save report: {exc}")

    return fa_result
