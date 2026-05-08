"""
features/external_data_connectors.py
=====================================
External market data source connectors for Quant-mode research.

Supports three data providers:

* **Alpha Vantage** — daily-adjusted and intraday OHLCV, company fundamentals
  (API key required: ``ALPHA_VANTAGE_API_KEY``).
* **CoinGecko** — crypto price/volume/market-cap history via the free public
  API (no API key required for the free tier; rate-limited to ~10–30 req/min).
* **FRED** (Federal Reserve Economic Data) — macro economic indicators: GDP,
  CPI, Fed Funds Rate, Unemployment, VIX, etc.
  (API key optional for higher rate limits: ``FRED_API_KEY``).

Architecture
------------
``DataSourceRegistry`` provides a unified ``fetch(source, symbol, ...)``
interface that dispatches to the appropriate connector.

``prepare_external_data(run_dir, config)`` downloads all requested datasets and
writes them as CSV files to ``{run_dir}/code/data/``.  Because
``backtest_runner`` checks for data files in that directory first (step 1 in
its resolution order), running the external connectors *before* the backtest
automatically provides real external data without modifying the backtest runner.

Data source resolution order in backtest_runner (unchanged):
  1. Project already has data files in ``code/data/`` ← this feature writes here
  2. Project has data_provider.py
  3. yfinance
  4. ccxt / Binance
  5. Synthetic GBM

Usage::

    from crucible.features.external_data_connectors import (
        ExternalDataConfig,
        prepare_external_data,
    )

    config = ExternalDataConfig(
        sources=["alpha_vantage", "coingecko"],
        symbols=["AAPL", "BTC"],
        start_date="2023-01-01",
        end_date="2024-01-01",
    )
    result = prepare_external_data(run_dir="/path/to/run", config=config)
    print(result.files_written)

Or via the enhanced runner::

    python run_crucible_enhanced.py run \\
        --external-data alpha_vantage,coingecko \\
        --external-symbols AAPL,BTC \\
        --external-start 2023-01-01

Environment variables
---------------------
ALPHA_VANTAGE_API_KEY       Required for Alpha Vantage source.
ALPHA_VANTAGE_BASE_URL      Override base URL (default: https://www.alphavantage.co).
FRED_API_KEY                Optional; increases FRED rate limits.
FRED_BASE_URL               Override FRED base URL (default: https://api.stlouisfed.org).
COINGECKO_BASE_URL          Override CoinGecko base URL (default: https://api.coingecko.com/api/v3).
EXTERNAL_DATA_TIMEOUT       HTTP request timeout in seconds (default: 30).
EXTERNAL_DATA_MAX_RETRIES   Retry attempts on transient HTTP errors (default: 3).
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


# ── Configuration helpers ─────────────────────────────────────────────────────

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_str(name: str, default: str) -> str:
    return _env.env_str(name, default)


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)


ALPHA_VANTAGE_API_KEY: str = _env_str("ALPHA_VANTAGE_API_KEY", "")
ALPHA_VANTAGE_BASE_URL: str = _env_str("ALPHA_VANTAGE_BASE_URL", "https://www.alphavantage.co")
FRED_API_KEY: str = _env_str("FRED_API_KEY", "")
FRED_BASE_URL: str = _env_str("FRED_BASE_URL", "https://api.stlouisfed.org")
COINGECKO_BASE_URL: str = _env_str("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3")
EXTERNAL_DATA_TIMEOUT: int = _env_int("EXTERNAL_DATA_TIMEOUT", 30)
EXTERNAL_DATA_MAX_RETRIES: int = _env_int("EXTERNAL_DATA_MAX_RETRIES", 3)

# Well-known FRED series IDs for common macro indicators
FRED_MACRO_SERIES: Dict[str, str] = {
    "GDP":       "GDP",        # Gross Domestic Product (quarterly, billions USD)
    "CPI":       "CPIAUCSL",   # Consumer Price Index (monthly, all urban consumers)
    "FEDFUNDS":  "FEDFUNDS",   # Federal Funds Effective Rate (monthly, %)
    "UNRATE":    "UNRATE",     # Unemployment Rate (monthly, %)
    "T10Y2Y":    "T10Y2Y",     # 10-Year minus 2-Year Treasury Spread (daily, %)
    "VIXCLS":    "VIXCLS",     # CBOE Volatility Index (daily)
    "DGS10":     "DGS10",      # 10-Year Treasury Constant Maturity Rate (daily, %)
    "SP500":     "SP500",      # S&P 500 Index (daily)
    "M2SL":      "M2SL",       # M2 Money Stock (weekly, billions USD)
    "DCOILWTICO": "DCOILWTICO", # WTI Crude Oil Price (daily, USD/barrel)
}

# CoinGecko coin IDs for common crypto tickers
COINGECKO_COIN_IDS: Dict[str, str] = {
    "BTC":   "bitcoin",
    "ETH":   "ethereum",
    "SOL":   "solana",
    "BNB":   "binancecoin",
    "USDT":  "tether",
    "USDC":  "usd-coin",
    "XRP":   "ripple",
    "ADA":   "cardano",
    "DOGE":  "dogecoin",
    "AVAX":  "avalanche-2",
    "LINK":  "chainlink",
    "MATIC": "matic-network",
    "DOT":   "polkadot",
    "UNI":   "uniswap",
    "ATOM":  "cosmos",
    "LTC":   "litecoin",
    "BCH":   "bitcoin-cash",
    "NEAR":  "near",
    "ARB":   "arbitrum",
    "OP":    "optimism",
}


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ExternalDataConfig:
    """
    Configuration for one external data fetch session.

    ``sources`` accepts: ``"alpha_vantage"``, ``"coingecko"``, ``"fred"``.

    ``symbols`` are interpreted per-source:

      - ``alpha_vantage``: stock/ETF ticker symbol (e.g. ``"AAPL"``, ``"SPY"``)
      - ``coingecko``: crypto ticker *or* CoinGecko coin ID
        (e.g. ``"BTC"``, ``"bitcoin"``)
      - ``fred``: FRED series ID shorthand or raw ID
        (e.g. ``"CPI"``, ``"CPIAUCSL"``)

    When ``start_date`` / ``end_date`` are empty strings they default to
    1 year ago / today.
    """
    sources: List[str] = field(default_factory=lambda: ["coingecko"])
    symbols: List[str] = field(default_factory=lambda: ["BTC"])
    start_date: str = ""    # YYYY-MM-DD; blank → 1 year ago
    end_date: str = ""      # YYYY-MM-DD; blank → today
    interval: str = "1d"    # used by alpha_vantage intraday when applicable

    def resolved_start(self) -> str:
        return self.start_date or (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")

    def resolved_end(self) -> str:
        return self.end_date or date.today().strftime("%Y-%m-%d")


@dataclass
class FetchedDataset:
    """Metadata for one successfully fetched and written dataset."""
    source: str
    symbol: str
    rows: int
    columns: List[str]
    file_path: str
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None and self.rows > 0


@dataclass
class ExternalDataResult:
    """Aggregated result from one ``prepare_external_data()`` call."""
    datasets: List[FetchedDataset] = field(default_factory=list)
    files_written: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    total_rows: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "datasets": [
                {
                    "source": d.source,
                    "symbol": d.symbol,
                    "rows": d.rows,
                    "columns": d.columns,
                    "file": d.file_path,
                    "error": d.error,
                }
                for d in self.datasets
            ],
            "files_written": self.files_written,
            "errors": self.errors,
            "total_rows": self.total_rows,
        }


# ── HTTP utilities ────────────────────────────────────────────────────────────

def _http_get(
    url: str,
    timeout: int = EXTERNAL_DATA_TIMEOUT,
    max_retries: int = EXTERNAL_DATA_MAX_RETRIES,
) -> bytes:
    """
    Perform a GET request with exponential backoff on transient failures.

    Retries on HTTP 429 / 5xx and network errors.  Raises immediately on
    permanent HTTP errors (4xx except 429).
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max(0, max_retries) + 1):  # 1 initial attempt + max_retries retries; guard against negative values
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Crucible-ExternalData/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504):
                last_exc = exc
                # Only sleep when there is a subsequent attempt to wait for;
                # sleeping after the *last* attempt adds pointless latency before
                # the exception propagates to the caller.
                if attempt < max(0, max_retries):
                    time.sleep(min(2 ** attempt, 30))
                continue
            raise  # Permanent error — don't retry
        except (urllib.error.URLError, OSError) as exc:
            last_exc = exc
            if attempt < max(0, max_retries):
                time.sleep(min(2 ** attempt, 30))
    raise last_exc or OSError("HTTP GET failed with unknown error")


def _csv_rows_from_bytes(
    data: bytes, delimiter: str = ","
) -> Tuple[List[str], List[List[str]]]:
    """Parse CSV bytes; return ``(header, data_rows)``."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _write_csv_atomic(path: str, header: List[str], rows: List[List[str]]) -> None:
    """Write CSV atomically: write to .tmp then rename to final path."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(tmp, path)
    except Exception:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# ── Alpha Vantage connector ───────────────────────────────────────────────────

class AlphaVantageConnector:
    """Fetch OHLCV data from Alpha Vantage's free and premium endpoints."""

    # Mapping from generic interval strings to Alpha Vantage intraday intervals
    _AV_INTERVAL_MAP: Dict[str, str] = {
        "1m": "1min", "5m": "5min", "15m": "15min",
        "30m": "30min", "1h": "60min", "60m": "60min",
    }

    def __init__(
        self,
        api_key: str = ALPHA_VANTAGE_API_KEY,
        base_url: str = ALPHA_VANTAGE_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def _require_key(self) -> None:
        if not self._api_key:
            raise ValueError(
                "Alpha Vantage API key not configured. "
                "Set ALPHA_VANTAGE_API_KEY environment variable."
            )

    def fetch_daily(
        self, symbol: str, start_date: str, end_date: str
    ) -> Tuple[List[str], List[List[str]]]:
        """
        Fetch TIME_SERIES_DAILY_ADJUSTED for *symbol*, filtered to
        [start_date, end_date] (inclusive), sorted ascending by date.
        """
        self._require_key()
        params = urllib.parse.urlencode({
            "function": "TIME_SERIES_DAILY_ADJUSTED",
            "symbol": symbol.upper(),
            "outputsize": "full",
            "datatype": "csv",
            "apikey": self._api_key,
        })
        raw = _http_get(f"{self._base_url}/query?{params}")
        header, rows = _csv_rows_from_bytes(raw)
        if not header or not rows:
            return header, []
        # Column 0 is "timestamp" (YYYY-MM-DD)
        filtered = [
            r for r in rows
            if r and len(r) > 0 and start_date <= r[0] <= end_date
        ]
        filtered.sort(key=lambda r: r[0])
        return header, filtered

    def fetch_intraday(
        self, symbol: str, interval: str = "60min"
    ) -> Tuple[List[str], List[List[str]]]:
        """Fetch TIME_SERIES_INTRADAY for *symbol* at *interval*."""
        self._require_key()
        av_interval = self._AV_INTERVAL_MAP.get(interval, interval)
        params = urllib.parse.urlencode({
            "function": "TIME_SERIES_INTRADAY",
            "symbol": symbol.upper(),
            "interval": av_interval,
            "outputsize": "full",
            "datatype": "csv",
            "apikey": self._api_key,
        })
        raw = _http_get(f"{self._base_url}/query?{params}")
        header, rows = _csv_rows_from_bytes(raw)
        # Filter empty rows (can occur in malformed CSV) before sorting so
        # callers are never handed a bare [] that raises IndexError on r[0].
        rows = [r for r in rows if r]
        rows.sort(key=lambda r: r[0])
        return header, rows


# ── CoinGecko connector ───────────────────────────────────────────────────────

class CoinGeckoConnector:
    """
    Fetch cryptocurrency price/volume/market-cap history from CoinGecko.

    Uses the public free-tier API (no key required).  The ``/market_chart/range``
    endpoint returns hourly granularity for ranges ≤ 90 days and daily for
    longer ranges.
    """

    def __init__(self, base_url: str = COINGECKO_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    def _resolve_coin_id(self, symbol: str) -> str:
        """Map a ticker symbol to a CoinGecko coin ID."""
        upper = symbol.upper()
        return COINGECKO_COIN_IDS.get(upper, symbol.lower())

    def fetch_ohlcv(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        vs_currency: str = "usd",
    ) -> Tuple[List[str], List[List[str]]]:
        """
        Fetch daily price history for *symbol*.

        Returns columns: timestamp, open, high, low, close, volume, market_cap.

        Because CoinGecko's ``market_chart/range`` endpoint provides only
        closing prices (not OHLC), open/high/low are set equal to close.
        Callers requiring true OHLC should use a premium CoinGecko plan.
        """
        coin_id = self._resolve_coin_id(symbol)

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Add 1 day to include end_date fully
        end_dt = min(end_dt + timedelta(days=1), datetime.now(timezone.utc))

        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        params = urllib.parse.urlencode({
            "vs_currency": vs_currency,
            "from": start_ts,
            "to": end_ts,
        })
        raw = _http_get(f"{self._base_url}/coins/{coin_id}/market_chart/range?{params}")
        payload: Dict[str, Any] = json.loads(raw.decode("utf-8"))

        prices: List[List[Any]] = payload.get("prices", [])
        volumes: List[List[Any]] = payload.get("total_volumes", [])
        market_caps: List[List[Any]] = payload.get("market_caps", [])

        # Build date-keyed lookup maps.
        # Volume is a FLOW: accumulate hourly values into a daily total.
        # Market cap is a STOCK: keep the last observed value for each day (EOD).
        # Use explicit index access (v[0], v[1]) consistent with the prices loop
        # below; this avoids ValueError if CoinGecko ever returns arrays with more
        # than 2 elements, unlike the iterable-unpack pattern `for a, b in list`.
        vol_map: Dict[str, float] = {}
        for v in volumes:
            if len(v) < 2:
                continue
            try:
                vol = float(v[1])
            except (TypeError, ValueError):
                # Skip entries where the API returns null or a non-numeric value.
                continue
            d = datetime.fromtimestamp(v[0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            vol_map[d] = vol_map.get(d, 0.0) + vol
        mc_map: Dict[str, str] = {}
        for v in market_caps:
            if len(v) < 2:
                continue
            try:
                mc_val = float(v[1])
            except (TypeError, ValueError):
                # Skip entries where the API returns null or a non-numeric value.
                continue
            d = datetime.fromtimestamp(v[0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            mc_map[d] = str(mc_val)  # last observed value per day (EOD snapshot)

        header = ["timestamp", "open", "high", "low", "close", "volume", "market_cap"]
        # Build a date-keyed dict so that when CoinGecko returns hourly granularity
        # (ranges ≤ 90 days), multiple intra-day entries collapse into one row per
        # calendar date.  We keep the LAST entry for each date (closest to EOD).
        rows_by_date: Dict[str, List[str]] = {}
        for entry in prices:
            if len(entry) < 2:
                continue
            ts_ms_val, price_val = entry[0], entry[1]
            d = datetime.fromtimestamp(ts_ms_val / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            price_s = str(price_val)
            rows_by_date[d] = [
                d,
                price_s, price_s, price_s, price_s,  # open=high=low=close
                str(vol_map.get(d, 0.0)),  # accumulated daily volume (sum of hourly)
                mc_map.get(d, "0.0"),  # EOD market cap; "0.0" matches volume's float format
            ]
        # Return rows sorted ascending by date
        rows: List[List[str]] = [rows_by_date[d] for d in sorted(rows_by_date)]
        return header, rows


# ── FRED connector ────────────────────────────────────────────────────────────

class FredConnector:
    """
    Fetch macro economic time series from the Federal Reserve Economic Data
    (FRED) API.

    API key is optional for the free public tier (FRED allows anonymous access
    with reduced rate limits).  Pass ``FRED_API_KEY`` for higher limits.
    """

    def __init__(
        self,
        api_key: str = FRED_API_KEY,
        base_url: str = FRED_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def _resolve_series_id(self, symbol: str) -> str:
        """Map shorthand symbols (e.g. ``'CPI'``) to official FRED series IDs."""
        return FRED_MACRO_SERIES.get(symbol.upper(), symbol.upper())

    def fetch_series(
        self, symbol: str, start_date: str, end_date: str
    ) -> Tuple[List[str], List[List[str]]]:
        """
        Fetch a FRED observation series as (timestamp, value) rows.

        Missing-value observations (FRED uses ``"."`` for periods with no data)
        are filtered out automatically.
        """
        series_id = self._resolve_series_id(symbol)
        params: Dict[str, str] = {
            "series_id": series_id,
            "observation_start": start_date,
            "observation_end": end_date,
            "file_type": "json",
            "sort_order": "asc",
        }
        # Include api_key only when configured.  Omitting it uses FRED's public
        # anonymous tier (rate-limited but functional); the hardcoded "demo" key
        # is not a real fallback and can cause unexpected 400 errors.
        if self._api_key:
            params["api_key"] = self._api_key
        url = f"{self._base_url}/fred/series/observations?{urllib.parse.urlencode(params)}"
        raw = _http_get(url)
        payload: Dict[str, Any] = json.loads(raw.decode("utf-8"))

        header = ["timestamp", "value"]
        rows: List[List[str]] = []
        for obs in payload.get("observations", []):
            d = str(obs.get("date", "")).strip()
            v = str(obs.get("value", "")).strip()
            # FRED uses "." for missing values
            if d and v and v != ".":
                rows.append([d, v])
        return header, rows


# ── Data source registry ──────────────────────────────────────────────────────

class DataSourceRegistry:
    """
    Unified interface for all external data connectors.

    Dispatch is based on the *source* string:

      ``"alpha_vantage"``  → :class:`AlphaVantageConnector`
      ``"coingecko"``      → :class:`CoinGeckoConnector`
      ``"fred"``           → :class:`FredConnector`

    Common aliases are normalised before dispatch so callers can use
    ``"av"``, ``"gecko"``, etc.
    """

    _ALIASES: Dict[str, str] = {
        "av": "alpha_vantage",
        "alphavantage": "alpha_vantage",
        "alpha-vantage": "alpha_vantage",
        "gecko": "coingecko",
        "cg": "coingecko",
        "coin-gecko": "coingecko",
        "coingecko_public": "coingecko",
        "federal_reserve": "fred",
        "stlouis": "fred",
        "stlouisfed": "fred",
    }

    def __init__(self) -> None:
        self._av = AlphaVantageConnector()
        self._cg = CoinGeckoConnector()
        self._fred = FredConnector()

    def _normalise_source(self, source: str) -> str:
        s = source.lower().strip()
        return self._ALIASES.get(s, s)

    def fetch(
        self,
        source: str,
        symbol: str,
        start_date: str,
        end_date: str,
        interval: str = "1d",
    ) -> Tuple[List[str], List[List[str]]]:
        """
        Fetch data from the named source and return ``(header, rows)``.

        Parameters
        ----------
        source:   Data source name (see class docstring for valid values).
        symbol:   Ticker / coin ID / series ID appropriate for the source.
        start_date, end_date: Date range as ``"YYYY-MM-DD"`` strings.
        interval: Candle interval; only used by the Alpha Vantage intraday path.
        """
        src = self._normalise_source(source)
        if src == "alpha_vantage":
            if interval in ("1d", "daily", "auto"):
                return self._av.fetch_daily(symbol, start_date, end_date)
            return self._av.fetch_intraday(symbol, interval)
        if src == "coingecko":
            return self._cg.fetch_ohlcv(symbol, start_date, end_date)
        if src == "fred":
            return self._fred.fetch_series(symbol, start_date, end_date)
        raise ValueError(
            f"Unknown data source '{source}'. "
            "Supported: alpha_vantage (av), coingecko (gecko), fred."
        )


# ── Public API ────────────────────────────────────────────────────────────────

def validate_config(config: "ExternalDataConfig") -> List[str]:
    """
    Validate that required API keys and environment variables are present
    for the requested data sources.

    Returns a list of warning messages (empty if all checks pass).
    Logs a WARNING for each missing credential.
    """
    import logging
    _logger = logging.getLogger(__name__)
    warnings_list: List[str] = []

    for source in config.sources:
        source_lower = source.lower().strip()
        if source_lower == "alpha_vantage":
            key = os.environ.get("ALPHA_VANTAGE_API_KEY", "").strip()
            if not key or key in ("YOUR_KEY_HERE", "DEMO"):
                msg = (
                    "Alpha Vantage requires ALPHA_VANTAGE_API_KEY — "
                    "data fetch will fail. Set ALPHA_VANTAGE_API_KEY in your .env file. "
                    "Get a free key at https://www.alphavantage.co/support/#api-key"
                )
                _logger.warning("[ExternalData] %s", msg)
                warnings_list.append(msg)
        elif source_lower == "fred":
            # FRED API key is optional but recommended; warn if missing
            key = os.environ.get("FRED_API_KEY", "").strip()
            if not key:
                msg = (
                    "FRED_API_KEY is not set — using anonymous access (rate-limited to "
                    "120 req/min). Set FRED_API_KEY for higher limits. "
                    "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
                )
                _logger.info("[ExternalData] %s", msg)
                # Not added to warnings_list as FRED works without a key
        elif source_lower == "coingecko":
            # CoinGecko free tier requires no key; nothing to validate
            pass
        else:
            msg = f"Unknown data source '{source}' — supported: alpha_vantage, coingecko, fred"
            _logger.warning("[ExternalData] %s", msg)
            warnings_list.append(msg)

    return warnings_list


def prepare_external_data(
    run_dir: str,
    config: ExternalDataConfig,
) -> ExternalDataResult:
    """
    Download all configured datasets and write them as CSV files to
    ``{run_dir}/code/data/``.

    Each output file is named ``{source}_{symbol}.csv``.  A manifest file
    ``external_data_manifest.json`` is also written to the same directory.

    Files are written **atomically** (tmp → rename) so a crash during write
    never leaves a partial CSV that silently corrupts a downstream backtest.

    Parameters
    ----------
    run_dir:
        Path to a completed (or in-progress) run directory.
    config:
        ``ExternalDataConfig`` specifying sources, symbols, and date range.
    """
    data_dir = os.path.join(run_dir, "code", "data")
    os.makedirs(data_dir, exist_ok=True)

    registry = DataSourceRegistry()
    result = ExternalDataResult()

    # Preflight: validate API keys and warn about missing credentials
    _preflight_warnings = validate_config(config)
    if _preflight_warnings:
        for w in _preflight_warnings:
            result.errors.append(f"[preflight] {w}")

    start = config.resolved_start()
    end = config.resolved_end()

    for source in config.sources:
        for symbol in config.symbols:
            safe_source = source.replace("-", "_").replace(" ", "_")
            safe_symbol = symbol.replace("/", "_").replace(":", "_").upper()
            csv_path = os.path.join(data_dir, f"{safe_source}_{safe_symbol}.csv")

            try:
                header, rows = registry.fetch(source, symbol, start, end, config.interval)
                if not rows:
                    err = (
                        f"{source}/{symbol}: no data returned "
                        f"for {start}..{end}"
                    )
                    result.errors.append(err)
                    result.datasets.append(FetchedDataset(
                        source=source, symbol=symbol, rows=0,
                        columns=header, file_path=csv_path, error=err,
                    ))
                    continue

                _write_csv_atomic(csv_path, header, rows)

                result.datasets.append(FetchedDataset(
                    source=source, symbol=symbol, rows=len(rows),
                    columns=header, file_path=csv_path,
                ))
                result.files_written.append(csv_path)
                result.total_rows += len(rows)

            except Exception as exc:  # noqa: BLE001
                err_msg = f"{source}/{symbol}: {exc}"
                result.errors.append(err_msg)
                result.datasets.append(FetchedDataset(
                    source=source, symbol=symbol, rows=0,
                    columns=[], file_path=csv_path, error=err_msg,
                ))

    # Write manifest
    manifest_path = os.path.join(data_dir, "external_data_manifest.json")
    _tmp_manifest = manifest_path + ".tmp"
    try:
        with open(_tmp_manifest, "w", encoding="utf-8") as fh:
            json.dump(result.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(_tmp_manifest, manifest_path)
    except OSError:
        try:
            os.unlink(_tmp_manifest)
        except OSError:
            pass

    return result
