"""
features/market_stream.py
==========================
Real-time market data snapshot and streaming configuration generator.

Provides three capabilities:

1. **Symbol detection** — scans the ``code/`` sub-directory of the run
   directory for Python source files and extracts likely ticker symbols using
   a capitalised-word regex filtered against a 50-symbol allowlist.

2. **Market snapshot** — for each detected (or explicitly configured) symbol,
   fetches current price, volume, and 1-day change percentage via:
   * yfinance (import-guarded, used when installed)
   * Binance REST v3 for recognised crypto pairs (BTC, ETH, BNB, SOL, XRP)
   * Yahoo Finance unofficial chart API as a fallback for equities

3. **Stream config generation** — produces ready-to-use connection config
   dicts for Binance WebSocket and Alpaca WebSocket.

Writes two artefacts to ``run_dir``:
* ``market_snapshot.json``   — live prices and metadata
* ``market_stream_config.json`` — WS endpoint config for runtime use

Environment variables
---------------------
MARKET_STREAM_SYMBOLS          Comma-separated list of symbols, or 'auto'
                                to detect from source files (default: 'auto').
MARKET_STREAM_SNAPSHOT_ONLY    '1' to skip stream config generation
                                (default: '1').
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from crucible.feature_registry import BaseFeature, FeatureConfig, FeatureResult, register

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 50-symbol allowlist of common tickers that uppercase-word regex might match
_TICKER_ALLOWLIST = frozenset([
    # US Equities / ETFs
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'TSLA', 'NVDA', 'NFLX',
    'SPY', 'QQQ', 'IWM', 'DIA', 'GLD', 'SLV', 'TLT', 'HYG',
    'JPM', 'BAC', 'GS', 'MS', 'WFC', 'C',
    'XOM', 'CVX', 'COP', 'OXY',
    'JNJ', 'PFE', 'MRK', 'ABBV',
    'WMT', 'COST', 'TGT', 'AMZN',
    'V', 'MA', 'AXP', 'PYPL',
    'BA', 'LMT', 'RTX', 'NOC',
    # Crypto (base symbols — will be normalised to USDT pairs for Binance)
    'BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'ADA', 'DOGE', 'MATIC', 'DOT',
])

_CRYPTO_BASES = frozenset(['BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'ADA', 'DOGE', 'MATIC', 'DOT'])

# Regex for 2-5 uppercase letter words (potential tickers)
_TICKER_REGEX = re.compile(r'\b([A-Z]{2,5})\b')

_USER_AGENT = 'Mozilla/5.0 (compatible; Crucible/1.0)'


# ---------------------------------------------------------------------------
# Symbol detection
# ---------------------------------------------------------------------------

def detect_symbols(run_dir: str) -> List[str]:
    """Scan Python files in ``run_dir/code/`` for ticker-like symbols.

    Applies a 2–5 uppercase letter regex and filters results against the
    ``_TICKER_ALLOWLIST``.  Returns a deduplicated, sorted list.

    Parameters
    ----------
    run_dir:
        Path to the current pipeline run directory.  The function looks in
        the ``code/`` sub-directory; if that does not exist it falls back to
        ``run_dir`` itself.
    """
    search_root = os.path.join(run_dir, 'code')
    if not os.path.isdir(search_root):
        search_root = run_dir

    found: set[str] = set()
    for dirpath, _dirs, filenames in os.walk(search_root):
        for fname in filenames:
            if not fname.endswith('.py'):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                    text = fh.read()
            except OSError:
                continue
            for match in _TICKER_REGEX.finditer(text):
                candidate = match.group(1)
                if candidate in _TICKER_ALLOWLIST:
                    found.add(candidate)

    return sorted(found)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: int = 8) -> Tuple[int, Any]:
    """Perform a GET request and parse JSON.  Returns (status_code, data)."""
    req = urllib.request.Request(url, headers={'User-Agent': _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(131072).decode('utf-8', errors='replace')
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (json.JSONDecodeError, Exception):
        return 0, {}


# ---------------------------------------------------------------------------
# Snapshot fetcher
# ---------------------------------------------------------------------------

def _fetch_via_yfinance(symbol: str) -> Optional[Dict[str, Any]]:
    """Try to get a price snapshot via yfinance (if installed)."""
    try:
        import yfinance as yf  # type: ignore[import]
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        last = float(getattr(info, 'last_price', 0) or 0)
        prev_close = float(getattr(info, 'previous_close', 0) or 0)
        volume = float(getattr(info, 'three_month_average_volume', 0) or 0)
        change_pct = ((last - prev_close) / prev_close * 100.0) if (prev_close and abs(prev_close) > 1e-10) else 0.0
        if last > 0:
            return {
                'last_price': round(last, 6),
                'volume_24h': round(volume, 2),
                'change_pct': round(change_pct, 4),
                'source': 'yfinance',
            }
    except Exception:
        pass
    return None


def _fetch_via_binance(symbol: str) -> Optional[Dict[str, Any]]:
    """Try Binance REST for a recognised crypto symbol (appends USDT pair)."""
    if symbol not in _CRYPTO_BASES:
        return None
    pair = f'{symbol}USDT'
    url = f'https://api.binance.com/api/v3/ticker/24hr?symbol={pair}'
    status, data = _get_json(url)
    if status == 200 and isinstance(data, dict) and data.get('lastPrice'):
        try:
            return {
                'last_price': round(float(data['lastPrice']), 6),
                'volume_24h': round(float(data.get('volume', 0)), 2),
                'change_pct': round(float(data.get('priceChangePercent', 0)), 4),
                'source': 'binance',
            }
        except (ValueError, KeyError):
            pass
    return None


def _fetch_via_yahoo_chart(symbol: str) -> Optional[Dict[str, Any]]:
    """Fallback: Yahoo Finance unofficial chart API."""
    url = (
        f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
        '?interval=1d&range=1d'
    )
    status, data = _get_json(url)
    if status != 200:
        return None
    try:
        result = data['chart']['result'][0]
        meta = result['meta']
        last = float(meta.get('regularMarketPrice', 0) or 0)
        prev_close = float(meta.get('chartPreviousClose', 0) or 0)
        volume = float(meta.get('regularMarketVolume', 0) or 0)
        change_pct = ((last - prev_close) / prev_close * 100.0) if (prev_close and abs(prev_close) > 1e-10) else 0.0
        if last > 0:
            return {
                'last_price': round(last, 6),
                'volume_24h': round(volume, 2),
                'change_pct': round(change_pct, 4),
                'source': 'yahoo_chart',
            }
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


def fetch_snapshot(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch a price snapshot for each symbol using a waterfall of sources.

    Priority order: yfinance → Binance REST (crypto only) → Yahoo Chart.
    If all sources fail, returns a placeholder with ``source: 'unavailable'``.

    Parameters
    ----------
    symbols:
        List of ticker symbols (e.g. ``['AAPL', 'BTC', 'SPY']``).

    Returns
    -------
    Dict mapping symbol → {last_price, volume_24h, change_pct, source}.
    """
    snapshot: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        result = (
            _fetch_via_yfinance(sym)
            or _fetch_via_binance(sym)
            or _fetch_via_yahoo_chart(sym)
        )
        if result is None:
            result = {
                'last_price': 0.0,
                'volume_24h': 0.0,
                'change_pct': 0.0,
                'source': 'unavailable',
            }
        snapshot[sym] = result
    return snapshot


# ---------------------------------------------------------------------------
# Stream config generator
# ---------------------------------------------------------------------------

def generate_stream_config(symbols: List[str]) -> Dict[str, Any]:
    """Return WebSocket streaming config for Binance and Alpaca.

    Crypto symbols get Binance combined stream URLs; equity symbols get
    Alpaca WebSocket subscription lists.

    Parameters
    ----------
    symbols:
        List of ticker symbols.

    Returns
    -------
    Dict with keys ``binance_ws`` and ``alpaca_ws``.
    """
    crypto_syms = [s for s in symbols if s in _CRYPTO_BASES]
    equity_syms = [s for s in symbols if s not in _CRYPTO_BASES]

    # Binance combined stream: wss://stream.binance.com:9443/stream?streams=...
    binance_streams: List[str] = []
    for sym in crypto_syms:
        pair = f'{sym.lower()}usdt'
        binance_streams.append(f'{pair}@ticker')
        binance_streams.append(f'{pair}@kline_1m')

    binance_ws: Dict[str, Any] = {
        'endpoint': 'wss://stream.binance.com:9443/stream',
        'streams': binance_streams,
        'combined_url': (
            'wss://stream.binance.com:9443/stream?streams='
            + '/'.join(binance_streams)
        ) if binance_streams else '',
        'ping_interval_seconds': 180,
        'reconnect_on_disconnect': True,
    }

    # Alpaca WebSocket
    alpaca_ws: Dict[str, Any] = {
        'endpoint': 'wss://stream.data.alpaca.markets/v2/iex',
        'paper_endpoint': 'wss://stream.data.alpaca.markets/v2/test',
        'subscribe': {
            'trades': equity_syms,
            'quotes': equity_syms,
            'bars': equity_syms,
        },
        'auth_required': True,
        'env_key_var': 'ALPACA_API_KEY',
        'env_secret_var': 'ALPACA_SECRET_KEY',
        'ping_interval_seconds': 10,
        'reconnect_on_disconnect': True,
    }

    return {'binance_ws': binance_ws, 'alpaca_ws': alpaca_ws}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_market_stream(run_dir: str) -> Dict[str, Any]:
    """Detect symbols, fetch snapshot, generate stream config, write artefacts.

    Writes:
    * ``{run_dir}/market_snapshot.json``
    * ``{run_dir}/market_stream_config.json``  (unless SNAPSHOT_ONLY)

    Parameters
    ----------
    run_dir:
        Path to the current pipeline run directory.

    Returns
    -------
    Dict with keys: symbols, snapshot, stream_config (may be None).
    """
    symbols_env = os.environ.get('MARKET_STREAM_SYMBOLS', 'auto').strip()
    snapshot_only = os.environ.get('MARKET_STREAM_SNAPSHOT_ONLY', '1').strip().lower() not in ('0', 'false', 'no', 'off')

    if symbols_env.lower() == 'auto':
        symbols = detect_symbols(run_dir)
    else:
        symbols = [s.strip().upper() for s in symbols_env.split(',') if s.strip()]

    # If detection yielded nothing, use a safe default set
    if not symbols:
        symbols = ['SPY', 'QQQ', 'BTC', 'ETH']

    snapshot = fetch_snapshot(symbols)

    snapshot_path = os.path.join(run_dir, 'market_snapshot.json')
    _tmp_snap = snapshot_path + ".tmp"
    try:
        with open(_tmp_snap, 'w', encoding='utf-8') as fh:
            json.dump({'symbols': symbols, 'snapshot': snapshot}, fh, indent=2)
        os.replace(_tmp_snap, snapshot_path)
    except OSError:
        try:
            os.unlink(_tmp_snap)
        except OSError:
            pass

    stream_config: Optional[Dict[str, Any]] = None
    if not snapshot_only:
        stream_config = generate_stream_config(symbols)
        config_path = os.path.join(run_dir, 'market_stream_config.json')
        _tmp_cfg = config_path + ".tmp"
        try:
            with open(_tmp_cfg, 'w', encoding='utf-8') as fh:
                json.dump(stream_config, fh, indent=2)
            os.replace(_tmp_cfg, config_path)
        except OSError:
            try:
                os.unlink(_tmp_cfg)
            except OSError:
                pass

    return {
        'symbols': symbols,
        'snapshot': snapshot,
        'stream_config': stream_config,
    }


# ---------------------------------------------------------------------------
# Feature registration
# ---------------------------------------------------------------------------

@register('market_stream')
class MarketStreamFeature(BaseFeature):
    """Real-time market data snapshot and streaming configuration generator."""

    name = 'market_stream'
    label = 'Market Data Stream'
    requires: list[str] = []

    def run(self, run_dir: str, config: FeatureConfig) -> FeatureResult:
        t0 = time.monotonic()
        try:
            result = run_market_stream(run_dir)
            symbols = result.get('symbols', [])
            snapshot = result.get('snapshot', {})
            available = sum(
                1 for v in snapshot.values() if v.get('source') != 'unavailable'
            )
            summary = (
                f'Market stream: {len(symbols)} symbols detected, '
                f'{available}/{len(symbols)} snapshots fetched'
            )
            return FeatureResult(
                feature=self.name,
                success=True,
                summary=summary,
                details={
                    'symbols': symbols,
                    'snapshots_available': available,
                },
                duration_seconds=time.monotonic() - t0,
            )
        except Exception as exc:
            return FeatureResult(
                feature=self.name,
                success=False,
                summary=str(exc),
                error=str(exc),
                duration_seconds=time.monotonic() - t0,
            )
