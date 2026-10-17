"""Tests for the --rollcheck feature."""

import argparse
import logging
from datetime import UTC, date, datetime, timedelta
from unittest.mock import ANY, Mock, patch

from tradestation.cli import create_download_parser, run_download
from tradestation.models import DownloadConfig
from tradestation.rollcheck import (
    _fmt_ratio,
    bar_for_day,
    check_symbol,
    describe_contract,
    fetch_contract_chain,
    last_closed_bar,
    resolve_roots,
    run_rollcheck,
)


def _make_config(**overrides):
    """Build a DownloadConfig with sensible defaults."""
    defaults = {
        "client_id": "client_id",
        "client_secret": "client_secret",
        "refresh_token": "refresh_token",
        "symbols": ["@ES"],
        "rate_limit_delay": 0,
    }
    defaults.update(overrides)
    return DownloadConfig(**defaults)


def _response(json_data, status_code=200):
    """Build a fake requests.Response."""
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def _root_of(api_symbol):
    """Derive a root the way the symbol-details endpoint would."""
    return api_symbol.lstrip("@").split("=", 1)[0]


def _contract(name, expiry):
    """Build a contract-chain search result item."""
    return {
        "Name": name,
        "ExpirationDate": expiry,
        "FutureType": "Electronic",
        "Category": "Future",
        "Exchange": "CME",
        "Description": name,
    }


def _bar(timestamp, volume, oi, status="Closed"):
    """Build a daily bar as returned by the barcharts endpoint."""
    return {
        "TimeStamp": timestamp,
        "TotalVolume": str(volume),
        "OpenInterest": str(oi),
        "BarStatus": status,
        "IsRealtime": False,
    }


def _quote(symbol, fnd=None):
    """Build a quote snapshot as returned by the v3 quotes endpoint."""
    quote = {"Symbol": symbol}
    if fnd is not None:
        quote["FirstNoticeDate"] = fnd
    return quote


def _make_get(chains=None, bars=None, quotes=None):
    """Build a requests.get replacement routing by URL."""
    chains = chains or {}
    bars = bars or {}
    quotes = quotes or {}

    def fake_get(url, **_kwargs):
        if "/marketdata/symbols/" in url:
            names = url.split("/marketdata/symbols/")[1].split(",")
            return _response(
                {
                    "Symbols": [{"Symbol": s, "Root": _root_of(s)} for s in names],
                    "Errors": [],
                }
            )
        if "/marketdata/quotes/" in url:
            names = url.split("/marketdata/quotes/")[1].split(",")
            return _response(
                {"Quotes": [quotes.get(n, {"Symbol": n}) for n in names]}
            )
        if "/symbols/search/" in url:
            root = url.split("R=")[1].split("&")[0]
            return _response(chains.get(root, []))
        if "/barcharts/" in url:
            contract = url.split("/barcharts/")[1]
            return _response({"Bars": bars.get(contract, [])})
        raise AssertionError(f"unexpected URL: {url}")

    return fake_get


def _token_post(*_args, **_kwargs):
    """Fake OAuth2 token refresh."""
    return _response({"access_token": "token", "expires_in": 3600})


def _expiry(days_ahead):
    """An expiration date N days in the future, in the ASP.NET /Date(ms)/ format
    returned by the v2 symbol-search endpoint."""
    dt = datetime.combine(date.today() + timedelta(days=days_ahead), datetime.min.time(), UTC)
    return f"/Date({int(dt.timestamp() * 1000)})/"


def _dt(days_ahead):
    """A UTC datetime N days in the future, as returned by fetch_contract_chain."""
    return datetime.combine(date.today() + timedelta(days=days_ahead), datetime.min.time(), UTC)


def _table_cells(out, symbol):
    """Return the stripped cells of the result-table row for a symbol."""
    line = next(line for line in out.splitlines() if line.startswith(symbol))
    return [cell.strip() for cell in line.split("|")]


def _expiry_iso(days_ahead):
    """An expiration date N days in the future, in ISO 8601 format."""
    return (date.today() + timedelta(days=days_ahead)).strftime("%Y-%m-%dT00:00:00Z")


def _es_chain():
    return [
        _contract("ESZ25", _expiry(30)),
        _contract("ESH26", _expiry(120)),
        _contract("ESM26", _expiry(210)),
    ]


def _run(capsys, chains=None, bars=None, quotes=None, config=None):
    """Run run_rollcheck with mocked HTTP and return (exit_code, output)."""
    config = config or _make_config()
    with (
        patch("requests.post", side_effect=_token_post),
        patch("requests.get", side_effect=_make_get(chains, bars, quotes)),
    ):
        code = run_rollcheck(config)
    return code, capsys.readouterr().out


class TestContractChain:
    """Contract-chain filtering and sorting."""

    def test_filters_continuous_and_sorts_by_expiry(self):
        auth = Mock()
        items = [
            _contract("ESM26", _expiry(210)),
            _contract("@ES", _expiry(30)),
            _contract("ESZ25", _expiry(30)),
            _contract("ES=11INC", _expiry(30)),
            _contract("ESH26", _expiry(120)),
            {"Name": "ESNOEXP", "ExpirationDate": None},
        ]
        with patch("requests.get", side_effect=_make_get(chains={"ES": items})):
            chain = fetch_contract_chain(auth, "ES")
        assert [name for name, _ in chain] == ["ESZ25", "ESH26", "ESM26"]

    def test_expired_contracts_are_skipped(self):
        auth = Mock()
        items = [
            _contract("ESZ20", "2020-12-18T00:00:00Z"),  # expired
            _contract("ESZ25", _expiry(30)),
            _contract("ESH26", _expiry(120)),
        ]
        with patch("requests.get", side_effect=_make_get(chains={"ES": items})):
            chain = fetch_contract_chain(auth, "ES")
        assert [name for name, _ in chain] == ["ESZ25", "ESH26"]

    def test_iso_expiration_dates_still_parse(self):
        auth = Mock()
        items = [
            _contract("ESZ25", _expiry_iso(30)),
            _contract("ESH26", _expiry_iso(120)),
        ]
        with patch("requests.get", side_effect=_make_get(chains={"ES": items})):
            chain = fetch_contract_chain(auth, "ES")
        assert [name for name, _ in chain] == ["ESZ25", "ESH26"]

    def test_dotted_session_variants_excluded(self):
        auth = Mock()
        items = [
            _contract("ESZ26", _expiry(30)),
            _contract("ESZ26.D", _expiry(30)),
            _contract("ESH27", _expiry(120)),
        ]
        with patch("requests.get", side_effect=_make_get(chains={"ES": items})):
            chain = fetch_contract_chain(auth, "ES")
        assert [name for name, _ in chain] == ["ESZ26", "ESH27"]


class TestResolveRoots:
    """Root resolution derives from the symbol string; the symbol-details
    endpoint is only a fallback for symbols that don't reduce locally."""

    def test_derives_roots_without_api(self):
        with patch("tradestation.rollcheck.fetch_symbol_details") as mock_fetch:
            roots = resolve_roots(
                Mock(), ["@ES", "MNG", "1OZ", "MNGV26", "@MNG=11ORC"]
            )
        assert roots == {
            "@ES": "ES",
            "MNG": "MNG",
            "1OZ": "1OZ",
            "MNGV26": "MNG",
            "@MNG=11ORC": "MNG",
        }
        mock_fetch.assert_not_called()

    def test_api_fallback_only_for_underived_symbols(self):
        with patch(
            "tradestation.rollcheck.fetch_symbol_details",
            return_value=[{"Symbol": "@V26", "Root": "V"}],
        ) as mock_fetch:
            roots = resolve_roots(Mock(), ["@ES", "@V26"])
        mock_fetch.assert_called_once_with(ANY, ["@V26"])
        assert roots == {"@ES": "ES", "@V26": "V"}

    def test_bare_roots_do_not_log_symbol_details_errors(self, caplog, capsys):
        """Regression: bare roots are not tradeable symbols; sending them to
        the symbol-details endpoint only produces misleading
        'NotFound - invalid symbol' errors for valid input."""

        def fake_get(url, **_kwargs):
            if "/marketdata/symbols/" in url:
                # The real endpoint rejects bare roots with per-symbol errors
                names = url.split("/marketdata/symbols/")[1].split(",")
                return _response(
                    {
                        "Symbols": [],
                        "Errors": [
                            {
                                "Symbol": s,
                                "Error": "NotFound",
                                "Message": "invalid symbol",
                            }
                            for s in names
                        ],
                    }
                )
            if "/marketdata/quotes/" in url:
                return _response({"Quotes": []})
            if "/symbols/search/" in url:
                root = url.split("R=")[1].split("&")[0]
                return _response(chains.get(root, []))
            if "/barcharts/" in url:
                return _response({"Bars": []})
            raise AssertionError(f"unexpected URL: {url}")

        chains = {
            "MNG": [
                _contract("MNGZ26", _expiry(30)),
                _contract("MNGH27", _expiry(120)),
            ]
        }
        with (
            patch("requests.post", side_effect=_token_post),
            patch("requests.get", side_effect=fake_get),
            caplog.at_level(logging.ERROR),
        ):
            code = run_rollcheck(_make_config(symbols=["MNG"]))
        out = capsys.readouterr().out
        assert code == 0
        assert "MNGZ26 Dec 2026" in out
        assert not caplog.records


class TestLastClosedBar:
    """Last completed trading day selection."""

    def test_ignores_non_closed_final_bar(self):
        bars = [
            _bar("2025-12-01T00:00:00Z", 100, 10),
            _bar("2025-12-02T00:00:00Z", 200, 20),
            _bar("2025-12-03T00:00:00Z", 999, 99, status="Open"),
        ]
        bar = last_closed_bar(bars)
        assert bar["TimeStamp"] == "2025-12-02T00:00:00Z"

    def test_bar_for_day_matches_by_date(self):
        bars = [_bar("2025-12-02T08:30:00Z", 200, 20)]
        from datetime import datetime

        ts = datetime.fromisoformat("2025-12-02T00:00:00+00:00")
        assert bar_for_day(bars, ts) is bars[0]

    def test_bar_for_day_matches_exact_timestamp(self):
        bars = [
            _bar("2025-12-02T08:30:00Z", 200, 20),
            _bar("2025-12-03T00:00:00Z", 300, 30),
        ]
        from datetime import datetime

        ts = datetime.fromisoformat("2025-12-03T00:00:00+00:00")
        assert bar_for_day(bars, ts) is bars[1]

    def test_bar_for_day_ignores_non_closed_bar(self):
        bars = [_bar("2025-12-02T00:00:00Z", 200, 20, status="Open")]
        from datetime import datetime

        ts = datetime.fromisoformat("2025-12-02T00:00:00+00:00")
        assert bar_for_day(bars, ts) is None


class TestRolloverCriterion:
    """YES/no rollover decision."""

    def _run_es(self, capsys, cur_oi, cur_vol, next_oi, next_vol):
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", cur_vol, cur_oi)],
            "ESH26": [_bar("2025-12-05T00:00:00Z", next_vol, next_oi)],
        }
        return _run(capsys, chains={"ES": _es_chain()}, bars=bars)

    def test_yes_on_oi_only(self, capsys):
        code, out = self._run_es(capsys, 1000, 5000, 1500, 100)
        assert code == 0
        assert "ROLL" in out

    def test_action_is_roll_to_next_on_yes(self, capsys):
        _, out = self._run_es(capsys, 1000, 5000, 1500, 100)
        assert _table_cells(out, "@ES")[-1] == "ROLL -> ESH26"

    def test_action_is_keep_current_on_no(self, capsys):
        _, out = self._run_es(capsys, 5000, 9000, 1000, 100)
        assert _table_cells(out, "@ES")[-1] == "keep ESZ25"

    def test_yes_on_vol_only(self, capsys):
        code, out = self._run_es(capsys, 5000, 100, 1000, 9000)
        assert "ROLL" in out

    def test_no_when_neither(self, capsys):
        code, out = self._run_es(capsys, 5000, 9000, 1000, 100)
        assert "keep" in out
        assert "ROLL" not in out

    def test_percent_formatting(self, capsys):
        # next OI 1046 / current 1000 = 104.6%
        _, out = self._run_es(capsys, 1000, 5000, 1046, 100)
        assert "104.6%" in out

    def test_inf_ratio_when_current_zero(self, capsys):
        code, out = self._run_es(capsys, 0, 0, 500, 0)
        assert "inf" in out
        assert "ROLL" in out

    def test_ratio_helper(self):
        assert _fmt_ratio(1046, 1000) == "104.6%"
        assert _fmt_ratio(None, 1000) == "n/a"
        assert _fmt_ratio(5, 0) == "inf"
        assert _fmt_ratio(0, 0) == "n/a"


class TestExpiryTrigger:
    """Roll triggered by days-to-expiry (--roll-days, default 6)."""

    def _run_es(self, capsys, expiry_days, cur_oi, cur_vol, next_oi, next_vol, roll_days=None):
        chain = [
            _contract("ESZ25", _expiry(expiry_days)),
            _contract("ESH26", _expiry(120)),
        ]
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", cur_vol, cur_oi)],
            "ESH26": [_bar("2025-12-05T00:00:00Z", next_vol, next_oi)],
        }
        config = _make_config() if roll_days is None else _make_config(roll_days=roll_days)
        return _run(capsys, chains={"ES": chain}, bars=bars, config=config)

    def test_triggers_at_default_threshold(self, capsys):
        # 6 days to expiry, lower next OI and Vol: expiry alone forces the roll
        code, out = self._run_es(capsys, 6, 5000, 9000, 1000, 100)
        assert code == 0
        assert "ROLL" in out

    def test_no_trigger_beyond_default_threshold(self, capsys):
        code, out = self._run_es(capsys, 7, 5000, 9000, 1000, 100)
        assert "keep" in out
        assert "ROLL" not in out

    def test_custom_threshold(self, capsys):
        code, out = self._run_es(capsys, 10, 5000, 9000, 1000, 100, roll_days=10)
        assert "ROLL" in out

    def test_custom_threshold_not_reached(self, capsys):
        _, out = self._run_es(capsys, 10, 5000, 9000, 1000, 100, roll_days=9)
        assert "ROLL" not in out

    def test_triggers_even_when_next_bar_missing(self, capsys):
        # The expiry safety net must fire even when next-contract data is unavailable
        chain = [
            _contract("ESZ25", _expiry(5)),
            _contract("ESH26", _expiry(120)),
        ]
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [],
        }
        code, out = _run(capsys, chains={"ES": chain}, bars=bars)
        assert code == 0
        assert "ROLL" in out

    def test_check_symbol_sets_expiry_fields(self):
        chain = [
            ("ESZ25", _dt(6)),
            ("ESH26", _dt(120)),
        ]
        with (
            patch("tradestation.rollcheck.fetch_contract_chain", return_value=chain),
            patch("tradestation.rollcheck.fetch_quotes", return_value={}),
            patch(
                "tradestation.rollcheck.fetch_daily_bars",
                return_value=[_bar("2025-12-05T00:00:00Z", 100, 10)],
            ),
        ):
            row = check_symbol(Mock(), "@ES", "ES")
        assert row.current_expiry == date.today() + timedelta(days=6)
        assert row.current_fnd is None
        assert row.roll_date == row.current_expiry
        assert row.roll_date_kind == "ED"
        assert row.days_to_expiry == 6
        assert row.rollover == "YES"


class TestFirstNoticeDate:
    """Roll date is the earlier of expiration (ED) and first notice (FND)."""

    def _es_quotes(self, fnd_days):
        return {"ESZ25": _quote("ESZ25", fnd=_expiry_iso(fnd_days))}

    def test_earlier_fnd_shown_with_fnd_tag(self, capsys):
        # FND in 20 days, expiry in 30: the earlier date (FND) wins
        _, out = _run(capsys, chains={"ES": _es_chain()}, quotes=self._es_quotes(20))
        cells = _table_cells(out, "@ES")
        assert cells[5] == f"{(date.today() + timedelta(days=20)).isoformat()} {{FND}}"
        assert cells[6] == "20"

    def test_later_fnd_ignored_in_favor_of_expiry(self, capsys):
        # FND in 45 days, expiry in 30: expiry remains the roll date
        _, out = _run(capsys, chains={"ES": _es_chain()}, quotes=self._es_quotes(45))
        cells = _table_cells(out, "@ES")
        assert cells[5] == f"{(date.today() + timedelta(days=30)).isoformat()} {{ED}}"
        assert cells[6] == "30"

    def test_missing_fnd_falls_back_to_expiry(self, capsys):
        _, out = _run(capsys, chains={"ES": _es_chain()})
        cells = _table_cells(out, "@ES")
        assert cells[5] == f"{(date.today() + timedelta(days=30)).isoformat()} {{ED}}"

    def test_fnd_alone_triggers_roll(self, capsys):
        # FND in 4 days, expiry in 30: FND within roll-days forces ROLL
        code, out = _run(capsys, chains={"ES": _es_chain()}, quotes=self._es_quotes(4))
        assert code == 0
        assert "ROLL -> ESH26" in out

    def test_passed_fnd_skips_contract(self, capsys):
        # FND already past (negative days): the contract is dead for trading
        # and skipped entirely; the next two contracts become current/next
        code, out = _run(capsys, chains={"ES": _es_chain()}, quotes=self._es_quotes(-2))
        assert code == 0
        cells = _table_cells(out, "@ES")
        assert cells[1].startswith("ESH26")
        assert cells[2].startswith("ESM26")

    def test_quote_lookup_failure_warns_and_uses_expiry(self, capsys):
        base_get = _make_get(chains={"ES": _es_chain()})

        def fake_get(url, **kwargs):
            if "/marketdata/quotes/" in url:
                return _response({"error": "boom"}, status_code=500)
            return base_get(url, **kwargs)

        with (
            patch("requests.post", side_effect=_token_post),
            patch("requests.get", side_effect=fake_get),
        ):
            code = run_rollcheck(_make_config())
        out = capsys.readouterr().out
        assert code == 0
        assert "quote lookup failed for ES chain" in out
        assert "(!)" in out
        assert "{ED}" in out
        assert "keep ESZ25" in out

    def test_check_symbol_sets_fnd_fields(self):
        chain = [
            ("ESZ25", _dt(30)),
            ("ESH26", _dt(120)),
        ]
        quotes = {"ESZ25": {"FirstNoticeDate": _expiry_iso(20)}}
        with (
            patch("tradestation.rollcheck.fetch_contract_chain", return_value=chain),
            patch("tradestation.rollcheck.fetch_quotes", return_value=quotes),
            patch(
                "tradestation.rollcheck.fetch_daily_bars",
                return_value=[_bar("2025-12-05T00:00:00Z", 100, 10)],
            ),
        ):
            row = check_symbol(Mock(), "@ES", "ES")
        assert row.current_expiry == date.today() + timedelta(days=30)
        assert row.current_fnd == date.today() + timedelta(days=20)
        assert row.roll_date == row.current_fnd
        assert row.roll_date_kind == "FND"
        assert row.days_to_expiry == 20

    def test_column_header_is_exp_fnd(self, capsys):
        _, out = _run(capsys, chains={"ES": _es_chain()})
        assert "EXP/FND" in out


class TestDeadContractFilter:
    """Contracts whose effective roll deadline (earlier of ED/FND) has passed
    are excluded from current/next selection."""

    def _gc_chain(self):
        return [
            _contract("GCV26", _expiry(30)),
            _contract("GCX26", _expiry(60)),
            _contract("GCZ26", _expiry(90)),
        ]

    def test_dead_front_contract_skipped(self, capsys):
        quotes = {
            "GCV26": _quote("GCV26", fnd=_expiry_iso(-2)),
            "GCX26": _quote("GCX26", fnd=_expiry_iso(20)),
        }
        code, out = _run(
            capsys,
            chains={"GC": self._gc_chain()},
            quotes=quotes,
            config=_make_config(symbols=["@GC"]),
        )
        assert code == 0
        cells = _table_cells(out, "@GC")
        assert cells[1].startswith("GCX26")
        assert cells[2].startswith("GCZ26")
        assert int(cells[6]) > 0

    def test_multiple_consecutive_dead_contracts_skipped(self, capsys):
        chain = [
            _contract("GCV26", _expiry(30)),
            _contract("GCX26", _expiry(60)),
            _contract("GCZ26", _expiry(90)),
            _contract("GCG27", _expiry(150)),
        ]
        quotes = {
            "GCV26": _quote("GCV26", fnd=_expiry_iso(-5)),
            "GCX26": _quote("GCX26", fnd=_expiry_iso(-1)),
        }
        code, out = _run(
            capsys,
            chains={"GC": chain},
            quotes=quotes,
            config=_make_config(symbols=["@GC"]),
        )
        assert code == 0
        cells = _table_cells(out, "@GC")
        assert cells[1].startswith("GCZ26")
        assert cells[2].startswith("GCG27")

    def test_fewer_than_two_alive_warns_with_counts(self, capsys):
        chain = self._gc_chain()
        quotes = {
            "GCV26": _quote("GCV26", fnd=_expiry_iso(-2)),
            "GCZ26": _quote("GCZ26", fnd=_expiry_iso(-1)),
        }
        code, out = _run(
            capsys,
            chains={"GC": chain},
            quotes=quotes,
            config=_make_config(symbols=["@GC"]),
        )
        assert code == 0
        assert "fewer than 2 tradable contracts" in out
        assert "1 alive of 3" in out
        cells = _table_cells(out, "@GC")
        assert cells[1].startswith("GCX26")
        assert cells[2] == "n/a"

    def test_missing_fnd_keeps_contract_alive_by_expiry(self, capsys):
        # No FND for any contract (cash-settled style): selection by expiry only
        code, out = _run(
            capsys,
            chains={"GC": self._gc_chain()},
            config=_make_config(symbols=["@GC"]),
        )
        assert code == 0
        cells = _table_cells(out, "@GC")
        assert cells[1].startswith("GCV26")
        assert cells[2].startswith("GCX26")


class TestDescribeContract:
    """Contract month/year descriptions."""

    def test_parses_month_code_from_symbol(self):
        assert describe_contract("ESZ25", None) == "Dec 2025"
        assert describe_contract("ESH26", None) == "Mar 2026"
        assert describe_contract("CLF26", None) == "Jan 2026"

    def test_parses_month_code_when_root_contains_digits(self):
        # Roots may contain digits: 1OZ (1-oz gold), M2K (micro Russell), 6E (euro FX)
        assert describe_contract("1OZZ26", None) == "Dec 2026"
        assert describe_contract("M2KZ26", None) == "Dec 2026"
        assert describe_contract("6EH27", None) == "Mar 2027"

    def test_symbol_month_wins_over_expiry(self):
        # CLF26 is the Jan 2026 contract even though it expires in late Dec 2025
        assert describe_contract("CLF26", date(2025, 12, 22)) == "Jan 2026"
        # 1OZZ26 is the Dec 2026 contract even though it expires in Nov 2026
        assert describe_contract("1OZZ26", date(2026, 11, 20)) == "Dec 2026"

    def test_falls_back_to_expiry(self):
        assert describe_contract("ODD", date(2026, 11, 15)) == "Nov 2026"

    def test_na_when_unknown(self):
        assert describe_contract(None, None) == "n/a"


class TestOutputLayout:
    """Header context and table structure of the rollcheck report."""

    def test_header_states_rule_and_default_threshold(self, capsys):
        _, out = _run(capsys, chains={"ES": _es_chain()})
        assert "Rollover check -" in out
        assert "Roll rule:" in out
        assert "within 6 days" in out

    def test_header_threshold_follows_roll_days(self, capsys):
        _, out = _run(capsys, chains={"ES": _es_chain()}, config=_make_config(roll_days=10))
        assert "within 10 days" in out

    def test_contracts_described_with_month_year(self, capsys):
        _, out = _run(capsys, chains={"ES": _es_chain()})
        assert "ESZ25 Dec 2025" in out
        assert "ESH26 Mar 2026" in out

    def test_contracts_described_with_month_year_for_digit_roots(self, capsys):
        # 1OZZ26 must render "Dec 2026" (contract month), not the Nov expiry month
        chains = {
            "1OZ": [
                _contract("1OZZ26", _expiry(30)),
                _contract("1OZG27", _expiry(120)),
            ]
        }
        _, out = _run(capsys, chains=chains, config=_make_config(symbols=["@1OZ"]))
        assert "1OZZ26 Dec 2026" in out
        assert "1OZG27 Feb 2027" in out

    def test_expiry_and_days_columns_populated(self, capsys):
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [_bar("2025-12-05T00:00:00Z", 100, 10)],
        }
        _, out = _run(capsys, chains={"ES": _es_chain()}, bars=bars)
        cells = _table_cells(out, "@ES")
        assert cells[5] == f"{(date.today() + timedelta(days=30)).isoformat()} {{ED}}"
        assert cells[6] == "30"

    def test_headline_count_singular(self, capsys):
        _, out = _run(capsys, chains={"ES": _es_chain()})
        assert "Roll recommended: 0 of 1 symbol" in out

    def test_headline_count_plural(self, capsys):
        chains = {
            "ES": _es_chain(),
            "NQ": [
                _contract("NQZ25", _expiry(30)),
                _contract("NQH26", _expiry(120)),
            ],
        }
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [_bar("2025-12-05T00:00:00Z", 100, 10)],
            "NQZ25": [_bar("2025-12-05T00:00:00Z", 4000, 900)],
            "NQH26": [_bar("2025-12-05T00:00:00Z", 90, 9)],
        }
        _, out = _run(
            capsys,
            chains=chains,
            bars=bars,
            config=_make_config(symbols=["@ES", "@NQ"]),
        )
        assert "Roll recommended: 0 of 2 symbols" in out


class TestMissingAndErrorData:
    """Missing data and error handling."""

    def test_missing_next_bar_shows_na_and_no(self, capsys):
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [_bar("2025-12-01T00:00:00Z", 100, 10)],
        }
        code, out = _run(capsys, chains={"ES": _es_chain()}, bars=bars)
        assert code == 0
        assert "n/a" in out
        assert "ROLL" not in out
        assert "(!)" in out
        assert "Warnings" in out
        assert "no bar for" in out

    def test_missing_current_data_marks_warning(self, capsys):
        bars = {
            "ESZ25": [],
            "ESH26": [_bar("2025-12-05T00:00:00Z", 9000, 5000)],
        }
        code, out = _run(capsys, chains={"ES": _es_chain()}, bars=bars)
        assert "(!)" in out
        assert "Warnings" in out
        assert "no completed daily bar" in out

    def test_chain_lookup_failure_produces_warning_row(self, capsys):
        def fake_get(url, **_kwargs):
            if "/marketdata/symbols/" in url:
                names = url.split("/marketdata/symbols/")[1].split(",")
                return _response({"Symbols": [{"Symbol": s, "Root": _root_of(s)} for s in names]})
            return _response({"error": "boom"}, status_code=500)

        with (
            patch("requests.post", side_effect=_token_post),
            patch("requests.get", side_effect=fake_get),
        ):
            code = run_rollcheck(_make_config())
        out = capsys.readouterr().out
        assert code == 1
        assert "(!)" in out
        assert "n/a" in out
        assert "Warnings" in out
        assert "contract chain lookup failed" in out

    def test_recommended_is_na_on_unresolved_row(self, capsys):
        def fake_get(url, **_kwargs):
            if "/marketdata/symbols/" in url:
                names = url.split("/marketdata/symbols/")[1].split(",")
                return _response({"Symbols": [{"Symbol": s, "Root": _root_of(s)} for s in names]})
            return _response({"error": "boom"}, status_code=500)

        with (
            patch("requests.post", side_effect=_token_post),
            patch("requests.get", side_effect=fake_get),
        ):
            run_rollcheck(_make_config())
        out = capsys.readouterr().out
        assert _table_cells(out, "@ES")[-1] == "n/a"

    def test_fewer_than_two_contracts_warns(self, capsys):
        code, out = _run(capsys, chains={"ES": [_contract("ESZ25", _expiry(30))]})
        assert "(!)" in out
        assert "fewer than 2 contracts" in out

    def test_context_line_printed(self, capsys):
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [_bar("2025-12-05T00:00:00Z", 100, 10)],
        }
        _, out = _run(capsys, chains={"ES": _es_chain()}, bars=bars)
        assert "Data day: 2025-12-05" in out

    def test_context_line_varies_when_days_differ(self, capsys):
        chains = {
            "ES": _es_chain(),
            "NQ": [
                _contract("NQZ25", _expiry(30)),
                _contract("NQH26", _expiry(120)),
            ],
        }
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [_bar("2025-12-05T00:00:00Z", 100, 10)],
            "NQZ25": [_bar("2025-12-04T00:00:00Z", 4000, 900)],
            "NQH26": [_bar("2025-12-04T00:00:00Z", 90, 9)],
        }
        _, out = _run(
            capsys,
            chains=chains,
            bars=bars,
            config=_make_config(symbols=["@ES", "@NQ"]),
        )
        assert "Data day: varies by symbol" in out


class TestCliDispatch:
    """CLI wiring for --rollcheck."""

    def _args(self, **overrides):
        defaults = {
            "config": "config.yaml",
            "list_symbols": False,
            "list_categories": False,
            "metadata": False,
            "export_csv": False,
            "export_ts_csv": False,
            "rollcheck": False,
            "roll_days": 6,
            "symbols": None,
            "category": None,
            "all_categories": False,
            "full": False,
            "storage_format": None,
            "compression": None,
            "parquet": False,
            "csv_output": False,
            "ts_csv_output": False,
            "no_datetime_index": False,
            "workers": 4,
            "verbose": False,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_rollcheck_flag_parsed(self):
        args = create_download_parser().parse_args(["--rollcheck"])
        assert args.rollcheck is True

    def test_roll_days_default_and_override(self):
        parser = create_download_parser()
        assert parser.parse_args(["--rollcheck"]).roll_days == 6
        assert parser.parse_args(["--rollcheck", "--roll-days", "10"]).roll_days == 10

    def test_rollcheck_dispatches_and_exits(self):
        config = _make_config(symbols=["@ES"])
        args = self._args(rollcheck=True)
        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.rollcheck.run_rollcheck", return_value=0) as mock_rc,
            patch("tradestation.cli.TradeStationDownloader") as mock_dl,
        ):
            result = run_download(args)
        assert result == 0
        assert config.roll_days == 6
        mock_rc.assert_called_once_with(config)
        mock_dl.assert_not_called()

    def test_rollcheck_passes_roll_days_override(self):
        config = _make_config(symbols=["@ES"])
        args = self._args(rollcheck=True, roll_days=12)
        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.rollcheck.run_rollcheck", return_value=0) as mock_rc,
        ):
            result = run_download(args)
        assert result == 0
        assert config.roll_days == 12
        mock_rc.assert_called_once_with(config)

    def test_rollcheck_uses_symbol_overrides(self):
        config = _make_config(symbols=[])
        args = self._args(rollcheck=True, symbols=["@NQ", "@CL"])
        with (
            patch("tradestation.cli.load_config", return_value=config),
            patch("tradestation.rollcheck.run_rollcheck", return_value=0) as mock_rc,
        ):
            result = run_download(args)
        assert result == 0
        assert config.symbols == ["@NQ", "@CL"]
        mock_rc.assert_called_once_with(config)


class TestCheckSymbol:
    """Direct check_symbol tests."""

    def test_missing_next_contract_data_returns_no(self):
        auth = Mock()
        bars = {
            "ESZ25": [_bar("2025-12-05T00:00:00Z", 5000, 1000)],
            "ESH26": [],
        }
        with (
            patch(
                "tradestation.rollcheck.fetch_contract_chain",
                return_value=[("ESZ25", _dt(30)), ("ESH26", _dt(120))],
            ),
            patch("tradestation.rollcheck.fetch_quotes", return_value={}),
            patch(
                "tradestation.rollcheck.fetch_daily_bars",
                side_effect=lambda _auth, contract: bars[contract],
            ),
        ):
            row = check_symbol(auth, "@ES", "ES")
        assert row.rollover == "no"
        assert row.next_oi is None
        assert row.next_vol is None
