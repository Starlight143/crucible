"""Tests for crucible.features.external_data_connectors"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest.mock as mock
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from crucible.features.external_data_connectors import (
    COINGECKO_COIN_IDS,
    FRED_MACRO_SERIES,
    AlphaVantageConnector,
    CoinGeckoConnector,
    DataSourceRegistry,
    ExternalDataConfig,
    ExternalDataResult,
    FetchedDataset,
    FredConnector,
    _csv_rows_from_bytes,
    _http_get,
    _write_csv_atomic,
    prepare_external_data,
)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _make_csv_bytes(header: list, rows: list, delimiter: str = ",") -> bytes:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ── _csv_rows_from_bytes ──────────────────────────────────────────────────────

class TestCsvRowsFromBytes:
    def test_parses_standard_csv(self):
        data = b"timestamp,open,close\n2024-01-01,100,101\n2024-01-02,101,102\n"
        header, rows = _csv_rows_from_bytes(data)
        assert header == ["timestamp", "open", "close"]
        assert len(rows) == 2
        assert rows[0][0] == "2024-01-01"

    def test_handles_empty_bytes(self):
        header, rows = _csv_rows_from_bytes(b"")
        assert header == []
        assert rows == []

    def test_handles_header_only(self):
        data = b"a,b,c\n"
        header, rows = _csv_rows_from_bytes(data)
        assert header == ["a", "b", "c"]
        assert rows == []

    def test_strips_utf8_bom(self):
        # UTF-8 BOM prefix
        data = b"\xef\xbb\xbftimestamp,value\n2024-01-01,42\n"
        header, rows = _csv_rows_from_bytes(data)
        assert header[0] == "timestamp"  # BOM stripped


# ── _write_csv_atomic ─────────────────────────────────────────────────────────

class TestWriteCsvAtomic:
    def test_writes_file(self, tmp_path):
        path = str(tmp_path / "out.csv")
        _write_csv_atomic(path, ["a", "b"], [["1", "2"], ["3", "4"]])
        assert os.path.isfile(path)

    def test_content_is_correct(self, tmp_path):
        path = str(tmp_path / "out.csv")
        _write_csv_atomic(path, ["ts", "val"], [["2024-01-01", "100"]])
        with open(path, "r", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
        assert rows[0] == ["ts", "val"]
        assert rows[1] == ["2024-01-01", "100"]

    def test_no_tmp_file_left(self, tmp_path):
        path = str(tmp_path / "out.csv")
        _write_csv_atomic(path, ["a"], [["1"]])
        tmp = path + ".tmp"
        assert not os.path.isfile(tmp)

    def test_creates_parent_directory(self, tmp_path):
        path = str(tmp_path / "subdir" / "nested" / "out.csv")
        _write_csv_atomic(path, ["x"], [["1"]])
        assert os.path.isfile(path)


# ── ExternalDataConfig ────────────────────────────────────────────────────────

class TestExternalDataConfig:
    def test_resolved_start_defaults_to_one_year_ago(self):
        from datetime import date, timedelta
        cfg = ExternalDataConfig()
        expected_year = (date.today() - timedelta(days=365)).year
        assert str(expected_year) in cfg.resolved_start()

    def test_resolved_end_defaults_to_today(self):
        from datetime import date, timedelta
        # Capture both the today() value at construction time and at assertion
        # time so a UTC midnight rollover between them does not flake the test.
        before = date.today()
        cfg = ExternalDataConfig()
        after = date.today()
        candidates = {
            before.strftime("%Y-%m-%d"),
            after.strftime("%Y-%m-%d"),
            (before + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        assert cfg.resolved_end() in candidates

    def test_explicit_dates_override_defaults(self):
        cfg = ExternalDataConfig(start_date="2022-01-01", end_date="2023-01-01")
        assert cfg.resolved_start() == "2022-01-01"
        assert cfg.resolved_end() == "2023-01-01"

    def test_default_sources_and_symbols(self):
        cfg = ExternalDataConfig()
        assert cfg.sources == ["coingecko"]
        assert cfg.symbols == ["BTC"]


# ── FetchedDataset ────────────────────────────────────────────────────────────

class TestFetchedDataset:
    def test_success_true_when_no_error_and_rows(self):
        ds = FetchedDataset(source="cg", symbol="BTC", rows=10,
                            columns=["ts", "close"], file_path="/x.csv")
        assert ds.success is True

    def test_success_false_when_error(self):
        ds = FetchedDataset(source="cg", symbol="BTC", rows=10,
                            columns=[], file_path="/x.csv", error="timeout")
        assert ds.success is False

    def test_success_false_when_zero_rows(self):
        ds = FetchedDataset(source="cg", symbol="BTC", rows=0,
                            columns=[], file_path="/x.csv")
        assert ds.success is False


# ── AlphaVantageConnector ─────────────────────────────────────────────────────

class TestAlphaVantageConnector:
    def test_require_key_raises_without_key(self):
        conn = AlphaVantageConnector(api_key="")
        with pytest.raises(ValueError, match="ALPHA_VANTAGE_API_KEY"):
            conn.fetch_daily("AAPL", "2023-01-01", "2024-01-01")

    def test_require_key_raises_for_intraday_too(self):
        conn = AlphaVantageConnector(api_key="")
        with pytest.raises(ValueError, match="ALPHA_VANTAGE_API_KEY"):
            conn.fetch_intraday("AAPL")

    def test_fetch_daily_filters_by_date_range(self):
        """fetch_daily should filter rows to the requested date range."""
        header = ["timestamp", "open", "high", "low", "close", "adjusted_close", "volume"]
        rows_data = [
            ["2022-12-30", "100", "101", "99", "100", "100", "1000"],
            ["2023-01-02", "101", "102", "100", "101", "101", "2000"],
            ["2023-06-01", "110", "112", "109", "111", "111", "3000"],
            ["2024-01-02", "120", "121", "119", "120", "120", "4000"],
        ]
        csv_bytes = _make_csv_bytes(header, rows_data)

        conn = AlphaVantageConnector(api_key="FAKE_KEY")
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=csv_bytes,
        ):
            returned_header, returned_rows = conn.fetch_daily(
                "AAPL", "2023-01-01", "2023-12-31"
            )

        # Only rows within 2023 should be returned
        assert len(returned_rows) == 2
        assert returned_rows[0][0] == "2023-01-02"
        assert returned_rows[1][0] == "2023-06-01"

    def test_fetch_daily_sorts_ascending(self):
        header = ["timestamp", "open", "high", "low", "close", "adjusted_close", "volume"]
        rows_data = [
            ["2023-06-01", "110", "112", "109", "111", "111", "3000"],
            ["2023-01-02", "101", "102", "100", "101", "101", "2000"],
        ]
        csv_bytes = _make_csv_bytes(header, rows_data)
        conn = AlphaVantageConnector(api_key="FAKE_KEY")
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=csv_bytes,
        ):
            _, rows = conn.fetch_daily("AAPL", "2023-01-01", "2023-12-31")
        dates = [r[0] for r in rows]
        assert dates == sorted(dates)

    def test_fetch_intraday_maps_interval(self):
        """Ensure the interval mapping translates generic to AV-specific format."""
        csv_bytes = _make_csv_bytes(["timestamp", "open", "close"], [])
        conn = AlphaVantageConnector(api_key="FAKE_KEY")
        captured_urls = []

        def _fake_http_get(url, **_kw):
            captured_urls.append(url)
            return csv_bytes

        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            side_effect=_fake_http_get,
        ):
            conn.fetch_intraday("AAPL", interval="1h")

        assert "60min" in captured_urls[0]

    def test_fetch_intraday_filters_empty_rows(self):
        """
        Regression (v16.0.8): malformed CSV may yield bare [] rows from csv.reader.
        Before the fix, the sort key `r[0] if r else ""` kept empty rows at the
        front of the output, causing IndexError on callers that access row[0].
        After the fix, empty rows are filtered out before sorting.
        """
        # Inject a raw CSV that includes one empty line between data rows
        raw_csv = b"timestamp,open,close\n2024-01-02,101,102\n\n2024-01-01,100,101\n"
        conn = AlphaVantageConnector(api_key="FAKE_KEY")
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=raw_csv,
        ):
            header, rows = conn.fetch_intraday("AAPL", interval="60min")

        # No empty rows in output
        assert all(len(r) > 0 for r in rows), "Empty rows must be filtered from fetch_intraday output"
        # Rows still sorted ascending
        dates = [r[0] for r in rows]
        assert dates == sorted(dates), "fetch_intraday rows must be sorted ascending by timestamp"
        assert len(rows) == 2


# ── CoinGeckoConnector ────────────────────────────────────────────────────────

class TestCoinGeckoConnector:
    def _make_cg_payload(self, n_days: int = 5) -> dict:
        import time as _time
        base_ts_ms = int(_time.time() - n_days * 86400) * 1000
        prices = [[base_ts_ms + i * 86400000, 30000.0 + i * 100] for i in range(n_days)]
        volumes = [[base_ts_ms + i * 86400000, 1e9 + i * 1e7] for i in range(n_days)]
        market_caps = [[base_ts_ms + i * 86400000, 5e11 + i * 1e9] for i in range(n_days)]
        return {"prices": prices, "total_volumes": volumes, "market_caps": market_caps}

    def test_resolves_btc_ticker(self):
        conn = CoinGeckoConnector()
        assert conn._resolve_coin_id("BTC") == "bitcoin"

    def test_resolves_eth_ticker(self):
        conn = CoinGeckoConnector()
        assert conn._resolve_coin_id("ETH") == "ethereum"

    def test_resolves_unknown_as_lowercase(self):
        conn = CoinGeckoConnector()
        assert conn._resolve_coin_id("MYTOKEN") == "mytoken"

    def test_fetch_ohlcv_returns_correct_columns(self):
        payload = self._make_cg_payload(10)
        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            header, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-12-31")
        assert header == ["timestamp", "open", "high", "low", "close", "volume", "market_cap"]

    def test_fetch_ohlcv_rows_count_matches_payload(self):
        payload = self._make_cg_payload(7)
        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-12-31")
        assert len(rows) == 7

    def test_fetch_ohlcv_open_equals_close(self):
        """CoinGecko only provides close price; open/high/low should equal close."""
        payload = self._make_cg_payload(3)
        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-12-31")
        for row in rows:
            assert row[1] == row[2] == row[3] == row[4], "open=high=low=close expected"

    def test_fetch_ohlcv_empty_payload(self):
        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=b'{"prices":[],"total_volumes":[],"market_caps":[]}',
        ):
            header, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-12-31")
        assert rows == []

    def test_fetch_ohlcv_deduplicates_hourly_to_daily(self):
        """
        Regression: CoinGecko returns hourly data for ranges ≤90 days.
        Multiple intra-day entries used to produce duplicate date rows.
        After the fix, only one row per calendar date should appear (the last
        price entry for that date).
        """
        import time as _time
        # Simulate hourly data: 3 entries on the same day (2023-01-01),
        # each at a different hour, plus one entry on the next day.
        base_ts = 1_672_531_200_000  # 2023-01-01 00:00:00 UTC in ms
        hour_ms = 3_600_000
        prices = [
            [base_ts + 0 * hour_ms, 16_000.0],   # 2023-01-01 00:00
            [base_ts + 1 * hour_ms, 16_100.0],   # 2023-01-01 01:00
            [base_ts + 2 * hour_ms, 16_200.0],   # 2023-01-01 02:00 (last for day)
            [base_ts + 24 * hour_ms, 16_300.0],  # 2023-01-02 00:00
        ]
        payload = {"prices": prices, "total_volumes": [], "market_caps": []}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-02")

        # Exactly 2 rows: one per calendar date
        assert len(rows) == 2, (
            f"Expected 2 daily rows (deduplicated from 4 hourly), got {len(rows)}"
        )
        dates = [r[0] for r in rows]
        assert dates == ["2023-01-01", "2023-01-02"]

        # The kept row for 2023-01-01 should be the LAST intra-day entry (16_200.0)
        assert rows[0][4] == str(16_200.0), (
            "Last intra-day price should be kept for the daily close"
        )

    def test_fetch_ohlcv_rows_sorted_ascending_by_date(self):
        """Returned rows must always be sorted ascending regardless of API order."""
        import time as _time
        base_ts = 1_672_531_200_000
        day_ms = 86_400_000
        # Supply prices in reverse (newest first) to simulate unsorted API response
        prices = [
            [base_ts + 2 * day_ms, 18_000.0],
            [base_ts + 1 * day_ms, 17_000.0],
            [base_ts + 0 * day_ms, 16_000.0],
        ]
        payload = {"prices": prices, "total_volumes": [], "market_caps": []}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-03")

        dates = [r[0] for r in rows]
        assert dates == sorted(dates), "Rows must be sorted ascending by date"

    def test_fetch_ohlcv_extra_fields_in_volume_payload_do_not_crash(self):
        """
        Robustness: if CoinGecko ever returns 3-element arrays (e.g. adds an extra
        field), the volumes/market_caps loops must not raise ValueError.
        The old pattern `for a, b in list_of_lists` raises when len(v) > 2;
        the fixed `v[0], v[1]` pattern is safe regardless of extra fields.
        """
        base_ts = 1_672_531_200_000  # 2023-01-01 00:00:00 UTC in ms
        prices = [[base_ts, 16_000.0]]
        # 3-element entries: [timestamp, value, extra_field]
        volumes = [[base_ts, 500.0, "extra_vol_field"]]
        market_caps = [[base_ts, 3e11, "extra_mc_field"]]
        payload = {"prices": prices, "total_volumes": volumes, "market_caps": market_caps}

        conn = CoinGeckoConnector()
        # Should not raise ValueError regardless of extra array elements
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            header, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-01")

        assert len(rows) == 1, "One price entry should yield exactly one daily row"
        assert float(rows[0][5]) == pytest.approx(500.0), "Volume must be read from index 1"
        assert rows[0][6] == str(3e11), "Market cap must be read from index 1"

    def test_fetch_ohlcv_keeps_last_market_cap_per_day(self):
        """
        Market cap is a stock (point-in-time) metric: when multiple intra-day
        entries exist for the same date, the LAST observed value should be kept,
        not the first or a sum.
        Three hourly market caps (1e11, 2e11, 3e11) for the same day must yield
        3e11 (the last one), not 6e11 (sum) or 1e11 (first).
        """
        base_ts = 1_672_531_200_000  # 2023-01-01 00:00:00 UTC in ms
        hour_ms = 3_600_000
        prices = [[base_ts, 16_000.0]]  # one price entry for 2023-01-01
        market_caps = [
            [base_ts + 0 * hour_ms, 1e11],  # 2023-01-01 00:00 UTC
            [base_ts + 1 * hour_ms, 2e11],  # 2023-01-01 01:00 UTC
            [base_ts + 2 * hour_ms, 3e11],  # 2023-01-01 02:00 UTC  ← last/EOD
        ]
        payload = {"prices": prices, "total_volumes": [], "market_caps": market_caps}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-01")

        assert len(rows) == 1
        # market_cap column is index 6: must be last (3e11), not sum (6e11)
        assert float(rows[0][6]) == pytest.approx(3e11), (
            f"Expected last EOD market cap 3e11, got {rows[0][6]}. "
            "Market cap must keep the last intra-day value, not accumulate."
        )

    def test_fetch_ohlcv_accumulates_hourly_volumes_into_daily_total(self):
        """
        Regression (v16.0.4): vol_map[d] = str(val) overwrote the last hourly
        volume instead of summing them.  After the fix, hourly volume entries for
        the same calendar date are summed (100 + 200 + 300 = 600.0).
        """
        base_ts = 1_672_531_200_000  # 2023-01-01 00:00:00 UTC in ms
        hour_ms = 3_600_000
        prices = [[base_ts, 16_000.0]]  # one price entry for 2023-01-01
        volumes = [
            [base_ts + 0 * hour_ms, 100.0],  # 2023-01-01 00:00 UTC
            [base_ts + 1 * hour_ms, 200.0],  # 2023-01-01 01:00 UTC
            [base_ts + 2 * hour_ms, 300.0],  # 2023-01-01 02:00 UTC
        ]
        payload = {"prices": prices, "total_volumes": volumes, "market_caps": []}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-01")

        assert len(rows) == 1, "One price entry should yield exactly one daily row"
        # Volume column is index 5: must be 100 + 200 + 300 = 600.0, not 300.0
        assert float(rows[0][5]) == pytest.approx(600.0), (
            f"Expected accumulated daily volume 600.0, got {rows[0][5]}. "
            "Hourly volumes must be summed, not overwritten."
        )

    def test_fetch_ohlcv_null_volume_entry_is_skipped(self):
        """
        Regression (v16.0.8): CoinGecko may return [ts, null] in total_volumes.
        float(None) raises TypeError; the entry must be silently skipped so that
        other entries for the same day are still accumulated correctly.
        """
        base_ts = 1_672_531_200_000  # 2023-01-01 00:00:00 UTC in ms
        hour_ms = 3_600_000
        prices = [[base_ts, 16_000.0]]
        volumes = [
            [base_ts + 0 * hour_ms, 100.0],   # valid
            [base_ts + 1 * hour_ms, None],     # null from API → must be skipped
            [base_ts + 2 * hour_ms, 200.0],   # valid
        ]
        payload = {"prices": prices, "total_volumes": volumes, "market_caps": []}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-01")

        assert len(rows) == 1
        # Only the two valid entries are summed; the null is skipped → 100 + 200 = 300
        assert float(rows[0][5]) == pytest.approx(300.0), (
            f"Expected 300.0 (null entry skipped), got {rows[0][5]}."
        )

    def test_fetch_ohlcv_null_market_cap_entry_is_skipped(self):
        """
        Regression (v16.0.8): CoinGecko may return [ts, null] in market_caps.
        str(None) = 'None' must NOT be stored; the entry must be skipped so that
        a subsequent valid market-cap entry overwrites the slot cleanly.
        """
        base_ts = 1_672_531_200_000
        hour_ms = 3_600_000
        prices = [[base_ts, 16_000.0]]
        market_caps = [
            [base_ts + 0 * hour_ms, None],       # null → skip
            [base_ts + 1 * hour_ms, 9e11],        # valid last value for the day
        ]
        payload = {"prices": prices, "total_volumes": [], "market_caps": market_caps}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-01")

        assert len(rows) == 1
        mc_val = rows[0][6]
        assert mc_val != "None", "null market cap entry must not produce the string 'None'"
        assert float(mc_val) == pytest.approx(9e11)

    def test_fetch_ohlcv_missing_market_cap_default_is_float_format(self):
        """
        Regression (v16.0.8): when no market_cap data exists for a date, the
        default must be '0.0' (matching volume's str(float) format), not '0'.
        """
        base_ts = 1_672_531_200_000
        prices = [[base_ts, 16_000.0]]
        payload = {"prices": prices, "total_volumes": [], "market_caps": []}

        conn = CoinGeckoConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_ohlcv("BTC", "2023-01-01", "2023-01-01")

        assert len(rows) == 1
        vol_default = rows[0][5]    # volume default when no data
        mc_default = rows[0][6]     # market_cap default when no data
        assert vol_default == mc_default, (
            f"Volume default '{vol_default}' and market_cap default '{mc_default}' "
            "must use the same string representation; both should be '0.0'."
        )


# ── FredConnector ─────────────────────────────────────────────────────────────

class TestFredConnector:
    def test_resolves_cpi_shorthand(self):
        conn = FredConnector()
        assert conn._resolve_series_id("CPI") == "CPIAUCSL"

    def test_resolves_unknown_as_uppercase(self):
        conn = FredConnector()
        assert conn._resolve_series_id("gdp") == "GDP"

    def test_fetch_series_returns_timestamp_value(self):
        payload = {
            "observations": [
                {"date": "2023-01-01", "value": "26500.0"},
                {"date": "2023-04-01", "value": "26800.0"},
            ]
        }
        conn = FredConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            header, rows = conn.fetch_series("GDP", "2023-01-01", "2023-12-31")
        assert header == ["timestamp", "value"]
        assert len(rows) == 2
        assert rows[0] == ["2023-01-01", "26500.0"]

    def test_fetch_series_filters_missing_values(self):
        """FRED uses '.' for missing observations; these should be excluded."""
        payload = {
            "observations": [
                {"date": "2023-01-01", "value": "5.33"},
                {"date": "2023-02-01", "value": "."},
                {"date": "2023-03-01", "value": "5.00"},
            ]
        }
        conn = FredConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_series("FEDFUNDS", "2023-01-01", "2023-12-31")
        assert len(rows) == 2
        values = [r[1] for r in rows]
        assert "." not in values

    def test_fetch_series_empty_observations(self):
        payload = {"observations": []}
        conn = FredConnector()
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            _, rows = conn.fetch_series("GDP", "2023-01-01", "2023-12-31")
        assert rows == []

    def test_fetch_series_omits_api_key_when_unconfigured(self):
        """
        Regression (v16.0.4): when no FRED api_key is configured the URL must NOT
        include an api_key parameter at all.  Previously the code passed the
        literal string "demo" as the key, which caused unexpected 400 errors on
        anonymous requests because FRED does not accept "demo" as a valid key.
        """
        payload = {"observations": [{"date": "2023-01-01", "value": "5.0"}]}
        captured_urls: list = []

        def _capture(url, **_kw):
            captured_urls.append(url)
            return json.dumps(payload).encode("utf-8")

        conn = FredConnector(api_key="")  # no key configured
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            side_effect=_capture,
        ):
            conn.fetch_series("GDP", "2023-01-01", "2023-12-31")

        assert len(captured_urls) == 1
        assert "api_key" not in captured_urls[0], (
            "api_key must not appear in the URL when no key is configured; "
            f"got URL: {captured_urls[0]}"
        )

    def test_fetch_series_includes_api_key_when_configured(self):
        """When an api_key IS configured, it must be present in the request URL."""
        payload = {"observations": [{"date": "2023-01-01", "value": "5.0"}]}
        captured_urls: list = []

        def _capture(url, **_kw):
            captured_urls.append(url)
            return json.dumps(payload).encode("utf-8")

        conn = FredConnector(api_key="MY_TEST_KEY_XYZ")
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            side_effect=_capture,
        ):
            conn.fetch_series("GDP", "2023-01-01", "2023-12-31")

        assert len(captured_urls) == 1
        assert "api_key=MY_TEST_KEY_XYZ" in captured_urls[0], (
            f"api_key must appear in URL when configured; got URL: {captured_urls[0]}"
        )


# ── DataSourceRegistry ────────────────────────────────────────────────────────

class TestDataSourceRegistry:
    def test_normalises_av_alias(self):
        reg = DataSourceRegistry()
        assert reg._normalise_source("av") == "alpha_vantage"

    def test_normalises_gecko_alias(self):
        reg = DataSourceRegistry()
        assert reg._normalise_source("gecko") == "coingecko"

    def test_normalises_stlouis_alias(self):
        reg = DataSourceRegistry()
        assert reg._normalise_source("stlouis") == "fred"

    def test_raises_on_unknown_source(self):
        reg = DataSourceRegistry()
        with pytest.raises(ValueError, match="Unknown data source"):
            reg.fetch("nonexistent", "AAPL", "2023-01-01", "2024-01-01")

    def test_dispatches_coingecko(self):
        reg = DataSourceRegistry()
        payload = {"prices": [], "total_volumes": [], "market_caps": []}
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            header, rows = reg.fetch("coingecko", "BTC", "2023-01-01", "2024-01-01")
        assert header == ["timestamp", "open", "high", "low", "close", "volume", "market_cap"]
        assert rows == []

    def test_dispatches_fred(self):
        reg = DataSourceRegistry()
        payload = {"observations": [{"date": "2023-01-01", "value": "100"}]}
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=json.dumps(payload).encode("utf-8"),
        ):
            header, rows = reg.fetch("fred", "GDP", "2023-01-01", "2024-01-01")
        assert "timestamp" in header
        assert len(rows) == 1


# ── prepare_external_data ─────────────────────────────────────────────────────

class TestPrepareExternalData:
    def _coingecko_response(self, n: int = 5) -> bytes:
        import time as _time
        base_ts = int(_time.time() - n * 86400) * 1000
        prices = [[base_ts + i * 86400000, 30000.0 + i] for i in range(n)]
        volumes = [[base_ts + i * 86400000, 1e9] for i in range(n)]
        mc = [[base_ts + i * 86400000, 5e11] for i in range(n)]
        return json.dumps({"prices": prices, "total_volumes": volumes, "market_caps": mc}).encode("utf-8")

    def test_writes_csv_to_code_data(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        os.makedirs(run_dir)
        config = ExternalDataConfig(sources=["coingecko"], symbols=["BTC"])
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=self._coingecko_response(10),
        ):
            result = prepare_external_data(run_dir, config)
        assert len(result.files_written) == 1
        assert os.path.isfile(result.files_written[0])

    def test_csv_path_contains_source_and_symbol(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        os.makedirs(run_dir)
        config = ExternalDataConfig(sources=["coingecko"], symbols=["ETH"])
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=self._coingecko_response(5),
        ):
            result = prepare_external_data(run_dir, config)
        assert any("coingecko" in f and "ETH" in f for f in result.files_written)

    def test_writes_manifest_json(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        os.makedirs(run_dir)
        config = ExternalDataConfig(sources=["coingecko"], symbols=["BTC"])
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=self._coingecko_response(5),
        ):
            prepare_external_data(run_dir, config)
        manifest = os.path.join(run_dir, "code", "data", "external_data_manifest.json")
        assert os.path.isfile(manifest)

    def test_error_recorded_on_fetch_failure(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        os.makedirs(run_dir)
        config = ExternalDataConfig(sources=["coingecko"], symbols=["BTC"])
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            side_effect=OSError("network error"),
        ):
            result = prepare_external_data(run_dir, config)
        assert len(result.errors) == 1
        assert "BTC" in result.errors[0] or "network error" in result.errors[0]

    def test_total_rows_accumulated(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        os.makedirs(run_dir)
        config = ExternalDataConfig(sources=["coingecko"], symbols=["BTC", "ETH"])
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=self._coingecko_response(10),
        ):
            result = prepare_external_data(run_dir, config)
        assert result.total_rows == 20  # 10 rows per symbol

    def test_no_tmp_files_left_on_success(self, tmp_path):
        run_dir = str(tmp_path / "run_001")
        os.makedirs(run_dir)
        config = ExternalDataConfig(sources=["coingecko"], symbols=["BTC"])
        with mock.patch(
            "crucible.features.external_data_connectors._http_get",
            return_value=self._coingecko_response(5),
        ):
            prepare_external_data(run_dir, config)
        data_dir = os.path.join(run_dir, "code", "data")
        tmp_files = [f for f in os.listdir(data_dir) if f.endswith(".tmp")]
        assert tmp_files == []


# ── _http_get retry behaviour ─────────────────────────────────────────────────

class TestHttpGetRetry:
    def test_retries_on_server_error_and_exhausts(self):
        """
        Regression: range(max(1, max_retries)) ran only max_retries total attempts,
        not the expected 1 initial + max_retries retries = max_retries+1 total.
        With max_retries=3 the loop should run 4 times before raising.
        """
        call_count = 0

        def _fail(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(
                "http://x", 500, "Server Error", {}, None
            )

        with mock.patch("time.sleep"):  # suppress actual sleep
            with mock.patch(
                "crucible.features.external_data_connectors.urllib.request.urlopen",
                side_effect=_fail,
            ):
                with pytest.raises(urllib.error.HTTPError):
                    _http_get("http://example.com", timeout=1, max_retries=3)

        # 1 initial attempt + 3 retries = 4 total
        assert call_count == 4, (
            f"Expected 4 total attempts (1 initial + 3 retries), got {call_count}"
        )

    def test_zero_retries_means_exactly_one_attempt(self):
        """max_retries=0 should make exactly 1 attempt (no retries)."""
        call_count = 0

        def _fail(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            raise urllib.error.HTTPError(
                "http://x", 503, "Unavailable", {}, None
            )

        with mock.patch("time.sleep"):
            with mock.patch(
                "crucible.features.external_data_connectors.urllib.request.urlopen",
                side_effect=_fail,
            ):
                with pytest.raises(urllib.error.HTTPError):
                    _http_get("http://example.com", timeout=1, max_retries=0)

        assert call_count == 1, f"Expected 1 attempt, got {call_count}"

    def test_succeeds_on_second_attempt(self):
        """Should succeed if a retry attempt returns 200."""
        call_count = 0

        def _flaky(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise urllib.error.HTTPError(
                    "http://x", 503, "Unavailable", {}, None
                )
            # Second call succeeds — return a mock response context manager
            cm = mock.MagicMock()
            cm.__enter__ = mock.MagicMock(return_value=cm)
            cm.__exit__ = mock.MagicMock(return_value=False)
            cm.read = mock.MagicMock(return_value=b"ok")
            return cm

        with mock.patch("time.sleep"):
            with mock.patch(
                "crucible.features.external_data_connectors.urllib.request.urlopen",
                side_effect=_flaky,
            ):
                result = _http_get("http://example.com", timeout=1, max_retries=3)

        assert result == b"ok"
        assert call_count == 2

    def test_retries_on_url_error_and_exhausts(self):
        """
        URLError (network-level failures) must also be retried up to max_retries
        times.  With max_retries=2 the loop should run 3 times before raising.
        This complements test_retries_on_server_error_and_exhausts which only
        tests HTTPError.
        """
        call_count = 0

        def _fail(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            raise urllib.error.URLError("Connection refused")

        with mock.patch("time.sleep"):
            with mock.patch(
                "crucible.features.external_data_connectors.urllib.request.urlopen",
                side_effect=_fail,
            ):
                with pytest.raises(urllib.error.URLError):
                    _http_get("http://example.com", timeout=1, max_retries=2)

        # 1 initial attempt + 2 retries = 3 total
        assert call_count == 3, (
            f"Expected 3 total attempts (1 initial + 2 retries) for URLError, got {call_count}"
        )

    def test_no_sleep_after_final_failed_attempt(self):
        """
        Regression: time.sleep() was called even after the *last* failed attempt,
        adding up to 30 s of pointless latency before the exception propagated.
        After the fix, sleep is only called between attempts (never after the last).
        With max_retries=3 (4 total attempts) there should be exactly 3 sleeps.
        """
        def _fail(*_a, **_kw):
            raise urllib.error.HTTPError(
                "http://x", 500, "Server Error", {}, None
            )

        with mock.patch("time.sleep") as mock_sleep:
            with mock.patch(
                "crucible.features.external_data_connectors.urllib.request.urlopen",
                side_effect=_fail,
            ):
                with pytest.raises(urllib.error.HTTPError):
                    _http_get("http://example.com", timeout=1, max_retries=3)

        # 3 sleeps: between attempts 0→1, 1→2, 2→3 — NOT after attempt 3
        assert mock_sleep.call_count == 3, (
            f"Expected 3 sleep calls (between retries only), got {mock_sleep.call_count}. "
            "Sleeping after the final failure adds unnecessary latency."
        )


# ── Constant coverage ─────────────────────────────────────────────────────────

class TestConstants:
    def test_fred_macro_series_not_empty(self):
        assert len(FRED_MACRO_SERIES) > 0

    def test_coingecko_coin_ids_contains_btc(self):
        assert COINGECKO_COIN_IDS["BTC"] == "bitcoin"

    def test_coingecko_coin_ids_contains_eth(self):
        assert COINGECKO_COIN_IDS["ETH"] == "ethereum"
