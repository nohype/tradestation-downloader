"""Tests for the CLI 'orders' sub-mode."""

import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tradestation.cli import (
    create_download_parser,
    create_orders_parser,
    main_download,
    run_orders_mode,
)
from tradestation.config import ConfigurationError
from tradestation.models import DownloadConfig
from tradestation.orders import ORDERS_ROOT, OrdersError


def _make_config(**overrides):
    """Build a DownloadConfig with sensible defaults (same convention as test_cli)."""
    defaults = {
        "client_id": "client_id",
        "client_secret": "client_secret",
        "refresh_token": "refresh_token",
        "symbols": [],
        "categories": [],
    }
    defaults.update(overrides)
    return DownloadConfig(**defaults)


def _run_orders_args(argv, account_id="ACC123", run_return=0):
    """Parse argv through the real orders parser and run run_orders_mode mocked.

    Returns (exit_code, mocks) where mocks is a dict of the applied patches.
    """
    parser = create_orders_parser()
    args = parser.parse_args(argv)
    config = _make_config()
    with (
        patch("tradestation.cli.load_credentials", return_value=config) as m_creds,
        patch("tradestation.auth.TradeStationAuth") as m_auth,
        patch(
            "tradestation.orders.resolve_account_id", return_value=account_id
        ) as m_resolve,
        patch("tradestation.orders.run_orders", return_value=run_return) as m_run,
    ):
        code = run_orders_mode(parser, args)
    return code, {
        "load_credentials": m_creds,
        "auth": m_auth,
        "resolve_account_id": m_resolve,
        "run_orders": m_run,
        "config": config,
    }


class TestDispatch:
    """Tests for argv[1] == 'orders' dispatch in main_download."""

    def test_orders_subcommand_routes_to_main_orders(self):
        """argv ['prog', 'orders', ...] routes to main_orders."""
        with (
            patch.object(
                sys, "argv", ["tradestation-download", "orders", "-s", "2024"]
            ),
            patch("tradestation.cli.main_orders", return_value=0) as m_orders,
            pytest.raises(SystemExit) as exc_info,
        ):
            main_download()
        assert exc_info.value.code == 0
        m_orders.assert_called_once_with()

    def test_help_shows_download_help(self, capsys):
        """argv ['prog', '--help'] still shows the download help."""
        with (
            patch.object(sys, "argv", ["tradestation-download", "--help"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main_download()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Download historical futures data" in out

    def test_orders_help_shows_orders_help(self, capsys):
        """argv ['prog', 'orders', '--help'] shows the orders help."""
        with (
            patch.object(
                sys, "argv", ["tradestation-download", "orders", "--help"]
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main_download()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "historical orders" in out

    def test_unknown_subcommand_exits_2(self):
        """argv ['prog', 'foo'] is rejected by argparse with exit code 2."""
        with (
            patch.object(sys, "argv", ["tradestation-download", "foo"]),
            pytest.raises(SystemExit) as exc_info,
        ):
            main_download()
        assert exc_info.value.code == 2

    def test_download_help_epilog_mentions_orders_mode(self):
        """The download parser's epilog advertises the 'orders' sub-mode."""
        epilog = create_download_parser().epilog
        assert "orders" in epilog
        assert "orders --help" in epilog


class TestOrdersDateParsing:
    """Tests for _make_orders_date_type via the orders parser."""

    def test_exact_date(self):
        """YYYY-MM-DD parses to the exact date."""
        args = create_orders_parser().parse_args(
            ["-s", "2024-01-15", "-e", "2024-06-30"]
        )
        assert args.start == date(2024, 1, 15)
        assert args.end == date(2024, 6, 30)

    def test_year_form(self):
        """YYYY resolves to Jan 1 for --start and Dec 31 for --end."""
        args = create_orders_parser().parse_args(["-s", "2024", "-e", "2024"])
        assert args.start == date(2024, 1, 1)
        assert args.end == date(2024, 12, 31)

    def test_year_month_form(self):
        """YYYY-MM resolves to first day for --start and last day for --end."""
        args = create_orders_parser().parse_args(["-s", "2024-06", "-e", "2024-06"])
        assert args.start == date(2024, 6, 1)
        assert args.end == date(2024, 6, 30)

    def test_leap_year_february_end(self):
        """-e 2024-02 resolves to 2024-02-29 (leap year)."""
        args = create_orders_parser().parse_args(["-e", "2024-02"])
        assert args.end == date(2024, 2, 29)

    def test_omitted_flags_resolve_to_today_bounds(self):
        """Omitted flags default to UTC today-729d (start) and today (end)."""
        today = datetime.now(UTC).date()
        args = create_orders_parser().parse_args([])
        assert args.start == today - timedelta(days=729)
        assert args.end == today

    def test_explicit_earliest_and_latest(self):
        """'earliest' on --start and 'latest' on --end resolve dynamically."""
        today = datetime.now(UTC).date()
        args = create_orders_parser().parse_args(
            ["-s", "earliest", "-e", "latest"]
        )
        assert args.start == today - timedelta(days=729)
        assert args.end == today

    def test_earliest_case_insensitive(self):
        """'Earliest' parses like 'earliest' (value is lowercased)."""
        args = create_orders_parser().parse_args(["-s", "Earliest"])
        assert args.start == datetime.now(UTC).date() - timedelta(days=729)

    @pytest.mark.parametrize("bad", ["2024-13-01", "not-a-date", "2024-1-1"])
    def test_invalid_dates_exit_2(self, bad):
        """Malformed dates are rejected with exit code 2."""
        with pytest.raises(SystemExit) as exc_info:
            create_orders_parser().parse_args(["-s", bad])
        assert exc_info.value.code == 2

    def test_invalid_start_date_stderr_explains_expected_formats(self, capsys):
        """-s 2024-13-01 → exit 2 + a friendly 'invalid start date' on stderr."""
        with pytest.raises(SystemExit) as exc_info:
            create_orders_parser().parse_args(["-s", "2024-13-01"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "invalid start date '2024-13-01'. Expected one of:" in err

    def test_invalid_end_date_stderr_explains_expected_formats(self, capsys):
        """-e 2024-02-30 → exit 2 + a friendly 'invalid end date' on stderr."""
        with pytest.raises(SystemExit) as exc_info:
            create_orders_parser().parse_args(["-e", "2024-02-30"])
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "invalid end date '2024-02-30'. Expected one of:" in err

    def test_latest_rejected_on_start(self):
        """-s latest is invalid ('latest' is only valid for --end)."""
        with pytest.raises(SystemExit) as exc_info:
            create_orders_parser().parse_args(["-s", "latest"])
        assert exc_info.value.code == 2

    def test_earliest_rejected_on_end(self):
        """-e earliest is invalid ('earliest' is only valid for --start)."""
        with pytest.raises(SystemExit) as exc_info:
            create_orders_parser().parse_args(["-e", "earliest"])
        assert exc_info.value.code == 2


class TestStartEndValidation:
    """Tests for the start <= end check in run_orders_mode."""

    def test_start_after_end_exits_2(self):
        """-s 2025-01-01 -e 2024-01-01 hits parser.error -> exit 2."""
        parser = create_orders_parser()
        args = parser.parse_args(["-s", "2025-01-01", "-e", "2024-01-01"])
        with pytest.raises(SystemExit) as exc_info:
            run_orders_mode(parser, args)
        assert exc_info.value.code == 2


class TestOrdersRunMode:
    """Tests for run_orders_mode orchestration."""

    def test_default_output_path(self):
        """Default output_path is orders/<acct>_<start>_<end>.csv."""
        code, mocks = _run_orders_args(["-s", "2024-01-01", "-e", "2024-02-01"])
        assert code == 0
        kwargs = mocks["run_orders"].call_args
        assert (
            kwargs.kwargs["output_path"]
            == ORDERS_ROOT / "ACC123_2024-01-01_2024-02-01.csv"
        )

    def test_out_override_honored(self):
        """-o overrides the default output path."""
        code, mocks = _run_orders_args(
            ["-s", "2024-01-01", "-e", "2024-02-01", "-o", "my_orders.csv"]
        )
        assert code == 0
        assert mocks["run_orders"].call_args.kwargs["output_path"] == Path(
            "my_orders.csv"
        )

    def test_explicit_account_forwarded(self):
        """-a ACC123 -> run_orders receives 'ACC123' as account_id."""
        code, mocks = _run_orders_args(
            ["-s", "2024-01-01", "-e", "2024-02-01", "-a", "ACC123"]
        )
        assert code == 0
        mocks["resolve_account_id"].assert_called_once_with(
            mocks["auth"].return_value, "ACC123"
        )
        assert mocks["run_orders"].call_args.args[2] == "ACC123"

    def test_missing_config_returns_1(self, tmp_path):
        """A missing config file -> ConfigurationError -> return 1."""
        parser = create_orders_parser()
        args = parser.parse_args(["-c", str(tmp_path / "nonexistent.yaml")])
        with patch("tradestation.orders.run_orders") as m_run:
            code = run_orders_mode(parser, args)
        assert code == 1
        m_run.assert_not_called()

    def test_verbose_flag_parses(self):
        """-v sets verbose on the parsed namespace."""
        args = create_orders_parser().parse_args(["-v"])
        assert args.verbose is True

    def test_single_auth_instance_shared(self):
        """Exactly one TradeStationAuth is built and shared with both calls."""
        code, mocks = _run_orders_args(
            ["-s", "2024-01-01", "-e", "2024-02-01"]
        )
        assert code == 0
        mocks["auth"].assert_called_once_with(
            "client_id", "client_secret", "refresh_token"
        )
        auth_instance = mocks["auth"].return_value
        assert mocks["resolve_account_id"].call_args.args[0] is auth_instance
        assert mocks["run_orders"].call_args.args[1] is auth_instance

    def test_orders_error_from_resolve_returns_1(self, caplog):
        """OrdersError raised by resolve_account_id -> logged, return 1."""
        parser = create_orders_parser()
        args = parser.parse_args(["-s", "2024-01-01", "-e", "2024-02-01"])
        with (
            caplog.at_level(logging.ERROR),
            patch(
                "tradestation.cli.load_credentials", return_value=_make_config()
            ),
            patch("tradestation.auth.TradeStationAuth"),
            patch(
                "tradestation.orders.resolve_account_id",
                side_effect=OrdersError("API failure"),
            ),
            patch("tradestation.orders.run_orders") as m_run,
        ):
            code = run_orders_mode(parser, args)
        assert code == 1
        assert "API failure" in caplog.text
        m_run.assert_not_called()

    def test_authentication_error_from_resolve_returns_1(self, caplog):
        """AuthenticationError raised by resolve_account_id -> logged, return 1."""
        from tradestation.auth import AuthenticationError

        parser = create_orders_parser()
        args = parser.parse_args(["-s", "2024-01-01", "-e", "2024-02-01"])
        with (
            caplog.at_level(logging.ERROR),
            patch(
                "tradestation.cli.load_credentials", return_value=_make_config()
            ),
            patch("tradestation.auth.TradeStationAuth"),
            patch(
                "tradestation.orders.resolve_account_id",
                side_effect=AuthenticationError("token refresh failed"),
            ),
            patch("tradestation.orders.run_orders") as m_run,
        ):
            code = run_orders_mode(parser, args)
        assert code == 1
        assert "token refresh failed" in caplog.text
        m_run.assert_not_called()

    def test_keyboard_interrupt_during_resolve_returns_130(self):
        """Ctrl-C while resolving the account → 130, no traceback."""
        parser = create_orders_parser()
        args = parser.parse_args(["-s", "2024-01-01", "-e", "2024-02-01"])
        with (
            patch(
                "tradestation.cli.load_credentials", return_value=_make_config()
            ),
            patch("tradestation.auth.TradeStationAuth"),
            patch(
                "tradestation.orders.resolve_account_id",
                side_effect=KeyboardInterrupt,
            ),
            patch("tradestation.orders.run_orders") as m_run,
        ):
            code = run_orders_mode(parser, args)
        assert code == 130
        m_run.assert_not_called()

    def test_config_error_returns_1(self, caplog):
        """ConfigurationError from load_credentials -> logged, return 1."""
        parser = create_orders_parser()
        args = parser.parse_args(["-s", "2024-01-01", "-e", "2024-02-01"])
        with (
            caplog.at_level(logging.ERROR),
            patch(
                "tradestation.cli.load_credentials",
                side_effect=ConfigurationError("bad config"),
            ),
            patch("tradestation.orders.run_orders") as m_run,
        ):
            code = run_orders_mode(parser, args)
        assert code == 1
        assert "bad config" in caplog.text
        m_run.assert_not_called()


class TestIncludeOpenFlag:
    """Tests for the --include-open flag."""

    def test_include_open_parses_true(self):
        """--include-open is a store_true flag consuming no value."""
        args = create_orders_parser().parse_args(["--include-open"])
        assert args.include_open is True

    def test_include_open_absent_is_false(self):
        """Without the flag, include_open defaults to False."""
        args = create_orders_parser().parse_args([])
        assert args.include_open is False

    def test_include_open_forwarded_to_run_orders(self):
        """--include-open is forwarded as include_open=True kwarg."""
        code, mocks = _run_orders_args(
            ["-s", "2024-01-01", "-e", "2024-02-01", "--include-open"]
        )
        assert code == 0
        assert mocks["run_orders"].call_args.kwargs["include_open"] is True

    def test_include_open_absent_forwarded_false(self):
        """Without --include-open, include_open=False is forwarded."""
        code, mocks = _run_orders_args(
            ["-s", "2024-01-01", "-e", "2024-02-01"]
        )
        assert code == 0
        assert mocks["run_orders"].call_args.kwargs["include_open"] is False

    def test_including_open_orders_logged_only_when_set(self, caplog):
        """'Including open orders' is logged only when --include-open is set."""
        parser = create_orders_parser()
        args = parser.parse_args(
            ["-s", "2024-01-01", "-e", "2024-02-01", "--include-open"]
        )
        with (
            caplog.at_level(logging.INFO),
            patch(
                "tradestation.cli.load_credentials", return_value=_make_config()
            ),
            patch("tradestation.auth.TradeStationAuth"),
            patch(
                "tradestation.orders.resolve_account_id", return_value="ACC123"
            ),
            patch("tradestation.orders.run_orders", return_value=0),
        ):
            run_orders_mode(parser, args)
        assert "Including open orders" in caplog.text

    def test_including_open_orders_not_logged_by_default(self, caplog):
        """'Including open orders' is not logged without the flag."""
        parser = create_orders_parser()
        args = parser.parse_args(["-s", "2024-01-01", "-e", "2024-02-01"])
        with (
            caplog.at_level(logging.INFO),
            patch(
                "tradestation.cli.load_credentials", return_value=_make_config()
            ),
            patch("tradestation.auth.TradeStationAuth"),
            patch(
                "tradestation.orders.resolve_account_id", return_value="ACC123"
            ),
            patch("tradestation.orders.run_orders", return_value=0),
        ):
            run_orders_mode(parser, args)
        assert "Including open orders" not in caplog.text
