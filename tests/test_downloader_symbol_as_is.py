"""Red-phase tests: barcharts must be requested with the exact user symbol.

The downloader must never append the continuous-contract suffix (=11INC) to a
user-provided symbol and must never probe the API for history coverage before
downloading. Every barchart request of a download run must use the symbol
exactly as provided via the CLI or config.yaml.
"""

import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from tradestation.downloader import TradeStationDownloader
from tradestation.models import DownloadConfig


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


class TestBarchartSymbolAsIs:
    """Barchart requests use each symbol exactly as the user provided it."""

    def test_continuous_symbol_requested_exactly_as_provided(self):
        """@ES is requested as @ES, never rewritten to @ES=11INC."""
        downloader = _make_downloader()
        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
        ):
            mock_get.return_value = _mock_bars_response([_bar("2025-01-01T00:00:00Z")])
            downloader.download_symbol("@ES", incremental=False)

        url = mock_get.call_args[0][0]
        assert url.endswith("/barcharts/@ES"), (
            f"Expected the exact user symbol in the barchart URL, got {url}"
        )
        assert mock_get.call_count == 1, (
            f"Expected a single barchart request, got {mock_get.call_count}"
        )

    def test_suffixed_and_plain_forms_requested_independently(self):
        """@ES=11INC and @ES are two distinct user symbols; each is used verbatim."""
        downloader = _make_downloader()
        urls = {}
        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
        ):
            mock_get.return_value = _mock_bars_response([_bar("2025-01-01T00:00:00Z")])
            for symbol in ("@ES=11INC", "@ES"):
                downloader.download_symbol(symbol, incremental=False)
                urls[symbol] = mock_get.call_args[0][0]

        assert urls["@ES=11INC"].endswith("/barcharts/@ES=11INC"), (
            f"Expected @ES=11INC requested as-is, got {urls['@ES=11INC']}"
        )
        assert urls["@ES"].endswith("/barcharts/@ES"), (
            f"Expected @ES requested as-is, got {urls['@ES']}"
        )

    def test_non_continuous_symbols_requested_exactly_as_provided(self):
        """Non-continuous symbols (@MNGV26, ESZ25) are never altered."""
        downloader = _make_downloader()
        urls = {}
        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
        ):
            mock_get.return_value = _mock_bars_response([_bar("2025-01-01T00:00:00Z")])
            for symbol in ("@MNGV26", "ESZ25"):
                downloader.download_symbol(symbol, incremental=False)
                urls[symbol] = mock_get.call_args[0][0]

        assert urls["@MNGV26"].endswith("/barcharts/@MNGV26"), (
            f"Expected @MNGV26 requested as-is, got {urls['@MNGV26']}"
        )
        assert urls["ESZ25"].endswith("/barcharts/ESZ25"), (
            f"Expected ESZ25 requested as-is, got {urls['ESZ25']}"
        )


class TestNoCoverageProbing:
    """A download run issues no extra history-coverage probe requests."""

    def test_download_run_issues_only_exact_barchart_requests(self):
        """download_all requests @ES once; no probe or suffixed-symbol requests."""
        downloader = _make_downloader(max_workers=1)
        with (
            patch("tradestation.downloader.requests.get") as mock_get,
            patch("tradestation.downloader.time.sleep"),
        ):
            mock_get.return_value = _mock_bars_response([_bar("2025-01-01T00:00:00Z")])
            downloader.download_all(["@ES"], incremental=False)

        assert mock_get.call_args_list, "Expected at least one barchart request"
        for call in mock_get.call_args_list:
            url = call[0][0]
            assert url.endswith("/barcharts/@ES"), (
                f"Unexpected request (coverage probe or suffixed symbol): {url}"
            )
        assert mock_get.call_count == 1, (
            f"Expected exactly one barchart request, got {mock_get.call_count}"
        )


class TestCoverageProbeMachineryRemoved:
    """The history-coverage probing machinery is removed from the downloader."""

    def test_probe_machinery_attributes_removed(self):
        """_resolve_api_symbol, _has_bars_at and _api_symbol_cache no longer exist."""
        downloader = _make_downloader()
        assert not hasattr(downloader, "_resolve_api_symbol")
        assert not hasattr(downloader, "_has_bars_at")
        assert not hasattr(downloader, "_api_symbol_cache")
