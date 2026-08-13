"""Tests for the --export-csv feature (Red phase)."""

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


def _create_sample_df():
    """Create a sample OHLCV DataFrame with distinct per-row values."""
    return pd.DataFrame({
        "datetime": pd.to_datetime([
            "2024-01-15 09:30:00",
            "2024-01-15 09:31:00",
            "2024-01-16 14:05:00",
        ]),
        "open": [100.0, 100.25, 101.0],
        "high": [101.0, 101.5, 102.0],
        "low": [99.0, 99.75, 100.0],
        "close": [100.5, 100.75, 101.5],
        "volume": [1000, 1100, 1200],
    })


def _create_sample_df_with_data_fields():
    """Create a sample DataFrame with the 2 additional data fields."""
    df = _create_sample_df().copy()
    df["open_interest"] = [10, 20, 30]
    df["total_ticks"] = [1000, 1100, 1200]
    return df


def _create_sample_df_with_data_fields_na():
    """Create a sample DataFrame with nullable Int64 NA values in data fields."""
    df = _create_sample_df().copy()
    df["open_interest"] = pd.array([10, pd.NA, 30], dtype="Int64")
    df["total_ticks"] = pd.array([1000, 1100, 1200], dtype="Int64")
    return df


@pytest.fixture(autouse=True)
def _clean_plain_data(temp_data_dir):
    """Remove the shared plain_data sibling directory before and after tests."""
    plain_data = temp_data_dir.parent / "plain_data"
    shutil.rmtree(plain_data, ignore_errors=True)
    yield
    shutil.rmtree(plain_data, ignore_errors=True)


class TestExportCsv:
    """Integration tests for the run_export_csv path."""

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
    def _save_symbol(storage_dir, symbol, df, storage_cls=SingleFileStorage):
        """Save a sample DataFrame to the specified storage backend."""
        storage = storage_cls(storage_dir)
        storage.save(symbol, df)

    @staticmethod
    def _call_export(config_path, symbols, config):
        """Call run_export_csv with a mocked load_config."""
        module = importlib.import_module("tradestation.csv_export")
        with patch.object(module, "load_config", return_value=config):
            return module.run_export_csv(config_path, symbols=symbols)

    @staticmethod
    def _assert_csv_output(temp_data_dir, symbol, df):
        """Verify the exported .txt file matches the expected CSV format."""
        plain_data = temp_data_dir.parent / "plain_data"
        output = plain_data / f"{symbol}.txt"
        assert output.exists()

        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()
        assert lines[0] == '"Date","Time","Open","High","Low","Close","Vol"'
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == int(row.volume)
            assert '"' not in lines[i]

    def test_csv_format_correctness(self, temp_data_dir):
        """CSV header, row count, date/time, OHLCV, and no data quoting."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df)

        loaded = SingleFileStorage(temp_data_dir).load("@ES")
        assert isinstance(loaded.index, pd.DatetimeIndex)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0
        self._assert_csv_output(temp_data_dir, "@ES", df)

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_csv_format_all_storage_backends(self, storage_cls, temp_data_dir):
        """Export produces the same CSV output regardless of storage backend."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df, storage_cls)

        loaded = storage_cls(temp_data_dir).load("@ES")
        assert isinstance(loaded.index, pd.DatetimeIndex)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0
        self._assert_csv_output(temp_data_dir, "@ES", df)

    def test_export_all_symbols(self, temp_data_dir):
        """No symbols argument exports every symbol on disk."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df)
        self._save_symbol(temp_data_dir, "@NQ", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            None,
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        files = sorted(p.name for p in plain_data.glob("*.txt"))
        assert files == ["@ES.txt", "@NQ.txt"]

    def test_export_specific_symbols(self, temp_data_dir):
        """A symbols filter exports only the requested symbols."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df)
        self._save_symbol(temp_data_dir, "@NQ", df)
        self._save_symbol(temp_data_dir, "@CL", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES", "@CL"],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        assert (plain_data / "@ES.txt").exists()
        assert (plain_data / "@CL.txt").exists()
        assert not (plain_data / "@NQ.txt").exists()

    @pytest.mark.parametrize("requested", ["BO", "@BO"])
    def test_at_prefix_normalization(self, requested, temp_data_dir):
        """A missing @ prefix is normalized for lookup and output filename."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@BO", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            [requested],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        assert (plain_data / "@BO.txt").exists()

    def test_missing_symbol_error(self, temp_data_dir, capsys):
        """Requesting a symbol with no on-disk data exits 1 and names the symbol."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@NQ"],
            config,
        )
        assert result == 1

        captured = capsys.readouterr()
        assert "@NQ" in (captured.out + captured.err)

        plain_data = temp_data_dir.parent / "plain_data"
        if plain_data.exists():
            assert list(plain_data.glob("*.txt")) == []

    def test_empty_data_dir(self, temp_data_dir, capsys):
        """An empty data directory exits 1 with a no-data-found message."""
        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            None,
            config,
        )
        assert result == 1

        captured = capsys.readouterr()
        assert "no data found" in (captured.out + captured.err).lower()

    def test_path_traversal_rejection(self, temp_data_dir):
        """A path-injection symbol is rejected by validate_symbol."""
        config = self._make_config(temp_data_dir)
        with pytest.raises(ValueError):
            self._call_export(
                str(temp_data_dir / "config.yaml"),
                ["../etc/passwd"],
                config,
            )

    def test_output_directory_location(self, temp_data_dir):
        """Output files live in a plain_data sibling of the data directory."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        output = plain_data / "@ES.txt"
        assert output.exists()
        assert output.parent == temp_data_dir.parent / "plain_data"
        assert temp_data_dir not in output.parents

    def test_csv_line_endings_crlf(self, temp_data_dir):
        """Exported .txt files use uniform CRLF line endings."""
        df = _create_sample_df()
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        raw = (plain_data / "@ES.txt").read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_datetime_index_false(self, temp_data_dir):
        """CSV export is correct when data is stored with datetime as a column."""
        df = _create_sample_df()
        storage = SingleFileStorage(temp_data_dir, datetime_index=False)
        storage.save("@ES", df)

        loaded = storage.load("@ES")
        assert not isinstance(loaded.index, pd.DatetimeIndex)
        assert "datetime" in loaded.columns

        config = self._make_config(temp_data_dir, datetime_index=False)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0
        self._assert_csv_output(temp_data_dir, "@ES", df)

    def test_csv_with_data_fields(self, temp_data_dir):
        """CSV appends the 2 new data field columns in the Vol path."""
        df = _create_sample_df_with_data_fields()
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        output = plain_data / "@ES.txt"
        assert output.exists()

        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()
        expected_header = (
            '"Date","Time","Open","High","Low","Close","Vol",'
            '"open_interest","total_ticks"'
        )
        assert lines[0] == expected_header
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 9
            assert '"' not in lines[i]

            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == int(row.volume)
            assert int(parts[7]) == row.open_interest
            assert int(parts[8]) == row.total_ticks

    def test_csv_with_data_fields_na(self, temp_data_dir):
        """Nullable Int64 NA values in the data fields are written as empty fields."""
        df = _create_sample_df_with_data_fields_na()
        self._save_symbol(temp_data_dir, "@ES", df)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        output = plain_data / "@ES.txt"
        assert output.exists()

        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()
        expected_header = (
            '"Date","Time","Open","High","Low","Close","Vol",'
            '"open_interest","total_ticks"'
        )
        assert lines[0] == expected_header
        assert len(lines) == len(df) + 1

        for i, row in enumerate(df.itertuples(index=False), start=1):
            parts = lines[i].split(",")
            assert len(parts) == 9
            assert '"' not in lines[i]

            ts = pd.to_datetime(row.datetime)
            assert parts[0] == ts.strftime("%m/%d/%Y")
            assert parts[1] == ts.strftime("%H:%M")
            assert float(parts[2]) == row.open
            assert float(parts[3]) == row.high
            assert float(parts[4]) == row.low
            assert float(parts[5]) == row.close
            assert int(parts[6]) == int(row.volume)
            if pd.isna(row.open_interest):
                assert parts[7] == ""
            else:
                assert int(parts[7]) == int(row.open_interest)
            assert int(parts[8]) == int(row.total_ticks)

    @pytest.mark.parametrize(
        "storage_cls",
        [SingleFileStorage, DailyPartitionedStorage, MonthlyPartitionedStorage],
    )
    def test_csv_data_fields_all_storage_backends(self, storage_cls, temp_data_dir):
        """The 2 new data field columns are exported from every storage backend."""
        df = _create_sample_df_with_data_fields()
        self._save_symbol(temp_data_dir, "@ES", df, storage_cls)

        config = self._make_config(temp_data_dir)
        result = self._call_export(
            str(temp_data_dir / "config.yaml"),
            ["@ES"],
            config,
        )
        assert result == 0

        plain_data = temp_data_dir.parent / "plain_data"
        output = plain_data / "@ES.txt"
        text = output.read_text(encoding="utf-8")
        lines = text.splitlines()
        expected_header = (
            '"Date","Time","Open","High","Low","Close","Vol",'
            '"open_interest","total_ticks"'
        )
        assert lines[0] == expected_header
        assert len(lines) == len(df) + 1
        for line in lines[1:]:
            assert len(line.split(",")) == 9
