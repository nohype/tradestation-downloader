"""Tests for attributes.ini generation during --export-ts-csv."""

import csv
import importlib
import io
import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from tradestation.models import DownloadConfig
from tradestation.storage import SingleFileStorage

CSV_EXPORT = importlib.import_module("tradestation.csv_export")

EXAMPLE_INI = Path(__file__).resolve().parent.parent / "example_attributes.INI"


def _make_config(data_dir):
    """Build a DownloadConfig with a temporary data directory."""
    return DownloadConfig(
        client_id="client_id",
        client_secret="client_secret",
        refresh_token="refresh_token",
        data_dir=str(data_dir),
        symbols=[],
        datetime_index=True,
    )


def _api(price_format=None, **overrides):
    """Build a minimal api metadata dict (ES-like Decimal PriceFormat by default)."""
    api = {
        "AssetType": "FUTURE",
        "Exchange": "CME",
        "Description": "E-mini S&P 500",
        "PriceFormat": {
            "Format": "Decimal",
            "Decimals": "2",
            "Increment": "0.25",
            "PointValue": "50",
        },
    }
    if price_format is not None:
        api["PriceFormat"] = price_format
    api.update(overrides)
    return api


def _write_metadata(data_dir, symbols):
    """Write metadata.json; symbols maps stored symbol -> (api dict, timezone)."""
    entries = {
        symbol: {
            "symbol": symbol,
            "api": api,
            "downloaded_data": {"exchange_timezone": timezone},
        }
        for symbol, (api, timezone) in symbols.items()
    }
    metadata = {
        "generated_at": "2024-01-01T00:00:00Z",
        "data_dir": str(data_dir),
        "timezone": "UTC",
        "symbols": entries,
    }
    (Path(data_dir) / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _save(data_dir, symbol, df):
    SingleFileStorage(data_dir).save(symbol, df)


def _small_df():
    return pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                ["2024-01-15 09:30:00", "2024-01-15 09:31:00", "2024-01-16 14:05:00"]
            ),
            "open": [100.0, 100.25, 101.0],
            "high": [101.0, 101.5, 102.0],
            "low": [99.0, 99.75, 100.0],
            "close": [100.5, 100.75, 101.5],
            "volume": [1000, 1100, 1200],
        }
    )


def _globex_week_df():
    """One Globex week of minute bars stored as naive UTC.

    Exchange-local (America/Chicago, winter UTC-6): Sunday 17:01 through Friday
    16:00 with a daily 16:01-17:00 maintenance halt.
    """
    local = pd.date_range("2024-01-14 17:01", "2024-01-19 16:00", freq="min")
    minute_of_day = local.hour * 60 + local.minute
    local = local[~((minute_of_day >= 16 * 60 + 1) & (minute_of_day <= 17 * 60))]
    utc = local + pd.Timedelta(hours=6)
    n = len(utc)
    return pd.DataFrame(
        {
            "datetime": utc,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1] * n,
        }
    )


def _run_ts_export(config_path, symbols, config):
    with patch.object(CSV_EXPORT, "load_config", return_value=config):
        return CSV_EXPORT.run_export_ts_csv(config_path, symbols=symbols)


def _ini_path(temp_data_dir):
    return temp_data_dir.parent / "plain_data" / "attributes.ini"


def _read_ini(path):
    """Return (header_fields, {SYMBOL: row dict}) parsed from attributes.ini."""
    records = [
        record
        for record in csv.reader(io.StringIO(path.read_text(encoding="utf-8")))
        if record
    ]
    header = records[0]
    return header, {
        record[0]: dict(zip(header, record, strict=False)) for record in records[1:]
    }


@pytest.fixture(autouse=True)
def _clean_plain_data(temp_data_dir):
    """Remove the shared plain_data sibling directory before and after tests."""
    plain_data = temp_data_dir.parent / "plain_data"
    shutil.rmtree(plain_data, ignore_errors=True)
    yield
    shutil.rmtree(plain_data, ignore_errors=True)


class TestAttributesIni:
    """Integration tests for attributes.ini upserts in run_export_ts_csv."""

    def test_creates_attributes_ini_with_example_header_and_crlf(self, temp_data_dir):
        """A missing attributes.ini is created; header matches the example, CRLF."""
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "UTC")})

        result = _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir))
        assert result == 0

        ini = _ini_path(temp_data_dir)
        assert ini.exists()
        raw = ini.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert b"\r\r\n" not in raw
        assert raw.count(b"\r") == raw.count(b"\r\n")

        example_header = EXAMPLE_INI.read_text(encoding="utf-8").splitlines()[0]
        assert raw.decode("utf-8").splitlines()[0] == example_header

    def test_symbol_field_is_filename_without_at(self, temp_data_dir):
        """@ES exports to @ES_ts.txt and the SYMBOL field is ES_ts."""
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "UTC")})

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        _, rows = _read_ini(_ini_path(temp_data_dir))
        assert list(rows) == ["ES_ts"]

    @pytest.mark.parametrize(
        "decimals,increment,point_value,scale,min_move,bpv",
        [
            ("3", "0.001", "10000", "1/1000", "1", "10000.00"),  # NG-like
            ("1", "0.1", "100", "1/10", "1", "100.00"),          # GC-like
            ("2", "0.25", "50", "1/100", "25", "50.00"),         # ES-like
        ],
    )
    def test_decimal_price_format_fields(
        self, temp_data_dir, decimals, increment, point_value, scale, min_move, bpv
    ):
        """PRICE SCALE, MINIMUM MOVEMENT and BIG POINT VALUE derive from PriceFormat."""
        price_format = {
            "Format": "Decimal",
            "Decimals": decimals,
            "Increment": increment,
            "PointValue": point_value,
        }
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(
            temp_data_dir, {"@ES": (_api(price_format=price_format), "UTC")}
        )

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        _, rows = _read_ini(_ini_path(temp_data_dir))
        row = rows["ES_ts"]
        assert row["PRICE SCALE"] == scale
        assert row["MINIMUM MOVEMENT"] == min_move
        assert row["BIG POINT VALUE"] == bpv
        assert re.fullmatch(r"\d+\.\d{2}", row["BIG POINT VALUE"])

    @pytest.mark.parametrize(
        "fmt,fraction,increment,point_value,scale,min_move,bpv",
        [
            ("Fraction", "8", "0.25", "50", "1/8", "2", "50.00"),
            ("SubFraction", "32", "0.03125", "1000", "1/32", "1", "1000.00"),
            # BIG POINT VALUE is always 2 decimals, whatever Increment looks like.
            ("Fraction", "8", "0.250", "50", "1/8", "2", "50.00"),
            # A whole-number Increment still yields a 2-decimal BIG POINT VALUE.
            ("Fraction", "8", "1", "50", "1/8", "8", "50.00"),
        ],
    )
    def test_fraction_price_format_fields(
        self, temp_data_dir, fmt, fraction, increment, point_value, scale, min_move, bpv
    ):
        """Fraction/SubFraction PRICE SCALE uses Fraction as denominator; MINIMUM
        MOVEMENT is Increment*denominator; BIG POINT VALUE is always 2 decimal
        places."""
        price_format = {
            "Format": fmt,
            "Fraction": fraction,
            "Increment": increment,
            "PointValue": point_value,
        }
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(
            temp_data_dir, {"@ES": (_api(price_format=price_format), "UTC")}
        )

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        _, rows = _read_ini(_ini_path(temp_data_dir))
        row = rows["ES_ts"]
        assert row["PRICE SCALE"] == scale
        assert row["MINIMUM MOVEMENT"] == min_move
        assert row["BIG POINT VALUE"] == bpv
        assert re.fullmatch(r"\d+\.\d{2}", row["BIG POINT VALUE"])

    def test_fraction_missing_fraction_field_after_regen_returns_1(
        self, temp_data_dir, caplog
    ):
        """Format=Fraction without a Fraction field is insufficient metadata."""
        _save(temp_data_dir, "@ES", _small_df())
        price_format = {
            "Format": "Fraction",
            "Increment": "0.25",
            "PointValue": "50",
        }
        _write_metadata(
            temp_data_dir, {"@ES": (_api(price_format=price_format), "UTC")}
        )

        with (
            patch("tradestation.metadata.run_metadata", return_value=0) as mock_run,
            caplog.at_level("ERROR"),
        ):
            result = _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir))

        assert result == 1
        mock_run.assert_called_once()
        assert "@ES" in caplog.text

    def test_session_rounded_and_csv_time_is_exchange_local(self, temp_data_dir):
        """17:01-16:00 Chicago bars -> SESSION 1 1700,1600; CSV Time is local."""
        _save(temp_data_dir, "@ES", _globex_week_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "America/Chicago")})

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        _, rows = _read_ini(_ini_path(temp_data_dir))
        row = rows["ES_ts"]
        assert row["SESSION 1 START TIME"] == "1700"
        assert row["SESSION 1 END TIME"] == "1600"

        csv_lines = (
            (temp_data_dir.parent / "plain_data" / "@ES_ts.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        # First stored bar is 2024-01-14 23:01 UTC -> 17:01 Chicago.
        first = csv_lines[1].split(",")
        assert first[0] == "01/14/2024"
        assert first[1] == "17:01"

    def test_session_days_umtwr_for_globex_week(self, temp_data_dir):
        """An overnight session opening Sun-Thu yields SESSION 1 DAYS UMTWR."""
        _save(temp_data_dir, "@ES", _globex_week_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "America/Chicago")})

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        _, rows = _read_ini(_ini_path(temp_data_dir))
        assert rows["ES_ts"]["SESSION 1 DAYS"] == "UMTWR"

    def test_upsert_preserves_other_rows_and_updates_symbol(self, temp_data_dir):
        """Existing rows for other symbols are kept; ES_ts is updated not duplicated."""
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "UTC")})

        ini = _ini_path(temp_data_dir)
        ini.parent.mkdir(parents=True, exist_ok=True)
        example_header = EXAMPLE_INI.read_text(encoding="utf-8").splitlines()[0]
        other_row = (
            'NG_DAILY_20Y_MAY2026,FUTURE,MM/DD/YYYY,NYMEX,1/1000,1,10000.00,'
            '1700,1600,UMTWR,"ng daily 20y",,,,,,,,,0x409'
        )
        old_es_row = (
            'ES_ts,FUTURE,MM/DD/YYYY,CME,1/100,25,50.00,'
            '1700,1600,UMTWR,"stale description",,,,,,,,,0x409'
        )
        ini.write_text(
            "\r\n".join([example_header, other_row, old_es_row]) + "\r\n",
            encoding="utf-8",
        )

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        _, rows = _read_ini(ini)
        assert rows["NG_DAILY_20Y_MAY2026"]["DESCRIPTION"] == "ng daily 20y"
        assert rows["NG_DAILY_20Y_MAY2026"]["EXCHANGE"] == "NYMEX"
        assert rows["ES_ts"]["DESCRIPTION"] == "E-mini S&P 500"

        # Re-export updates in place rather than duplicating the row.
        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0
        _, rows = _read_ini(ini)
        assert list(rows).count("ES_ts") == 1
        assert len(rows) == 2

    def test_missing_metadata_triggers_regeneration(self, temp_data_dir):
        """Without metadata.json, run_metadata is invoked and the export proceeds."""
        _save(temp_data_dir, "@ES", _small_df())
        assert not (temp_data_dir / "metadata.json").exists()

        def fake_run_metadata(_config_path):
            _write_metadata(temp_data_dir, {"@ES": (_api(), "UTC")})
            return 0

        with patch(
            "tradestation.metadata.run_metadata", side_effect=fake_run_metadata
        ) as mock_run:
            result = _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir))

        assert result == 0
        mock_run.assert_called_once()
        _, rows = _read_ini(_ini_path(temp_data_dir))
        assert "ES_ts" in rows

    def test_incomplete_metadata_after_regen_returns_1(self, temp_data_dir, caplog):
        """A symbol still missing PriceFormat after regeneration fails the export."""
        _save(temp_data_dir, "@ES", _small_df())
        api_no_pf = {k: v for k, v in _api().items() if k != "PriceFormat"}
        _write_metadata(temp_data_dir, {"@ES": (api_no_pf, "UTC")})

        with (
            patch("tradestation.metadata.run_metadata", return_value=0) as mock_run,
            caplog.at_level("ERROR"),
        ):
            result = _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir))

        assert result == 1
        mock_run.assert_called_once()
        assert "@ES" in caplog.text

    def test_export_csv_does_not_write_attributes_ini(self, temp_data_dir):
        """--export-csv leaves attributes.ini alone and keeps UTC times."""
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "UTC")})

        config = _make_config(temp_data_dir)
        with patch.object(CSV_EXPORT, "load_config", return_value=config):
            assert CSV_EXPORT.run_export_csv("config.yaml", symbols=["@ES"]) == 0

        assert not _ini_path(temp_data_dir).exists()
        lines = (
            (temp_data_dir.parent / "plain_data" / "@ES.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert lines[1].split(",")[1] == "09:30"

    def test_description_truncated_to_50_and_quoted(self, temp_data_dir):
        """DESCRIPTION is truncated to 50 chars and always written quoted."""
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(Description="D" * 60), "UTC")})

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        raw_lines = _ini_path(temp_data_dir).read_text(encoding="utf-8").splitlines()
        data_line = next(line for line in raw_lines if line.startswith("ES_ts"))
        assert '"' + "D" * 50 + '"' in data_line

        _, rows = _read_ini(_ini_path(temp_data_dir))
        assert rows["ES_ts"]["DESCRIPTION"] == "D" * 50

    def test_session2_fields_empty_and_locale_0x409(self, temp_data_dir):
        """SESSION 2/option fields are empty and the row ends with 0x409."""
        _save(temp_data_dir, "@ES", _small_df())
        _write_metadata(temp_data_dir, {"@ES": (_api(), "UTC")})

        assert _run_ts_export("config.yaml", ["@ES"], _make_config(temp_data_dir)) == 0

        raw_lines = _ini_path(temp_data_dir).read_text(encoding="utf-8").splitlines()
        data_line = next(line for line in raw_lines if line.startswith("ES_ts"))
        assert data_line.endswith(",,,,,,,,,0x409")

        _, rows = _read_ini(_ini_path(temp_data_dir))
        row = rows["ES_ts"]
        for field in (
            "SESSION 2 START TIME",
            "SESSION 2 END TIME",
            "SESSION 2 DAYS",
            "OPTION TYPE",
            "STRIKE PRICE",
            "DAILY LIMIT",
            "MARGIN",
            "EXPIRATION DATE",
        ):
            assert row[field] == ""
        assert row["LOCALE"] == "0x409"
        assert row["CATEGORY"] == "FUTURE"
        assert row["DATE FORMAT"] == "MM/DD/YYYY"
        assert row["EXCHANGE"] == "CME"
