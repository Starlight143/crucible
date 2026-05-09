"""
features/quant_smoke.py
========================
Synthetic-data dry-run smoke test for Quant-mode generated projects.

Web frameworks (FastAPI / Flask) get a real smoke test in
``crucible.modules.section_06_runtime_quality_api._run_smoke_test`` — the app
object is started, ``GET /``-style endpoints are probed, and runtime errors at
import or first-request time become high-severity review issues. Quant mode
has no equivalent: ``runtime_validation`` only ran ``py_compile`` and looked
for FastAPI/Flask app objects, so an entire class of "the code imports fine
but crashes the moment ``backtest.py`` is run" bugs slipped through.

This module fills that gap:

1. **Synthesise** 30 days of OHLCV data (geometric Brownian motion, capped to a
   sensible volatility) and write it to a stable in-bundle path so any data
   loader that reads from ``data/sample_data.csv`` (or the conventional
   ``BACKTEST_DATA_FILE`` env var) finds it.
2. **Try to find an executable backtest entrypoint.** Preferred names:
   ``run_backtest.py``, ``backtest.py``, ``main.py``, ``cli.py``. Then try
   ``python -m strategy`` / ``python -m backtest`` as fallbacks.
3. **Run** the chosen entrypoint as a subprocess with a hard timeout, capturing
   stdout/stderr and the exit code.
4. **Translate** any Python traceback in stderr into a high-severity
   ``ReviewIssue``-shaped dict so the quality-loop fix-step has a concrete
   failure to fix on the next round (rather than an LLM-only review verdict).

Public entry point::

    from crucible.features.quant_smoke import quant_smoke_dryrun
    result = quant_smoke_dryrun(code_dir, timeout_seconds=60)
    if not result.passes:
        for issue in result.issues:
            print(issue["severity"], issue["description"])

Notes on isolation:

- The runner sets ``BACKTEST_NO_NETWORK=1`` and ``CODEX_VALIDATION=1`` so any
  data-provider implementation that branches on those env vars can short-circuit
  external API calls.
- ``PYTHONPATH`` is restricted to the bundle directory so siblings on the host's
  ``PYTHONPATH`` cannot shadow generated modules.
- Stdout/stderr are length-limited so a runaway loop cannot wedge the parent
  process with multi-megabyte log buffers.
"""
from __future__ import annotations

import math
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


__all__ = [
    "QuantSmokeIssue",
    "QuantSmokeResult",
    "quant_smoke_dryrun",
    "synthesise_ohlcv_csv",
]


# ─── Constants ────────────────────────────────────────────────────────────────


_PREFERRED_ENTRYPOINTS: Tuple[str, ...] = (
    "run_backtest.py",
    "backtest.py",
    "main.py",
    "cli.py",
)
_FALLBACK_MODULE_ENTRYPOINTS: Tuple[str, ...] = (
    "backtest",
    "run_backtest",
    "strategy",
    "main",
)
_DATA_RELATIVE_PATHS: Tuple[str, ...] = (
    "data/sample_data.csv",
    "data/ohlcv.csv",
    "sample_data.csv",
)
_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_CAPTURED_BYTES = 8000  # per stream
_TRACEBACK_TAIL_LINES = 60
# v1.0.5 round 3: the live_trader smoke script ramps price from
# _PRICE_SERIES_FIRST to _PRICE_SERIES_LAST across CODEX_LIVE_TRADER_TICKS
# (default 10) — this is documented here so Q024 issue messages and unit
# tests can reference the same numbers as the subprocess.
_PRICE_SERIES_FIRST: float = 100.0
_PRICE_SERIES_LAST: float = 64.0  # 100.0 - 4.0 * (10 - 1)


# ─── Data classes ─────────────────────────────────────────────────────────────


@dataclass
class QuantSmokeIssue:
    severity: str
    category: str
    description: str
    file: Optional[str]
    suggestion: Optional[str] = None
    rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "file": self.file,
            "suggestion": self.suggestion,
            "rule": self.rule,
        }


@dataclass
class QuantSmokeResult:
    passes: bool
    skipped: bool = False
    skip_reason: Optional[str] = None
    entrypoint_used: Optional[str] = None
    issues: List[Dict[str, Any]] = field(default_factory=list)
    log: str = ""
    # v1.0.5 round 2: separate result block for the live_trader sub-step so
    # callers can tell "backtest passed but live_trader smoke crashed" apart
    # from "everything passed".
    live_trader_passes: Optional[bool] = None
    live_trader_skipped: bool = False
    live_trader_log: str = ""

    def append_issue(self, issue: QuantSmokeIssue) -> None:
        self.issues.append(issue.to_dict())


# ─── Synthetic OHLCV helper ───────────────────────────────────────────────────


def synthesise_ohlcv_csv(
    n_rows: int = 30,
    start_price: float = 100.0,
    daily_vol: float = 0.02,
    start_date: str = "2024-01-01",
    seed: int = 42,
    *,
    inject_anomalies: bool = False,
) -> str:
    """
    Build a CSV (header included) of ``n_rows`` daily OHLCV bars.

    The text layout is intentionally minimal — ``date,open,high,low,close,volume``
    in lower-case — to match the convention enforced by ``_quant_codegen_rules``
    so generated data-loaders find the columns they expect.

    The series is GBM with bounded daily move so the synthetic data passes the
    common "volume > 0, monotonic dates, no NaN" sanity check, but does not
    require numpy/pandas at generation time (Python ``random`` is enough).

    Parameters
    ----------
    inject_anomalies : bool, default False
        v1.0.5 round 2: when True, replace 4 of the generated rows with
        controlled "dirty data" patterns the LLM-written data-cleaning code
        often forgets to handle:

        - row 5: NaN in ``volume`` (data outage)
        - row 7: ``volume = 0`` (no trading activity)
        - row 11: a 3-day time gap (server downtime — next row's date is +4d
          rather than +1d)
        - row 13: NaN in ``open`` and ``close`` (partial corruption)

        Code paths that branch on ``volume == 0`` rather than
        ``not (volume > 1e-14)``, or that assume contiguous daily index, will
        crash on this fixture. GBM-only data hides every one of these
        defects, which is why pre-1.0.5 dry-runs always passed.
    """
    if n_rows <= 0:
        n_rows = 30
    rng = random.Random(seed)
    rows: List[str] = ["date,open,high,low,close,volume"]
    price = float(start_price)
    # Parse start_date into a datetime so we can step day-by-day without
    # pulling in an external library.
    import datetime as _dt

    try:
        d = _dt.date.fromisoformat(start_date)
    except ValueError:
        d = _dt.date(2024, 1, 1)

    for i in range(n_rows):
        # GBM daily return with deterministic-ish noise.
        ret = rng.gauss(0.0, daily_vol)
        # Clamp to keep prices in plausible territory.
        ret = max(-0.5, min(0.5, ret))
        new_price = max(0.01, price * (1.0 + ret))
        intra = max(abs(ret) * price, 0.05 * price)
        high = max(price, new_price) + 0.5 * intra
        low = max(0.01, min(price, new_price) - 0.5 * intra)
        # OHLC sanity: clamp open/close into [low, high]
        op = min(max(price, low), high)
        cl = min(max(new_price, low), high)
        # Volume — random positive integer.
        vol = max(1, int(rng.uniform(1_000, 100_000)))

        if inject_anomalies and i == 4:
            # row 5: NaN volume — data-feed gap.
            rows.append(f"{d.isoformat()},{op:.4f},{high:.4f},{low:.4f},{cl:.4f},NaN")
        elif inject_anomalies and i == 6:
            # row 7: zero-volume bar — flat trading session.
            rows.append(f"{d.isoformat()},{op:.4f},{high:.4f},{low:.4f},{cl:.4f},0")
        elif inject_anomalies and i == 12:
            # row 13: partial NaN OHLC — stream corruption.
            rows.append(f"{d.isoformat()},NaN,{high:.4f},{low:.4f},NaN,{vol}")
        else:
            rows.append(
                f"{d.isoformat()},{op:.4f},{high:.4f},{low:.4f},{cl:.4f},{vol}"
            )
        price = new_price
        if inject_anomalies and i == 9:
            # row 11 boundary: 3-day gap (rows 11..) — server downtime.
            d = d + _dt.timedelta(days=4)
        else:
            d = d + _dt.timedelta(days=1)

    return "\n".join(rows) + "\n"


def _write_synthetic_csv(code_dir: str, *, inject_anomalies: bool = False) -> List[str]:
    """Write the synthetic CSV to all conventional locations. Return paths written."""
    csv_text = synthesise_ohlcv_csv(inject_anomalies=inject_anomalies)
    written: List[str] = []
    for rel in _DATA_RELATIVE_PATHS:
        target = os.path.join(code_dir, rel)
        os.makedirs(os.path.dirname(target) or code_dir, exist_ok=True)
        # Don't clobber a real file the LLM already produced — only fill in
        # gaps so existing fixtures keep working.
        if os.path.exists(target):
            continue
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(csv_text)
            written.append(rel)
        except OSError:
            continue
    return written


# ─── Entry point detection ────────────────────────────────────────────────────


def _find_executable_entrypoint(code_dir: str) -> Optional[str]:
    """Return relative path of the preferred backtest entrypoint, or None."""
    # Direct files in the project root first — most common layout.
    for name in _PREFERRED_ENTRYPOINTS:
        candidate = os.path.join(code_dir, name)
        if os.path.isfile(candidate):
            return name
    # Fall back to first match anywhere in the tree (one level deep is enough).
    for dirpath, dirnames, filenames in os.walk(code_dir):
        # Skip noise dirs.
        dirnames[:] = [
            d for d in dirnames
            if d not in {"__pycache__", "tests", "node_modules", ".git", "data", ".venv"}
        ]
        for fname in filenames:
            if fname in _PREFERRED_ENTRYPOINTS:
                rel = os.path.relpath(os.path.join(dirpath, fname), code_dir)
                return rel
    return None


def _has_module(code_dir: str, mod: str) -> bool:
    candidate = os.path.join(code_dir, mod + ".py")
    return os.path.isfile(candidate)


# ─── Traceback parsing ────────────────────────────────────────────────────────


def _truncate(text: str, max_bytes: int = _MAX_CAPTURED_BYTES) -> str:
    if not text:
        return ""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    head = encoded[: max_bytes // 2]
    tail = encoded[-max_bytes // 2:]
    return (
        head.decode("utf-8", errors="replace")
        + "\n…[truncated]…\n"
        + tail.decode("utf-8", errors="replace")
    )


def _extract_error_summary(stderr: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (exception_class, last_message) for the most recent traceback in stderr.

    Looks at the last ``_TRACEBACK_TAIL_LINES`` lines for ``ExceptionClass: msg``.
    """
    if not stderr:
        return None, None
    lines = stderr.strip().splitlines()
    tail = lines[-_TRACEBACK_TAIL_LINES:]
    # Walk backwards for the first line matching `Name: message` outside indented
    # frames; this is the actual exception message.
    for line in reversed(tail):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("File \"", "  File \"", "Traceback")):
            continue
        if ":" in stripped and not stripped.startswith(("  ", "\t")):
            head, _, msg = stripped.partition(":")
            head = head.strip()
            # Heuristic: exception names start with an uppercase letter and
            # don't contain whitespace.
            if head and head[0].isupper() and " " not in head:
                return head, msg.strip()
    return None, None


# ─── Main entry point ─────────────────────────────────────────────────────────


def _build_subprocess_env(code_dir: str) -> Dict[str, str]:
    sensitive_patterns = (
        "API_KEY",
        "API_SECRET",
        "SECRET_KEY",
        "TOKEN",
        "PASSWORD",
        "CREDENTIAL",
        "OPENROUTER",
        "OPENAI_API",
        "ANTHROPIC_API",
        "ALIBABA_",
        "AWS_SECRET",
        "TELEGRAM_",
    )
    env: Dict[str, str] = {
        k: v
        for k, v in os.environ.items()
        if not any(p in k.upper() for p in sensitive_patterns)
        and k.upper() != "PYTHONPATH"
    }
    env["PYTHONPATH"] = code_dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CODEX_VALIDATION"] = "1"
    env["BACKTEST_NO_NETWORK"] = "1"
    env["BACKTEST_DATA_FILE"] = os.path.join(code_dir, "data", "sample_data.csv")
    env["BACKTEST_DATA_SOURCE"] = "csv"
    return env


def _looks_like_quant_bundle(code_dir: str) -> bool:
    """Cheap heuristic — only run dry-run if some Quant-typical files exist."""
    markers = ("strategy.py", "backtest.py", "trade.py", "data_provider.py")
    for marker in markers:
        if os.path.isfile(os.path.join(code_dir, marker)):
            return True
    # Nested layout (e.g. src/ or package dir) — look one level down.
    for entry in os.listdir(code_dir):
        sub = os.path.join(code_dir, entry)
        if not os.path.isdir(sub):
            continue
        for marker in markers:
            if os.path.isfile(os.path.join(sub, marker)):
                return True
    return False


def _build_live_trader_smoke_script(code_dir: str) -> Optional[str]:
    """Build a Python smoke-script that imports live_trader.py with a stubbed
    ccxt exchange and runs at most one tick.

    Returns ``None`` when ``live_trader.py`` is absent. The script is fully
    self-contained — the parent process writes it to a temp file inside
    ``code_dir`` and runs it as a subprocess so a hung loop / runaway network
    call cannot wedge the validation pipeline.
    """
    live_trader_path = os.path.join(code_dir, "live_trader.py")
    if not os.path.isfile(live_trader_path):
        return None
    # We monkey-patch the ``ccxt`` module before live_trader imports it so
    # ``ccxt.binance(...)`` and friends return a stub that:
    # - returns scripted prices from ``fetch_ticker`` / ``fetch_ohlcv``
    # - immediately fills ``create_market_order`` / ``create_order``
    # - never opens a real network connection
    # The stub also raises StopIteration after `max_ticks` calls so any
    # ``while True:`` loop in ``LiveTrader.run_loop`` exits cleanly without
    # requiring the LLM to add explicit smoke-mode handling.
    # NB: keep this a raw string with NO docstrings / no f-strings in the
    # subprocess body — the script is read verbatim and we want to keep the
    # parser straightforward (no triple-quote escaping inside).
    return r"""
import os
import sys
import importlib
import types

# v1.0.5 round 3: tick count + steep ramp-down let us catch the silent
# stop-loss-never-fires bug class. Ramp default = 100 -> 60 over 10 ticks =
# 40% drawdown — well past any reasonable SL. Override via env vars for unit
# tests that want a flat series.
try:
    _MAX_TICKS = max(int(os.environ.get("CODEX_LIVE_TRADER_TICKS", "10") or "10"), 5)
except (TypeError, ValueError):
    _MAX_TICKS = 10
_PRICE_PROFILE = (os.environ.get("CODEX_LIVE_TRADER_PRICE_PROFILE", "ramp_down") or "ramp_down").strip().lower()
if _PRICE_PROFILE == "flat":
    _PRICE_SERIES = [100.0] * max(_MAX_TICKS, 6)
elif _PRICE_PROFILE == "ramp_up":
    _PRICE_SERIES = [100.0 + 4.0 * i for i in range(max(_MAX_TICKS, 6))]
else:
    _PRICE_SERIES = [max(1.0, 100.0 - 4.0 * i) for i in range(max(_MAX_TICKS, 6))]
_tick_count = {"n": 0}
_close_calls = []
_open_calls = []


class _StubOrder(dict):
    pass


class _StubExchange:
    id = "binance"
    has = {"fetchOHLCV": True, "createMarketOrder": True}
    timeframes = {"1m": "1m", "5m": "5m", "1h": "1h", "1d": "1d"}

    def __init__(self, *_args, **_kwargs):
        self._opened = []
        self._closed = []

    def load_markets(self):
        return {"BTC/USDT": {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT"}}

    def fetch_ticker(self, symbol="BTC/USDT", *_a, **_kw):
        if _tick_count["n"] >= _MAX_TICKS:
            raise StopIteration("smoke-mode tick limit reached")
        idx = min(_tick_count["n"], len(_PRICE_SERIES) - 1)
        _tick_count["n"] += 1
        last = _PRICE_SERIES[idx]
        return {"symbol": symbol, "last": last, "bid": last - 0.05, "ask": last + 0.05, "close": last}

    def fetch_ohlcv(self, symbol="BTC/USDT", timeframe="1m", since=None, limit=100):
        out = []
        ts = 1700000000_000
        for i in range(min(limit or 30, 30)):
            p = _PRICE_SERIES[i % len(_PRICE_SERIES)]
            out.append([ts + i * 60_000, p, p + 0.5, p - 0.5, p, 1000])
        return out

    def create_market_order(self, symbol, side, amount, *_a, **_kw):
        record = {"symbol": symbol, "side": str(side), "amount": amount, "tick": _tick_count["n"]}
        side_norm = str(side).lower()
        # buy = open long; sell = close long. (LLM-generated long-only LiveTrader is
        # the overwhelming majority; short strategies that flip this still get the
        # behavioral check via the wrapped close_position method path.)
        if side_norm == "buy":
            self._opened.append(record)
            _open_calls.append(record)
        else:
            self._closed.append(record)
            _close_calls.append(record)
        order = _StubOrder(
            id="smoke-" + str(len(self._opened) + len(self._closed)),
            symbol=symbol,
            side=side,
            type="market",
            amount=amount,
            filled=amount,
            status="closed",
            price=_PRICE_SERIES[max(0, _tick_count["n"] - 1)],
        )
        return order

    def create_order(self, symbol, type_, side, amount, price=None, *_a, **_kw):
        return self.create_market_order(symbol, side, amount)

    def cancel_order(self, *_a, **_kw):
        return {"status": "canceled"}

    def fetch_balance(self, *_a, **_kw):
        return {"USDT": {"free": 10000.0, "used": 0.0, "total": 10000.0}, "free": {"USDT": 10000.0}, "total": {"USDT": 10000.0}}

    def close(self):
        return None


def _wrap_close_methods(instance):
    # Wrap any callable on `instance` whose name suggests a close/exit so we
    # can record direct method calls. Not all LLM-generated live traders route
    # closes through create_market_order — many keep an internal positions
    # dict and update it in-place.
    for name in list(dir(instance)):
        if name.startswith("__"):
            continue
        lower = name.lower()
        if not any(token in lower for token in (
            "close_pos", "_close", "exit_pos", "close_trade",
            "manage_pos", "check_stop", "exit_trade", "stop_out",
        )):
            continue
        try:
            attr = getattr(instance, name)
        except Exception:
            continue
        if not callable(attr):
            continue
        def _make(orig, label):
            def _wrapper(*args, **kwargs):
                _close_calls.append({
                    "method": label,
                    "args_preview": [str(a)[:60] for a in args[:3]],
                    "kwargs_preview": {str(k): str(v)[:60] for k, v in list(kwargs.items())[:3]},
                })
                return orig(*args, **kwargs)
            return _wrapper
        try:
            setattr(instance, name, _make(attr, name))
        except Exception:
            pass


def _has_stop_loss_marker():
    try:
        with open(os.environ["CODEX_LIVE_TRADER_PATH"], encoding="utf-8") as f:
            src = f.read().lower()
    except OSError:
        return False
    markers = (
        "stop_loss", "stoploss", "self.sl", "self._sl",
        " sl =", " sl=", "stop loss", "trailing_stop",
    )
    return any(m in src for m in markers)


_stub_ccxt = types.ModuleType("ccxt")
_stub_ccxt.binance = _StubExchange
_stub_ccxt.bybit = _StubExchange
_stub_ccxt.okx = _StubExchange
_stub_ccxt.kraken = _StubExchange
_stub_ccxt.coinbase = _StubExchange
_stub_ccxt.Exchange = _StubExchange
_stub_ccxt.AuthenticationError = type("AuthenticationError", (Exception,), {})
_stub_ccxt.NetworkError = type("NetworkError", (Exception,), {})
_stub_ccxt.ExchangeError = type("ExchangeError", (Exception,), {})
sys.modules["ccxt"] = _stub_ccxt


def _emit_behavioral_verdict(opens, closes, has_sl):
    # Print a single LIVE_TRADER_BEHAVIORAL line and return the appropriate
    # exit code. The runner translates exit code 3 into a Q024 high issue.
    if not has_sl:
        print("LIVE_TRADER_BEHAVIORAL: opens=" + str(opens) + " closes=" + str(closes) + " sl_marker=False")
        return 0
    if opens == 0:
        print("LIVE_TRADER_BEHAVIORAL: opens=0 closes=" + str(closes) + " sl_marker=True (no-op: never opened)")
        return 0
    if closes == 0:
        first = _PRICE_SERIES[0]
        last = _PRICE_SERIES[-1]
        pct = (last / first - 1.0) * 100.0
        print(
            "LIVE_TRADER_BEHAVIORAL_FAIL: opened " + str(opens) +
            " position(s); price ramped from " + ("%.2f" % first) +
            " to " + ("%.2f" % last) + " (" + ("%.1f" % pct) +
            "%), but no close/exit/sell ever fired."
        )
        return 3
    print("LIVE_TRADER_BEHAVIORAL: opens=" + str(opens) + " closes=" + str(closes) + " sl_marker=True (passed)")
    return 0


try:
    import importlib.util as _util
    spec = _util.spec_from_file_location("live_trader_smoke", os.environ["CODEX_LIVE_TRADER_PATH"])
    mod = _util.module_from_spec(spec)
    spec.loader.exec_module(mod)
except StopIteration:
    print("LIVE_TRADER_SMOKE_OK_TICK_LIMIT")
    sys.exit(0)

candidate_class = None
for name in ("LiveTrader", "LiveTradingEngine", "Trader", "PaperTrader"):
    candidate_class = getattr(mod, name, None)
    if candidate_class is not None:
        break

if candidate_class is None:
    for fname in ("main", "run", "run_live", "start"):
        fn = getattr(mod, fname, None)
        if callable(fn):
            try:
                fn()
            except StopIteration:
                pass
            except SystemExit:
                pass
            print("LIVE_TRADER_SMOKE_OK_FN_" + fname)
            sys.exit(0)
    print("LIVE_TRADER_SMOKE_NO_ENTRY")
    sys.exit(0)

try:
    instance = candidate_class()
except TypeError:
    try:
        instance = candidate_class(symbol="BTC/USDT")
    except Exception as exc:
        print("LIVE_TRADER_SMOKE_CTOR_FAILED: " + type(exc).__name__ + ": " + str(exc))
        sys.exit(2)
except Exception as exc:
    print("LIVE_TRADER_SMOKE_CTOR_FAILED: " + type(exc).__name__ + ": " + str(exc))
    sys.exit(2)

_wrap_close_methods(instance)
has_sl = _has_stop_loss_marker()

method_run = None
for method_name in ("run_loop", "run", "tick", "main", "start"):
    method = getattr(instance, method_name, None)
    if callable(method):
        try:
            method()
        except StopIteration:
            pass
        except SystemExit:
            pass
        except Exception as exc:
            print("LIVE_TRADER_SMOKE_METHOD_FAILED (" + method_name + "): " + type(exc).__name__ + ": " + str(exc))
            sys.exit(2)
        method_run = method_name
        break

if method_run is None:
    print("LIVE_TRADER_SMOKE_NO_RUNNER")
    sys.exit(0)

opens_via_exchange = 0
closes_via_exchange = 0
ex = getattr(instance, "ex", None) or getattr(instance, "exchange", None) or getattr(instance, "client", None)
if ex is not None:
    opens_via_exchange = len(getattr(ex, "_opened", []) or [])
    closes_via_exchange = len(getattr(ex, "_closed", []) or [])
opens = max(opens_via_exchange, len(_open_calls))
closes = max(closes_via_exchange, len(_close_calls))

verdict_exit = _emit_behavioral_verdict(opens, closes, has_sl)
print("LIVE_TRADER_SMOKE_OK_METHOD_" + method_run)
sys.exit(verdict_exit)
"""


def _run_live_trader_smoke(
    code_dir: str, env: Dict[str, str], timeout_seconds: int = 15
) -> Tuple[Optional[bool], List[QuantSmokeIssue], str]:
    """Run the live_trader.py smoke subprocess. Returns (passes, issues, log).

    ``passes`` is None when the bundle has no ``live_trader.py`` (skip);
    True when the smoke ran cleanly; False when it raised inside the
    monkeypatched stub. Issues are pre-built QuantSmokeIssue records for the
    caller to translate into ReviewIssue.
    """
    script = _build_live_trader_smoke_script(code_dir)
    if script is None:
        return None, [], "[live_trader_smoke] skipped: no live_trader.py in bundle"

    script_path = os.path.join(code_dir, "__live_trader_smoke.py")
    try:
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
    except OSError as exc:
        return None, [], f"[live_trader_smoke] could not write script: {exc}"

    sub_env = dict(env)
    sub_env["CODEX_LIVE_TRADER_PATH"] = os.path.join(code_dir, "live_trader.py")
    sub_env["CODEX_VALIDATION"] = "1"
    sub_env["BACKTEST_NO_NETWORK"] = "1"

    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=code_dir,
            env=sub_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        try:
            os.remove(script_path)
        except OSError:
            pass
        issue = QuantSmokeIssue(
            severity="high",
            category="bug",
            description=(
                f"live_trader smoke timed out after {timeout_seconds}s. The "
                "live trader hangs on its first tick — likely an unguarded "
                "blocking call (network, sleep, input prompt) or a tight loop "
                "with no exit condition."
            ),
            file="live_trader.py",
            suggestion=(
                "Honour BACKTEST_NO_NETWORK=1 / CODEX_VALIDATION=1 in any "
                "blocking path. Add a max-iterations guard so the run loop "
                "exits cleanly under smoke-mode."
            ),
            rule="Q020-live-trader-timeout",
        )
        return False, [issue], f"[live_trader_smoke] TIMEOUT after {timeout_seconds}s"
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass

    stdout = _truncate(proc.stdout or "")
    stderr = _truncate(proc.stderr or "")
    log_parts: List[str] = []
    if stdout:
        log_parts.append("[live_trader_smoke stdout]\n" + stdout)
    if stderr:
        log_parts.append("[live_trader_smoke stderr]\n" + stderr)
    log_parts.append(f"[live_trader_smoke] exit_code={proc.returncode}")
    log = "\n\n".join(log_parts)

    if proc.returncode == 0:
        return True, [], log

    if proc.returncode == 3:
        # v1.0.5 round 3 (Q024): behavioral assertion failed — positions were
        # opened but no close/exit ever fired even after a 40% drawdown. This
        # catches the silent stop-loss-never-fires bug class that Q020-Q023
        # cannot see (no exception is raised; the SL branch is just
        # unreachable due to a time-gate / off-by-one / early-return).
        first = _PRICE_SERIES_FIRST
        last = _PRICE_SERIES_LAST
        description = (
            "live_trader behavioral check: positions were opened but no "
            "close/exit/sell call ever fired after the price ramped from "
            f"{first:.2f} to {last:.2f} ("
            f"{(last/first - 1.0)*100.0:.1f}%). The stop-loss / take-profit "
            "branch in run_loop / _manage_positions / _check_exits is "
            "unreachable in production — typically caused by: (a) a time-gate "
            "that is always False (e.g. now < entry_time + hold_minutes), "
            "(b) an off-by-one comparison (`>=` vs `>`), or (c) an early "
            "return in the manage-positions step that skips the SL branch."
        )
        suggestion = (
            "Reproduce: stub ccxt with a 100→60 price ramp, instantiate the "
            "LiveTrader, call run_loop once. Add an explicit "
            "assert: at least one close_position call must fire. Trace which "
            "condition was False at tick 10 — that is the silent SL bug."
        )
        issue = QuantSmokeIssue(
            severity="high",
            category="bug",
            description=description,
            file="live_trader.py",
            suggestion=suggestion,
            rule="Q024-live-trader-sl-unreachable",
        )
        return False, [issue], log

    exc_class, exc_message = _extract_error_summary(proc.stderr or "")
    rule = "Q021-live-trader-failed"
    if exc_class == "TypeError":
        rule = "Q022-live-trader-typeerror"
    elif exc_class == "AttributeError":
        rule = "Q023-live-trader-attributeerror"
    description = (
        f"live_trader smoke failed (exit {proc.returncode}) with stubbed "
        "exchange and a 100→60 scripted price ramp over 10 ticks."
    )
    if exc_class:
        description += f" Last exception: {exc_class}"
        if exc_message:
            description += f": {exc_message}"
    issue = QuantSmokeIssue(
        severity="high",
        category="bug",
        description=description,
        file="live_trader.py",
        suggestion=(
            "Reproduce: monkeypatch ccxt with a stub returning fixed prices, "
            "construct LiveTrader (or equivalent class), and call run_loop / "
            "tick / main once. Fix the import-time or first-tick error."
        ),
        rule=rule,
    )
    return False, [issue], log


def quant_smoke_dryrun(
    code_dir: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    *,
    inject_synthetic_data: bool = True,
) -> QuantSmokeResult:
    """
    Run a synthetic-data dry-run of the Quant bundle's backtest entrypoint.

    Parameters
    ----------
    code_dir : str
        Path to the bundle root (the directory containing ``strategy.py`` /
        ``backtest.py`` / etc.).
    timeout_seconds : int, default 60
        Hard subprocess timeout. Anything longer is treated as a failure
        (stuck loop, hung network call) — the caller already runs ``py_compile``,
        so a healthy backtest on 30 rows of synthetic data finishes in seconds.
    inject_synthetic_data : bool, default True
        Set to False to skip writing ``data/sample_data.csv`` — useful when a
        caller wants to reuse a cached fixture or test the bundle's own
        synthetic-data fallback.

    Returns
    -------
    QuantSmokeResult
        ``.passes`` is True iff the entrypoint exits 0 with no traceback.
    """
    result = QuantSmokeResult(passes=False)
    log_chunks: List[str] = []

    if not os.path.isdir(code_dir):
        result.skipped = True
        result.skip_reason = f"Not a directory: {code_dir}"
        result.passes = True  # nothing to validate against
        return result

    if not _looks_like_quant_bundle(code_dir):
        result.skipped = True
        result.skip_reason = (
            "No Quant-mode marker files found (strategy.py / backtest.py / trade.py / data_provider.py). "
            "Skipping synthetic dry-run."
        )
        result.passes = True
        log_chunks.append("[quant_smoke] skipped: no Quant marker files")
        result.log = "\n".join(log_chunks)
        return result

    written_paths: List[str] = []
    if inject_synthetic_data:
        # v1.0.5 round 2: opt-in dirty-data fixture (NaN volume / 0 volume /
        # time gap / partial NaN OHLC). Default OFF to keep the regression
        # baseline green for clean bundles; enable via env var when the
        # caller wants to validate the bundle's data-cleaning robustness.
        dirty_env = os.environ.get(
            "CRUCIBLE_QUANT_DRYRUN_DIRTY_DATA", ""
        ).strip().lower() in ("1", "true", "yes", "on")
        try:
            written_paths = _write_synthetic_csv(
                code_dir, inject_anomalies=dirty_env
            )
            if written_paths:
                log_chunks.append(
                    "[quant_smoke] synthesised OHLCV"
                    + (" (dirty)" if dirty_env else "")
                    + ": " + ", ".join(written_paths)
                )
            else:
                log_chunks.append(
                    "[quant_smoke] synthetic CSVs already present in bundle"
                )
        except Exception as exc:
            log_chunks.append(f"[quant_smoke] failed to write synthetic data: {exc}")

    entrypoint_rel = _find_executable_entrypoint(code_dir)
    cmd: Optional[List[str]] = None
    label: Optional[str] = None
    if entrypoint_rel is not None:
        cmd = [sys.executable, entrypoint_rel]
        label = entrypoint_rel
    else:
        # Try `python -m <module>` fallback.
        for mod in _FALLBACK_MODULE_ENTRYPOINTS:
            if _has_module(code_dir, mod):
                cmd = [sys.executable, "-m", mod]
                label = f"-m {mod}"
                break

    if cmd is None:
        result.skipped = True
        result.skip_reason = (
            "No backtest entrypoint detected (looked for "
            + ", ".join(_PREFERRED_ENTRYPOINTS)
            + " or `python -m "
            + "/".join(_FALLBACK_MODULE_ENTRYPOINTS)
            + "`)."
        )
        result.passes = True
        log_chunks.append(f"[quant_smoke] skipped: {result.skip_reason}")
        result.log = "\n".join(log_chunks)
        return result

    result.entrypoint_used = label

    env = _build_subprocess_env(code_dir)

    log_chunks.append(f"[quant_smoke] running: {' '.join(cmd)} (timeout={timeout_seconds}s)")

    try:
        proc = subprocess.run(
            cmd,
            cwd=code_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        log_chunks.append(
            f"[quant_smoke] TIMEOUT after {timeout_seconds}s — likely an "
            "infinite loop, an unguarded network call, or a missing data fixture."
        )
        if exc.stdout:
            log_chunks.append("[quant_smoke stdout]\n" + _truncate(exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", errors="replace")))
        if exc.stderr:
            log_chunks.append("[quant_smoke stderr]\n" + _truncate(exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", errors="replace")))
        result.append_issue(
            QuantSmokeIssue(
                severity="high",
                category="bug",
                description=(
                    f"Backtest dry-run timed out after {timeout_seconds}s while running "
                    f"`{label}` with synthetic data. The bundle hangs before producing any "
                    "result — likely an infinite loop, an unguarded network call, or a "
                    "blocking input prompt."
                ),
                file=label if isinstance(label, str) and label.endswith(".py") else None,
                suggestion=(
                    "Avoid blocking calls when CODEX_VALIDATION=1 or BACKTEST_NO_NETWORK=1 is set. "
                    "Add an explicit timeout to network requests and a fast-exit check at the top of main()."
                ),
                rule="Q010-quant-dryrun-timeout",
            )
        )
        result.log = "\n".join(log_chunks)
        return result
    except FileNotFoundError as exc:
        log_chunks.append(f"[quant_smoke] interpreter or script not found: {exc}")
        result.append_issue(
            QuantSmokeIssue(
                severity="high",
                category="bug",
                description=(
                    f"Could not launch backtest dry-run: {exc}. The expected entrypoint "
                    f"({label}) is missing or unreadable."
                ),
                file=label if isinstance(label, str) and label.endswith(".py") else None,
                suggestion="Generate a runnable backtest entrypoint at the bundle root.",
                rule="Q011-quant-entrypoint-missing",
            )
        )
        result.log = "\n".join(log_chunks)
        return result

    stdout = _truncate(proc.stdout or "")
    stderr = _truncate(proc.stderr or "")
    if stdout:
        log_chunks.append("[quant_smoke stdout]\n" + stdout)
    if stderr:
        log_chunks.append("[quant_smoke stderr]\n" + stderr)
    log_chunks.append(f"[quant_smoke] exit_code={proc.returncode}")

    if proc.returncode == 0:
        result.passes = True
        # v1.0.5 round 2: chained live_trader smoke. Only runs when the
        # backtest dry-run already succeeded — if backtest crashed, fix that
        # first; the live trader almost always shares the same root cause.
        lt_passes, lt_issues, lt_log = _run_live_trader_smoke(code_dir, env)
        if lt_passes is None:
            result.live_trader_skipped = True
        else:
            result.live_trader_passes = bool(lt_passes)
            if not lt_passes:
                result.passes = False
                for issue in lt_issues:
                    result.append_issue(issue)
        result.live_trader_log = lt_log or ""
        if lt_log:
            log_chunks.append(lt_log)
        result.log = "\n".join(log_chunks)
        return result

    exc_class, exc_message = _extract_error_summary(proc.stderr or "")
    description = (
        f"Backtest dry-run failed (exit {proc.returncode}) running `{label}` "
        "against 30 rows of synthetic OHLCV data."
    )
    if exc_class:
        description += f" Last exception: {exc_class}"
        if exc_message:
            description += f": {exc_message}"
    suggestion = (
        "Reproduce locally with the same command; the synthetic CSV at "
        "data/sample_data.csv has 30 GBM bars. Fix the import/runtime error in the "
        "implicated module."
    )
    rule = "Q012-quant-dryrun-failed"
    if exc_class == "TypeError":
        rule = "Q013-quant-dryrun-typeerror"
    elif exc_class == "AttributeError":
        rule = "Q014-quant-dryrun-attributeerror"
    elif exc_class == "ImportError" or exc_class == "ModuleNotFoundError":
        rule = "Q015-quant-dryrun-importerror"

    result.append_issue(
        QuantSmokeIssue(
            severity="high",
            category="bug",
            description=description,
            file=label if isinstance(label, str) and label.endswith(".py") else None,
            suggestion=suggestion,
            rule=rule,
        )
    )
    result.log = "\n".join(log_chunks)
    return result
