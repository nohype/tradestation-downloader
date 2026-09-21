"""Tests for the CLI download command."""

import argparse
import logging
from unittest.mock import patch

from tradestation.cli import create_download_parser, run_download
from tradestation.config import ConfigurationError
from tradestation.models import (
    DEFAULT_SYMBOLS,
    DownloadConfig,
    get_symbols_by_categories,
)


def _make_args(**overrides):
    """Build an argparse Namespace with sensible defaults."""
    defaults = {
        "config": "config.yaml",
        "list_symbols": False,
        "list_categories": False,
        "metadata": False,
        "export_csv": False,
        "export_ts_csv": False,
        "rollcheck": False,
        "symbols": None,
        "category": None,
        "all_categories": False,
        "full": False,
        "storage_format": None,
        "compression": None,
        "no_datetime_index": False,
        "workers": 4,
        "verbose": False,
        "use_continuous_default_fallback": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_config(**overrides):
    """Build a DownloadConfig with sensible defaults."""
    defaults = {
        "client_id": "client_id",
        "client_secret": "client_secret",
        "refresh_token": "refresh_token",
        "symbols": [],
        "categories": [],
    }
    defaults.update(overrides)
    return DownloadConfig(**defaults)


class TestRunDownload:
    """Tests for run_download in the CLI."""

    def test_all_categories_expands_and_downloads(self):
        """--all-categories expands configured categories and calls download_all."""
        config = _make_config(categories=["index", "energy"])
        args = _make_args(all_categories=True)

        with (
            patch("tradestation.cli.load_config", return_value=config) as mock_load,
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            mock_downloader.return_value.stats.errors = 0

            result = run_download(args)

        assert result == 0
        mock_load.assert_called_once_with("config.yaml")
        assert config.symbols == get_symbols_by_categories(["index", "energy"])
        mock_downloader.assert_called_once_with(config)
        mock_downloader.return_value.download_all.assert_called_once_with(
            incremental=True
        )

    def test_all_categories_empty_logs_error_and_returns(self, caplog):
        """--all-categories with no configured categories logs an error and exits."""
        config = _make_config(categories=[])
        args = _make_args(all_categories=True)

        with (
            caplog.at_level(logging.ERROR),
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            result = run_download(args)

        assert result == 1
        assert "no categories are configured" in caplog.text
        mock_downloader.assert_not_called()

    def test_default_downloads_configured_symbols(self):
        """Default run downloads symbols already configured in the config."""
        config = _make_config(symbols=["@ES"])
        args = _make_args()

        with (
            patch("tradestation.cli.load_config", return_value=config) as mock_load,
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            mock_downloader.return_value.stats.errors = 0

            result = run_download(args)

        assert result == 0
        mock_load.assert_called_once_with("config.yaml")
        assert config.symbols == ["@ES"]
        mock_downloader.assert_called_once_with(config)
        mock_downloader.return_value.download_all.assert_called_once_with(
            incremental=True
        )

    def test_default_no_symbols_logs_error_and_returns(self, caplog):
        """Default with no configured symbols logs an error and exits."""
        config = _make_config(symbols=[])
        args = _make_args()

        with (
            caplog.at_level(logging.ERROR),
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            result = run_download(args)

        assert result == 1
        assert "No symbols are configured" in caplog.text
        mock_downloader.assert_not_called()

    def test_explicit_symbols_override_all_categories(self):
        """-s overrides --all-categories and uses the provided symbols."""
        config = _make_config(categories=["index", "energy"])
        args = _make_args(all_categories=True, symbols=["@NQ", "@CL"])

        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            mock_downloader.return_value.stats.errors = 0

            result = run_download(args)

        assert result == 0
        assert config.symbols == ["@NQ", "@CL"]
        mock_downloader.assert_called_once_with(config)
        mock_downloader.return_value.download_all.assert_called_once_with(
            incremental=True
        )

    def test_category_override_all_categories(self):
        """--category overrides --all-categories and uses that category's symbols."""
        config = _make_config(categories=["index", "energy"])
        args = _make_args(all_categories=True, category="index")

        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            mock_downloader.return_value.stats.errors = 0

            result = run_download(args)

        assert result == 0
        assert config.symbols == DEFAULT_SYMBOLS["index"]
        mock_downloader.assert_called_once_with(config)
        mock_downloader.return_value.download_all.assert_called_once_with(
            incremental=True
        )

    def test_use_continuous_default_fallback_flag_enables_config(self):
        """--use-continuous-default-fallback sets the config field."""
        config = _make_config(symbols=["@RTY"])
        args = _make_args(use_continuous_default_fallback=True)

        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            mock_downloader.return_value.stats.errors = 0

            result = run_download(args)

        assert result == 0
        assert config.use_continuous_default_fallback is True
        mock_downloader.assert_called_once_with(config)

    def test_use_continuous_default_fallback_off_by_default(self):
        """Without the flag the config field stays False."""
        config = _make_config(symbols=["@RTY"])
        args = _make_args()

        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            mock_downloader.return_value.stats.errors = 0

            result = run_download(args)

        assert result == 0
        assert config.use_continuous_default_fallback is False

    def test_all_categories_invalid_category_logs_error_and_returns(self, caplog):
        """--all-categories logs a config error and returns when a category is invalid."""
        valid = ", ".join(DEFAULT_SYMBOLS.keys())
        error_msg = f"Unknown category: 'indexx'. Valid categories: {valid}"
        args = _make_args(all_categories=True)

        with (
            caplog.at_level(logging.ERROR),
            patch("tradestation.cli.load_config", side_effect=ConfigurationError(error_msg)),
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            result = run_download(args)

        assert result == 1
        assert "Unknown category: 'indexx'" in caplog.text
        mock_downloader.assert_not_called()

    def test_export_ts_csv_flag_parsed(self):
        """--export-ts-csv sets export_ts_csv on the parsed namespace."""
        args = create_download_parser().parse_args(["--export-ts-csv"])
        assert args.export_ts_csv is True
        assert args.export_csv is False

    def test_export_ts_csv_routes_to_ts_export(self):
        """--export-ts-csv alone calls run_export_ts_csv and exits."""
        args = _make_args(export_ts_csv=True, symbols=["@ES"])

        with (
            patch("tradestation.csv_export.run_export_csv") as mock_csv,
            patch(
                "tradestation.csv_export.run_export_ts_csv", return_value=0
            ) as mock_ts,
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            result = run_download(args)

        assert result == 0
        mock_ts.assert_called_once_with("config.yaml", ["@ES"])
        mock_csv.assert_not_called()
        mock_downloader.assert_not_called()

    def test_export_ts_csv_failure_returns_1(self):
        """A failing TS export propagates exit code 1."""
        args = _make_args(export_ts_csv=True)

        with patch(
            "tradestation.csv_export.run_export_ts_csv", return_value=1
        ):
            result = run_download(args)

        assert result == 1

    def test_both_export_flags_run_both_exports(self):
        """--export-csv and --export-ts-csv together run both exports."""
        args = _make_args(export_csv=True, export_ts_csv=True, symbols=["@ES"])

        with (
            patch(
                "tradestation.csv_export.run_export_csv", return_value=0
            ) as mock_csv,
            patch(
                "tradestation.csv_export.run_export_ts_csv", return_value=0
            ) as mock_ts,
            patch("tradestation.cli.TradeStationDownloader") as mock_downloader,
        ):
            result = run_download(args)

        assert result == 0
        mock_csv.assert_called_once_with("config.yaml", ["@ES"])
        mock_ts.assert_called_once_with("config.yaml", ["@ES"])
        mock_downloader.assert_not_called()

    def test_both_export_flags_failure_still_runs_both(self):
        """When the CSV export fails, the TS export still runs and exit is 1."""
        args = _make_args(export_csv=True, export_ts_csv=True)

        with (
            patch(
                "tradestation.csv_export.run_export_csv", return_value=1
            ) as mock_csv,
            patch(
                "tradestation.csv_export.run_export_ts_csv", return_value=0
            ) as mock_ts,
        ):
            result = run_download(args)

        assert result == 1
        mock_csv.assert_called_once()
        mock_ts.assert_called_once()
