"""Tests for CSV export of Up/Down volume and Upticks/Downticks columns (Red phase)."""

import importlib
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

_EXPECTED_UP_DOWN_TICKS_CSV = (
    '"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks"\r\n'
    "01/15/2024,09:30,100.0,101.0,99.0,100.5,110,103,91,71\r\n"
    "01/15/2024,09:31,100.25,101.5,99.75,100.75,87,88,46,67\r\n"
    "01/16/2024,14:05,101.0,102.0,100.0,101.5,79,88,49,69\r\n"
)


def _create_sample_df_with_up_down(dates, include_ticks=True, include_volume=True):
    """Create a sample DataFrame with optional up/down volume and tick columns."""
    data = {
        "datetime": pd.to_datetime(dates),
        "open": [100.0, 100.25, 101.0],
        "high": [101.0, 101.5, 102.0],
        "low": [99.0, 99.75, 100.0],
        "close": [100.5, 100.75, 101.5],
        "up_volume": [110, 87, 79],
        "down_volume": [103, 88, 88],
    }
    if include_volume:
        data["volume"] = [213, 175, 167]
    if include_ticks:
        data["up_ticks"] = [91, 46, 49]
        data["down_ticks"] = [71, 67, 69]
    return pd.DataFrame(data)


def _create_sample_df_vol_only(dates):
    """Create a sample DataFrame with only the legacy volume column."""
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(dates),
            "open": [100.0, 100.25, 101.0],
            "high": [101.0, 101.5, 102.0],
            "low": [99.0, 99.75, 100.0],
            "close": [100.5, 100.75, 101.5],
            "volume": [1000, 1100, 1200],
        }
    )


def _create_sample_df_with_up_down_and_data(dates):
    """Create a sample DataFrame with up/down volume, ticks, and data fields."""
    df = _create_sample_df_with_up_down(dates)
    df["open_interest"] = [0, 10, 20]
    df["total_ticks"] = [162, 113, 167]
    return df


def _create_sample_df_with_up_down_and_data_na(dates):
    """Create a sample DataFrame with up/down volume, ticks, and nullable data fields."""
    df = _create_sample_df_with_up_down(dates)
    df["open_interest"] = pd.array([0, pd.NA, 20], dtype="Int64")
    df["total_ticks"] = pd.array([162, 113, 167], dtype="Int64")
    return df


@pytest.fixture(autouse=True)
def _clean_plain_data(temp_data_dir):
    """Remove the shared plain_data sibling directory before and after tests."""
    plain_data = temp_data_dir.parent / "plain_data"
    shutil.rmtree(plain_data, ignore_errors=True)
    yield
    shutil.rmtree(plain_data, ignore_errors=True)


class TestExportCsvUpDown:
    """Integration tests for the up/down volume CSV export path."""

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
        """Call run_export_csv with a mocked load_config."""
        with patch.object(CSV_EXPORT, "load_config", return_value=config):
            return CSV_EXPORT.run_export_csv(config_path, symbols=symbols)

    @staticmethod
    def _output_path(temp_data_dir, symbol):
        """Return the expected output .txt path for a symbol."""
        return temp_data_dir.parent / "plain_data" / f"{symbol}.txt"

    def test_csv_with_up_down_volume(self, temp_data_dir):
        """CSV includes Up/Down and Upticks/Downticks, drops Vol, no data quoting, CRLF."""
        df = _create_sample_df_with_up_down(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        assert output.exists()

        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()

        assert lines[0] == (
            '"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks"'
        )
        assert "Vol" not in lines[0]
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 10
            assert '"' not in lines[i]

            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == row.up_volume
            assert int(parts[7]) == row.down_volume
            assert int(parts[8]) == row.up_ticks
            assert int(parts[9]) == row.down_ticks

        raw = output.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_csv_up_down_without_ticks(self, temp_data_dir):
        """CSV uses Up/Down but omits Upticks/Downticks when tick columns are absent."""
        df = _create_sample_df_with_up_down(_DATES, include_ticks=False)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()

        assert lines[0] == '"Date","Time","Open","High","Low","Close","Up","Down"'
        assert "Vol" not in lines[0]
        assert "Upticks" not in lines[0]
        assert "Downticks" not in lines[0]
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 8
            assert '"' not in lines[i]
            assert int(parts[6]) == row.up_volume
            assert int(parts[7]) == row.down_volume

    def test_csv_fallback_to_vol(self, temp_data_dir):
        """Old data with only volume still produces the legacy Vol column."""
        df = _create_sample_df_vol_only(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()

        assert lines[0] == '"Date","Time","Open","High","Low","Close","Vol"'
        assert "Up" not in lines[0]
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 7
            assert '"' not in lines[i]
            assert int(parts[6]) == row.volume

    def test_csv_up_down_values_correct(self, temp_data_dir):
        """Up/Down/Upticks/Downticks are written with per-row correctness."""
        dates = [
            "2024-06-10 10:00:00",
            "2024-06-10 10:01:00",
            "2024-06-10 10:02:00",
        ]
        df = pd.DataFrame(
            {
                "datetime": pd.to_datetime(dates),
                "open": [10.0, 11.0, 12.0],
                "high": [11.0, 12.0, 13.0],
                "low": [9.0, 10.0, 11.0],
                "close": [10.5, 11.5, 12.5],
                "volume": [100, 200, 300],
                "up_volume": [55, 111, 222],
                "down_volume": [45, 89, 78],
                "up_ticks": [33, 66, 99],
                "down_ticks": [22, 44, 66],
            }
        )
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()

        assert lines[0] == (
            '"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks"'
        )
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 10
            assert '"' not in lines[i]
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == row.up_volume
            assert int(parts[7]) == row.down_volume
            assert int(parts[8]) == row.up_ticks
            assert int(parts[9]) == row.down_ticks

        up_values = {int(lines[i].split(",")[6]) for i in range(1, len(lines))}
        assert up_values == {55, 111, 222}

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_csv_up_down_all_storage_backends(self, storage_cls, temp_data_dir):
        """Export produces the same up/down CSV output regardless of storage backend."""
        df = _create_sample_df_with_up_down(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df, storage_cls)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        assert output.exists()

        text = output.read_text(encoding="utf-8", newline="")
        assert text == _EXPECTED_UP_DOWN_TICKS_CSV

    def test_csv_up_down_with_data_fields(self, temp_data_dir):
        """CSV appends the 2 new data field columns in the Up/Down path."""
        df = _create_sample_df_with_up_down_and_data(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        assert output.exists()

        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()
        expected_header = (
            '"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks",'
            '"open_interest","total_ticks"'
        )
        assert lines[0] == expected_header
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 12
            assert '"' not in lines[i]

            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == row.up_volume
            assert int(parts[7]) == row.down_volume
            assert int(parts[8]) == row.up_ticks
            assert int(parts[9]) == row.down_ticks
            assert int(parts[10]) == row.open_interest
            assert int(parts[11]) == row.total_ticks

        raw = output.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_csv_up_down_with_data_fields_na(self, temp_data_dir):
        """Nullable Int64 NA values in the data fields are written as empty fields in the Up/Down path."""
        df = _create_sample_df_with_up_down_and_data_na(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        assert output.exists()

        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()
        expected_header = (
            '"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks",'
            '"open_interest","total_ticks"'
        )
        assert lines[0] == expected_header
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 12
            assert '"' not in lines[i]

            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == row.up_volume
            assert int(parts[7]) == row.down_volume
            assert int(parts[8]) == row.up_ticks
            assert int(parts[9]) == row.down_ticks
            if pd.isna(row.open_interest):
                assert parts[10] == ""
            else:
                assert int(parts[10]) == int(row.open_interest)
            assert int(parts[11]) == int(row.total_ticks)

        raw = output.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_csv_up_down_with_data_fields_all_storage_backends(
        self, storage_cls, temp_data_dir
    ):
        """The 2 new data field columns are exported from every storage backend."""
        df = _create_sample_df_with_up_down_and_data(_DATES)
        self._save_symbol(temp_data_dir, "@ES", df, storage_cls)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        output = self._output_path(temp_data_dir, "@ES")
        text = output.read_text(encoding="utf-8", newline="")
        lines = text.splitlines()
        expected_header = (
            '"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks",'
            '"open_interest","total_ticks"'
        )
        assert lines[0] == expected_header
        assert len(lines) == len(df) + 1
        for line in lines[1:]:
            assert len(line.split(",")) == 12
