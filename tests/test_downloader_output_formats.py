"""Tests for downloader output format support."""

import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from tradestation.downloader import TradeStationDownloader
from tradestation.models import DownloadConfig, OutputFormat


def _make_downloader(**overrides):
    """Build a downloader with prewarmed auth and an empty temporary data dir."""
    config = DownloadConfig(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        data_dir=tempfile.mkdtemp(),
        start_date="2025-01-01",
        rate_limit_delay=0,
        **overrides,
    )
    config.config_path = "config.yaml"
    downloader = TradeStationDownloader(config)
    downloader._auth._access_token = "current_token"
    downloader._auth._token_expiry = datetime.now() + timedelta(hours=1)
    return downloader


def _bar(timestamp):
    """Build a minimal 1-min bar as returned by the barcharts endpoint."""
    return {
        "TimeStamp": timestamp,
        "Open": "100",
        "High": "101",
        "Low": "99",
        "Close": "100",
        "TotalVolume": "10",
    }


def _mock_bars_response(bars):
    resp = Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {"Bars": bars}
    resp.raise_for_status = Mock()
    return resp


class TestOutputFormatBackends:
    """Downloader always creates a single Parquet storage backend."""

    def test_default_creates_parquet_backend(self):
        downloader = _make_downloader()
        assert downloader._storage.__class__.__name__ == "SingleFileStorage"

    def test_csv_only_still_creates_parquet_backend(self):
        downloader = _make_downloader(output_formats=[OutputFormat.CSV])
        assert downloader._storage.__class__.__name__ == "SingleFileStorage"

    def test_ts_csv_only_still_creates_parquet_backend(self):
        downloader = _make_downloader(output_formats=[OutputFormat.TS_CSV])
        assert downloader._storage.__class__.__name__ == "SingleFileStorage"


class TestOutputFormatExport:
    """Downloader exports to requested plain-text formats after downloading."""

    def test_csv_flag_triggers_export(self):
        downloader = _make_downloader(output_formats=[OutputFormat.CSV])

        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
            patch("tradestation.csv_export.run_export_csv", return_value=0) as mock_csv,
            patch("tradestation.csv_export.run_export_ts_csv") as mock_ts,
        ):
            mock_get.return_value = _mock_bars_response([
                _bar("2025-01-01T00:00:00Z"),
                _bar("2025-01-01T00:01:00Z"),
                _bar("2025-01-01T00:02:00Z"),
            ])
            downloader.download_all(["@ES"], incremental=False)

        mock_csv.assert_called_once()
        assert mock_csv.call_args.args[0] == "config.yaml"
        assert mock_csv.call_args.args[1] == ["@ES"]
        assert mock_csv.call_args.kwargs["config"] is downloader.config
        assert mock_csv.call_args.kwargs["storage"] is downloader._storage
        mock_ts.assert_not_called()

    def test_ts_csv_flag_triggers_export(self):
        downloader = _make_downloader(output_formats=[OutputFormat.TS_CSV])

        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
            patch("tradestation.csv_export.run_export_csv") as mock_csv,
            patch("tradestation.csv_export.run_export_ts_csv", return_value=0) as mock_ts,
        ):
            mock_get.return_value = _mock_bars_response([
                _bar("2025-01-01T00:00:00Z"),
                _bar("2025-01-01T00:01:00Z"),
                _bar("2025-01-01T00:02:00Z"),
            ])
            downloader.download_all(["@ES"], incremental=False)

        mock_ts.assert_called_once()
        assert mock_ts.call_args.args[0] == "config.yaml"
        assert mock_ts.call_args.args[1] == ["@ES"]
        assert mock_ts.call_args.kwargs["config"] is downloader.config
        assert mock_ts.call_args.kwargs["storage"] is downloader._storage
        mock_csv.assert_not_called()

    def test_multiple_flags_trigger_both_exports(self):
        downloader = _make_downloader(
            output_formats=[OutputFormat.PARQUET, OutputFormat.CSV, OutputFormat.TS_CSV]
        )

        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
            patch("tradestation.csv_export.run_export_csv", return_value=0) as mock_csv,
            patch("tradestation.csv_export.run_export_ts_csv", return_value=0) as mock_ts,
        ):
            mock_get.return_value = _mock_bars_response([
                _bar("2025-01-01T00:00:00Z"),
                _bar("2025-01-01T00:01:00Z"),
                _bar("2025-01-01T00:02:00Z"),
            ])
            downloader.download_all(["@ES"], incremental=False)

        mock_csv.assert_called_once()
        mock_ts.assert_called_once()

    def test_export_failure_increments_error_count(self):
        downloader = _make_downloader(output_formats=[OutputFormat.CSV])

        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
            patch("tradestation.csv_export.run_export_csv", return_value=1),
        ):
            mock_get.return_value = _mock_bars_response([
                _bar("2025-01-01T00:00:00Z"),
                _bar("2025-01-01T00:01:00Z"),
                _bar("2025-01-01T00:02:00Z"),
            ])
            downloader.download_all(["@ES"], incremental=False)

        assert downloader.stats.export_errors == 1
