"""
features/dynamic_correlation.py
=================================
Rolling Correlation Matrix + PCA Decomposition for the Crucible pipeline.

Computes time-varying pairwise Pearson correlations between asset/strategy
return series and decomposes the full-period correlation structure into
principal components — all in pure Python stdlib without NumPy or Pandas.

Usage::

    from crucible.features.dynamic_correlation import (
        DynamicCorrelationConfig,
        run_dynamic_correlation_single,
    )

    result = run_dynamic_correlation_single("/path/to/run_dir")
    print(f"Diversification score: {result.diversification_score:.3f}")
    print(f"PC1 explains: {result.pca_components[0].explained_variance_ratio:.1%}")
"""
from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# ── Env helpers ───────────────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_bool(name: str, default: bool) -> bool:
    return _env.env_bool(name, default)

def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)

def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default, finite_only=True)

def _env_str(name: str, default: str) -> str:
    return _env.env_str_passthrough(name, default)


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class DynamicCorrelationConfig:
    window: int = 60
    step: int = 5
    min_observations: int = 30
    n_pca_components: int = 3


@dataclass
class CorrelationSnapshot:
    timestamp: str
    matrix: Dict[str, Dict[str, float]]   # sym_row → sym_col → corr
    avg_correlation: float
    max_correlation: float
    min_correlation: float


@dataclass
class PCAComponent:
    component_idx: int                  # 0-indexed
    explained_variance_ratio: float
    cumulative_variance: float
    loadings: Dict[str, float]          # asset/label → weight in this PC


@dataclass
class DynamicCorrelationResult:
    snapshots: List[CorrelationSnapshot] = field(default_factory=list)
    current_correlation: Dict[str, Dict[str, float]] = field(default_factory=dict)
    avg_correlation_series: List[Dict[str, Any]] = field(default_factory=list)
    pca_components: List[PCAComponent] = field(default_factory=list)
    total_variance_explained: float = 0.0
    diversification_score: float = 0.0
    report_path: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── Math helpers ──────────────────────────────────────────────────────────────

def _mean(series: List[float]) -> float:
    return sum(series) / len(series) if series else 0.0

def _std(series: List[float], ddof: int = 1) -> float:
    n = len(series)
    if n <= ddof:
        return 0.0
    mu = _mean(series)
    v = sum((x - mu) ** 2 for x in series) / (n - ddof)
    # v1.1.0 fourth-pass: tighten subnormal guard per CLAUDE.md rule
    # (``not (x > 1e-14)``).  Previously ``v > 0.0`` admitted IEEE 754
    # subnormals which then divided into surrounding code paths.
    return math.sqrt(v) if v > 1e-14 else 0.0

def _pearson_r(x: List[float], y: List[float]) -> float:
    # v1.1.0 fifth-pass (G-9): pair-wise NaN drop before Pearson.
    # ``_compute_returns`` now emits NaN sentinels for invalid bars;
    # without this filter NaN propagates through mean / num and the
    # later ``math.isfinite(raw)`` short-circuit silently maps to 0.0,
    # losing the genuine correlation signal on the surviving bars.
    n0 = min(len(x), len(y))
    if n0 < 2:
        return 0.0
    paired = [(x[i], y[i]) for i in range(n0) if math.isfinite(x[i]) and math.isfinite(y[i])]
    n = len(paired)
    if n < 2:
        return 0.0
    x = [p[0] for p in paired]
    y = [p[1] for p in paired]
    mx, my = _mean(x), _mean(y)
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if not (sx > 1e-14) or not (sy > 1e-14):
        return 0.0
    raw = num / (sx * sy)
    # NaN-aware clamp.  ``max(-1, min(1, nan))`` is
    # order-dependent in Python (``min(1.0, nan) == 1.0`` but
    # ``min(nan, 1.0) == nan``) so the previous one-line clamp could
    # silently leak NaN into downstream metric tables when *any*
    # intermediate produced NaN — typically when the input series
    # contained NaN values that propagated through ``_mean`` and
    # ``num``.  Now we explicitly verify the result is finite first
    # and treat non-finite as "no measurable correlation".
    if not math.isfinite(raw):
        return 0.0
    return max(-1.0, min(1.0, raw))

def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))

def _norm(v: List[float]) -> float:
    return math.sqrt(sum(x * x for x in v))

def _normalize(v: List[float]) -> List[float]:
    n = _norm(v)
    if not (n > 1e-14):
        return v[:]
    return [x / n for x in v]


# ── Correlation matrix ────────────────────────────────────────────────────────

def _correlation_matrix(
    returns_dict: Dict[str, List[float]],
    symbols: List[str],
) -> Tuple[Dict[str, Dict[str, float]], float, float, float]:
    """
    Compute the N×N Pearson correlation matrix for the given (aligned) return
    series.  Returns (matrix_dict, avg_corr, max_corr, min_corr).
    """
    matrix: Dict[str, Dict[str, float]] = {}
    off_diag: List[float] = []

    for si in symbols:
        matrix[si] = {}
        for sj in symbols:
            if si == sj:
                matrix[si][sj] = 1.0
            elif sj in matrix and si in matrix.get(sj, {}):
                r = matrix[sj][si]
                matrix[si][sj] = r
            else:
                r = _pearson_r(returns_dict[si], returns_dict[sj])
                matrix[si][sj] = r
                if si != sj:
                    off_diag.append(r)

    avg_corr = _mean(off_diag) if off_diag else 0.0
    max_corr = max(off_diag) if off_diag else 0.0
    min_corr = min(off_diag) if off_diag else 0.0
    return matrix, avg_corr, max_corr, min_corr


# ── Rolling correlation ───────────────────────────────────────────────────────

def _rolling_correlation_matrix(
    returns_dict: Dict[str, List[float]],
    timestamps: List[str],
    window: int,
    step: int,
) -> List[CorrelationSnapshot]:
    """
    Slide a window of *window* observations over aligned return series,
    stepping by *step* periods.

    Returns a list of CorrelationSnapshot (one per window position).
    """
    if step <= 0:
        step = 1  # sentinel: advance by at least one bar to prevent infinite loop
    symbols = sorted(returns_dict.keys())
    n_obs = min(len(v) for v in returns_dict.values())
    snapshots: List[CorrelationSnapshot] = []

    t = window
    while t <= n_obs:
        window_returns: Dict[str, List[float]] = {
            sym: returns_dict[sym][t - window: t]
            for sym in symbols
        }
        matrix, avg_c, max_c, min_c = _correlation_matrix(window_returns, symbols)

        ts_label = timestamps[t - 1] if t - 1 < len(timestamps) else str(t)
        snapshots.append(CorrelationSnapshot(
            timestamp=ts_label,
            matrix=matrix,
            avg_correlation=avg_c,
            max_correlation=max_c,
            min_correlation=min_c,
        ))
        t += step

    return snapshots


# ── PCA via power iteration + deflation ──────────────────────────────────────

def _mat_mul_vec(matrix: List[List[float]], vec: List[float]) -> List[float]:
    """Multiply square matrix by column vector."""
    n = len(matrix)
    return [sum(matrix[i][j] * vec[j] for j in range(n)) for i in range(n)]


def _covariance_matrix(
    data: List[List[float]],
) -> Tuple[List[List[float]], List[float]]:
    """
    Compute the n_features × n_features covariance matrix from a
    (n_samples × n_features) data matrix.

    Returns (cov_matrix, column_means).
    Uses ddof=1.
    """
    n_samples = len(data)
    if n_samples < 2:
        n_features = len(data[0]) if data else 0
        return [[0.0] * n_features for _ in range(n_features)], [0.0] * n_features

    n_features = len(data[0])
    col_means = [_mean([data[i][j] for i in range(n_samples)]) for j in range(n_features)]

    cov = [[0.0] * n_features for _ in range(n_features)]
    for i in range(n_features):
        for j in range(i, n_features):
            c = sum(
                (data[s][i] - col_means[i]) * (data[s][j] - col_means[j])
                for s in range(n_samples)
            ) / (n_samples - 1)
            cov[i][j] = c
            cov[j][i] = c

    return cov, col_means


def _power_iteration(
    matrix: List[List[float]],
    max_iter: int = 500,
    tol: float = 1e-6,
) -> Tuple[float, List[float]]:
    """
    Compute the dominant eigenvalue/eigenvector of a symmetric matrix via
    the power iteration method.

    Returns (eigenvalue, eigenvector).
    Raises ValueError if the matrix is degenerate.

    v1.1.0: the convergence test is now **relative** instead of absolute.
    Previously ``abs(new - old) < tol`` (with tol=1e-6) accepted any change
    smaller than 1e-6 — too loose for tiny eigenvalues (1e-3: 0.1 %
    relative) and too tight for large eigenvalues (1e+6: never converges
    within max_iter).  The relative form ``abs(new - old) < tol *
    max(abs(new), 1.0)`` scales with the eigenvalue magnitude so both
    extremes converge consistently.
    """
    n = len(matrix)
    if n == 0:
        raise ValueError("Empty matrix.")

    # Initialise with a non-zero vector
    v: List[float] = [1.0 / math.sqrt(n)] * n

    eigenvalue = 0.0
    for _ in range(max_iter):
        v_new = _mat_mul_vec(matrix, v)
        norm_new = _norm(v_new)
        if norm_new < 1e-14:
            raise ValueError("Power iteration: zero vector encountered.")
        v_new_norm = [x / norm_new for x in v_new]

        # Rayleigh quotient
        mv = _mat_mul_vec(matrix, v_new_norm)
        eigenvalue_new = _dot(v_new_norm, mv)

        # Relative convergence: scale tolerance by the current eigenvalue
        # magnitude (floored at 1.0 so the test is at least as strict as
        # the original absolute version when the eigenvalue is small).
        scale = max(abs(eigenvalue_new), 1.0)
        if abs(eigenvalue_new - eigenvalue) < tol * scale:
            return eigenvalue_new, v_new_norm
        eigenvalue = eigenvalue_new
        v = v_new_norm

    return eigenvalue, v


def _deflate(
    matrix: List[List[float]],
    eigenvalue: float,
    eigenvector: List[float],
) -> List[List[float]]:
    """
    Hotelling's deflation: remove the contribution of one eigenvector.
    A' = A - λ·(v·v^T)
    """
    n = len(matrix)
    return [
        [
            matrix[i][j] - eigenvalue * eigenvector[i] * eigenvector[j]
            for j in range(n)
        ]
        for i in range(n)
    ]


def _pca_pure_python(
    matrix: List[List[float]],
    n_components: int,
) -> List[PCAComponent]:
    """
    Compute PCA of a (n_samples × n_features) data matrix using power
    iteration and deflation.

    matrix: rows are observations, columns are features (assets/labels).
    n_components: number of principal components to extract.

    Returns a list of PCAComponent sorted by explained_variance_ratio desc.
    """
    if not matrix or not matrix[0]:
        return []

    n_samples = len(matrix)
    n_features = len(matrix[0])
    n_components = min(n_components, n_features, n_samples - 1)
    if n_components <= 0:
        return []

    # Build covariance matrix
    try:
        cov, _ = _covariance_matrix(matrix)
    except Exception as exc:
        _log.debug("PCA covariance failed: %s", exc)
        return []

    # Total variance = trace of covariance matrix.
    # Subnormal-safe guard: ``<= 0`` admits IEEE 754 subnormals (e.g. 5e-324),
    # which divided into eigenvalues at line 361 would produce ratios on the
    # order of 1e+300 and contaminate every PCAComponent.explained_variance_ratio
    # plus the cumulative-variance sum used downstream.  Project-standard
    # threshold: require a strictly normal divisor ``> 1e-14``.
    total_variance = sum(cov[i][i] for i in range(n_features))
    if not (total_variance > 1e-14):
        return []

    components: List[PCAComponent] = []
    deflated = [row[:] for row in cov]
    cumulative = 0.0

    for idx in range(n_components):
        try:
            eigenvalue, eigenvector = _power_iteration(deflated)
        except ValueError as exc:
            _log.debug("Power iteration failed at component %d: %s", idx, exc)
            break

        if eigenvalue <= 0.0:
            break

        ratio = eigenvalue / total_variance
        cumulative += ratio

        components.append(PCAComponent(
            component_idx=idx,
            explained_variance_ratio=ratio,
            cumulative_variance=min(1.0, cumulative),
            loadings={},  # filled below when labels are attached
        ))
        # Store raw eigenvector for label attachment; reuse loadings dict
        # We'll store the numeric components in loadings temporarily as
        # indices until the caller attaches labels.
        components[-1].loadings = {
            f"_raw_{i}": eigenvector[i] for i in range(len(eigenvector))
        }

        # Deflate for next iteration
        deflated = _deflate(deflated, eigenvalue, eigenvector)

    return components


def _attach_labels_to_pca(
    components: List[PCAComponent],
    labels: List[str],
) -> List[PCAComponent]:
    """
    Replace the raw index keys in each component's loadings dict with the
    proper asset/label names.
    """
    for pc in components:
        new_loadings: Dict[str, float] = {}
        for i, label in enumerate(labels):
            new_loadings[label] = pc.loadings.get(f"_raw_{i}", 0.0)
        # Remove any extra raw keys
        pc.loadings = new_loadings
    return components


# ── Return series computation ─────────────────────────────────────────────────

def _compute_returns(equity_curve: List[float]) -> List[float]:
    """Convert equity curve to period (arithmetic) returns.

    v1.1.0 fifth-pass (G-9): NaN sentinel for invalid bars instead of
    0.0.  Substituting 0.0 made rolling correlation see a string of
    correlated zeros across all assets → spurious high cross-asset
    correlation and an artificially low ``diversification_score``.
    Subnormal positive prev (5e-324) also slipped through the prior
    ``> 0.0`` floor and produced 1e+300 returns; tightened to 1e-14
    per CLAUDE.md § 9.3.
    """
    if len(equity_curve) < 2:
        return []
    returns: List[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        curr = equity_curve[i]
        if prev > 1e-14 and math.isfinite(prev) and math.isfinite(curr):
            r = (curr - prev) / prev
            returns.append(r if math.isfinite(r) else float("nan"))
        else:
            returns.append(float("nan"))
    return returns


# ── Data loading helpers ──────────────────────────────────────────────────────

def _load_equity_curve_from_backtest(run_dir: str) -> Tuple[List[str], List[float]]:
    """
    Load equity curve from backtest_report.json in run_dir.
    Returns (timestamps, equity_values).
    """
    report_path = os.path.join(run_dir, "backtest_report.json")
    if not os.path.isfile(report_path):
        return [], []
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return [], []

    curve = data.get("equity_curve") or []
    timestamps: List[str] = []
    values: List[float] = []

    for item in curve:
        if isinstance(item, dict):
            # Use explicit None checks instead of `or ""` to avoid falsely
            # discarding a legitimate timestamp value of 0 (integer epoch).
            _ts_raw = item.get("date")
            if _ts_raw is None:
                _ts_raw = item.get("timestamp")
            if _ts_raw is None:
                _ts_raw = item.get("t")
            ts = str(_ts_raw) if _ts_raw is not None else ""
            val_raw = next(
                (item[k] for k in ("value", "equity", "portfolio_value") if item.get(k) is not None),
                None,
            )
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            ts = str(item[0])
            val_raw = item[1]
        else:
            continue
        try:
            val = float(val_raw)
        except (TypeError, ValueError):
            continue
        if math.isnan(val) or math.isinf(val):
            continue
        timestamps.append(ts)
        values.append(val)

    return timestamps, values


def _load_csv_close(path: str) -> Tuple[List[str], List[float]]:
    """Load timestamps and close prices from a CSV file."""
    timestamps: List[str] = []
    prices: List[float] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.DictReader(fh)
            fieldnames_lower = [f.strip().lower() for f in (reader.fieldnames or [])]
            lower_to_orig = {
                f.strip().lower(): f for f in (reader.fieldnames or [])
            }

            date_cands = ["date", "timestamp", "time", "datetime", "index"]
            price_cands = ["close", "adj close", "adjusted_close", "price", "last"]

            date_col = next(
                (lower_to_orig[c] for c in date_cands if c in fieldnames_lower), None
            )
            price_col = next(
                (lower_to_orig[c] for c in price_cands if c in fieldnames_lower), None
            )
            if price_col is None and fieldnames_lower:
                # Fallback: first numeric-looking column
                for fn_low in fieldnames_lower:
                    if fn_low == (date_col or "").lower():
                        continue
                    price_col = lower_to_orig.get(fn_low)
                    break

            if price_col is None:
                return [], []

            for row in reader:
                ts = str(row.get(date_col, "")).strip() if date_col else ""
                try:
                    p = float(str(row.get(price_col, "nan")).strip().replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if math.isnan(p) or math.isinf(p) or p <= 0.0:
                    continue
                timestamps.append(ts)
                prices.append(p)
    except Exception:
        pass
    return timestamps, prices


# ── Timestamp alignment ───────────────────────────────────────────────────────

def _align_return_series(
    raw: Dict[str, Tuple[List[str], List[float]]],
) -> Tuple[List[str], Dict[str, List[float]]]:
    """
    Align multiple (timestamp, return) series on a common sorted timestamp set.
    Missing returns are filled with ``float("nan")`` so that pairwise Pearson r
    in ``_pearson_r`` (already NaN-aware per v1.1.0 fifth-pass G-9) can skip
    the missing observation rather than treating it as a correlated flat-day
    zero return.
    Returns (common_timestamps, aligned_return_dict).
    """
    all_ts: set = set()
    for ts_list, _ in raw.values():
        all_ts.update(ts_list)

    if not all_ts:
        return [], {}

    sorted_ts = sorted(all_ts)
    aligned: Dict[str, List[float]] = {}

    # v1.1.2 (sixth-pass H-1): substitute ``float("nan")`` (was ``0.0``) for
    # missing observations.  A multi-asset universe with sparse overlap
    # (asset A trades Mon-Fri, asset B trades 24/7) would otherwise see a
    # string of correlated zeros across all assets — inflating
    # ``avg_correlation``, deflating ``diversification_score`` and biasing
    # every PCA component.  ``_pearson_r`` (line 120) already strips NaN
    # pairs, so the producer side is the only correctness gap.
    for label, (ts_list, returns) in raw.items():
        ts_map: Dict[str, float] = dict(zip(ts_list, returns))
        aligned[label] = [ts_map.get(ts, float("nan")) for ts in sorted_ts]

    return sorted_ts, aligned


# ── Run-mode guard ────────────────────────────────────────────────────────────

def _is_quant_run(run_dir: str) -> bool:
    """Return True if this is a quant-mode run (or mode unknown)."""
    result_path = os.path.join(run_dir, "analysis_result.json")
    if not os.path.isfile(result_path):
        return True
    try:
        with open(result_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode = str(data.get("mode", "")).lower()
        return mode in ("quant", "")
    except (OSError, json.JSONDecodeError):
        return True


# ── JSON sanitisation ─────────────────────────────────────────────────────────

def _san(v: object) -> object:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ── Save report ───────────────────────────────────────────────────────────────

def _save_report(output_dir: str, result: DynamicCorrelationResult) -> None:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "dynamic_correlation_report.json")

    def _corr_matrix_to_serialisable(
        m: Dict[str, Dict[str, float]]
    ) -> Dict[str, Dict[str, Any]]:
        return {
            r: {c: _san(v) for c, v in row.items()}
            for r, row in m.items()
        }

    snapshots_out = [
        {
            "timestamp": s.timestamp,
            "avg_correlation": _san(s.avg_correlation),
            "max_correlation": _san(s.max_correlation),
            "min_correlation": _san(s.min_correlation),
            "matrix": _corr_matrix_to_serialisable(s.matrix),
        }
        for s in result.snapshots
    ]
    pca_out = [
        {
            "component_idx": pc.component_idx,
            "explained_variance_ratio": _san(pc.explained_variance_ratio),
            "cumulative_variance": _san(pc.cumulative_variance),
            "loadings": {k: _san(v) for k, v in pc.loadings.items()},
        }
        for pc in result.pca_components
    ]

    payload = {
        "snapshots": snapshots_out,
        "current_correlation": _corr_matrix_to_serialisable(result.current_correlation),
        "avg_correlation_series": [
            {"ts": d.get("ts"), "value": _san(d.get("value"))}
            for d in result.avg_correlation_series
        ],
        "pca_components": pca_out,
        "total_variance_explained": _san(result.total_variance_explained),
        "diversification_score": _san(result.diversification_score),
        "errors": result.errors,
    }
    _tmp_path = path + ".tmp"
    try:
        with open(_tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(_tmp_path, path)
        result.report_path = path
    except Exception as exc:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        result.errors.append(f"Failed to save report: {exc}")


# ── Core computation ──────────────────────────────────────────────────────────

def _compute_dynamic_correlation(
    return_series: Dict[str, List[float]],
    timestamps: List[str],
    labels: List[str],
    config: DynamicCorrelationConfig,
) -> DynamicCorrelationResult:
    """
    Given aligned return series, compute rolling correlation and PCA.
    """
    result = DynamicCorrelationResult()

    if len(return_series) < 2:
        result.errors.append("Need ≥ 2 return series for correlation analysis.")
        return result

    # Rolling correlation snapshots
    result.snapshots = _rolling_correlation_matrix(
        return_series,
        timestamps,
        window=config.window,
        step=config.step,
    )

    # Current correlation (last window)
    n_obs = min(len(v) for v in return_series.values())
    if n_obs >= config.min_observations:
        w = min(config.window, n_obs)
        current_window: Dict[str, List[float]] = {
            sym: v[-w:] for sym, v in return_series.items()
        }
        cur_matrix, _, _, _ = _correlation_matrix(current_window, sorted(return_series.keys()))
        result.current_correlation = cur_matrix
    elif result.snapshots:
        result.current_correlation = result.snapshots[-1].matrix

    # Average correlation time series
    result.avg_correlation_series = [
        {"ts": s.timestamp, "value": s.avg_correlation}
        for s in result.snapshots
    ]

    # Diversification score = 1 - avg pairwise correlation (current)
    if result.current_correlation:
        syms = sorted(result.current_correlation.keys())
        off_diag: List[float] = [
            result.current_correlation[si][sj]
            for si in syms for sj in syms if si != sj
        ]
        avg_pair_corr = _mean(off_diag) if off_diag else 0.0
        result.diversification_score = max(0.0, min(1.0, 1.0 - avg_pair_corr))

    # PCA: build (n_obs × n_features) data matrix of returns
    min_len = min(len(v) for v in return_series.values())
    if min_len >= config.min_observations:
        sym_order = sorted(return_series.keys())
        data_matrix: List[List[float]] = [
            [return_series[sym][t] for sym in sym_order]
            for t in range(min_len)
        ]
        n_comp = min(config.n_pca_components, len(sym_order))
        raw_components = _pca_pure_python(data_matrix, n_comp)
        result.pca_components = _attach_labels_to_pca(raw_components, sym_order)
        if result.pca_components:
            result.total_variance_explained = result.pca_components[-1].cumulative_variance

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def run_dynamic_correlation(
    run_dirs: List[str],
    labels: Optional[List[str]] = None,
    output_dir: Optional[str] = None,
    config: Optional[DynamicCorrelationConfig] = None,
) -> DynamicCorrelationResult:
    """
    Load equity curves from multiple run directories' backtest_report.json,
    compute period returns, align on common timestamps, and run rolling
    correlation + PCA.

    Parameters
    ----------
    run_dirs:
        List of paths to completed pipeline run directories.
    labels:
        Human-readable labels for each run_dir (defaults to directory name).
    output_dir:
        Where to save dynamic_correlation_report.json (defaults to run_dirs[0]).
    config:
        DynamicCorrelationConfig instance.

    Returns
    -------
    DynamicCorrelationResult
    """
    if config is None:
        config = DynamicCorrelationConfig()

    result = DynamicCorrelationResult()

    if not run_dirs:
        result.errors.append("No run directories provided.")
        return result

    if labels is None or len(labels) != len(run_dirs):
        labels = [os.path.basename(d) for d in run_dirs]

    if output_dir is None:
        output_dir = run_dirs[0]

    raw_returns: Dict[str, Tuple[List[str], List[float]]] = {}

    for rd, label in zip(run_dirs, labels):
        ts_list, equity = _load_equity_curve_from_backtest(rd)
        if len(equity) < 2:
            result.errors.append(
                f"Insufficient equity curve data in {rd} — skipping."
            )
            continue
        rets = _compute_returns(equity)
        # Timestamps for returns are offset by 1 (from t=1 onward)
        ret_ts = ts_list[1:] if len(ts_list) > 1 else [str(i) for i in range(len(rets))]
        raw_returns[label] = (ret_ts, rets)

    if len(raw_returns) < 2:
        result.errors.append(
            f"Need ≥ 2 run directories with valid equity curves (got {len(raw_returns)})."
        )
        _save_report(output_dir, result)
        return result

    common_ts, aligned = _align_return_series(raw_returns)
    sym_labels = sorted(aligned.keys())
    result = _compute_dynamic_correlation(aligned, common_ts, sym_labels, config)
    _save_report(output_dir, result)
    return result


def run_dynamic_correlation_single(
    run_dir: str,
    config: Optional[DynamicCorrelationConfig] = None,
) -> DynamicCorrelationResult:
    """
    Load all CSV files from ``{run_dir}/code/data/`` as separate assets,
    convert close prices to returns, and compute rolling correlation + PCA.

    Falls back to loading from backtest_report.json equity curve if no CSVs
    are found.  Saves report to ``{run_dir}/dynamic_correlation_report.json``.
    """
    if config is None:
        config = DynamicCorrelationConfig()

    result = DynamicCorrelationResult()

    data_dir = os.path.join(run_dir, "code", "data")
    raw: Dict[str, Tuple[List[str], List[float]]] = {}

    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if not fname.lower().endswith(".csv"):
                continue
            symbol = re.sub(r"\.(csv|CSV)$", "", fname)
            fpath = os.path.join(data_dir, fname)
            try:
                ts_list, prices = _load_csv_close(fpath)
            except Exception as exc:
                result.errors.append(f"Failed to load {fname}: {exc}")
                continue
            if len(prices) < 2:
                continue
            rets = _compute_returns(prices)
            ret_ts = ts_list[1:] if len(ts_list) > 1 else [str(i) for i in range(len(rets))]
            raw[symbol] = (ret_ts, rets)

    _single_asset_fallback = False
    if len(raw) < 2:
        # Fallback: equity curve from backtest_report.json
        _log.info(
            "DynCorr: only %d CSV assets found — trying backtest equity curve.", len(raw)
        )
        ts_list, equity = _load_equity_curve_from_backtest(run_dir)
        if len(equity) >= 4:
            rets = _compute_returns(equity)
            ret_ts = ts_list[1:] if len(ts_list) > 1 else [str(i) for i in range(len(rets))]
            # Split into even/odd bars so both halves share the same
            # timestamp span.  The old midpoint split produced two
            # non-overlapping halves whose alignment filled all gaps with
            # 0.0, giving a near-zero correlation by construction (false
            # diversification signal).
            raw["strategy_even"] = (ret_ts[0::2], rets[0::2])
            raw["strategy_odd"] = (ret_ts[1::2], rets[1::2])
            # Track that we used an artificial split so diversification_score
            # can be overridden to 0.0 — a single real asset has no genuine
            # cross-asset diversification regardless of the even/odd correlation.
            if len(raw) == 2:  # only the two halves; no real multi-asset data
                _single_asset_fallback = True

    if len(raw) < 2:
        result.errors.append(
            "Need ≥ 2 assets/series with return data for dynamic correlation."
        )
        _save_report(run_dir, result)
        return result

    common_ts, aligned = _align_return_series(raw)

    # Filter series with insufficient non-flat observations.
    # Use set-cardinality instead of != 0.0: a flat-return strategy with all
    # legitimate zero daily returns would otherwise be excluded because the
    # alignment fill value is also 0.0, making the two cases indistinguishable.
    # len(set(v)) > 1 correctly requires at least two distinct values (i.e.
    # the series has some variance) while accepting legitimate zero returns.
    clean: Dict[str, List[float]] = {
        sym: v for sym, v in aligned.items()
        if len(set(v)) > 1 and sum(1 for x in v if x != 0.0) >= config.min_observations
    }
    if len(clean) < 2:
        clean = aligned  # relax the filter

    sym_labels = sorted(clean.keys())
    result = _compute_dynamic_correlation(clean, common_ts, sym_labels, config)

    if _single_asset_fallback:
        # The two series are artificial (even/odd halves of one equity curve).
        # Their computed correlation is not meaningful for real diversification
        # assessment, so we override diversification_score to 0.0 and warn.
        result.diversification_score = 0.0
        result.warnings.append(
            "Single-asset fallback: only one equity curve was available so it "
            "was split into even/odd bars for rolling correlation visualisation. "
            "diversification_score is set to 0.0 — no genuine cross-asset "
            "diversification data was available."
        )

    _save_report(run_dir, result)
    return result
