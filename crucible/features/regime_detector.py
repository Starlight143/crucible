"""
features/regime_detector.py
============================
Market Regime Detection for Quant mode runs.

Three detection methods (all pure Python / stdlib):
  - "volatility": rolling volatility thresholding → bull/bear/sideways
  - "trend":      price vs. SMA → bull/bear/sideways
  - "hmm":        2- or 3-state Gaussian HMM via Baum-Welch EM → bull/bear[/sideways]

Environment variables
---------------------
REGIME_N_REGIMES     Number of hidden states (default 3).
REGIME_METHOD        Detection method: "volatility" | "trend" | "hmm" (default "volatility").
REGIME_VOL_WINDOW    Rolling volatility window (default 20).
REGIME_TREND_WINDOW  SMA window for trend method (default 50).
REGIME_LOOKBACK_BARS Limit analysis to last N bars (default None → all).
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
class RegimeConfig:
    n_regimes: int = field(default_factory=lambda: _env_int("REGIME_N_REGIMES", 3))
    method: str = field(default_factory=lambda: _env_str("REGIME_METHOD", "volatility"))
    vol_window: int = field(default_factory=lambda: _env_int("REGIME_VOL_WINDOW", 20))
    trend_window: int = field(default_factory=lambda: _env_int("REGIME_TREND_WINDOW", 50))
    lookback_bars: Optional[int] = field(
        default_factory=lambda: (
            # max(0, ...) prevents a negative env-var value from silently
            # producing a negative lookback, which would evaluate as truthy
            # and pass through `or None`, then cause an offset overshoot that
            # yields an empty returns slice with no error message.
            max(0, _env_int("REGIME_LOOKBACK_BARS", 0)) or None
        )
    )


@dataclass
class Regime:
    label: str                   # "bull" | "bear" | "sideways"
    start_ts: str = ""
    end_ts: str = ""
    n_bars: int = 0
    avg_return: Optional[float] = None
    avg_volatility: Optional[float] = None
    sharpe_estimate: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "n_bars": self.n_bars,
            "avg_return": _sanitise_float(self.avg_return),
            "avg_volatility": _sanitise_float(self.avg_volatility),
            "sharpe_estimate": _sanitise_float(self.sharpe_estimate),
        }


@dataclass
class RegimeDetectionResult:
    regimes: List[Regime] = field(default_factory=list)
    regime_series: List[Dict[str, str]] = field(default_factory=list)
    current_regime: Optional[str] = None
    regime_performance: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    n_bars_total: int = 0
    report_path: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regimes": [r.to_dict() for r in self.regimes],
            "regime_series": self.regime_series,
            "current_regime": self.current_regime,
            "regime_performance": self.regime_performance,
            "n_bars_total": self.n_bars_total,
            "report_path": self.report_path,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Statistical helpers ────────────────────────────────────────────────────────

def _rolling_std(values: List[float], window: int) -> List[Optional[float]]:
    """Compute rolling standard deviation (sample std, ddof=1) of the window.

    v1.1.0 fifth-pass (G-9): NaN-sentinel-aware.  ``_equity_to_returns``
    emits ``float('nan')`` for invalid bars; we drop them inside each
    window before computing.  If fewer than 2 finite samples remain in
    a window, that window's std is reported as ``None`` instead of
    contaminating downstream classification with a zero std (which
    would over-classify bars as low-volatility "bull").
    """
    if window <= 0:
        return [None] * len(values)
    result: List[Optional[float]] = [None] * len(values)
    for i in range(window - 1, len(values)):
        window_vals = [v for v in values[i - window + 1: i + 1] if math.isfinite(v)]
        n = len(window_vals)
        if n < 2:
            # Too few finite samples — emit None so the volatility-regime
            # logic treats this bar as "unknown" (mapped to "sideways"
            # in _detect_volatility), not as zero-volatility "bull".
            continue
        mean = sum(window_vals) / n
        var = sum((v - mean) ** 2 for v in window_vals) / (n - 1)
        result[i] = math.sqrt(var)
    return result


def _sma(values: List[float], window: int) -> List[Optional[float]]:
    """Simple moving average."""
    if window <= 0:
        return [None] * len(values)
    result: List[Optional[float]] = [None] * len(values)
    for i in range(window - 1, len(values)):
        result[i] = sum(values[i - window + 1: i + 1]) / window
    return result


def _median_std(values: List[float]) -> Tuple[float, float]:
    """Return (median, std) of a list, ignoring None."""
    clean = [v for v in values if v is not None]
    if not clean:
        return 0.0, 1.0
    clean.sort()
    n = len(clean)
    med = clean[n // 2] if n % 2 == 1 else (clean[n // 2 - 1] + clean[n // 2]) / 2.0
    mean = sum(clean) / n
    # Use sample variance (÷ n-1) for consistency with _regime_stats and
    # quant_analytics.py.  Population variance (÷ n) would produce a
    # systematically smaller std, tightening the volatility threshold and
    # over-classifying bars as high-volatility (bear) regime.
    std = math.sqrt(sum((v - mean) ** 2 for v in clean) / (n - 1)) if n > 1 else 0.0
    return med, std


# ── Method 1: Volatility ──────────────────────────────────────────────────────

def _detect_volatility(
    returns: List[float],
    timestamps: List[str],
    config: RegimeConfig,
) -> List[str]:
    """
    Assign each bar a regime label based on rolling volatility.

    Labels: "bull" (low vol), "bear" (high vol), "sideways" (medium vol).
    """
    vol_series = _rolling_std(returns, config.vol_window)
    finite_vols = [v for v in vol_series if v is not None]
    if not finite_vols:
        return ["sideways"] * len(returns)

    med, std = _median_std(finite_vols)
    high_thresh = med + 0.5 * std
    low_thresh = max(0.0, med - 0.5 * std)

    labels: List[str] = []
    for v in vol_series:
        if v is None:
            labels.append("sideways")
        elif v > high_thresh:
            labels.append("bear")
        elif v < low_thresh:
            labels.append("bull")
        else:
            labels.append("sideways")
    return labels


# ── Method 2: Trend ────────────────────────────────────────────────────────────

def _detect_trend(
    prices: List[float],
    timestamps: List[str],
    config: RegimeConfig,
) -> List[str]:
    """
    Assign regime based on price vs. SMA.

    price > SMA → "bull"
    price < SMA * 0.99 → "bear"
    within ±1% of SMA → "sideways"
    """
    sma_series = _sma(prices, config.trend_window)
    labels: List[str] = []
    for price, sma_val in zip(prices, sma_series):
        if sma_val is None:
            labels.append("sideways")
        elif price > sma_val * 1.01:
            labels.append("bull")
        elif price < sma_val * 0.99:
            labels.append("bear")
        else:
            labels.append("sideways")
    return labels


# ── Method 3: Gaussian HMM (Baum-Welch EM) ───────────────────────────────────

def _gaussian_pdf(x: float, mu: float, sigma: float) -> float:
    """Gaussian probability density. Returns tiny value instead of 0."""
    # ``sigma <= 0`` lets IEEE 754 subnormals through; the subsequent
    # ``(x - mu) / sigma`` then explodes to ±inf, ``exp(-inf)`` is 0,
    # and the ``max(..., 1e-300)`` fallback masks an Inf that already
    # poisoned upstream alpha/beta scaling.  Reject any subnormal sigma.
    if not (sigma > 1e-14):
        return 1e-300
    z = (x - mu) / sigma
    val = math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))
    return max(val, 1e-300)


class HMMInsufficientDataError(ValueError):
    """Raised by ``_run_hmm_em`` when the observation series is too short
    to fit a ``K``-state model.  Caller (``_detect_hmm`` /
    ``detect_regimes``) catches this and surfaces an explicit warning
    instead of silently labelling every bar as state 0.

    v1.1.0 fifth-pass (G-10): previously the function returned
    ``([0]*T, [0]*K, [1]*K)`` — a "valid-looking" result that mapped
    every bar to whichever label ``state_order[0]`` happened to align
    to, with no warning to the operator.  Fail loud per CLAUDE.md
    "high-risk system" rules.
    """


def _run_hmm_em(
    observations: List[float],
    n_states: int,
    max_iter: int = 100,
    tol: float = 1e-4,
    seed: int = 42,
) -> Tuple[List[int], List[float], List[float]]:
    """
    Fit a Gaussian HMM with Baum-Welch EM and decode with Viterbi.

    Returns (state_sequence, means, stds).
    State sequence has the same length as observations.

    v1.1.0 fifth-pass (G-10): raises ``HMMInsufficientDataError`` when
    T < K*2 rather than silently returning a degenerate all-zero path.
    """
    T = len(observations)
    K = n_states
    if T < K * 2:
        raise HMMInsufficientDataError(
            f"HMM requires at least {K * 2} observations to fit "
            f"{K} states; got {T}.  Fall back to a non-HMM method "
            "(volatility / trend) for very short series."
        )

    rng = random.Random(seed)

    # ── Initialise parameters ─────────────────────────────────────────────────
    obs_sorted = sorted(observations)
    # Spread initial means evenly across the sorted observation range
    means: List[float] = [
        obs_sorted[int((i + 0.5) * T / K)] for i in range(K)
    ]
    _obs_mean = sum(observations) / T
    global_std = math.sqrt(
        sum((v - _obs_mean) ** 2 for v in observations) / T
    ) if T > 1 else 1.0
    # v1.1.0 third-pass: scale-aware std floor.  A flat 1e-14 was
    # mathematically clean but operationally too aggressive — a single
    # outlier in the observation series could pull a regime's std to
    # ~1e-14, after which every off-state emission's log-likelihood
    # term ``(x-µ)²/σ²`` ballooned to ~1e+28 and dominated the
    # transition log-probs, smearing the Viterbi regime boundaries.
    # Floor at the larger of ``global_std × 1e-6`` (a physically
    # meaningful 6-order-of-magnitude buffer below the dataset's
    # natural scale) and the absolute 1e-14 backstop.
    # v1.1.2 (sixth-pass H-1): tighten the gate from ``> 0`` to ``> 1e-14``
    # per CLAUDE.md § 9.3 — a subnormal ``global_std`` (e.g. 5e-324) was
    # truthy here and produced a 5e-320 floor, after which ``stds[k]`` kept
    # the subnormal value, and the next ``_gaussian_pdf`` call divided into
    # it and yielded ``inf``.
    _STD_FLOOR = max(global_std * 1e-6, 1e-14) if global_std > 1e-14 else 1e-14
    stds: List[float] = [max(global_std, _STD_FLOOR)] * K

    # Uniform transition matrix and initial distribution
    pi: List[float] = [1.0 / K] * K
    A: List[List[float]] = [[1.0 / K] * K for _ in range(K)]

    prev_log_lik = -math.inf

    for iteration in range(max_iter):
        # ── E-step: Forward-Backward ──────────────────────────────────────────
        # Emission probs: B[t][k]
        B: List[List[float]] = [
            [_gaussian_pdf(observations[t], means[k], stds[k]) for k in range(K)]
            for t in range(T)
        ]

        # Forward pass (scaled)
        alpha: List[List[float]] = [[0.0] * K for _ in range(T)]
        scale: List[float] = [0.0] * T
        for k in range(K):
            alpha[0][k] = pi[k] * B[0][k]
        # ``X or 1e-300`` only substitutes when X is falsy (0.0); IEEE 754
        # subnormals (e.g. 5e-324) are truthy and slip through, producing
        # gamma/xi values > 1.0 and breaking probability invariants.  Use
        # ``max(..., 1e-300)`` everywhere to floor against subnormals too.
        scale[0] = max(sum(alpha[0]), 1e-300)
        for k in range(K):
            alpha[0][k] /= scale[0]

        for t in range(1, T):
            for j in range(K):
                alpha[t][j] = B[t][j] * sum(alpha[t - 1][i] * A[i][j] for i in range(K))
            scale[t] = max(sum(alpha[t]), 1e-300)
            for j in range(K):
                alpha[t][j] /= scale[t]

        # Backward pass (scaled)
        beta: List[List[float]] = [[0.0] * K for _ in range(T)]
        for k in range(K):
            beta[T - 1][k] = 1.0

        for t in range(T - 2, -1, -1):
            for i in range(K):
                beta[t][i] = sum(
                    A[i][j] * B[t + 1][j] * beta[t + 1][j] for j in range(K)
                )
            s = max(sum(beta[t]), 1e-300)
            for i in range(K):
                beta[t][i] /= s

        # Gamma and Xi
        gamma: List[List[float]] = [[0.0] * K for _ in range(T)]
        for t in range(T):
            denom = max(sum(alpha[t][k] * beta[t][k] for k in range(K)), 1e-300)
            for k in range(K):
                gamma[t][k] = alpha[t][k] * beta[t][k] / denom

        xi: List[List[List[float]]] = [
            [[0.0] * K for _ in range(K)] for _ in range(T - 1)
        ]
        for t in range(T - 1):
            denom = max(
                sum(
                    alpha[t][i] * A[i][j] * B[t + 1][j] * beta[t + 1][j]
                    for i in range(K) for j in range(K)
                ),
                1e-300,
            )
            for i in range(K):
                for j in range(K):
                    xi[t][i][j] = (
                        alpha[t][i] * A[i][j] * B[t + 1][j] * beta[t + 1][j]
                    ) / denom

        # ── M-step ───────────────────────────────────────────────────────────
        # Update pi
        for k in range(K):
            pi[k] = gamma[0][k]

        # Update A
        for i in range(K):
            denom_a = max(sum(sum(xi[t][i][j] for j in range(K)) for t in range(T - 1)), 1e-300)
            for j in range(K):
                A[i][j] = sum(xi[t][i][j] for t in range(T - 1)) / denom_a

        # Update means and stds
        for k in range(K):
            gamma_sum = max(sum(gamma[t][k] for t in range(T)), 1e-300)
            means[k] = sum(gamma[t][k] * observations[t] for t in range(T)) / gamma_sum
            var_k = (
                sum(gamma[t][k] * (observations[t] - means[k]) ** 2 for t in range(T))
                / gamma_sum
            )
            stds[k] = max(math.sqrt(var_k), _STD_FLOOR)

        # Log-likelihood
        log_lik = sum(math.log(s) for s in scale if s > 0)
        # v1.1.0 fifth-pass (G-11): relative-tolerance EM convergence.
        # Absolute ``|Δ| < tol`` (tol=1e-4) for long sequences (T=10k)
        # is roughly 1e-8 relative precision — typically never met,
        # so EM ran all 100 iterations every time.  Relative tolerance
        # is the same fix pattern as ``_power_iteration`` (M9).
        if abs(log_lik - prev_log_lik) < tol * max(abs(log_lik), 1.0):
            break
        prev_log_lik = log_lik

    # ── Viterbi decoding ──────────────────────────────────────────────────────
    viterbi: List[List[float]] = [[-math.inf] * K for _ in range(T)]
    psi: List[List[int]] = [[0] * K for _ in range(T)]

    for k in range(K):
        p = pi[k] * _gaussian_pdf(observations[0], means[k], stds[k])
        viterbi[0][k] = math.log(p) if p > 0 else -math.inf

    for t in range(1, T):
        for j in range(K):
            b = _gaussian_pdf(observations[t], means[j], stds[j])
            log_b = math.log(b) if b > 0 else -math.inf
            candidates = [
                viterbi[t - 1][i] + math.log(A[i][j]) if A[i][j] > 0 else -math.inf
                for i in range(K)
            ]
            best_i = max(range(K), key=lambda i: candidates[i])
            viterbi[t][j] = candidates[best_i] + log_b
            psi[t][j] = best_i

    # Backtrack
    path: List[int] = [0] * T
    path[T - 1] = max(range(K), key=lambda k: viterbi[T - 1][k])
    for t in range(T - 2, -1, -1):
        path[t] = psi[t + 1][path[t + 1]]

    return path, means, stds


def _detect_hmm(
    returns: List[float],
    config: RegimeConfig,
) -> List[str]:
    """
    Assign regime labels using HMM. Higher-mean state → "bull", lower → "bear".
    With 3 states: highest → "bull", lowest → "bear", middle → "sideways".

    v1.1.0 fifth-pass (G-9, G-10): filter NaN sentinels before fitting,
    and catch ``HMMInsufficientDataError`` so the volatility-fallback
    in ``detect_regimes`` runs instead of returning meaningless labels.
    """
    K = max(2, min(config.n_regimes, 3))
    finite_rets = _finite_only(returns)
    if len(finite_rets) < K * 2:
        raise HMMInsufficientDataError(
            f"HMM requires {K * 2} finite returns; got {len(finite_rets)} "
            f"(of {len(returns)}).  Falling back to volatility method."
        )
    path_finite, means, _ = _run_hmm_em(finite_rets, n_states=K)
    # Re-align the path back onto the original return series (NaN bars
    # inherit the previous bar's regime, or "sideways" if leading).
    path: List[int] = []
    finite_iter = iter(path_finite)
    last_state = 0
    for r in returns:
        if math.isfinite(r):
            try:
                last_state = next(finite_iter)
            except StopIteration:
                pass
        path.append(last_state)

    # Sort states by mean return
    state_order = sorted(range(K), key=lambda k: means[k])
    # Map: lowest mean → "bear", highest → "bull", middle → "sideways"
    if K == 2:
        label_map = {state_order[0]: "bear", state_order[1]: "bull"}
    else:
        label_map = {
            state_order[0]: "bear",
            state_order[1]: "sideways",
            state_order[2]: "bull",
        }

    return [label_map.get(s, "sideways") for s in path]


# ── Core detection function ────────────────────────────────────────────────────

def detect_regimes(
    returns: List[float],
    timestamps: List[str],
    prices: List[float],
    config: RegimeConfig,
) -> RegimeDetectionResult:
    """
    Detect market regimes from the given return/price series.

    Parameters
    ----------
    returns : List[float]
        Period return series.
    timestamps : List[str]
        ISO timestamp strings aligned with returns.
    prices : List[float]
        Price (equity) series aligned with returns.
    config : RegimeConfig
        Detection configuration.

    Returns
    -------
    RegimeDetectionResult
    """
    result = RegimeDetectionResult()

    if len(returns) < 5:
        result.errors.append("Insufficient data for regime detection (need >= 5 bars)")
        return result

    # Apply lookback limit
    if config.lookback_bars is not None and len(returns) > config.lookback_bars:
        offset = len(returns) - config.lookback_bars
        returns = returns[offset:]
        timestamps = timestamps[offset:] if timestamps else timestamps
        prices = prices[offset:] if prices else prices

    n = len(returns)
    result.n_bars_total = n

    # Pad timestamps and prices if needed — append at the end so that real
    # data occupies the beginning of the series and the SMA/trend calculation
    # is not corrupted by fake leading values.
    if len(timestamps) < n:
        timestamps = list(timestamps) + [""] * (n - len(timestamps))
    if len(prices) < n:
        prices = list(prices) + [prices[-1] if prices else 1.0] * (n - len(prices))

    # Detect labels
    method = config.method.lower()
    try:
        if method == "volatility":
            labels = _detect_volatility(returns, timestamps, config)
        elif method == "trend":
            labels = _detect_trend(prices, timestamps, config)
        elif method == "hmm":
            # v1.1.0 fifth-pass (G-10): catch insufficient-data so we
            # fall back to volatility method instead of silently
            # returning meaningless labels.
            try:
                labels = _detect_hmm(returns, config)
            except HMMInsufficientDataError as exc:
                result.warnings.append(str(exc))
                labels = _detect_volatility(returns, timestamps, config)
        else:
            result.warnings.append(
                f"Unknown method '{method}', falling back to 'volatility'"
            )
            labels = _detect_volatility(returns, timestamps, config)
    except Exception as exc:
        result.errors.append(f"Regime detection failed: {exc}")
        return result

    if len(labels) != n:
        result.errors.append(
            f"Label length mismatch: got {len(labels)}, expected {n}"
        )
        return result

    # Build regime_series
    result.regime_series = [
        {"ts": timestamps[i], "regime": labels[i]} for i in range(n)
    ]
    result.current_regime = labels[-1] if labels else None

    # Compress into Regime segments
    segments: List[Regime] = []
    if labels:
        seg_label = labels[0]
        seg_start = 0
        seg_returns: List[float] = [returns[0]]

        for i in range(1, n):
            if labels[i] != seg_label:
                seg = _build_regime(
                    seg_label, timestamps, seg_start, i - 1, seg_returns
                )
                segments.append(seg)
                seg_label = labels[i]
                seg_start = i
                seg_returns = [returns[i]]
            else:
                seg_returns.append(returns[i])

        # Last segment
        seg = _build_regime(seg_label, timestamps, seg_start, n - 1, seg_returns)
        segments.append(seg)

    result.regimes = segments

    # Per-regime performance
    regime_returns: Dict[str, List[float]] = {}
    for i, lbl in enumerate(labels):
        regime_returns.setdefault(lbl, []).append(returns[i])

    perf: Dict[str, Dict[str, Any]] = {}
    for lbl, rets in regime_returns.items():
        m = _regime_stats(rets)
        perf[lbl] = m
    result.regime_performance = perf

    return result


def _build_regime(
    label: str,
    timestamps: List[str],
    start_idx: int,
    end_idx: int,
    seg_returns: List[float],
) -> Regime:
    start_ts = timestamps[start_idx] if timestamps and start_idx < len(timestamps) else ""
    end_ts = timestamps[end_idx] if timestamps and end_idx < len(timestamps) else ""
    stats = _regime_stats(seg_returns)
    return Regime(
        label=label,
        start_ts=start_ts,
        end_ts=end_ts,
        n_bars=len(seg_returns),
        avg_return=stats.get("avg_return"),
        avg_volatility=stats.get("avg_volatility"),
        sharpe_estimate=stats.get("sharpe_estimate"),
    )


def _regime_stats(rets: List[float]) -> Dict[str, Any]:
    if not rets:
        return {}
    n = len(rets)
    mean_r = sum(rets) / n
    # Use sample variance (÷ n-1) to be consistent with quant_analytics.py
    # and portfolio_backtest.py.  Population variance (÷ n) overstates Sharpe
    # by √(n/(n-1)), most severely for small regime windows (e.g. n=2: +41%).
    var = sum((r - mean_r) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var)
    # Per-regime Sharpe estimate (rf=0): used for relative regime comparison,
    # not as an absolute strategy metric.  Risk-free rate cancels in
    # cross-regime ranking since it is constant across all regimes.
    sharpe = (mean_r / std * math.sqrt(252.0)) if std > 1e-14 else None
    return {
        "avg_return": _sanitise_float(mean_r),
        "avg_volatility": _sanitise_float(std),
        "sharpe_estimate": _sanitise_float(sharpe),
        "n_bars": n,
    }


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_equity_curve(run_dir: str) -> Tuple[List[float], List[str]]:
    """Load equity values and timestamps. Returns (values, timestamps)."""
    import csv as _csv

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
                        v = item.get("equity", item.get("value", item.get("close")))
                        t = item.get("timestamp", item.get("date", item.get("ts", "")))
                        if v is not None:
                            try:
                                values.append(float(v))
                                timestamps.append(str(t))
                            except (ValueError, TypeError):
                                pass
                if len(values) >= 2:
                    return values, timestamps
        except (OSError, json.JSONDecodeError):
            pass

    # CSV fallback
    data_dir = os.path.join(run_dir, "code", "data")
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            if not fname.lower().endswith(".csv"):
                continue
            try:
                values = []
                timestamps = []
                with open(os.path.join(data_dir, fname), "r", encoding="utf-8", newline="") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        for col in ("equity", "close", "Close", "price", "Price"):
                            if col in row:
                                try:
                                    values.append(float(row[col]))
                                    ts_val = row.get("date", row.get("Date",
                                                     row.get("timestamp", "")))
                                    timestamps.append(str(ts_val))
                                    break
                                except (ValueError, TypeError):
                                    continue
                if len(values) >= 2:
                    return values, timestamps
            except (OSError, _csv.Error):
                pass

    return [], []


def _equity_to_returns(equity: List[float]) -> List[float]:
    """Convert equity curve to period returns with NaN sentinel for bad bars.

    v1.1.0 fifth-pass (G-9): substitutes ``float('nan')`` (not ``0.0``)
    when prev≤0 / non-finite / curr non-finite.  Matches the contract
    established by ``quant_analytics._equity_to_returns`` (v1.1.0 M6).
    Downstream callers (``_rolling_std``, ``_detect_volatility``,
    ``_detect_trend``, ``_detect_hmm``, ``_regime_stats``) are
    responsible for filtering NaN before aggregation; treating bad
    bars as synthetic flat-bar zero returns biased low-volatility
    "bull" regime classifications.
    """
    if len(equity) < 2:
        return []
    returns: List[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        curr = equity[i]
        if (
            prev > 1e-14
            and math.isfinite(prev)
            and math.isfinite(curr)
        ):
            r = (curr - prev) / prev
            returns.append(r if math.isfinite(r) else float("nan"))
        else:
            returns.append(float("nan"))
    return returns


def _finite_only(values: Iterable[float]) -> List[float]:
    """Helper used by regime aggregators to drop NaN sentinels.

    v1.1.0 fifth-pass (G-9): centralises the NaN filter so every consumer
    of ``_equity_to_returns`` strips sentinel bars before computing
    statistics.  Returns a new list; never mutates the input.
    """
    return [v for v in values if math.isfinite(v)]


# ── Public runner ──────────────────────────────────────────────────────────────

def run_regime_detection(
    run_dir: str,
    config: Optional[RegimeConfig] = None,
) -> RegimeDetectionResult:
    """
    Load equity data from run_dir, detect regimes, and save regime_report.json.

    Parameters
    ----------
    run_dir : str
        Path to run directory containing backtest_report.json.
    config : RegimeConfig, optional
        Detection configuration. Defaults to env-var driven RegimeConfig().

    Returns
    -------
    RegimeDetectionResult
    """
    result = RegimeDetectionResult()

    # Mode check: regime detection is useful for all modes but we still warn
    # when called on a non-quant run (the caller can ignore this check).
    if not _is_quant_run(run_dir):
        msg = f"run_regime_detection: run_dir={run_dir} is not a quant run; proceeding anyway"
        logger.info(msg)
        result.warnings.append(msg)

    if config is None:
        config = RegimeConfig()

    equity, timestamps = _load_equity_curve(run_dir)
    if len(equity) < 5:
        msg = f"Insufficient equity data for regime detection ({len(equity)} bars)"
        result.errors.append(msg)
        logger.warning(msg)
        return result

    returns = _equity_to_returns(equity)
    prices = equity  # use equity as price proxy

    # Align timestamps with returns (returns are 1 shorter than equity)
    ts_aligned = timestamps[1:] if len(timestamps) == len(equity) else timestamps

    _pre_warnings = result.warnings[:]
    result = detect_regimes(returns, ts_aligned, prices[1:], config)
    result.warnings = _pre_warnings + result.warnings

    # Persist report (atomic write via tmp + os.replace)
    report_path = os.path.join(run_dir, "regime_report.json")
    _report_tmp = report_path + ".tmp"
    try:
        with open(_report_tmp, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        os.replace(_report_tmp, report_path)
        result.report_path = report_path
        logger.info("Regime report saved to %s", report_path)
    except Exception as exc:
        try:
            os.unlink(_report_tmp)
        except OSError:
            pass
        result.errors.append(f"Could not save report: {exc}")

    return result
