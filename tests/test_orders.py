"""Tests for tradestation.orders (orders mode data layer)."""

import json
import logging
from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pandas as pd
import pytest
import requests

from tradestation.auth import AuthenticationError
from tradestation.models import DownloadConfig
from tradestation.orders import (
    BASE_URL,
    ORDER_COLUMNS,
    PAGE_SIZE,
    AccountResolutionError,
    Coverage,
    OrdersError,
    _account_cache_dir,
    _api_get,
    _build_fetch_plan,
    _empty_orders_dataframe,
    _fetch_historical_order_ids,
    _fetch_historical_orders,
    _load_cache,
    _load_coverage,
    _merge_orders,
    _orders_to_dataframe,
    _save_cache,
    _save_coverage,
    _write_orders_csv,
    resolve_account_id,
    run_orders,
)


def _order(**overrides):
    """Build a plausible API order dict; overrides replace/add fields."""
    order = {
        "OrderID": "ORD-1",
        "AccountID": "ACC123",
        "OpenedDateTime": "2024-01-15T14:30:00Z",
        "ClosedDateTime": "2024-01-15T15:00:00Z",
        "Status": "FLL",
        "StatusDescription": "Filled",
        "OrderType": "Limit",
        "Duration": "DAY",
        "LimitPrice": "5025.25",
        "FilledPrice": "5024.75",
        "Legs": [{"Symbol": "@ESH24", "Quantity": "2", "BuyOrSell": "Buy"}],
    }
    order.update(overrides)
    return order


def _response(json_data=None, status_code=200, headers=None, text=""):
    """Build a fake requests.Response."""
    resp = Mock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.headers = headers or {}
    resp.text = text
    resp.json.return_value = json_data
    return resp


def _make_auth():
    """Fake TradeStationAuth exposing the methods orders.py uses."""
    auth = Mock()
    auth.get_access_token.return_value = "access-token"
    return auth


def _account(account_id, **extra):
    """Build a brokerage account payload entry."""
    account = {
        "AccountID": account_id,
        "Alias": "Main",
        "AccountType": "Futures",
        "Status": "Active",
        "Currency": "USD",
    }
    account.update(extra)
    return account


class TestOrdersApiGet:
    """Tests for _api_get retry/backoff and error mapping."""

    def test_429_honors_retry_after_header(self):
        """A 429 with Retry-After sleeps that long and retries."""
        responses = [
            _response(status_code=429, headers={"Retry-After": "7"}),
            _response({"Orders": []}),
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses),
            patch("tradestation.orders.time.sleep") as mock_sleep,
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert result == {"Orders": []}
        mock_sleep.assert_called_once_with(7)

    def test_429_without_retry_after_uses_60s_default(self):
        """A 429 without the header falls back to a 60s wait."""
        responses = [
            _response(status_code=429),
            _response({"Orders": []}),
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses),
            patch("tradestation.orders.time.sleep") as mock_sleep,
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert result == {"Orders": []}
        mock_sleep.assert_called_once_with(60)

    def test_429_malformed_retry_after_uses_60s_default(self):
        """A non-numeric Retry-After is treated like a missing one (60s)."""
        responses = [
            _response(status_code=429, headers={"Retry-After": "soon"}),
            _response({"Orders": []}),
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses),
            patch("tradestation.orders.time.sleep") as mock_sleep,
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert result == {"Orders": []}
        mock_sleep.assert_called_once_with(60)

    def test_429_exhausted_retries_raises_orders_error(self):
        """Sustained 429s beyond max_retries raise OrdersError."""
        responses = [_response(status_code=429)] * 4
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses),
            patch("tradestation.orders.time.sleep"),
            pytest.raises(OrdersError, match="Rate limited"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

    def test_401_invalidates_token_and_retries_once(self):
        """A 401 invalidates the token and the retry can succeed."""
        responses = [
            _response(status_code=401),
            _response({"Orders": [_order()]}),
        ]
        auth = _make_auth()

        with patch("tradestation.orders.requests.get", side_effect=responses) as mock_get:
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert len(result["Orders"]) == 1
        auth.invalidate.assert_called_once_with()
        assert mock_get.call_count == 2

    def test_double_401_raises_authentication_error(self):
        """A second 401 after the token refresh raises AuthenticationError."""
        responses = [_response(status_code=401), _response(status_code=401)]
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses),
            pytest.raises(AuthenticationError),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

        auth.invalidate.assert_called_once_with()

    def test_429_then_401_then_200_still_retries_auth_once(self):
        """A prior 429 retry must NOT consume the single 401 retry."""
        responses = [
            _response(status_code=429),
            _response(status_code=401),
            _response({"Orders": []}),
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses) as mock_get,
            patch("tradestation.orders.time.sleep"),
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert result == {"Orders": []}
        auth.invalidate.assert_called_once_with()
        assert mock_get.call_count == 3

    def test_502_retries_once_then_succeeds(self):
        """A 5xx is retried with 2**retry backoff and can then succeed."""
        responses = [
            _response(status_code=502),
            _response({"Orders": []}),
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses) as mock_get,
            patch("tradestation.orders.time.sleep") as mock_sleep,
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert result == {"Orders": []}
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(1)

    def test_502_exhausted_retries_raises_orders_error(self):
        """Sustained 5xx beyond max_retries raises OrdersError."""
        responses = [_response(status_code=502)] * 4
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", side_effect=responses) as mock_get,
            patch("tradestation.orders.time.sleep"),
            pytest.raises(OrdersError, match="502"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

        assert mock_get.call_count == 4

    def test_request_exception_backs_off_then_raises_orders_error(self):
        """RequestException retries with 2**retry backoff, then OrdersError."""
        auth = _make_auth()

        with (
            patch(
                "tradestation.orders.requests.get",
                side_effect=requests.ConnectionError("down"),
            ),
            patch("tradestation.orders.time.sleep") as mock_sleep,
            pytest.raises(OrdersError, match="Request failed after 3 retries"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

        assert [c.args[0] for c in mock_sleep.call_args_list] == [1, 2, 4]

    def test_request_exception_eventually_succeeds(self):
        """A transient RequestException is retried and can succeed."""
        auth = _make_auth()

        with (
            patch(
                "tradestation.orders.requests.get",
                side_effect=[
                    requests.ConnectionError("down"),
                    _response({"Orders": []}),
                ],
            ),
            patch("tradestation.orders.time.sleep") as mock_sleep,
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert result == {"Orders": []}
        mock_sleep.assert_called_once_with(1)

    def test_404_raises_orders_error(self):
        """A 404 raises OrdersError mentioning the missing resource."""
        auth = _make_auth()

        with (
            patch(
                "tradestation.orders.requests.get",
                return_value=_response(status_code=404),
            ) as mock_get,
            pytest.raises(OrdersError, match="404"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

        assert mock_get.call_count == 1

    def test_other_error_status_raises_orders_error_with_excerpt(self):
        """A non-2xx status raises OrdersError with a bounded body excerpt."""
        auth = _make_auth()
        resp = _response(status_code=500, text="x" * 500)

        with (
            patch("tradestation.orders.requests.get", return_value=resp),
            patch("tradestation.orders.time.sleep"),
            pytest.raises(OrdersError, match="500"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

    def test_invalid_json_raises_orders_error(self):
        """A 200 with a non-JSON body raises OrdersError."""
        resp = _response()
        resp.json.side_effect = ValueError("no json")
        auth = _make_auth()

        with (
            patch("tradestation.orders.requests.get", return_value=resp),
            pytest.raises(OrdersError, match="Invalid JSON"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)


class TestHistoricalPagination:
    """Tests for _fetch_orders_pages via _fetch_historical_orders."""

    URL = "https://api.tradestation.com/v3/brokerage/accounts/ACC123/historicalorders"
    SINCE = date(2024, 1, 1)

    def test_second_page_repeats_params_and_sends_next_token(self):
        """Page 2 repeats since/pageSize=600 and adds the nextToken cursor."""
        pages = [
            {"Orders": [_order()], "NextToken": "T1"},
            {"Orders": [_order(OrderID="ORD-2")]},
        ]
        auth = _make_auth()

        with patch("tradestation.orders._api_get", side_effect=pages) as mock_get:
            orders = _fetch_historical_orders(
                auth, "ACC123", self.SINCE, max_retries=3, rate_limit_delay=0
            )

        assert [o["OrderID"] for o in orders] == ["ORD-1", "ORD-2"]
        assert mock_get.call_count == 2
        call1 = mock_get.call_args_list[0]
        call2 = mock_get.call_args_list[1]
        assert call1.args[1] == self.URL
        assert call1.args[2] == {"since": "2024-01-01", "pageSize": PAGE_SIZE}
        assert call2.args[2] == {
            "since": "2024-01-01",
            "pageSize": PAGE_SIZE,
            "nextToken": "T1",
        }

    def test_sleep_between_pages_only(self):
        """rate_limit_delay is slept between pages, not before page 1."""
        pages = [
            {"Orders": [_order()], "NextToken": "T1"},
            {"Orders": [_order()], "NextToken": "T2"},
            {"Orders": [_order()]},
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders._api_get", side_effect=pages),
            patch("tradestation.orders.time.sleep") as mock_sleep,
        ):
            _fetch_historical_orders(
                auth, "ACC123", self.SINCE, max_retries=3, rate_limit_delay=0.25
            )

        assert [c.args[0] for c in mock_sleep.call_args_list] == [0.25, 0.25]

    def test_empty_first_page_terminates(self):
        """An empty first page returns [] and makes no further calls."""
        auth = _make_auth()

        with patch(
            "tradestation.orders._api_get", return_value={"Orders": []}
        ) as mock_get:
            orders = _fetch_historical_orders(
                auth, "ACC123", self.SINCE, max_retries=3, rate_limit_delay=0
            )

        assert orders == []
        assert mock_get.call_count == 1

    def test_identical_next_token_raises_orders_error(self):
        """A NextToken identical to the cursor just used aborts pagination."""
        pages = [
            {"Orders": [_order()], "NextToken": "T1"},
            {"Orders": [_order()], "NextToken": "T1"},
        ]
        auth = _make_auth()

        with (
            patch("tradestation.orders._api_get", side_effect=pages),
            patch("tradestation.orders.time.sleep"),
            pytest.raises(OrdersError, match="identical NextToken"),
        ):
            _fetch_historical_orders(
                auth, "ACC123", self.SINCE, max_retries=3, rate_limit_delay=0
            )

    @pytest.mark.parametrize(
        "page",
        [{"Orders": [_order()]}, {"Orders": [_order()], "NextToken": None},
         {"Orders": [_order()], "NextToken": ""}],
        ids=["absent", "none", "empty"],
    )
    def test_falsy_next_token_terminates(self, page):
        """Absent/None/'' NextToken each end the traversal after one call."""
        auth = _make_auth()

        with patch("tradestation.orders._api_get", return_value=page) as mock_get:
            orders = _fetch_historical_orders(
                auth, "ACC123", self.SINCE, max_retries=3, rate_limit_delay=0
            )

        assert len(orders) == 1
        assert mock_get.call_count == 1


class TestOrdersResponseErrors:
    """Tests for the Errors[] handling inside _api_get."""

    def test_orders_with_errors_returns_orders_and_warns(self, caplog):
        """A payload with both Orders and Errors returns data + logs a warning."""
        payload = {
            "Orders": [_order()],
            "Errors": [{"Error": "PARTIAL", "Message": "some orders unavailable"}],
        }
        auth = _make_auth()

        with (
            caplog.at_level(logging.WARNING, logger="tradestation.orders"),
            patch(
                "tradestation.orders.requests.get",
                return_value=_response(payload),
            ),
        ):
            result = _api_get(auth, "https://x/orders", max_retries=3)

        assert len(result["Orders"]) == 1
        assert "some orders unavailable" in caplog.text

    def test_errors_without_usable_payload_raises_orders_error(self, caplog):
        """Account-level Errors with no Orders/NextToken/Accounts raise OrdersError."""
        payload = {"Errors": [{"Error": "FORBIDDEN", "Message": "no access"}]}
        auth = _make_auth()

        with (
            caplog.at_level(logging.WARNING, logger="tradestation.orders"),
            patch(
                "tradestation.orders.requests.get",
                return_value=_response(payload),
            ),
            pytest.raises(OrdersError, match="no usable payload"),
        ):
            _api_get(auth, "https://x/orders", max_retries=3)

        assert "no access" in caplog.text


class TestResolveAccountId:
    """Tests for resolve_account_id account selection."""

    def test_zero_accounts_raises(self):
        """An empty Accounts list raises AccountResolutionError."""
        with (
            patch("tradestation.orders._api_get", return_value={"Accounts": []}),
            pytest.raises(AccountResolutionError, match="No brokerage accounts"),
        ):
            resolve_account_id(_make_auth(), None)

    def test_multiple_accounts_without_requested_raises_with_table(self):
        """Multiple accounts + no -a → error containing the table and hint."""
        payload = {"Accounts": [_account("ACC1"), _account("ACC2")]}
        auth = _make_auth()

        with (
            patch("tradestation.orders._api_get", return_value=payload),
            pytest.raises(AccountResolutionError) as excinfo,
        ):
            resolve_account_id(auth, None)

        message = str(excinfo.value)
        assert "AccountID" in message
        assert "ACC1" in message and "ACC2" in message
        assert "-a/--account" in message

    def test_requested_id_not_present_raises_with_table(self):
        """A -a value not among the accounts → error listing available accounts."""
        payload = {"Accounts": [_account("ACC1"), _account("ACC2")]}

        with (
            patch("tradestation.orders._api_get", return_value=payload),
            pytest.raises(AccountResolutionError) as excinfo,
        ):
            resolve_account_id(_make_auth(), "NOPE")

        message = str(excinfo.value)
        assert "'NOPE'" in message
        assert "ACC1" in message and "ACC2" in message

    def test_exactly_one_account_returns_its_id(self):
        """A single account auto-selects without -a."""
        payload = {"Accounts": [_account("ACC1")]}

        with patch("tradestation.orders._api_get", return_value=payload):
            assert resolve_account_id(_make_auth(), None) == "ACC1"

    def test_requested_match_on_multiple_returns_it(self):
        """A valid -a value on a multi-account user returns that ID."""
        payload = {"Accounts": [_account("ACC1"), _account("ACC2")]}

        with patch("tradestation.orders._api_get", return_value=payload):
            assert resolve_account_id(_make_auth(), "ACC2") == "ACC2"

    def test_transport_error_propagates_unwrapped(self):
        """An OrdersError from the API call propagates, not AccountResolutionError."""
        with (
            patch(
                "tradestation.orders._api_get",
                side_effect=OrdersError("boom"),
            ),
            pytest.raises(OrdersError, match="boom"),
        ):
            resolve_account_id(_make_auth(), None)


class TestOrderNormalization:
    """Tests for _orders_to_dataframe and schema persistence through _merge_orders."""

    def test_canonical_29_columns_in_fixed_order(self):
        """A fully-documented order produces exactly the 29 canonical columns."""
        df = _orders_to_dataframe([_order()])
        assert list(df.columns) == ORDER_COLUMNS
        assert len(df.columns) == 29

    def test_missing_fields_produce_null_columns(self):
        """Fields absent from the payload are present-but-null columns."""
        sparse = {"OrderID": "ORD-9", "AccountID": "ACC123",
                  "OpenedDateTime": "2024-01-15T14:30:00Z"}
        df = _orders_to_dataframe([sparse])
        assert list(df.columns) == ORDER_COLUMNS
        assert pd.isna(df.loc[0, "LimitPrice"])
        assert pd.isna(df.loc[0, "Legs"])

    def test_scalar_fields_have_string_dtype(self):
        """Scalar fields are stored as pandas string dtype."""
        df = _orders_to_dataframe([_order()])
        assert isinstance(df["Status"].dtype, pd.StringDtype)
        assert df.loc[0, "Status"] == "FLL"

    def test_iso_strings_become_tz_naive_utc_datetime(self):
        """ISO timestamps parse to tz-naive UTC datetime64[ns]."""
        df = _orders_to_dataframe([_order()])
        assert str(df["OpenedDateTime"].dtype) == "datetime64[ns]"
        assert df.loc[0, "ClosedDateTime"] == pd.Timestamp("2024-01-15 15:00:00")
        assert df.loc[0, "OpenedDateTime"].tz is None

    def test_datetime_prefers_closed_datetime(self):
        """datetime is the ClosedDateTime for closed orders."""
        df = _orders_to_dataframe([_order()])
        assert df.loc[0, "datetime"] == pd.Timestamp("2024-01-15 15:00:00")

    def test_datetime_falls_back_to_opened_datetime(self):
        """datetime is the OpenedDateTime when the order is still open."""
        df = _orders_to_dataframe([_order(ClosedDateTime=None, Status="OPN")])
        assert df.loc[0, "datetime"] == pd.Timestamp("2024-01-15 14:30:00")
        assert pd.isna(df.loc[0, "ClosedDateTime"])

    def test_malformed_timestamp_becomes_nat_with_warning(self, caplog):
        """An unparseable timestamp → NaT + warning naming the OrderID; row kept."""
        order = _order(ClosedDateTime="not-a-date")
        with caplog.at_level(logging.WARNING, logger="tradestation.orders"):
            df = _orders_to_dataframe([order])
        assert len(df) == 1
        assert pd.isna(df.loc[0, "ClosedDateTime"])
        assert "ORD-1" in caplog.text
        assert "ClosedDateTime" in caplog.text

    def test_nested_fields_serialize_to_compact_sorted_json(self):
        """Legs/TrailingStop dicts/lists become compact sorted-key JSON strings."""
        order = _order(TrailingStop={"Amount": "5", "Type": "Percent"})
        df = _orders_to_dataframe([order])
        expected_legs = json.dumps(
            [{"Symbol": "@ESH24", "Quantity": "2", "BuyOrSell": "Buy"}],
            sort_keys=True, separators=(",", ":"),
        )
        assert df.loc[0, "Legs"] == expected_legs
        assert df.loc[0, "TrailingStop"] == '{"Amount":"5","Type":"Percent"}'

    def test_absent_nested_fields_stay_null(self):
        """Nested fields absent from the payload remain pd.NA, not 'null' text."""
        sparse = _order()
        del sparse["Legs"]
        df = _orders_to_dataframe([sparse])
        assert pd.isna(df.loc[0, "Legs"])
        assert pd.isna(df.loc[0, "TrailingStop"])

    def test_extra_fields_appended_after_canonical_in_sorted_order(self):
        """Undocumented top-level fields append sorted after the canonical 29."""
        order = _order(ZebraField="z", AlphaField="a")
        df = _orders_to_dataframe([order])
        assert list(df.columns) == ORDER_COLUMNS + ["AlphaField", "ZebraField"]

    def test_dict_extra_serializes_to_json_scalar_extra_stays_string(self):
        """A dict extra becomes compact JSON; a scalar extra is a string column."""
        order = _order(ExtraDict={"b": 2, "a": 1}, ExtraScalar="x")
        df = _orders_to_dataframe([order])
        assert df.loc[0, "ExtraDict"] == '{"a":1,"b":2}'
        assert isinstance(df["ExtraScalar"].dtype, pd.StringDtype)
        assert df.loc[0, "ExtraScalar"] == "x"

    def test_extra_column_persists_as_null_after_merge(self):
        """An extra seen in an earlier frame stays a null column when absent later."""
        frame_with = _orders_to_dataframe([_order(ExtraField="v")])
        frame_without = _orders_to_dataframe(
            [_order(OrderID="ORD-2", ClosedDateTime="2024-01-16T15:00:00Z")]
        )
        merged = _merge_orders(
            _empty_orders_dataframe(), [frame_with, frame_without]
        )
        assert "ExtraField" in merged.columns
        row2 = merged[merged["OrderID"] == "ORD-2"].iloc[0]
        assert pd.isna(row2["ExtraField"])


def _config(**overrides):
    """Build a minimal DownloadConfig for run_orders (0 delay, 3 retries)."""
    kwargs = {
        "client_id": "cid",
        "client_secret": "secret",
        "refresh_token": "token",
        "rate_limit_delay": 0,
        "max_retries": 3,
    }
    kwargs.update(overrides)
    return DownloadConfig(**kwargs)


def _open_order(**overrides):
    """Build a still-open order dict (no ClosedDateTime)."""
    overrides.setdefault("ClosedDateTime", None)
    overrides.setdefault("Status", "OPN")
    overrides.setdefault("StatusDescription", "Open")
    return _order(**overrides)


def _utcnow():
    """Naive UTC 'now', matching orders.py's convention."""
    return datetime.now(UTC).replace(tzinfo=None)


class TestFetchPlanning:
    """Tests for _build_fetch_plan's §5 decision table (fixed today/now)."""

    TODAY = date(2024, 6, 15)
    NOW = datetime(2024, 6, 15, 12, 0, 0)
    EARLIEST = TODAY - timedelta(days=729)

    def _plan(self, start, end, coverage=None, cached_open_ids=(),
              include_open=False):
        return _build_fetch_plan(
            start, end, self.TODAY, self.NOW, coverage,
            list(cached_open_ids), include_open=include_open,
        )

    def _coverage(self, since=None, through=None, completed_at=None):
        return Coverage(
            since=since or self.EARLIEST,
            through=through or self.NOW,
            completed_at=completed_at or self.NOW,
        )

    def test_no_coverage_since_is_r0(self):
        """No coverage → historical traversal starts at requested R0."""
        plan = self._plan(date(2024, 1, 1), date(2024, 1, 31))
        assert plan.historical_since == date(2024, 1, 1)

    def test_no_coverage_clamps_to_retention(self):
        """requested_start < today−729d → since clamped to earliest_supported."""
        plan = self._plan(date(2000, 1, 1), date(2024, 1, 31))
        assert plan.historical_since == self.EARLIEST

    def test_backfill_when_r0_precedes_covered_since(self):
        """R0 < coverage.since → since = R0 (backfill reason)."""
        coverage = self._coverage(since=date(2024, 3, 1))
        plan = self._plan(date(2024, 1, 1), date(2024, 6, 1), coverage)
        assert plan.historical_since == date(2024, 1, 1)
        assert "backfill" in plan.reason

    def test_refresh_since_is_through_minus_overlap(self):
        """Covered base, top to extend → since = CT.date() − 2d."""
        coverage = self._coverage(
            through=datetime(2024, 6, 10, 9, 30),
            completed_at=self.NOW - timedelta(minutes=10),  # stale → refresh
        )
        plan = self._plan(date(2024, 1, 1), date(2024, 6, 15), coverage)
        assert plan.historical_since == date(2024, 6, 8)

    def test_refresh_since_clamped_to_earliest(self):
        """CT.date()−2d before retention boundary → clamped to earliest."""
        coverage = self._coverage(
            through=datetime(2022, 1, 1),
            completed_at=self.NOW - timedelta(minutes=10),
        )
        plan = self._plan(self.EARLIEST, date(2024, 6, 15), coverage)
        assert plan.historical_since == self.EARLIEST

    def test_zero_call_past_only_fully_covered(self):
        """Past-only request fully covered with no cached opens → all off."""
        coverage = self._coverage(
            since=date(2024, 1, 1),
            through=datetime(2024, 6, 10),
        )
        plan = self._plan(date(2024, 2, 1), date(2024, 5, 31), coverage)
        assert plan.historical_since is None
        assert plan.fetch_current_orders is False
        assert plan.reconcile_open_ids == []

    def test_cached_opens_negate_past_only_zero_call(self):
        """Cached open IDs defeat the past-only zero-call rule."""
        coverage = self._coverage(
            since=date(2024, 1, 1),
            through=datetime(2024, 6, 10),
        )
        plan = self._plan(
            date(2024, 2, 1), date(2024, 5, 31), coverage,
            cached_open_ids=["ORD-OPEN"],
        )
        assert plan.historical_since is not None
        assert plan.reconcile_open_ids == ["ORD-OPEN"]

    def test_zero_call_today_spanning_fresh_coverage(self):
        """Today-spanning request, completed_at within 5min → all off."""
        coverage = self._coverage(
            since=date(2024, 1, 1),
            completed_at=self.NOW - timedelta(minutes=4),
        )
        plan = self._plan(
            date(2024, 2, 1), self.TODAY, coverage,
            cached_open_ids=["ORD-OPEN"],  # opens do NOT negate this rule
        )
        assert plan.historical_since is None
        assert plan.fetch_current_orders is False
        assert plan.reconcile_open_ids == []

    def test_stale_coverage_today_spanning_fetches(self):
        """completed_at >5min ago on today-spanning request → refresh path."""
        coverage = self._coverage(
            since=date(2024, 1, 1),
            completed_at=self.NOW - timedelta(minutes=6),
        )
        plan = self._plan(date(2024, 2, 1), self.TODAY, coverage)
        assert plan.historical_since is not None
        assert plan.fetch_current_orders is True

    def test_default_past_only_non_zero_call_does_not_fetch_current(self):
        """Default mode + past-only span on a non-zero-call path → no /orders."""
        coverage = self._coverage(
            since=date(2024, 1, 1),
            through=datetime(2024, 4, 30),  # CT before R1x → refresh path
        )
        plan = self._plan(date(2024, 2, 1), date(2024, 5, 31), coverage)
        assert plan.historical_since is not None
        assert plan.fetch_current_orders is False

    def test_default_today_spanning_no_coverage_fetches_current(self):
        """Default mode + today-spanning with absent coverage → /orders."""
        plan = self._plan(date(2024, 2, 1), self.TODAY)
        assert plan.fetch_current_orders is True

    @pytest.mark.parametrize("with_coverage", [False, True],
                             ids=["empty_cache", "fresh_coverage"])
    def test_include_open_past_only_always_fetches_current(self, with_coverage):
        """include_open=True → /orders fetched even on a past-only span."""
        coverage = (
            self._coverage(since=date(2024, 1, 1),
                           through=datetime(2024, 6, 10))
            if with_coverage else None
        )
        plan = self._plan(
            date(2024, 2, 1), date(2024, 5, 31), coverage,
            include_open=True,
        )
        assert plan.fetch_current_orders is True

    def test_include_open_fresh_coverage_fetches_current(self):
        """include_open=True → /orders even when coverage is fresh."""
        coverage = self._coverage(
            since=date(2024, 1, 1),
            completed_at=self.NOW - timedelta(minutes=1),
        )
        plan = self._plan(
            date(2024, 2, 1), self.TODAY, coverage, include_open=True,
        )
        assert plan.fetch_current_orders is True


class TestCoveragePersistence:
    """Tests for _load_coverage/_save_coverage handling of bad metadata."""

    def test_wrong_account_id_returns_none_with_warning(self, tmp_path, caplog):
        """Coverage written for another account is ignored (+ warning)."""
        coverage = Coverage(
            since=date(2024, 1, 1),
            through=datetime(2024, 6, 1, 12, 0),
            completed_at=datetime(2024, 6, 1, 12, 0),
        )
        _save_coverage(tmp_path, coverage, "OTHER")

        with caplog.at_level(logging.WARNING, logger="tradestation.orders"):
            assert _load_coverage(tmp_path, "ACC123") is None
        assert "coverage" in caplog.text.lower()

    def test_corrupt_json_returns_none(self, tmp_path, caplog):
        """Unreadable coverage.json → None (+ warning), not an exception."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "coverage.json").write_text("{not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="tradestation.orders"):
            assert _load_coverage(tmp_path, "ACC123") is None

    def test_round_trip_preserves_fields(self, tmp_path):
        """Saved coverage loads back with identical since/through/completed."""
        coverage = Coverage(
            since=date(2024, 1, 1),
            through=datetime(2024, 6, 1, 12, 30, 45),
            completed_at=datetime(2024, 6, 1, 12, 30, 46),
        )
        _save_coverage(tmp_path, coverage, "ACC123")

        loaded = _load_coverage(tmp_path, "ACC123")
        assert loaded == coverage


class TestIncludeOpenZeroCall:
    """include_open defeats the default-mode zero-call path for /orders."""

    def _seed_fresh_coverage(self, cache_dir, account_id):
        now = _utcnow()
        _save_coverage(
            cache_dir,
            Coverage(since=date(2020, 1, 1), through=now, completed_at=now),
            account_id,
        )

    def test_fresh_today_spanning_default_makes_no_calls(
        self, tmp_path, monkeypatch
    ):
        """Fresh coverage + today-spanning + default → zero _api_get calls."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        _save_cache(cache_dir, _empty_orders_dataframe())
        self._seed_fresh_coverage(cache_dir, "ACC123")

        with (
            patch("tradestation.orders._api_get") as mock_get,
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today(),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        mock_get.assert_not_called()
        assert (tmp_path / "out.csv").exists()

    def test_include_open_still_calls_orders_endpoint(
        self, tmp_path, monkeypatch
    ):
        """Same run with include_open=True → at least one /orders call."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        _save_cache(cache_dir, _empty_orders_dataframe())
        self._seed_fresh_coverage(cache_dir, "ACC123")

        with (
            patch(
                "tradestation.orders._api_get",
                return_value={"Orders": []},
            ) as mock_get,
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today(),
                full=False, include_open=True,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        assert mock_get.call_count >= 1
        urls = [c.args[1] for c in mock_get.call_args_list]
        assert any(u.endswith("/orders") for u in urls)


class TestIncludeOpenMergeAndCsv:
    """End-to-end include_open behavior through run_orders."""

    def _fake_get(self, orders_by_suffix):
        """Return an _api_get side effect dispatching on URL suffix."""

        def fake(_auth, url, _params=None, **_kwargs):
            for suffix, orders in orders_by_suffix.items():
                if url.endswith(suffix):
                    return {"Orders": orders}
            raise AssertionError(f"unexpected URL {url}")

        return fake

    def test_default_mode_orders_fetch_drops_opens_before_merge(
        self, tmp_path, monkeypatch
    ):
        """Default-mode /orders rows with null ClosedDateTime never enter cache."""
        monkeypatch.chdir(tmp_path)
        open_order = _open_order(OrderID="ORD-OPEN")
        closed_order = _order(OrderID="ORD-CLOSED")
        fake = self._fake_get({
            "/historicalorders": [closed_order],
            "/orders": [open_order, _order(OrderID="ORD-TODAY")],
        })

        with (
            patch("tradestation.orders._api_get", side_effect=fake),
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today(),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        cache = _load_cache(_account_cache_dir("ACC123"))
        assert "ORD-OPEN" not in cache["OrderID"].tolist()
        assert "ORD-CLOSED" in cache["OrderID"].tolist()
        assert "ORD-TODAY" in cache["OrderID"].tolist()

    def test_include_open_keeps_opens_in_cache_and_csv(
        self, tmp_path, monkeypatch
    ):
        """include_open run caches opens; CSV datetime = OpenedDateTime."""
        monkeypatch.chdir(tmp_path)
        open_order = _open_order(
            OrderID="ORD-OPEN", OpenedDateTime="2024-01-15T14:30:00Z",
        )
        fake = self._fake_get({
            "/historicalorders": [],
            "/orders": [open_order],
        })

        with (
            patch("tradestation.orders._api_get", side_effect=fake),
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today(),
                full=False, include_open=True,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        cache = _load_cache(_account_cache_dir("ACC123"))
        assert "ORD-OPEN" in cache["OrderID"].tolist()

        csv = pd.read_csv(tmp_path / "out.csv")
        row = csv[csv["OrderID"] == "ORD-OPEN"].iloc[0]
        assert row["datetime"].startswith("2024-01-15T14:30:00")

    def test_include_open_past_only_span_keeps_in_range_open(
        self, tmp_path, monkeypatch
    ):
        """An order opened in-range on a past-only span, still open, is kept."""
        monkeypatch.chdir(tmp_path)
        yesterday = date.today() - timedelta(days=1)
        opened = f"{yesterday.isoformat()}T14:30:00Z"
        fake = self._fake_get({
            "/historicalorders": [],
            "/orders": [_open_order(OrderID="ORD-OPEN",
                                    OpenedDateTime=opened)],
        })

        with (
            patch("tradestation.orders._api_get", side_effect=fake),
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                yesterday - timedelta(days=10), yesterday,
                full=False, include_open=True,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        csv = pd.read_csv(tmp_path / "out.csv")
        assert "ORD-OPEN" in csv["OrderID"].tolist()

    def test_default_csv_excludes_opens_cached_by_earlier_run(
        self, tmp_path, monkeypatch
    ):
        """A later default-mode CSV drops opens an include_open run cached."""
        monkeypatch.chdir(tmp_path)
        closed = _order(OrderID="ORD-CLOSED")
        open_order = _open_order(OrderID="ORD-OPEN")
        fake = self._fake_get({
            "/historicalorders": [closed],
            "/orders": [open_order],
        })
        output = tmp_path / "out.csv"

        with (
            patch("tradestation.orders._api_get", side_effect=fake),
            patch("tradestation.orders.time.sleep"),
        ):
            run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today(),
                full=False, include_open=True, output_path=output,
            )
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today(),
                full=False, include_open=False, output_path=output,
            )

        assert rc == 0
        csv = pd.read_csv(output)
        assert "ORD-OPEN" not in csv["OrderID"].tolist()
        assert "ORD-CLOSED" in csv["OrderID"].tolist()


class TestOrderMerge:
    """Tests for _merge_orders precedence, dedupe, and ordering."""

    def test_fetched_closed_beats_cached_open(self):
        """A fetched closed row replaces a cached open row (same OrderID)."""
        cached = _orders_to_dataframe([_open_order()])
        fetched = _orders_to_dataframe([_order()])
        merged = _merge_orders(cached, [fetched])
        assert len(merged) == 1
        assert merged.loc[0, "ClosedDateTime"] == pd.Timestamp(
            "2024-01-15 15:00:00")

    def test_fetched_open_does_not_clobber_cached_closed(self):
        """A fetched open row never overwrites a cached closed row."""
        cached = _orders_to_dataframe([_order()])
        fetched = _orders_to_dataframe([_open_order()])
        merged = _merge_orders(cached, [fetched])
        assert len(merged) == 1
        assert merged.loc[0, "ClosedDateTime"] == pd.Timestamp(
            "2024-01-15 15:00:00")

    def test_same_order_id_across_endpoints_deduped(self):
        """The same OrderID fetched from two endpoints collapses to one row."""
        cached = _empty_orders_dataframe()
        frame1 = _orders_to_dataframe([_order(FilledPrice="1")])
        frame2 = _orders_to_dataframe([_order(FilledPrice="2")])
        merged = _merge_orders(cached, [frame1, frame2])
        assert len(merged) == 1
        assert merged.loc[0, "FilledPrice"] == "2"  # later frame wins

    def test_sorted_by_datetime_then_order_id(self):
        """Ties on datetime are broken by OrderID; output is ascending."""
        orders = [
            _order(OrderID="B", ClosedDateTime="2024-01-15T15:00:00Z"),
            _order(OrderID="A", ClosedDateTime="2024-01-15T15:00:00Z"),
            _order(OrderID="C", ClosedDateTime="2024-01-14T15:00:00Z"),
        ]
        merged = _merge_orders(
            _empty_orders_dataframe(), [_orders_to_dataframe(orders)]
        )
        assert merged["OrderID"].tolist() == ["C", "A", "B"]

    def test_nat_datetime_rows_sort_last(self):
        """Rows whose datetime coerced to NaT are placed last."""
        orders = [
            _order(OrderID="GOOD", ClosedDateTime="2024-01-15T15:00:00Z"),
            _order(OrderID="BAD", ClosedDateTime="not-a-date",
                   OpenedDateTime=None),
        ]
        merged = _merge_orders(
            _empty_orders_dataframe(), [_orders_to_dataframe(orders)]
        )
        assert merged["OrderID"].tolist() == ["GOOD", "BAD"]
        assert pd.isna(merged.loc[1, "datetime"])

    def test_missing_order_id_rows_kept_with_warning(self, caplog):
        """Null-OrderID rows are kept (+ warning); exact dups dedupe last."""
        no_id = _order()
        del no_id["OrderID"]
        dup = dict(no_id)
        distinct = dict(no_id, LimitPrice="9999")
        with caplog.at_level(logging.WARNING, logger="tradestation.orders"):
            merged = _merge_orders(
                _empty_orders_dataframe(),
                [_orders_to_dataframe([no_id, dup, distinct])],
            )
        assert len(merged) == 2
        assert "null OrderID" in caplog.text


class TestOpenOrderReconciliation:
    """Tests for by-ID reconciliation of cached open orders in run_orders."""

    def _seed(self, cache_dir, orders, since=date(2020, 1, 1)):
        _save_cache(cache_dir, _orders_to_dataframe(orders))
        past = _utcnow() - timedelta(days=10)
        _save_coverage(
            cache_dir,
            Coverage(since=since, through=past, completed_at=past),
            "ACC123",
        )

    def _calls_for_suffix(self, mock_get, suffix):
        return [c for c in mock_get.call_args_list if c.args[1].endswith(suffix)]

    def test_cached_open_now_closed_updates_row(self, tmp_path, monkeypatch):
        """A cached open returned closed by the by-ID fetch is updated."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        opened = date.today() - timedelta(days=100)  # within retention
        self._seed(cache_dir, [_open_order(
            OrderID="ORD-1",
            OpenedDateTime=f"{opened.isoformat()}T14:30:00Z",
        )])

        def fake(_auth, url, _params=None, **_kwargs):
            if "historicalorders/ORD-1" in url:
                return {"Orders": [_order(OrderID="ORD-1")]}
            return {"Orders": []}

        with (
            patch("tradestation.orders._api_get", side_effect=fake) as mock_get,
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        by_id = self._calls_for_suffix(mock_get, "ORD-1")
        assert len(by_id) == 1
        assert by_id[0].args[2] == {"since": opened.isoformat()}
        cache = _load_cache(cache_dir)
        assert cache.loc[0, "ClosedDateTime"] == pd.Timestamp(
            "2024-01-15 15:00:00")

    def test_still_open_keeps_cached_row(self, tmp_path, monkeypatch):
        """A by-ID response that omits the open ID keeps the cached row."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        self._seed(cache_dir, [_open_order(OrderID="ORD-1")])

        with (
            patch(
                "tradestation.orders._api_get",
                return_value={"Orders": []},
            ),
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        cache = _load_cache(cache_dir)
        assert "ORD-1" in cache["OrderID"].tolist()

    def test_51_opens_batch_50_plus_1(self, tmp_path, monkeypatch):
        """51 cached opens produce two by-ID calls (50 then 1)."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        opens = [
            _open_order(OrderID=f"ORD-{i:03d}",
                        OpenedDateTime="2024-01-15T14:30:00Z")
            for i in range(51)
        ]
        self._seed(cache_dir, opens)

        with (
            patch(
                "tradestation.orders._api_get",
                return_value={"Orders": []},
            ) as mock_get,
            patch("tradestation.orders.time.sleep"),
        ):
            run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        by_id_urls = [
            c.args[1] for c in mock_get.call_args_list
            if "/historicalorders/ORD-" in c.args[1]
        ]
        assert len(by_id_urls) == 2
        assert len(by_id_urls[0].split("/")[-1].split(",")) == 50
        assert len(by_id_urls[1].split("/")[-1].split(",")) == 1

    def test_pre_retention_open_clamps_since_and_warns(
        self, tmp_path, monkeypatch, caplog
    ):
        """An open order opened before retention → clamped since + warning."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        self._seed(
            cache_dir,
            [_open_order(OrderID="ORD-OLD",
                         OpenedDateTime="2020-01-01T00:00:00Z")],
        )
        earliest = date.today() - timedelta(days=729)

        with (
            caplog.at_level(logging.WARNING, logger="tradestation.orders"),
            patch(
                "tradestation.orders._api_get",
                return_value={"Orders": []},
            ) as mock_get,
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        by_id = self._calls_for_suffix(mock_get, "ORD-OLD")
        assert by_id[0].args[2] == {"since": earliest.isoformat()}
        assert "before" in caplog.text


class TestFullOrdersDownload:
    """--full wipes the whole cache dir before refetching."""

    def test_full_removes_preseeded_rows(self, tmp_path, monkeypatch):
        """Pre-seeded cache rows are gone after a --full run."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        _save_cache(
            cache_dir,
            _orders_to_dataframe([_order(OrderID="ORD-SEED")]),
        )
        now = _utcnow()
        _save_coverage(
            cache_dir,
            Coverage(since=date(2020, 1, 1), through=now, completed_at=now),
            "ACC123",
        )

        with (
            patch(
                "tradestation.orders._api_get",
                return_value={"Orders": [_order(OrderID="ORD-NEW")]},
            ),
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=True, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        assert rc == 0
        cache = _load_cache(cache_dir)
        assert "ORD-SEED" not in cache["OrderID"].tolist()
        assert "ORD-NEW" in cache["OrderID"].tolist()


class TestInterruptedDownload:
    """KeyboardInterrupt mid-pagination: keep merged pages, no coverage/CSV."""

    def test_interrupt_after_page1_persists_rows_no_coverage_no_csv(
        self, tmp_path, monkeypatch
    ):
        """Page 1 stays in parquet; coverage.json and CSV are not written."""
        monkeypatch.chdir(tmp_path)
        pages = [
            {"Orders": [_order(OrderID="ORD-P1")], "NextToken": "T1"},
            KeyboardInterrupt(),
        ]

        with (
            patch("tradestation.orders._api_get", side_effect=pages),
            patch("tradestation.orders.time.sleep"),
            pytest.raises(KeyboardInterrupt),
        ):
            run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )

        cache_dir = _account_cache_dir("ACC123")
        cache = _load_cache(cache_dir)
        assert cache["OrderID"].tolist() == ["ORD-P1"]
        assert not (cache_dir / "coverage.json").exists()
        assert not (tmp_path / "out.csv").exists()


class TestOrdersCsvExport:
    """Tests for _write_orders_csv bounds, schema, ordering, atomicity."""

    START = date(2024, 1, 15)
    END = date(2024, 1, 16)

    def _write(self, orders, path, include_open=False, start=None, end=None):
        df = _orders_to_dataframe(orders)
        return _write_orders_csv(
            df, start or self.START, end or self.END, path,
            include_open=include_open,
        )

    def test_bounds_are_start_inclusive_end_plus_1d_exclusive(self, tmp_path):
        """Order at start 00:00 included; at end+1d 00:00 excluded."""
        orders = [
            _order(OrderID="AT-START",
                   ClosedDateTime="2024-01-15T00:00:00Z"),
            _order(OrderID="AT-EXCL",
                   ClosedDateTime="2024-01-17T00:00:00Z"),
            _order(OrderID="AT-END",
                   ClosedDateTime="2024-01-16T23:59:59Z"),
        ]
        out = tmp_path / "o.csv"
        n = self._write(orders, out)
        assert n == 2
        ids = pd.read_csv(out)["OrderID"].tolist()
        assert ids == ["AT-START", "AT-END"]

    def test_empty_result_writes_canonical_29_headers(self, tmp_path):
        """Zero matching rows still produces a 29-column header row."""
        out = tmp_path / "o.csv"
        n = self._write([], out)
        assert n == 0
        header = out.read_text(encoding="utf-8").splitlines()[0]
        assert header.split(",") == ORDER_COLUMNS

    def test_empty_result_with_extras_writes_extra_headers(self, tmp_path):
        """Evolved extra columns appear in the header after the canonical 29."""
        df = _orders_to_dataframe([_order(Zeta="z")])
        df = df.iloc[0:0]  # same schema, no rows
        out = tmp_path / "o.csv"
        n = _write_orders_csv(
            df, self.START, self.END, out, include_open=False
        )
        assert n == 0
        header = out.read_text(encoding="utf-8").splitlines()[0]
        assert header.split(",") == ORDER_COLUMNS + ["Zeta"]

    def test_sort_is_deterministic_regardless_of_input_order(self, tmp_path):
        """Two differently-ordered inputs produce byte-identical CSVs."""
        orders = [
            _order(OrderID="B", ClosedDateTime="2024-01-15T15:00:00Z"),
            _order(OrderID="A", ClosedDateTime="2024-01-15T15:00:00Z"),
            _order(OrderID="C", ClosedDateTime="2024-01-15T10:00:00Z"),
        ]
        out1, out2 = tmp_path / "a.csv", tmp_path / "b.csv"
        self._write(orders, out1)
        self._write(list(reversed(orders)), out2)
        assert out1.read_bytes() == out2.read_bytes()
        ids = pd.read_csv(out1)["OrderID"].tolist()
        assert ids == ["C", "A", "B"]

    def test_overwrites_existing_output(self, tmp_path):
        """A pre-existing output file is replaced."""
        out = tmp_path / "o.csv"
        out.write_text("STALE\n", encoding="utf-8")
        self._write([_order()], out)
        assert "STALE" not in out.read_text(encoding="utf-8")

    def test_nested_json_cell_round_trips(self, tmp_path):
        """A Legs JSON cell parses back via json.loads."""
        out = tmp_path / "o.csv"
        self._write([_order()], out)
        row = pd.read_csv(out).iloc[0]
        parsed = json.loads(row["Legs"])
        assert parsed == [
            {"Symbol": "@ESH24", "Quantity": "2", "BuyOrSell": "Buy"}
        ]

    def test_failed_to_csv_removes_tmp_and_keeps_existing_output(
        self, tmp_path
    ):
        """A mid-write failure removes the .tmp and leaves old output intact."""
        out = tmp_path / "o.csv"
        out.write_text("ORIGINAL\n", encoding="utf-8")

        with (
            patch.object(
                pd.DataFrame, "to_csv", side_effect=OSError("disk full")
            ),
            pytest.raises(OSError, match="disk full"),
        ):
            _write_orders_csv(
                _orders_to_dataframe([_order()]),
                self.START, self.END, out, include_open=False,
            )

        assert out.read_text(encoding="utf-8") == "ORIGINAL\n"
        assert not (tmp_path / "o.csv.tmp").exists()

    def test_returns_row_count(self, tmp_path):
        """The return value is the number of exported rows."""
        n = self._write([_order(), _order(OrderID="ORD-2")],
                        tmp_path / "o.csv")
        assert n == 2


class TestFetchHistoricalOrderIds:
    """Direct tests for the unpaginated by-ID endpoint."""

    def test_single_call_with_comma_ids_and_since(self):
        """One call to /historicalorders/{ids} with only a since param."""
        auth = _make_auth()

        with patch(
            "tradestation.orders._api_get",
            return_value={"Orders": [_order()]},
        ) as mock_get:
            orders = _fetch_historical_order_ids(
                auth, "ACC123", ["A", "B"], date(2024, 1, 1),
                max_retries=3, rate_limit_delay=0,
            )

        assert len(orders) == 1
        assert mock_get.call_count == 1
        call = mock_get.call_args_list[0]
        assert call.args[1] == (
            f"{BASE_URL}/brokerage/accounts/ACC123/historicalorders/A,B"
        )
        assert call.args[2] == {"since": "2024-01-01"}


class TestRunOrdersFailures:
    """Run-level failure paths: error → return 1 with no CSV; corrupt cache."""

    def test_api_failure_during_fetch_returns_1_and_writes_no_csv(
        self, tmp_path, monkeypatch
    ):
        """An OrdersError mid-fetch → rc 1 and the output CSV never appears."""
        monkeypatch.chdir(tmp_path)
        output = tmp_path / "out.csv"

        with (
            patch(
                "tradestation.orders._api_get",
                side_effect=OrdersError("API failure"),
            ),
            patch("tradestation.orders.time.sleep"),
        ):
            rc = run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=output,
            )

        assert rc == 1
        assert not output.exists()

    def test_corrupt_parquet_cache_raises_orders_error_suggesting_full(
        self, tmp_path, monkeypatch
    ):
        """An unreadable orders.parquet → OrdersError advising --full."""
        monkeypatch.chdir(tmp_path)
        cache_dir = _account_cache_dir("ACC123")
        cache_dir.mkdir(parents=True)
        (cache_dir / "orders.parquet").write_bytes(b"not a parquet file")

        with pytest.raises(OrdersError, match="--full"):
            run_orders(
                _config(), _make_auth(), "ACC123",
                date(2024, 1, 1), date.today() - timedelta(days=1),
                full=False, include_open=False,
                output_path=tmp_path / "out.csv",
            )
