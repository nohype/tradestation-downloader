"""Tests for the --metadata feature and supporting metadata utilities."""

import json
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
from tradestation.models import DownloadConfig
from tradestation.storage import (
    DailyPartitionedStorage,
    MonthlyPartitionedStorage,
    SingleFileStorage,
)


def _create_sample_df(dates):
    """Create a sample OHLCV DataFrame for storage tests."""
    return pd.DataFrame({
        "datetime": pd.to_datetime(dates),
        "open": [100.0] * len(dates),
        "high": [101.0] * len(dates),
        "low": [99.0] * len(dates),
        "close": [100.5] * len(dates),
        "volume": [1000] * len(dates),
    })


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

        with patch("tradestation.metadata.requests.get") as mock_get, \
             patch("tradestation.metadata.time.sleep"):
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

        with patch("tradestation.metadata.requests.get") as mock_get, \
             patch("tradestation.metadata.time.sleep") as mock_sleep:
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
        with patch("tradestation.metadata.requests.get") as mock_get, \
             patch("tradestation.metadata.time.sleep"):
            mock_get.return_value = self._mock_response({}, status_code=500)
            result = fetch_symbol_details(auth, ["@ES"])

        assert result == []
        assert mock_get.call_count == 1


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

        with patch("tradestation.metadata.requests.get") as mock_get, \
             patch("tradestation.metadata.time.sleep"):
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
        first_payload = {"Quotes": [{"Symbol": s, "Last": float(i)} for i, s in enumerate(symbols[:100])]}
        second_payload = {"Quotes": [{"Symbol": s, "Last": float(i)} for i, s in enumerate(symbols[100:])]}

        with patch("tradestation.metadata.requests.get") as mock_get, \
             patch("tradestation.metadata.time.sleep") as mock_sleep:
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
        with patch("tradestation.metadata.requests.get") as mock_get, \
             patch("tradestation.metadata.time.sleep"):
            mock_get.return_value = self._mock_response({}, status_code=500)
            result = fetch_quote_snapshots(auth, ["@ES"])

        assert result == []
        assert mock_get.call_count == 1


class TestGetFirstTimestamp:
    """Tests for StorageBackend.get_first_timestamp overrides."""

    def test_get_first_timestamp_single_file(self, temp_data_dir):
        """SingleFileStorage returns the minimum datetime from the file."""
        storage = SingleFileStorage(temp_data_dir)
        df = _create_sample_df([
            "2024-01-08 09:30",
            "2024-01-08 09:35",
            "2024-01-08 09:40",
        ])
        storage.save("ES", df)

        first = storage.get_first_timestamp("ES")

        assert first == datetime(2024, 1, 8, 9, 30)

    def test_get_first_timestamp_daily_partition(self, temp_data_dir):
        """DailyPartitionedStorage returns the minimum datetime across partitions."""
        storage = DailyPartitionedStorage(temp_data_dir)
        df = _create_sample_df([
            "2024-01-08 09:30",
            "2024-01-09 09:35",
        ])
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
        df = _create_sample_df([
            "2024-01-15 09:30",
            "2024-01-16 09:35",
        ])
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
        df = pd.DataFrame({
            "datetime": dates,
            "open": [100.0] * len(dates),
            "high": [101.0] * len(dates),
            "low": [99.0] * len(dates),
            "close": [100.5] * len(dates),
            "volume": [1000] * len(dates),
        })
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

        with patch("tradestation.metadata.load_config", return_value=config), \
             patch("tradestation.metadata.TradeStationAuth", return_value=auth), \
             patch("tradestation.metadata.datetime", _FixedDateTime), \
             patch("tradestation.metadata.requests.get", side_effect=[details_resp, quotes_resp]) as mock_get, \
             patch("tradestation.metadata.time.sleep"):
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
        expected_session_times = derive_session_times(convert_df_timezone(df, "America/Chicago"))
        expected_sessions = derive_sessions(convert_df_timezone(df, "America/Chicago"))
        expected_session_times_utc = derive_session_times(df)
        expected_sessions_utc = derive_sessions(df)

        assert dd["session_times"] == expected_session_times
        assert dd["session_times_utc"] == expected_session_times_utc
        assert dd["sessions"] == expected_sessions
        assert dd["sessions_utc"] == expected_sessions_utc

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

        with patch("tradestation.metadata.load_config", return_value=config), \
             patch("tradestation.metadata.TradeStationAuth", return_value=auth), \
             patch("tradestation.metadata.requests.get", return_value=error_resp) as mock_get, \
             patch("tradestation.metadata.time.sleep"):
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
            day_dates = pd.date_range(
                f"{day.date()} 09:00", f"{day.date()} 17:00", freq="1min"
            )
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
        df_index = pd.DataFrame(
            index=pd.to_datetime(dates), data={"open": [100.0] * len(dates)}
        )
        df_col = pd.DataFrame({"datetime": dates, "open": [100.0] * len(dates)})
        expected = [
            {"start": "09:00", "end": "17:00", "duration_hours": 8.02, "spans_midnight": False}
        ]
        assert derive_sessions(df_index) == expected
        assert derive_sessions(df_col) == expected

    def test_sessions_in_run_metadata_output(self, temp_data_dir, capsys):
        TestRunMetadata._save_symbol(temp_data_dir, "@ES")
        config = TestRunMetadata._make_config(temp_data_dir)
        details = {"Symbols": [{"Symbol": "@ES", "Name": "E-mini S&P", "Exchange": "CME"}]}
        quotes = {"Quotes": [{"Symbol": "@ES", "Last": 4500.0, "Bid": 4499.5, "Ask": 4500.5}]}
        details_resp = TestRunMetadata._mock_response(details)
        quotes_resp = TestRunMetadata._mock_response(quotes)

        auth = Mock()
        auth.get_access_token.return_value = "test_token"

        with patch("tradestation.metadata.load_config", return_value=config), \
             patch("tradestation.metadata.TradeStationAuth", return_value=auth), \
             patch("tradestation.metadata.datetime", _FixedDateTime), \
             patch("tradestation.metadata.requests.get", side_effect=[details_resp, quotes_resp]) as mock_get, \
             patch("tradestation.metadata.time.sleep"):
            result = run_metadata(str(temp_data_dir / "config.yaml"))

        assert result == 0
        output = json.loads(capsys.readouterr().out)
        dd = output["symbols"]["@ES"]["downloaded_data"]
        assert "sessions" in dd
        assert dd["exchange_timezone"] == "America/Chicago"

        storage = SingleFileStorage(temp_data_dir)
        df = storage.load("@ES")
        expected_session_times = derive_session_times(convert_df_timezone(df, "America/Chicago"))
        expected_sessions = derive_sessions(convert_df_timezone(df, "America/Chicago"))
        expected_session_times_utc = derive_session_times(df)
        expected_sessions_utc = derive_sessions(df)

        assert dd["session_times"] == expected_session_times
        assert dd["session_times_utc"] == expected_session_times_utc
        assert dd["sessions"] == expected_sessions
        assert dd["sessions_utc"] == expected_sessions_utc
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
        df = pd.DataFrame({
            "datetime": pd.to_datetime(["2024-07-08 09:30", "2024-07-08 09:31"]),
            "open": [100.0, 100.0],
        })
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
