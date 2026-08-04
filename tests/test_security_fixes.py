"""Security regression tests for TradeStation downloader fixes."""

import contextlib
import re
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import Mock, patch

import pytest

from tradestation.auth import AuthenticationError
from tradestation.auth_setup import (
    CallbackHandler,
    exchange_code_for_tokens,
    get_authorization_code,
)
from tradestation.downloader import TradeStationDownloader
from tradestation.models import DownloadConfig, validate_symbol


class TestValidateSymbol:
    """Tests for validate_symbol security hardening."""

    @pytest.mark.parametrize(
        "symbol",
        [
            "@ES",
            "@NQ",
            "@CL",
            "$SPX",
            ":AAPL",
            "ES",
            "NQ",
            "AAPL",
            "BTC-USD",
            "@MES",
            "@MCL",
        ],
    )
    def test_valid_symbols(self, symbol):
        """Real-world TradeStation symbols should be accepted."""
        validate_symbol(symbol)

    @pytest.mark.parametrize(
        "symbol",
        [
            "../../etc/cron.d/x",
            "..\\windows\\system32",
            "foo/../bar",
            "x/../../y",
        ],
    )
    def test_rejects_path_traversal(self, symbol):
        with pytest.raises(ValueError):
            validate_symbol(symbol)

    @pytest.mark.parametrize(
        "symbol",
        [
            "ES?malicious=1",
            "ES#fragment",
            "ES path",
            "ES%00",
            "ES/../../etc",
        ],
    )
    def test_rejects_url_injection(self, symbol):
        with pytest.raises(ValueError):
            validate_symbol(symbol)

    @pytest.mark.parametrize(
        "symbol",
        [
            "ES\x00",
            "ES\n",
            "ES\t",
            "\x00",
        ],
    )
    def test_rejects_control_chars(self, symbol):
        with pytest.raises(ValueError):
            validate_symbol(symbol)

    @pytest.mark.parametrize("symbol", [None, 123, []])
    def test_rejects_non_string(self, symbol):
        with pytest.raises((ValueError, TypeError)):
            validate_symbol(symbol)

    @pytest.mark.parametrize("symbol", ["ES..X", "..ES", "ES.."])
    def test_rejects_dotdot_substring(self, symbol):
        with pytest.raises(ValueError):
            validate_symbol(symbol)


class TestApiRequest401:
    """Tests for 401 refresh-once-then-fail behavior."""

    @pytest.fixture
    def downloader(self):
        config = DownloadConfig(
            client_id="id",
            client_secret="secret",
            refresh_token="refresh",
            data_dir=tempfile.mkdtemp(),
            max_retries=3,
        )
        return TradeStationDownloader(config)

    @pytest.fixture(autouse=True)
    def _prewarm_auth(self, downloader):
        """Give the downloader a token that does not need refreshing yet."""
        downloader._auth._access_token = "current_token"
        downloader._auth._token_expiry = datetime.now() + timedelta(hours=1)

    def _mock_ok(self):
        resp = Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"Bars": []}
        resp.raise_for_status = Mock()
        return resp

    def _mock_401(self):
        resp = Mock()
        resp.status_code = 401
        resp.headers = {}
        resp.raise_for_status = Mock()
        return resp

    def _mock_token_response(self):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"access_token": "new_token", "expires_in": 3600}
        resp.raise_for_status = Mock()
        return resp

    @patch("tradestation.downloader.requests.get")
    @patch("tradestation.auth.requests.post")
    def test_401_refreshes_once_then_succeeds(self, mock_post, mock_get, downloader):
        mock_get.side_effect = [self._mock_401(), self._mock_ok()]
        mock_post.return_value = self._mock_token_response()

        result = downloader._api_request("ES", datetime.now(timezone.utc))

        assert result == {"Bars": []}
        assert mock_get.call_count == 2
        assert mock_post.call_count == 1

    @patch("tradestation.downloader.requests.get")
    @patch("tradestation.auth.requests.post")
    def test_401_persistent_raises_after_one_retry(self, mock_post, mock_get, downloader):
        mock_get.side_effect = [self._mock_401(), self._mock_401()]
        mock_post.return_value = self._mock_token_response()

        with pytest.raises(AuthenticationError):
            downloader._api_request("ES", datetime.now(timezone.utc))

        assert mock_get.call_count == 2
        assert mock_post.call_count == 1

    @patch("tradestation.downloader.requests.get")
    @patch("tradestation.auth.requests.post")
    def test_401_with_max_retries_zero_fails_immediately(self, mock_post, mock_get, downloader):
        downloader.config.max_retries = 0
        mock_get.return_value = self._mock_401()

        with pytest.raises(AuthenticationError):
            downloader._api_request("ES", datetime.now(timezone.utc))

        assert mock_get.call_count == 1
        assert mock_post.call_count == 0


class TestApiRequest429:
    """Tests for 429 retry-then-stop behavior."""

    @pytest.fixture
    def downloader(self):
        config = DownloadConfig(
            client_id="id",
            client_secret="secret",
            refresh_token="refresh",
            data_dir=tempfile.mkdtemp(),
            max_retries=3,
        )
        return TradeStationDownloader(config)

    @pytest.fixture(autouse=True)
    def _prewarm_auth(self, downloader):
        """Give the downloader a token that does not need refreshing."""
        downloader._auth._access_token = "current_token"
        downloader._auth._token_expiry = datetime.now() + timedelta(hours=1)

    def _mock_429(self):
        resp = Mock()
        resp.status_code = 429
        resp.headers = {"Retry-After": "0"}
        resp.raise_for_status = Mock()
        return resp

    def _mock_ok(self):
        resp = Mock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = {"Bars": []}
        resp.raise_for_status = Mock()
        return resp

    @patch("tradestation.downloader.requests.get")
    @patch("tradestation.downloader.time.sleep")
    def test_429_retries_then_succeeds(self, _mock_sleep, mock_get, downloader):
        mock_get.side_effect = [self._mock_429(), self._mock_ok()]

        result = downloader._api_request("ES", datetime.now(timezone.utc))

        assert result == {"Bars": []}
        assert mock_get.call_count == 2

    @patch("tradestation.downloader.requests.get")
    @patch("tradestation.downloader.time.sleep")
    def test_429_persistent_stops_after_max_retries(self, _mock_sleep, mock_get, downloader):
        mock_get.return_value = self._mock_429()

        result = downloader._api_request("ES", datetime.now(timezone.utc))

        assert result is None
        assert mock_get.call_count == downloader.config.max_retries + 1


class TestOAuthState:
    """Tests for OAuth state parameter security."""

    def _make_handler(self, path):
        handler = Mock()
        handler.path = path
        handler.wfile = BytesIO()
        return handler

    def test_callback_accepts_matching_state(self):
        CallbackHandler.auth_code = None
        CallbackHandler.state = "expected_state"

        handler = self._make_handler("/?code=abc&state=expected_state")
        CallbackHandler.do_GET(handler)

        assert CallbackHandler.auth_code == "abc"
        assert handler.send_response.call_args[0][0] == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/?code=abc&state=wrong",
            "/?code=abc",
            "/?state=expected_state",
        ],
    )
    def test_callback_rejects_missing_or_mismatched_state(self, path):
        CallbackHandler.auth_code = None
        CallbackHandler.state = "expected_state"

        handler = self._make_handler(path)
        CallbackHandler.do_GET(handler)

        assert CallbackHandler.auth_code is None
        assert handler.send_response.call_args[0][0] == 400

    @patch("tradestation.auth_setup.HTTPServer")
    @patch("tradestation.auth_setup.webbrowser.open")
    @patch("tradestation.auth_setup.threading.Thread")
    def test_state_is_unique_per_authorization(self, _mock_thread, mock_webbrowser, _mock_server):
        CallbackHandler.auth_code = None
        captured_urls = []

        def capture(url):
            captured_urls.append(url)

        mock_webbrowser.side_effect = capture

        for _ in range(2):
            with contextlib.suppress(TimeoutError):
                get_authorization_code("client_id")

        assert len(captured_urls) == 2
        states = [re.search(r"state=([^&]+)", url).group(1) for url in captured_urls]
        assert states[0] != states[1]


class TestTokenExchangeTimeout:
    """Tests for token exchange timeout."""

    @patch("tradestation.auth_setup.requests.post")
    def test_token_exchange_has_timeout(self, mock_post):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
        }
        mock_post.return_value = mock_response

        exchange_code_for_tokens("client_id", "client_secret", "auth_code")

        assert mock_post.called
        _, kwargs = mock_post.call_args
        assert "timeout" in kwargs
        assert kwargs["timeout"] == 30
