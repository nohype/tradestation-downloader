"""Tests for the --export-ts-csv feature (TradeStation third-party ASCII format)."""

import importlib
import json
import shutil
from unittest.mock import patch

import pandas as pd
import pytest

from tradestation.models import DownloadConfig
from tradestation.storage import (
    DailyPartitionedStorage,
    MonthlyPartitionedStorage,
    SingleFileStorage,
)

CSV_EXPORT = importlib.import_module("tradestation.csv_export")

_DATES = [
    "2024-01-15 09:30:00",
    "2024-01-15 09:31:00",
    "2024-01-16 14:05:00",
]

_BASE_OHLC = {
    "open": [100.0, 100.25, 101.0],
    "high": [101.0, 101.5, 102.0],
    "low": [99.0, 99.75, 100.0],
    "close": [100.5, 100.75, 101.5],
}

_HEADER_NO_OI = '"Date","Time","Open","High","Low","Close","Volume"'
_HEADER_WITH_OI = '"Date","Time","Open","High","Low","Close","Volume","OpenInt"'


def _create_sample_df(dates, include_volume=True):
    """Create a sample OHLCV DataFrame with the legacy volume column."""
    data = {"datetime": pd.to_datetime(dates), **_BASE_OHLC}
    if include_volume:
        data["volume"] = [1000, 1100, 1200]
    return pd.DataFrame(data)


def _create_sample_df_with_up_down(dates, include_ticks=True, include_volume=True):
    """Create a sample DataFrame with up/down volume and optional tick columns."""
    df = _create_sample_df(dates, include_volume=include_volume)
    df["up_volume"] = [110, 87, 79]
    df["down_volume"] = [103, 88, 88]
    if include_ticks:
        df["up_ticks"] = [91, 46, 49]
        df["down_ticks"] = [71, 67, 69]
    return df


def _create_sample_df_with_open_interest(dates, na=False):
    """Create a sample DataFrame with up/down volume and open_interest."""
    df = _create_sample_df_with_up_down(dates)
    if na:
        df["open_interest"] = pd.array([10, pd.NA, 30], dtype="Int64")
    else:
        df["open_interest"] = [10, 20, 30]
    df["total_ticks"] = [162, 113, 167]
    return df


@pytest.fixture(autouse=True)
def _clean_plain_data(temp_data_dir):
    """Remove the shared plain_data sibling directory before and after tests."""
    plain_data = temp_data_dir.parent / "plain_data"
    shutil.rmtree(plain_data, ignore_errors=True)
    yield
    shutil.rmtree(plain_data, ignore_errors=True)


@pytest.fixture(autouse=True)
def _metadata_json(temp_data_dir):
    """Write a minimal metadata.json for @ES so the TS export skips regeneration.

    exchange_timezone "UTC" makes the exchange-local conversion an identity so
    the UTC clock-time assertions in these tests keep working.
    """
    metadata = {
        "generated_at": "2024-01-01T00:00:00Z",
        "data_dir": str(temp_data_dir),
        "timezone": "UTC",
        "symbols": {
            "@ES": {
                "symbol": "@ES",
                "api": {
                    "AssetType": "FUTURE",
                    "Exchange": "CME",
                    "Description": "E-mini S&P 500",
                    "PriceFormat": {
                        "Format": "Decimal",
                        "Decimals": "2",
                        "Increment": "0.25",
                        "PointValue": "50",
                    },
                },
                "downloaded_data": {"exchange_timezone": "UTC"},
            }
        },
    }
    (temp_data_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


class TestExportTsCsv:
    """Integration tests for the run_export_ts_csv path."""

    @staticmethod
    def _make_config(data_dir, datetime_index=True):
        """Build a DownloadConfig with a temporary data directory."""
        return DownloadConfig(
            client_id="client_id",
            client_secret="client_secret",
            refresh_token="refresh_token",
            data_dir=str(data_dir),
            symbols=[],
            datetime_index=datetime_index,
        )

    @staticmethod
    def _save_symbol(storage_dir, symbol, df, storage_cls=SingleFileStorage, datetime_index=True):
        """Save a sample DataFrame to the specified storage backend."""
        storage = storage_cls(storage_dir, datetime_index=datetime_index)
        storage.save(symbol, df)

    @staticmethod
    def _call_export(config_path, symbols, config):
        """Call run_export_ts_csv with a mocked load_config."""
        with patch.object(CSV_EXPORT, "load_config", return_value=config):
            return CSV_EXPORT.run_export_ts_csv(config_path, symbols=symbols)

    @staticmethod
    def _output_path(temp_data_dir, symbol):
        """Return the expected _ts.txt output path for a symbol."""
        return temp_data_dir.parent / "plain_data" / f"{symbol}_ts.txt"

    def test_header_without_open_interest(self, temp_data_dir):
        """Header is exactly the 7 TradeStation columns when open_interest is absent."""
        df = _create_sample_df(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        assert output.exists()
        assert not (temp_data_dir.parent / "plain_data" / "@ES.txt").exists()

        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == _HEADER_NO_OI
        assert len(lines) == len(df) + 1
        for line in lines[1:]:
            assert len(line.split(",")) == 7

    def test_header_with_open_interest(self, temp_data_dir):
        """Header gains a trailing OpenInt column when open_interest is present."""
        df = _create_sample_df_with_open_interest(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == _HEADER_WITH_OI
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 8
            assert int(parts[7]) == row.open_interest

    def test_volume_is_up_plus_down(self, temp_data_dir):
        """Volume is up_volume + down_volume, ignoring the volume column."""
        df = _create_sample_df_with_up_down(_DATES)
        # Give volume values that differ from up+down to prove it is not used.
        df["volume"] = [99999, 88888, 77777]
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert int(parts[6]) == row.up_volume + row.down_volume

    def test_volume_up_down_na_treated_as_zero(self, temp_data_dir):
        """NaN values in up/down volume count as 0 and Volume is written as an integer."""
        df = _create_sample_df_with_up_down(_DATES, include_volume=False)
        # Production dtype: pd.to_numeric(errors="coerce") yields float64 with NaN.
        df["up_volume"] = pd.Series([110.0, float("nan"), 79.0])
        df["down_volume"] = pd.Series([103.0, 88.0, float("nan")])
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[1].split(",")[6] == "213"
        assert lines[2].split(",")[6] == "88"
        assert lines[3].split(",")[6] == "79"

    def test_volume_fallback_to_volume_column(self, temp_data_dir):
        """Volume falls back to the volume column when up/down are absent."""
        df = _create_sample_df(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert int(parts[6]) == row.volume

    def test_volume_fallback_na_writes_zero(self, temp_data_dir):
        """NaN in the volume column is written as 0, not an empty field."""
        df = _create_sample_df(_DATES)
        df["volume"] = pd.Series([1000.0, float("nan"), 1200.0])
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[1].split(",")[6] == "1000"
        assert lines[2].split(",")[6] == "0"
        assert lines[3].split(",")[6] == "1200"

    def test_volume_zero_when_no_volume_columns(self, temp_data_dir):
        """Volume is 0 when volume and up/down columns are all absent."""
        df = _create_sample_df(_DATES, include_volume=False)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        for line in lines[1:]:
            assert int(line.split(",")[6]) == 0

    def test_no_extra_columns_in_output(self, temp_data_dir):
        """Up/Down/ticks/TotalTicks columns are not present in the output."""
        df = _create_sample_df_with_open_interest(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        header = output.read_text(encoding="utf-8").splitlines()[0]
        for dropped in (
            "Up", "Down", "Upticks", "Downticks", "Vol\",", "TotalTicks",
        ):
            assert dropped not in header
        assert header == _HEADER_WITH_OI

    def test_both_exports_coexist(self, temp_data_dir):
        """--export-csv writes @ES.txt and --export-ts-csv writes @ES_ts.txt."""
        df = _create_sample_df_with_up_down(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        config_path = str(temp_data_dir / "config.yaml")
        with patch.object(CSV_EXPORT, "load_config", return_value=config):
            assert CSV_EXPORT.run_export_csv(config_path, symbols=["@ES"]) == 0
            assert CSV_EXPORT.run_export_ts_csv(config_path, symbols=["@ES"]) == 0

        plain_data = temp_data_dir.parent / "plain_data"
        assert (plain_data / "@ES.txt").exists()
        assert (plain_data / "@ES_ts.txt").exists()

    def test_ts_csv_format_correctness(self, temp_data_dir):
        """Date MM/DD/YYYY, Time HH:MM, CRLF endings, and unquoted data rows."""
        df = _create_sample_df(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert '"' not in lines[i]

        raw = output.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_ts_csv_all_storage_backends(self, storage_cls, temp_data_dir):
        """TS export produces identical output regardless of storage backend."""
        df = _create_sample_df_with_up_down(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df, storage_cls)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        assert output.exists()
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == _HEADER_NO_OI
        assert len(lines) == len(df) + 1
        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert int(parts[6]) == row.up_volume + row.down_volume

    def test_datetime_index_false(self, temp_data_dir):
        """TS export is correct when data is stored with datetime as a column."""
        df = _create_sample_df(_DATES)
        storage = SingleFileStorage(temp_data_dir, datetime_index=False)
        storage.save("@ES", df)

        loaded = storage.load("@ES")
        assert not isinstance(loaded.index, pd.DatetimeIndex)
        assert "datetime" in loaded.columns

        config = self._make_config(temp_data_dir, datetime_index=False)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == _HEADER_NO_OI
        assert len(lines) == len(df) + 1
        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")

    def test_open_interest_na_writes_empty_field(self, temp_data_dir):
        """NA open_interest values are written as empty fields."""
        df = _create_sample_df_with_open_interest(_DATES, na=True)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@ES"], config
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        lines = output.read_text(encoding="utf-8").splitlines()
        assert lines[0] == _HEADER_WITH_OI
        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            if pd.isna(row.open_interest):
                assert parts[7] == ""
            else:
                assert int(parts[7]) == int(row.open_interest)

    def test_missing_symbol_error(self, temp_data_dir, capsys):
        """Requesting a symbol with no on-disk data exits 1 and names the symbol."""
        df = _create_sample_df(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), ["@NQ"], config
        )
        assert result == 1

        captured = capsys.readouterr()
        assert "@NQ" in (captured.out + captured.err)

    def test_empty_data_dir(self, temp_data_dir, capsys):
        """An empty data directory exits 1 with a no-data-found message."""
        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"), None, config
        )
        assert result == 1

        captured = capsys.readouterr()
        assert "no data found" in (captured.out + captured.err).lower()
