"""Tests for the --metadata feature and supporting metadata utilities."""

import json
import logging
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from tradestation.auth import TradeStationAuth
from tradestation.metadata import (
    convert_df_timezone,
    derive_session_times,
    derive_sessions,
    fetch_quote_snapshots,
    fetch_symbol_details,
    get_exchange_timezone,
    run_metadata,
)

# Red phase: the helper used by the session-derivation fix does not yet exist.
# Import it if available; otherwise provide a placeholder that raises ImportError
# so the test file can still be collected and existing tests remain runnable.
try:
    from tradestation.metadata import select_standard_time_bars
except ImportError:

    def select_standard_time_bars(*_args, **_kwargs):
        raise ImportError("select_standard_time_bars is not yet implemented")


from tradestation.models import DownloadConfig
from tradestation.storage import (
    DailyPartitionedStorage,
    MonthlyPartitionedStorage,
    SingleFileStorage,
)


def _create_sample_df(dates):
    """Create a sample OHLCV DataFrame for storage tests."""
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(dates),
            "open": [100.0] * len(dates),
            "high": [101.0] * len(dates),
            "low": [99.0] * len(dates),
            "close": [100.5] * len(dates),
            "volume": [1000] * len(dates),
        }
    )


def _dst_spanning_session_df():
    """1-min bars for 3 winter days + 3 summer days; CME session 17:00->16:00 CT,
    maintenance pause 16:00-17:00 CT.

    Winter (CST, UTC-6): pause 22:00-23:00 UTC -> bars 00:00-22:00 and 23:00-23:59 each day.
    Summer (CDT, UTC-5): pause 21:00-22:00 UTC -> bars 00:00-21:00 and 22:00-23:59 each day.
    Feeding the FULL frame to derive_sessions() today collapses to a 24h block (the bug).
    """
    ts = []
    for d in ("2024-01-08", "2024-01-09", "2024-01-10"):
        ts += pd.date_range(f"{d} 00:00", f"{d} 22:00", freq="1min").tolist()
        ts += pd.date_range(f"{d} 23:00", f"{d} 23:59", freq="1min").tolist()
    for d in ("2024-07-08", "2024-07-09", "2024-07-10"):
        ts += pd.date_range(f"{d} 00:00", f"{d} 21:00", freq="1min").tolist()
        ts += pd.date_range(f"{d} 22:00", f"{d} 23:59", freq="1min").tolist()
    n = len(ts)
    return pd.DataFrame(
        {
            "datetime": pd.DatetimeIndex(ts),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000] * n,
        }
    )


def _dst_spanning_ny_session_df():
    """1-min bars for a NY 09:30-16:00 ET session across winter and summer.

    Winter (EST, UTC-5): UTC bars 14:30-21:00.
    Summer (EDT, UTC-4): UTC bars 13:30-20:00.
    Mixing both in derive_session_times stretches the UTC start to 13:30 (the bug).
    """
    ts = []
    for d in ("2024-01-08", "2024-01-09", "2024-01-10"):
        ts += pd.date_range(f"{d} 14:30", f"{d} 21:00", freq="1min").tolist()
    for d in ("2024-07-08", "2024-07-09", "2024-07-10"):
        ts += pd.date_range(f"{d} 13:30", f"{d} 20:00", freq="1min").tolist()
    n = len(ts)
    return pd.DataFrame(
        {
            "datetime": pd.DatetimeIndex(ts),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000] * n,
        }
    )


class _FixedDateTime(datetime):
    """datetime replacement that returns a deterministic "now" for tests."""

    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 7, 15, 12, 0, 0)
        if tz is not None:
            return fixed.replace(tzinfo=tz)
        return fixed


class TestDeriveSessionTimes:
    """Tests for derive_session_times weekday/session derivation."""

    def test_session_times_basic(self):
        """Single trading day produces one weekday entry with correct bounds."""
        dates = pd.date_range("2024-01-08 09:30", "2024-01-08 16:00", freq="1min")
        df = pd.DataFrame({"datetime": dates})

        result = derive_session_times(df)

        assert result == {
            "Monday": {
                "start": "09:30",
                "end": "16:00",
                "bar_count": 391,
            },
        }

    def test_session_times_multi_day(self):
        """Multiple weekdays with different session hours are grouped separately."""
        mon = pd.date_range("2024-01-08 09:00", "2024-01-08 11:00", freq="1min")
        tue = pd.date_range("2024-01-09 10:00", "2024-01-09 14:00", freq="1min")
        wed = pd.date_range("2024-01-10 08:00", "2024-01-10 12:00", freq="1min")
        dates = list(mon) + list(tue) + list(wed)
        df = pd.DataFrame({"datetime": dates})

        result = derive_session_times(df)

        assert set(result.keys()) == {"Monday", "Tuesday", "Wednesday"}
        assert result["Monday"]["start"] == "09:00"
        assert result["Monday"]["end"] == "11:00"
        assert result["Monday"]["bar_count"] == 121
        assert result["Tuesday"]["start"] == "10:00"
        assert result["Tuesday"]["end"] == "14:00"
        assert result["Tuesday"]["bar_count"] == 241
        assert result["Wednesday"]["start"] == "08:00"
        assert result["Wednesday"]["end"] == "12:00"
        assert result["Wednesday"]["bar_count"] == 241

    def test_session_times_overnight(self):
        """Overnight bars that cross midnight are grouped by their own timestamp."""
        dates = pd.date_range("2024-01-07 18:00", "2024-01-08 17:00", freq="1min")
        df = pd.DataFrame({"datetime": dates})

        result = derive_session_times(df)

        assert result["Sunday"]["start"] == "18:00"
        assert result["Sunday"]["end"] == "23:59"
        assert result["Sunday"]["bar_count"] == 360
        assert result["Monday"]["start"] == "00:00"
        assert result["Monday"]["end"] == "17:00"
        assert result["Monday"]["bar_count"] == 1021

    def test_session_times_empty_df(self):
        """An empty DataFrame returns an empty mapping."""
        df = pd.DataFrame()

        result = derive_session_times(df)

        assert result == {}

    def test_session_times_datetime_index(self):
        """A DataFrame with a DatetimeIndex is supported."""
        dates = pd.date_range("2024-01-08 09:30", "2024-01-08 16:00", freq="1min")
        df = pd.DataFrame(index=dates, data={"open": [100.0] * len(dates)})

        result = derive_session_times(df)

        assert result == {
            "Monday": {
                "start": "09:30",
                "end": "16:00",
                "bar_count": 391,
            },
        }

    def test_session_times_datetime_column(self):
        """A DataFrame with a 'datetime' column (but no DatetimeIndex) is supported."""
        dates = pd.date_range("2024-01-08 09:30", "2024-01-08 16:00", freq="1min")
        df = pd.DataFrame({"datetime": dates, "open": [100.0] * len(dates)})

        result = derive_session_times(df)

        assert result == {
            "Monday": {
                "start": "09:30",
                "end": "16:00",
                "bar_count": 391,
            },
        }

    def test_session_times_excludes_empty_weekdays(self):
        """Weekdays with no bars do not appear in the output."""
        mon = pd.date_range("2024-01-08 09:00", "2024-01-08 10:00", freq="1min")
        tue = pd.date_range("2024-01-09 09:00", "2024-01-09 10:00", freq="1min")
        wed = pd.date_range("2024-01-10 09:00", "2024-01-10 10:00", freq="1min")
        dates = list(mon) + list(tue) + list(wed)
        df = pd.DataFrame({"datetime": dates})

        result = derive_session_times(df)

        assert set(result.keys()) == {"Monday", "Tuesday", "Wednesday"}
        assert "Saturday" not in result
        assert "Sunday" not in result

    def test_session_times_non_wraparound_dst_mirror(self):
        """Bug 1: mixed DST seasons stretch the UTC session-times window.

        A single-season (winter) canonical frame is required so that
        session_times_utc mirrors session_times with a constant standard
        offset. The unfiltered UTC mix currently returns the stretched
        13:30-21:00 window.
        """
        df = _dst_spanning_ny_session_df()

        mixed = derive_session_times(df)
        for day in ("Monday", "Tuesday", "Wednesday"):
            assert mixed[day]["start"] == "13:30"
            assert mixed[day]["end"] == "21:00"

        canon = select_standard_time_bars(df, "America/New_York")
        utc_times = derive_session_times(canon)
        local_times = derive_session_times(convert_df_timezone(canon, "America/New_York"))

        for day in ("Monday", "Tuesday", "Wednesday"):
            assert utc_times[day]["start"] == "14:30"
            assert utc_times[day]["end"] == "21:00"
            assert local_times[day]["start"] == "09:30"
            assert local_times[day]["end"] == "16:00"


class TestFetchSymbolDetails:
    """Tests for fetch_symbol_details API batching."""

    @pytest.fixture
    def auth(self):
        """Pre-warmed TradeStationAuth instance to avoid network calls."""
        auth = TradeStationAuth("client_id", "client_secret", "refresh_token")
        auth._access_token = "test_token"
        auth._token_expiry = datetime.now() + timedelta(hours=1)
        return auth

    def _mock_response(self, payload, status_code=200):
        resp = Mock()
        resp.status_code = status_code
        resp.text = "error"
        resp.json.return_value = payload
        return resp

    def test_fetch_symbol_details_single_batch(self, auth):
        """A single batch returns the Symbols array with correct headers/timeout."""
        payload = {"Symbols": [{"Symbol": "@ES", "Name": "E-mini S&P 500"}]}

        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            mock_get.return_value = self._mock_response(payload)
            result = fetch_symbol_details(auth, ["@ES"])

        assert result == payload["Symbols"]
        assert mock_get.call_count == 1
        url = mock_get.call_args[0][0]
        assert "marketdata/symbols" in url
        assert "@ES" in url
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer test_token"
        assert mock_get.call_args.kwargs["timeout"] == 30

    def test_fetch_symbol_details_batching(self, auth):
        """60 symbols are split into two calls (50 + 10) with one sleep between."""
        symbols = [f"S{i}" for i in range(60)]
        first_payload = {"Symbols": [{"Symbol": s, "Name": s} for s in symbols[:50]]}
        second_payload = {"Symbols": [{"Symbol": s, "Name": s} for s in symbols[50:]]}

        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                self._mock_response(first_payload),
                self._mock_response(second_payload),
            ]
            result = fetch_symbol_details(auth, symbols)

        assert len(result) == 60
        assert mock_get.call_count == 2

        first_url = mock_get.call_args_list[0][0][0]
        second_url = mock_get.call_args_list[1][0][0]
        assert ",".join(symbols[:50]) in first_url
        assert ",".join(symbols[50:]) in second_url
        assert ",".join(symbols[50:]) not in first_url
        assert ",".join(symbols[:50]) not in second_url
        assert mock_sleep.call_count == 1

    def test_fetch_symbol_details_error_response(self, auth):
        """A 500 response is handled gracefully without raising."""
        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            mock_get.return_value = self._mock_response({}, status_code=500)
            result = fetch_symbol_details(auth, ["@ES"])

        assert result == []
        assert mock_get.call_count == 1

    def test_fetch_symbol_details_logs_error_on_api_error(self, auth, caplog):
        """Per-symbol API errors (e.g. NotFound @BRN) must be logged, not silently dropped."""
        caplog.set_level(logging.ERROR)
        payload = {
            "Symbols": [],
            "Errors": [{"Symbol": "@BRN", "Error": "NotFound", "Message": "invalid symbol"}],
        }

        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            mock_get.return_value = self._mock_response(payload)
            result = fetch_symbol_details(auth, ["@BRN"])

        assert result == []
        assert any(
            record.levelno == logging.ERROR
            and "@BRN" in record.getMessage()
            and ("NotFound" in record.getMessage() or "invalid symbol" in record.getMessage())
            for record in caplog.records
        )


class TestFetchQuoteSnapshots:
    """Tests for fetch_quote_snapshots API batching."""

    @pytest.fixture
    def auth(self):
        """Pre-warmed TradeStationAuth instance to avoid network calls."""
        auth = TradeStationAuth("client_id", "client_secret", "refresh_token")
        auth._access_token = "test_token"
        auth._token_expiry = datetime.now() + timedelta(hours=1)
        return auth

    def _mock_response(self, payload, status_code=200):
        resp = Mock()
        resp.status_code = status_code
        resp.text = "error"
        resp.json.return_value = payload
        return resp

    def test_fetch_quote_snapshots_single_batch(self, auth):
        """A single batch returns the Quotes array with correct headers/timeout."""
        payload = {"Quotes": [{"Symbol": "@ES", "Last": 4500.0}]}

        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            mock_get.return_value = self._mock_response(payload)
            result = fetch_quote_snapshots(auth, ["@ES"])

        assert result == payload["Quotes"]
        assert mock_get.call_count == 1
        url = mock_get.call_args[0][0]
        assert "marketdata/quotes" in url
        assert "@ES" in url
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == "Bearer test_token"
        assert mock_get.call_args.kwargs["timeout"] == 30

    def test_fetch_quote_snapshots_batching(self, auth):
        """150 symbols are split into two calls (100 + 50) with one sleep between."""
        symbols = [f"S{i}" for i in range(150)]
        first_payload = {
            "Quotes": [{"Symbol": s, "Last": float(i)} for i, s in enumerate(symbols[:100])]
        }
        second_payload = {
            "Quotes": [{"Symbol": s, "Last": float(i)} for i, s in enumerate(symbols[100:])]
        }

        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep") as mock_sleep,
        ):
            mock_get.side_effect = [
                self._mock_response(first_payload),
                self._mock_response(second_payload),
            ]
            result = fetch_quote_snapshots(auth, symbols)

        assert len(result) == 150
        assert mock_get.call_count == 2

        first_url = mock_get.call_args_list[0][0][0]
        second_url = mock_get.call_args_list[1][0][0]
        assert ",".join(symbols[:100]) in first_url
        assert ",".join(symbols[100:]) in second_url
        assert ",".join(symbols[100:]) not in first_url
        assert ",".join(symbols[:100]) not in second_url
        assert mock_sleep.call_count == 1

    def test_fetch_quote_snapshots_error(self, auth):
        """A 500 response is handled gracefully without raising."""
        with (
            patch("tradestation.metadata.requests.get") as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            mock_get.return_value = self._mock_response({}, status_code=500)
            result = fetch_quote_snapshots(auth, ["@ES"])

        assert result == []
        assert mock_get.call_count == 1


class TestGetFirstTimestamp:
    """Tests for StorageBackend.get_first_timestamp overrides."""

    def test_get_first_timestamp_single_file(self, temp_data_dir):
        """SingleFileStorage returns the minimum datetime from the file."""
        storage = SingleFileStorage(temp_data_dir)
        df = _create_sample_df(
            [
                "2024-01-08 09:30",
                "2024-01-08 09:35",
                "2024-01-08 09:40",
            ]
        )
        storage.save("ES", df)

        first = storage.get_first_timestamp("ES")

        assert first == datetime(2024, 1, 8, 9, 30)

    def test_get_first_timestamp_daily_partition(self, temp_data_dir):
        """DailyPartitionedStorage returns the minimum datetime across partitions."""
        storage = DailyPartitionedStorage(temp_data_dir)
        df = _create_sample_df(
            [
                "2024-01-08 09:30",
                "2024-01-09 09:35",
            ]
        )
        storage.save("ES", df)

        first = storage.get_first_timestamp("ES")

        assert first == datetime(2024, 1, 8, 9, 30)

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_get_first_timestamp_no_data(self, storage_cls, temp_data_dir):
        """Missing symbol data returns None for all backends."""
        storage = storage_cls(temp_data_dir)

        assert storage.get_first_timestamp("MISSING") is None

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_get_first_timestamp_matches_load_min(self, storage_cls, temp_data_dir):
        """get_first_timestamp equals the min of a full load."""
        storage = storage_cls(temp_data_dir)
        df = _create_sample_df(
            [
                "2024-01-15 09:30",
                "2024-01-16 09:35",
            ]
        )
        storage.save("ES", df)

        loaded = storage.load("ES")
        first = storage.get_first_timestamp("ES")

        assert first == loaded.index.min().to_pydatetime()


class TestRunMetadata:
    """Integration tests for the run_metadata CLI path."""

    @staticmethod
    def _mock_response(payload, status_code=200):
        resp = Mock()
        resp.status_code = status_code
        resp.text = "error"
        resp.json.return_value = payload
        return resp

    @staticmethod
    def _save_symbol(storage_dir, symbol="@ES"):
        """Save a small sample DataFrame to a SingleFileStorage."""
        storage = SingleFileStorage(storage_dir)
        dates = pd.date_range("2024-01-08 09:30", "2024-01-08 09:35", freq="1min")
        df = pd.DataFrame(
            {
                "datetime": dates,
                "open": [100.0] * len(dates),
                "high": [101.0] * len(dates),
                "low": [99.0] * len(dates),
                "close": [100.5] * len(dates),
                "volume": [1000] * len(dates),
            }
        )
        storage.save(symbol, df)

    @staticmethod
    def _save_dst_spanning_symbol(storage_dir, symbol="@ES"):
        """Save the DST-spanning CME session fixture for run_metadata tests."""
        storage = SingleFileStorage(storage_dir)
        df = _dst_spanning_session_df()
        storage.save(symbol, df)

    @staticmethod
    def _make_config(data_dir):
        """Build a DownloadConfig with a temporary data directory."""
        return DownloadConfig(
            client_id="client_id",
            client_secret="client_secret",
            refresh_token="refresh_token",
            data_dir=str(data_dir),
            symbols=[],
        )

    def test_run_metadata_full_flow(self, temp_data_dir, capsys):
        """run_metadata writes metadata.json and prints expected JSON output."""
        self._save_symbol(temp_data_dir, "@ES")
        config = self._make_config(temp_data_dir)

        details = {"Symbols": [{"Symbol": "@ES", "Name": "E-mini S&P", "Exchange": "CME"}]}
        quotes = {"Quotes": [{"Symbol": "@ES", "Last": 4500.0, "Bid": 4499.5, "Ask": 4500.5}]}
        details_resp = self._mock_response(details)
        quotes_resp = self._mock_response(quotes)

        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with (
            patch("tradestation.metadata.load_config", return_value=config),
            patch("tradestation.metadata.TradeStationAuth", return_value=auth),
            patch("tradestation.metadata.datetime", _FixedDateTime),
            patch(
                "tradestation.metadata.requests.get", side_effect=[details_resp, quotes_resp]
            ) as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        captured = capsys.readouterr()
        assert result == 0

        output = json.loads(captured.out)
        assert output["data_dir"] == str(temp_data_dir)
        assert "@ES" in output["symbols"]

        sym = output["symbols"]["@ES"]
        assert sym["symbol"] == "@ES"
        assert sym["api"]["Symbol"] == "@ES"
        assert sym["api"]["Name"] == "E-mini S&P"
        assert sym["api"]["Exchange"] == "CME"
        assert sym["quote"]["Symbol"] == "@ES"
        assert sym["quote"]["Last"] == 4500.0

        dd = sym["downloaded_data"]
        assert dd["first_timestamp"] == "2024-01-08T09:30:00"
        assert dd["last_timestamp"] == "2024-01-08T09:35:00"
        assert dd["total_bars"] == 6
        assert dd["exchange_timezone"] == "America/Chicago"

        storage = SingleFileStorage(temp_data_dir)
        df = storage.load("@ES")
        canon = select_standard_time_bars(df, "America/Chicago")
        expected_session_times = derive_session_times(convert_df_timezone(canon, "America/Chicago"))
        expected_sessions = derive_sessions(convert_df_timezone(canon, "America/Chicago"))
        expected_session_times_utc = derive_session_times(canon)
        expected_sessions_utc = derive_sessions(canon)

        assert dd["session_times"] == expected_session_times
        assert dd["session_times_utc"] == expected_session_times_utc
        assert dd["sessions"] == expected_sessions
        assert dd["sessions_utc"] == expected_sessions_utc
        assert len(dd["sessions"]) == len(dd["sessions_utc"])
        for a, b in zip(dd["sessions"], dd["sessions_utc"], strict=True):
            assert a["spans_midnight"] == b["spans_midnight"]
            assert a["duration_hours"] == b["duration_hours"]

        assert output["timezone"] == "UTC"
        assert mock_get.call_count == 2

        metadata_path = temp_data_dir / "metadata.json"
        assert metadata_path.exists()
        assert json.loads(metadata_path.read_text(encoding="utf-8")) == output

    def test_run_metadata_no_downloaded_symbols(self, temp_data_dir, capsys):
        """Empty data directory prints an error and returns 1."""
        config = self._make_config(temp_data_dir)

        with patch("tradestation.metadata.load_config", return_value=config):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        captured = capsys.readouterr()
        assert result == 1
        assert "No downloaded symbols found" in captured.err

    def test_run_metadata_api_error(self, temp_data_dir, capsys):
        """API errors leave api/quote null but still emit downloaded_data."""
        self._save_symbol(temp_data_dir, "@ES")
        config = self._make_config(temp_data_dir)

        error_resp = self._mock_response({}, status_code=500)
        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with (
            patch("tradestation.metadata.load_config", return_value=config),
            patch("tradestation.metadata.TradeStationAuth", return_value=auth),
            patch("tradestation.metadata.requests.get", return_value=error_resp) as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        captured = capsys.readouterr()
        assert result == 0

        output = json.loads(captured.out)
        assert "@ES" in output["symbols"]
        sym = output["symbols"]["@ES"]
        assert sym["api"] is None
        assert sym["quote"] is None

        dd = sym["downloaded_data"]
        assert dd["first_timestamp"] == "2024-01-08T09:30:00"
        assert dd["last_timestamp"] == "2024-01-08T09:35:00"
        assert dd["total_bars"] == 6
        assert "Monday" in dd["session_times"]

        assert mock_get.call_count == 2

    def test_run_metadata_dst_spanning_mirror(self, temp_data_dir, capsys):
        """Bug 1: sessions and sessions_utc are mirror images for DST-spanning data.

        The current run_metadata derives sessions_utc from the full mixed-season
        UTC frame, so the UTC maintenance gap collapses to a 24h block. After
        the fix, both are derived from the same canonical single-season row set.
        """
        self._save_dst_spanning_symbol(temp_data_dir, "@ES")
        config = self._make_config(temp_data_dir)

        details = {"Symbols": [{"Symbol": "@ES", "Name": "E-mini S&P", "Exchange": "CME"}]}
        quotes = {"Quotes": [{"Symbol": "@ES", "Last": 4500.0}]}
        details_resp = self._mock_response(details)
        quotes_resp = self._mock_response(quotes)

        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with (
            patch("tradestation.metadata.load_config", return_value=config),
            patch("tradestation.metadata.TradeStationAuth", return_value=auth),
            patch("tradestation.metadata.datetime", _FixedDateTime),
            patch(
                "tradestation.metadata.requests.get", side_effect=[details_resp, quotes_resp]
            ) as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        assert result == 0
        output = json.loads(capsys.readouterr().out)
        dd = output["symbols"]["@ES"]["downloaded_data"]
        assert dd["exchange_timezone"] == "America/Chicago"

        assert len(dd["sessions"]) == 1
        assert len(dd["sessions_utc"]) == 1
        assert dd["sessions"][0] == {
            "start": "17:00",
            "end": "16:00",
            "duration_hours": 23.02,
            "spans_midnight": True,
        }
        assert dd["sessions_utc"][0] == {
            "start": "23:00",
            "end": "22:00",
            "duration_hours": 23.02,
            "spans_midnight": True,
        }

        assert len(dd["sessions"]) == len(dd["sessions_utc"])
        for a, b in zip(dd["sessions"], dd["sessions_utc"], strict=True):
            assert a["spans_midnight"] == b["spans_midnight"]
            assert a["duration_hours"] == b["duration_hours"]

        assert output["timezone"] == "UTC"
        assert mock_get.call_count == 2

    def test_run_metadata_warns_on_unmapped_ice_exchange(self, temp_data_dir, caplog):
        """Unmapped ICE-family exchange strings emit a loud ERROR and keep tz/sessions null."""
        caplog.set_level(logging.ERROR)
        symbol = "@ICEFOO"
        self._save_symbol(temp_data_dir, symbol)
        config = self._make_config(temp_data_dir)

        details = [{"Symbol": symbol, "Name": "Test ICE", "Exchange": "ICEFOO"}]
        quotes = []

        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with (
            patch("tradestation.metadata.load_config", return_value=config),
            patch("tradestation.metadata.TradeStationAuth", return_value=auth),
            patch("tradestation.metadata.fetch_symbol_details", return_value=details),
            patch("tradestation.metadata.fetch_quote_snapshots", return_value=quotes),
            patch("tradestation.metadata.datetime", _FixedDateTime),
        ):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        assert result == 0
        assert any(
            record.levelno == logging.ERROR
            and symbol in record.getMessage()
            and "ICEFOO" in record.getMessage()
            and "timezone" in record.getMessage().lower()
            and "null" in record.getMessage().lower()
            for record in caplog.records
        )

    def test_mapped_iceus_does_not_warn(self, temp_data_dir, caplog):
        """A mapped ICE U.S. exchange resolves and does not trigger an unmapped-ICE warning.

        Guard for the Green phase: once ICEUS is in the map, run_metadata must not
        emit an ERROR about an unmapped ICE exchange for an ICEUS symbol.
        """
        caplog.set_level(logging.ERROR)
        symbol = "@SB"
        self._save_symbol(temp_data_dir, symbol)
        config = self._make_config(temp_data_dir)

        details = [{"Symbol": symbol, "Name": "Sugar", "Exchange": "ICEUS"}]
        quotes = []

        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with (
            patch("tradestation.metadata.load_config", return_value=config),
            patch("tradestation.metadata.TradeStationAuth", return_value=auth),
            patch("tradestation.metadata.fetch_symbol_details", return_value=details),
            patch("tradestation.metadata.fetch_quote_snapshots", return_value=quotes),
            patch("tradestation.metadata.datetime", _FixedDateTime),
        ):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        assert result == 0
        assert not any(
            record.levelno == logging.ERROR
            and symbol in record.getMessage()
            and "ICE" in record.getMessage()
            and (
                "unmapped" in record.getMessage().lower()
                or "timezone" in record.getMessage().lower()
            )
            for record in caplog.records
        )


class TestDeriveSessions:
    """Tests for derive_sessions contiguous session-block derivation."""

    def test_sessions_empty_df(self):
        df = pd.DataFrame()
        result = derive_sessions(df)
        assert result == []

    def test_sessions_around_the_clock(self):
        dates = pd.date_range("2024-01-08 00:00", "2024-01-08 23:59", freq="1min")
        df = pd.DataFrame({"datetime": dates})
        result = derive_sessions(df)
        assert result == [
            {"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}
        ]

    def test_sessions_single_block_no_midnight(self):
        dates = []
        for day in pd.date_range("2024-01-08", periods=3, freq="D"):
            dates += pd.date_range(
                f"{day.date()} 09:00", f"{day.date()} 17:00", freq="1min"
            ).tolist()
        df = pd.DataFrame({"datetime": dates})
        result = derive_sessions(df)
        assert result == [
            {"start": "09:00", "end": "17:00", "duration_hours": 8.02, "spans_midnight": False}
        ]

    def test_sessions_spans_midnight(self):
        dates = []
        for i in range(3):
            base = pd.Timestamp("2024-01-08") + pd.Timedelta(days=i)
            nxt = base + pd.Timedelta(days=1)
            dates += pd.date_range(
                f"{base.date()} 22:00", f"{nxt.date()} 04:00", freq="1min"
            ).tolist()
        df = pd.DataFrame({"datetime": dates})
        result = derive_sessions(df)
        assert result == [
            {"start": "22:00", "end": "04:00", "duration_hours": 6.02, "spans_midnight": True}
        ]

    def test_sessions_two_blocks(self):
        dates = []
        for day in pd.date_range("2024-01-08", periods=3, freq="D"):
            dates += pd.date_range(
                f"{day.date()} 09:00", f"{day.date()} 12:00", freq="1min"
            ).tolist()
            dates += pd.date_range(
                f"{day.date()} 13:00", f"{day.date()} 17:00", freq="1min"
            ).tolist()
        df = pd.DataFrame({"datetime": dates})
        result = derive_sessions(df)
        assert result == [
            {"start": "09:00", "end": "12:00", "duration_hours": 3.02, "spans_midnight": False},
            {"start": "13:00", "end": "17:00", "duration_hours": 4.02, "spans_midnight": False},
        ]

    def test_sessions_real_world_wheat(self):
        dates = []
        for day in pd.date_range("2024-01-08", periods=3, freq="D"):
            dates += pd.date_range(
                f"{day.date()} 00:01", f"{day.date()} 19:20", freq="1min"
            ).tolist()
        df = pd.DataFrame({"datetime": dates})
        result = derive_sessions(df)
        assert result == [
            {"start": "00:01", "end": "19:20", "duration_hours": 19.33, "spans_midnight": False}
        ]

    def test_sessions_small_gaps_ignored(self):
        removed_minutes = set()
        for h, m, length in [(10, 5, 3), (12, 15, 5), (15, 30, 2)]:
            start = h * 60 + m
            removed_minutes.update(range(start + 1, start + 1 + length))

        dates = []
        for day in pd.date_range("2024-01-08", periods=3, freq="D"):
            day_dates = pd.date_range(f"{day.date()} 09:00", f"{day.date()} 17:00", freq="1min")
            dates += [d for d in day_dates if d.hour * 60 + d.minute not in removed_minutes]

        df = pd.DataFrame({"datetime": dates})
        result = derive_sessions(df)
        assert result == [
            {"start": "09:00", "end": "17:00", "duration_hours": 8.02, "spans_midnight": False}
        ]

    def test_sessions_datetime_index_and_column(self):
        dates = []
        for day in pd.date_range("2024-01-08", periods=3, freq="D"):
            dates += pd.date_range(
                f"{day.date()} 09:00", f"{day.date()} 17:00", freq="1min"
            ).tolist()
        df_index = pd.DataFrame(index=pd.to_datetime(dates), data={"open": [100.0] * len(dates)})
        df_col = pd.DataFrame({"datetime": dates, "open": [100.0] * len(dates)})
        expected = [
            {"start": "09:00", "end": "17:00", "duration_hours": 8.02, "spans_midnight": False}
        ]
        assert derive_sessions(df_index) == expected
        assert derive_sessions(df_col) == expected

    def test_select_standard_time_bars_prefers_winter(self):
        """Bug 1: the standard-time selector keeps only winter (standard) bars.

        For a DST-spanning frame and a known Chicago timezone the helper must
        return only the January (CST) rows; July (CDT) rows are excluded.
        """
        df = _dst_spanning_session_df()
        result = select_standard_time_bars(df, "America/Chicago")

        assert (result["datetime"].dt.month == 1).all()
        assert (result["datetime"].dt.month != 7).all()
        winter_bars_per_day = len(
            pd.date_range("2024-01-08 00:00", "2024-01-08 22:00", freq="1min")
        ) + len(pd.date_range("2024-01-08 23:00", "2024-01-08 23:59", freq="1min"))
        assert len(result) == 3 * winter_bars_per_day

    def test_derive_sessions_preserves_gap_on_dst_spanning_utc(self):
        """Bug 1: deriving sessions from a single season preserves the maintenance gap.

        After selecting the winter (standard-time) subset, the UTC bar set
        contains the 22:00-23:00 pause and returns a wraparound session block.
        """
        df = _dst_spanning_session_df()
        canon = select_standard_time_bars(df, "America/Chicago")
        result = derive_sessions(canon)

        assert len(result) == 1
        assert result[0]["spans_midnight"] is True
        assert result[0]["start"] == "23:00"
        assert result[0]["end"] == "22:00"
        assert result[0]["duration_hours"] == 23.02

    def test_derive_sessions_collapses_on_mixed_seasons_today(self):
        """Characterization: the unfiltered, DST-spanning UTC frame collapses.

        This documents the union-collapse bug at the derive_sessions level:
        the minute-of-day union across winter and summer fills the 24h circle.
        """
        df = _dst_spanning_session_df()
        result = derive_sessions(df)

        assert result == [
            {"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}
        ]

    def test_sessions_in_run_metadata_output(self, temp_data_dir, capsys):
        TestRunMetadata._save_symbol(temp_data_dir, "@ES")
        config = TestRunMetadata._make_config(temp_data_dir)
        details = {"Symbols": [{"Symbol": "@ES", "Name": "E-mini S&P", "Exchange": "CME"}]}
        quotes = {"Quotes": [{"Symbol": "@ES", "Last": 4500.0, "Bid": 4499.5, "Ask": 4500.5}]}
        details_resp = TestRunMetadata._mock_response(details)
        quotes_resp = TestRunMetadata._mock_response(quotes)

        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with (
            patch("tradestation.metadata.load_config", return_value=config),
            patch("tradestation.metadata.TradeStationAuth", return_value=auth),
            patch("tradestation.metadata.datetime", _FixedDateTime),
            patch(
                "tradestation.metadata.requests.get", side_effect=[details_resp, quotes_resp]
            ) as mock_get,
            patch("tradestation.metadata.time.sleep"),
        ):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        assert result == 0
        output = json.loads(capsys.readouterr().out)
        dd = output["symbols"]["@ES"]["downloaded_data"]
        assert "sessions" in dd
        assert dd["exchange_timezone"] == "America/Chicago"

        storage = SingleFileStorage(temp_data_dir)
        df = storage.load("@ES")
        canon = select_standard_time_bars(df, "America/Chicago")
        expected_session_times = derive_session_times(convert_df_timezone(canon, "America/Chicago"))
        expected_sessions = derive_sessions(convert_df_timezone(canon, "America/Chicago"))
        expected_session_times_utc = derive_session_times(canon)
        expected_sessions_utc = derive_sessions(canon)

        assert dd["session_times"] == expected_session_times
        assert dd["session_times_utc"] == expected_session_times_utc
        assert dd["sessions"] == expected_sessions
        assert dd["sessions_utc"] == expected_sessions_utc
        assert len(dd["sessions"]) == len(dd["sessions_utc"])
        for a, b in zip(dd["sessions"], dd["sessions_utc"], strict=True):
            assert a["spans_midnight"] == b["spans_midnight"]
            assert a["duration_hours"] == b["duration_hours"]
        assert output["timezone"] == "UTC"
        assert mock_get.call_count == 2


class TestGetExchangeTimezone:
    """Tests for get_exchange_timezone exchange-to-timezone mapping."""

    def test_get_exchange_timezone_known(self):
        assert get_exchange_timezone("CME") == "America/Chicago"
        assert get_exchange_timezone("NYSE") == "America/New_York"
        assert get_exchange_timezone("EUREX") == "Europe/Berlin"

    def test_get_exchange_timezone_unknown(self):
        assert get_exchange_timezone("UNKNOWN_EXCHANGE") is None
        assert get_exchange_timezone(None) is None

    def test_get_exchange_timezone_case_insensitive(self):
        assert get_exchange_timezone("cme") == "America/Chicago"
        assert get_exchange_timezone("CbOt") == "America/Chicago"

    def test_nymex_and_comex_map_to_new_york(self):
        """Bug 2: NYMEX and COMEX are New York exchanges, not Chicago."""
        assert get_exchange_timezone("NYMEX") == "America/New_York"
        assert get_exchange_timezone("COMEX") == "America/New_York"

    def test_cboef_maps_to_chicago(self):
        """Bug 2: CBOE Futures Exchange (CBOEF, e.g. @VX) maps to Chicago."""
        assert get_exchange_timezone("CBOEF") == "America/Chicago"
        assert get_exchange_timezone("cboef") == "America/Chicago"

    def test_ice_futures_europe_maps_to_london(self):
        """Bug 2: ICE Futures Europe (@BRN) maps to Europe/London.

        The exact TradeStation API Exchange string is not yet confirmed; the
        design's safe default covers the two most likely uppercase identifiers.
        """
        assert get_exchange_timezone("IFEU") == "Europe/London"
        assert get_exchange_timezone("ICE FUTURES EUROPE") == "Europe/London"
        assert get_exchange_timezone("ice futures europe") == "Europe/London"

    def test_ice_us_still_new_york(self):
        """Bug 2: the ICE U.S. fallback must remain America/New_York."""
        assert get_exchange_timezone("ICE") == "America/New_York"

    def test_get_exchange_timezone_iceus(self):
        """Bug 2 follow-up: ICE U.S. symbols return the specific 'ICEUS' string."""
        assert get_exchange_timezone("ICEUS") == "America/New_York"
        assert get_exchange_timezone("iceus") == "America/New_York"


class TestConvertDfTimezone:
    """Tests for convert_df_timezone DataFrame timestamp conversion."""

    def test_convert_df_timezone_datetime_index(self):
        """DataFrame with a DatetimeIndex in UTC converts to the target timezone."""
        dates = pd.to_datetime(["2024-07-08 09:30", "2024-07-08 09:31"])
        df = pd.DataFrame(index=dates, data={"open": [100.0, 100.0]})
        result = convert_df_timezone(df, "America/Chicago")
        assert list(result.index) == [
            pd.Timestamp("2024-07-08 04:30"),
            pd.Timestamp("2024-07-08 04:31"),
        ]

    def test_convert_df_timezone_datetime_column(self):
        """DataFrame with a 'datetime' column converts to the target timezone."""
        df = pd.DataFrame(
            {
                "datetime": pd.to_datetime(["2024-07-08 09:30", "2024-07-08 09:31"]),
                "open": [100.0, 100.0],
            }
        )
        result = convert_df_timezone(df, "America/Chicago")
        assert list(result["datetime"]) == [
            pd.Timestamp("2024-07-08 04:30"),
            pd.Timestamp("2024-07-08 04:31"),
        ]

    def test_convert_df_timezone_empty(self):
        """An empty DataFrame is returned as-is."""
        df = pd.DataFrame()
        result = convert_df_timezone(df, "America/Chicago")
        assert result.empty

    def test_convert_df_timezone_dst_winter(self):
        """A winter UTC bar is converted with the CST offset."""
        df = pd.DataFrame(
            index=pd.to_datetime(["2024-01-08 19:20"]),
            data={"open": [100.0]},
        )
        result = convert_df_timezone(df, "America/Chicago")
        assert result.index[0] == pd.Timestamp("2024-01-08 13:20")

    def test_convert_df_timezone_dst_summer(self):
        """A summer UTC bar is converted with the CDT offset."""
        df = pd.DataFrame(
            index=pd.to_datetime(["2024-07-08 18:20"]),
            data={"open": [100.0]},
        )
        result = convert_df_timezone(df, "America/Chicago")
        assert result.index[0] == pd.Timestamp("2024-07-08 13:20")


def test_sessions_per_bar_dst_correct():
    """Winter and summer UTC bars at the session close both map to 13:20 local."""
    winter = pd.date_range("2024-01-08 19:00", "2024-01-08 19:20", freq="1min")
    summer = pd.date_range("2024-07-08 18:00", "2024-07-08 18:20", freq="1min")
    df = pd.DataFrame({"datetime": list(winter) + list(summer)})
    df_local = convert_df_timezone(df, "America/Chicago")
    result = derive_sessions(df_local)
    assert result == [
        {"start": "13:00", "end": "13:20", "duration_hours": 0.35, "spans_midnight": False}
    ]
