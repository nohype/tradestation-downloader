"""Tests for tradestation.tdpro (orders CSV -> TradingDiary Pro converter)."""

import csv
import json
import logging
import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from tradestation import fx, tdpro
from tradestation.cli import create_tdpro_parser, main_tdpro, run_tdpro_mode
from tradestation.tdpro import (
    TDPRO_COLUMNS,
    TdproError,
    convert_orders_csv,
)


def _leg(**overrides):
    """Build a Legs-array leg dict matching the real orders CSV shape."""
    leg = {
        "AssetType": "FUTURE",
        "BuyOrSell": "Buy",
        "ExecQuantity": "1",
        "ExecutionPrice": "6779",
        "ExpirationDate": "2026-03-20T00:00:00Z",
        "Symbol": "MESH26",
        "Underlying": "MES",
    }
    leg.update(overrides)
    return leg


def _order_row(**overrides):
    """Build an orders-CSV row dict with the columns the converter needs."""
    row = {
        "datetime": "2026-02-06T00:46:00.000000",
        "OrderID": "1001",
        "Status": "FLL",
        "Legs": json.dumps([_leg()]),
        "FilledPrice": "6779",
        "CommissionFee": "0.5",
        "Currency": "USD",
        "ConversionRate": "1",
    }
    row.update(overrides)
    return row


def _write_orders_csv(path: Path, rows: list[dict],
                      columns: list[str] | None = None) -> Path:
    """Write orders-CSV row dicts to path via pandas and return the path."""
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    return path


def _read_output(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read an output CSV, returning (header, data rows)."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _convert(tmp_path: Path, rows: list[dict], multipliers=None, name="orders.csv"):
    """Write rows to a CSV in tmp_path, convert it, and return the output path."""
    input_path = _write_orders_csv(tmp_path / name, rows)
    output_path = tmp_path / "out.csv"
    convert_orders_csv(input_path, output_path, multipliers or {})
    return output_path


class TestConvert:
    """Tests for tdpro.convert_orders_csv row mapping."""

    def test_golden_buy_and_sell(self, tmp_path):
        """A buy and a sell fill map to the exact 17-column TD Pro rows."""
        rows = [
            _order_row(
                OrderID="1001",
                CommissionFee="0.5",
                Legs=json.dumps([_leg(BuyOrSell="Buy")]),
            ),
            _order_row(
                OrderID="1002",
                CommissionFee="1",
                Legs=json.dumps([
                    _leg(BuyOrSell="Sell", ExecQuantity="2",
                         ExecutionPrice="6780.25")
                ]),
            ),
        ]
        out = _convert(tmp_path, rows)
        header, data = _read_output(out)

        assert header == TDPRO_COLUMNS
        assert len(header) == 17
        assert len(data) == 2

        buy = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert buy["TransactionID"] == "1001-1"
        assert buy["DateTime"] == "2026-02-06 00:46:00"
        assert buy["Ticker"] == "MESH26"
        assert buy["Description"] == ""
        assert buy["Quantity"] == "1"
        assert buy["Price"] == "6779"
        assert buy["Commission"] == "0.5"
        assert buy["Asset Category"] == "Futures"
        assert buy["Multiplier"] == "5"
        assert buy["Currency"] == "USD"
        assert buy["Conversion rate"] == ""
        assert buy["Expiration Date"] == "2026-03-20"
        assert buy["Call or Put"] == ""
        assert buy["Strike"] == ""
        assert buy["Note"] == ""
        assert buy["Exchange"] == ""
        assert buy["Tag(s)"] == ""

        sell = dict(zip(TDPRO_COLUMNS, data[1], strict=True))
        assert sell["TransactionID"] == "1002-1"
        assert sell["Quantity"] == "-2"
        assert sell["Price"] == "6780.25"
        assert sell["Commission"] == "1"

    def test_non_executed_orders_produce_no_rows(self, tmp_path):
        """Orders whose legs all have ExecQuantity 0 yield zero output rows."""
        zero = _leg(ExecQuantity="0")
        rows = [
            _order_row(OrderID="o1", Status="OUT", Legs=json.dumps([zero])),
            _order_row(OrderID="o2", Status="EXP", Legs=json.dumps([zero])),
            _order_row(OrderID="o3", Status="REJ", Legs=json.dumps([zero])),
        ]
        out = _convert(tmp_path, rows)
        header, data = _read_output(out)
        assert header == TDPRO_COLUMNS
        assert data == []

    def test_non_future_leg_skipped_with_warning(self, tmp_path, caplog):
        """A leg with AssetType != FUTURE is skipped with a warning."""
        leg = _leg(AssetType="OPTION")
        rows = [
            _order_row(OrderID="opt1", Legs=json.dumps([leg])),
            _order_row(OrderID="ok"),
        ]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 1
        assert data[0][0] == "ok-1"
        assert any(
            "opt1" in r.message and "OPTION" in r.message
            for r in caplog.records
        )

    def test_unparseable_exec_quantity_skipped_with_warning(
        self, tmp_path, caplog
    ):
        """A leg with unparseable ExecQuantity warns and is not non-executed."""
        leg = _leg(ExecQuantity="N/A")
        rows = [_order_row(OrderID="bad1", Legs=json.dumps([leg]))]
        with caplog.at_level(logging.INFO, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert data == []
        assert any(
            "bad1" in r.message and "ExecQuantity" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )
        assert not any(
            "non-executed" in r.message for r in caplog.records
        )

    @pytest.mark.parametrize(
        "fee",
        ["garbage", "NaN", "Infinity", "", "1e999999999", "1e-999999999"],
    )
    def test_unusable_commission_emits_row_with_empty_cell(
        self, tmp_path, caplog, fee
    ):
        """Garbage/NaN/Infinity/empty CommissionFee still emits the fill row
        with an empty Commission cell and a warning naming the OrderID."""
        rows = [_order_row(OrderID="fee1", CommissionFee=fee)]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 1
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["TransactionID"] == "fee1-1"
        assert trade["Commission"] == ""
        assert trade["Quantity"] == "1"
        assert trade["Price"] == "6779"
        assert any(
            "fee1" in r.message and "CommissionFee" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_pathological_commission_cli_exits_0(self, tmp_path):
        """A pathological CommissionFee via the CLI still exits 0 and writes
        the row (round-3 regression: Decimal overflow used to die as
        'Unexpected error')."""
        rows = [_order_row(OrderID="big", CommissionFee="1e999999999")]
        input_path = _write_orders_csv(tmp_path / "orders.csv", rows)
        out = tmp_path / "out.csv"
        args = create_tdpro_parser().parse_args(
            [str(input_path), "-o", str(out)]
        )
        assert run_tdpro_mode(args) == 0
        _, data = _read_output(out)
        assert len(data) == 1
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Commission"] == ""

    def test_zero_commission_emits_zero_without_warning(
        self, tmp_path, caplog
    ):
        """CommissionFee '0' is a legitimate fee: cell '0', no warning."""
        rows = [_order_row(OrderID="z1", CommissionFee="0")]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Commission"] == "0"
        assert not any("CommissionFee" in r.message for r in caplog.records)

    def test_whitespace_padded_commission_accepted(self, tmp_path, caplog):
        """CommissionFee ' 2.25 ' parses to '2.25' with no warning."""
        rows = [_order_row(OrderID="ws1", CommissionFee=" 2.25 ")]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Commission"] == "2.25"
        assert not any("CommissionFee" in r.message for r in caplog.records)

    def test_empty_order_id_digest_disambiguates_by_commission(
        self, tmp_path
    ):
        """Two empty-OrderID rows differing only in CommissionFee get
        different TransactionIDs (round-3 regression: digest used to ignore
        CommissionFee, so TD Pro dedup would drop one)."""
        rows = [
            _order_row(OrderID="", CommissionFee="0.5"),
            _order_row(OrderID="", CommissionFee="1.0"),
        ]
        out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 2
        assert data[0][0].startswith("ts-") and data[1][0].startswith("ts-")
        assert data[0][0] != data[1][0]

    def test_output_path_is_directory_raises_tdpro_error(self, tmp_path):
        """-o naming an existing directory surfaces a TdproError, not a raw
        OSError."""
        input_path = _write_orders_csv(tmp_path / "orders.csv", [_order_row()])
        out_dir = tmp_path / "existing_dir"
        out_dir.mkdir()
        with pytest.raises(TdproError, match="Could not write"):
            convert_orders_csv(input_path, out_dir, {})
        assert not (tmp_path / "existing_dir.tmp").exists()

    def test_cli_output_write_failure_exits_1(self, tmp_path):
        """An unwritable -o path makes run_tdpro_mode return 1."""
        input_path = _write_orders_csv(tmp_path / "orders.csv", [_order_row()])
        out_dir = tmp_path / "existing_dir"
        out_dir.mkdir()
        args = create_tdpro_parser().parse_args(
            [str(input_path), "-o", str(out_dir)]
        )
        assert run_tdpro_mode(args) == 1

    def test_negative_exec_quantity_warns_and_counts_bad(
        self, tmp_path, caplog
    ):
        """ExecQuantity '-5' is invalid: warning + counted bad, NOT
        non-executed."""
        leg = _leg(ExecQuantity="-5")
        rows = [_order_row(OrderID="neg1", Legs=json.dumps([leg]))]
        with caplog.at_level(logging.INFO, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert data == []
        assert any(
            "neg1" in r.message and "invalid ExecQuantity" in r.message
            and r.levelno == logging.WARNING
            for r in caplog.records
        )
        assert not any(
            "non-executed" in r.message for r in caplog.records
        )
        assert any(
            "bad legs" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_empty_order_id_gets_deterministic_ts_transaction_id(
        self, tmp_path, caplog
    ):
        """An empty OrderID yields a deterministic 'ts-' TransactionID, not
        '-1'; different orders get different IDs, same order re-converted gets
        the same ID."""
        leg_a = _leg(ExecQuantity="1", ExecutionPrice="6779")
        leg_b = _leg(ExecQuantity="2", ExecutionPrice="6780", BuyOrSell="Sell")
        rows = [
            _order_row(OrderID="", Legs=json.dumps([leg_a])),
            _order_row(OrderID="", datetime="2026-02-06T00:47:00.000000",
                       Legs=json.dumps([leg_b])),
        ]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 2
        id_a, id_b = data[0][0], data[1][0]
        assert id_a.startswith("ts-") and id_a != "-1"
        assert id_b.startswith("ts-")
        assert id_a != id_b
        assert any("OrderID" in r.message for r in caplog.records)

        # Same input re-converted → identical TransactionID (dedup still works)
        out2 = tmp_path / "out2.csv"
        convert_orders_csv(tmp_path / "orders.csv", out2, {})
        _, data2 = _read_output(out2)
        assert data2[0][0] == id_a

    def test_non_dict_leg_element_warns_and_skips(self, tmp_path, caplog):
        """A non-dict element inside a valid Legs array warns + counts bad."""
        legs = [_leg(), "not a leg", 42]
        rows = [_order_row(OrderID="mix1", Legs=json.dumps(legs))]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 1
        assert data[0][0] == "mix1-1"
        assert any(
            "mix1" in r.message and "malformed leg" in r.message
            for r in caplog.records
        )

    def test_non_future_leg_with_unknown_root_does_not_raise(self, tmp_path):
        """A non-FUTURE leg whose root is not in the multiplier map warns +
        skips; it must NOT trigger the missing-root error."""
        bad_root = _leg(AssetType="OPTION", Underlying="ZZZ", Symbol="ZZZH26")
        rows = [
            _order_row(OrderID="opt", Legs=json.dumps([bad_root])),
            _order_row(OrderID="ok"),
        ]
        out = _convert(tmp_path, rows)  # must not raise TdproError
        _, data = _read_output(out)
        assert len(data) == 1
        assert data[0][0] == "ok-1"

    @pytest.mark.parametrize(
        ("fee", "expected_cell", "warns"),
        [
            ("1000000000", "1000000000", False),   # 1e9 boundary: accepted
            ("1000000001", "", True),              # just over: rejected
        ],
    )
    def test_commission_magnitude_boundary(
        self, tmp_path, caplog, fee, expected_cell, warns
    ):
        """|fee| == 1e9 is accepted; |fee| > 1e9 leaves the cell empty
        with a warning."""
        rows = [_order_row(OrderID="b1", CommissionFee=fee)]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Commission"] == expected_cell
        assert warns == any("CommissionFee" in r.message
                            for r in caplog.records)

    def test_zero_huge_exponent_commission_formats_as_zero(
        self, tmp_path, caplog
    ):
        """CommissionFee '0e-999999999' is a legitimate zero: canonicalized so
        the cell is '0', not a million-character string, and no warning."""
        rows = [_order_row(OrderID="z2", CommissionFee="0e-999999999")]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Commission"] == "0"
        assert not any("CommissionFee" in r.message for r in caplog.records)

    def test_output_same_as_input_raises_tdpro_error(self, tmp_path):
        """-o equal to the input path raises TdproError and leaves the
        input file untouched."""
        input_path = _write_orders_csv(tmp_path / "orders.csv", [_order_row()])
        original = input_path.read_bytes()
        with pytest.raises(TdproError, match="same as the input"):
            convert_orders_csv(input_path, input_path, {})
        assert input_path.read_bytes() == original

    def test_input_matching_tmp_alias_raises_tdpro_error(self, tmp_path):
        """Input 'x.csv.tmp' with -o 'x.csv' raises TdproError and leaves the
        input untouched (round-4: the .tmp staging alias would otherwise
        truncate and rename the input away)."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv.tmp", [_order_row()]
        )
        original = input_path.read_bytes()
        with pytest.raises(TdproError, match="same as the input"):
            convert_orders_csv(input_path, tmp_path / "orders.csv", {})
        assert input_path.read_bytes() == original

    def test_unexpected_buy_or_sell_skipped_with_warning(
        self, tmp_path, caplog
    ):
        """A leg with a BuyOrSell other than Buy/Sell warns and is skipped."""
        for side in ("Buy to Cover", None):
            caplog.clear()
            leg = _leg(BuyOrSell=side)
            rows = [_order_row(OrderID="side1", Legs=json.dumps([leg]))]
            with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
                out = _convert(tmp_path, rows)
            _, data = _read_output(out)
            assert data == []
            assert any(
                "side1" in r.message and "BuyOrSell" in r.message
                for r in caplog.records
            )

    def test_commission_split_across_multi_leg_order(self, tmp_path):
        """A multi-leg order splits CommissionFee proportional to ExecQuantity."""
        legs = [
            _leg(ExecQuantity="1"),
            _leg(ExecQuantity="2", BuyOrSell="Sell"),
        ]
        rows = [_order_row(OrderID="m1", CommissionFee="3",
                           Legs=json.dumps(legs))]
        out = _convert(tmp_path, rows)
        _, data = _read_output(out)

        assert len(data) == 2
        first = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        second = dict(zip(TDPRO_COLUMNS, data[1], strict=True))
        assert first["TransactionID"] == "m1-1"
        assert second["TransactionID"] == "m1-2"
        assert first["Commission"] == "1"
        assert second["Commission"] == "2"

    def test_multiplier_falls_back_to_symbol_root(self, tmp_path):
        """A leg without Underlying resolves its multiplier via Symbol root."""
        leg = _leg(Underlying="", Symbol="MESH26")
        out = _convert(tmp_path, [_order_row(Legs=json.dumps([leg]))])
        _, data = _read_output(out)
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Multiplier"] == "5"

    def test_unknown_root_raises_tdpro_error_naming_root(self, tmp_path):
        """An executed leg with an unknown underlying root raises TdproError."""
        leg = _leg(Underlying="ZZZ", Symbol="ZZZH26")
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row(Legs=json.dumps([leg]))]
        )
        with pytest.raises(TdproError, match="ZZZ"):
            convert_orders_csv(input_path, tmp_path / "out.csv", {})

    def test_multipliers_param_supplies_unknown_root(self, tmp_path):
        """convert_orders_csv's multipliers param supplies an unknown root."""
        leg = _leg(Underlying="ES", Symbol="ESH26")
        out = _convert(
            tmp_path,
            [_order_row(Legs=json.dumps([leg]))],
            multipliers={"ES": 50},
        )
        _, data = _read_output(out)
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Multiplier"] == "50"

    def test_malformed_legs_json_skips_row_with_warning(self, tmp_path, caplog):
        """Malformed Legs JSON logs a warning and skips that row only."""
        rows = [
            _order_row(OrderID="bad", Legs="not json"),
            _order_row(OrderID="good"),
        ]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 1
        assert data[0][0] == "good-1"
        assert any("malformed Legs" in r.message for r in caplog.records)

    def test_missing_execution_price_falls_back_to_filled_price(self, tmp_path):
        """A leg without ExecutionPrice uses the row's FilledPrice."""
        leg = _leg(ExecutionPrice="")
        rows = [_order_row(FilledPrice="6781.5", Legs=json.dumps([leg]))]
        out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Price"] == "6781.5"

    def test_leg_with_no_price_at_all_skipped_with_warning(
        self, tmp_path, caplog
    ):
        """A leg lacking both ExecutionPrice and FilledPrice is skipped."""
        leg = _leg(ExecutionPrice="")
        rows = [_order_row(FilledPrice="", Legs=json.dumps([leg]))]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert data == []
        assert any("no ExecutionPrice" in r.message for r in caplog.records)

    def test_unparseable_datetime_skips_row_with_warning(
        self, tmp_path, caplog
    ):
        """An unparseable datetime logs a warning and skips the order."""
        rows = [_order_row(datetime="not-a-date"), _order_row(OrderID="ok")]
        with caplog.at_level(logging.WARNING, logger="tradestation.tdpro"):
            out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        assert len(data) == 1
        assert data[0][0] == "ok-1"
        assert any("could not parse datetime" in r.message
                   for r in caplog.records)

    def test_missing_required_column_raises_tdpro_error(self, tmp_path):
        """An input CSV missing a required column raises TdproError."""
        row = _order_row()
        del row["Legs"]
        input_path = _write_orders_csv(tmp_path / "orders.csv", [row])
        with pytest.raises(TdproError, match="Legs"):
            convert_orders_csv(input_path, tmp_path / "out.csv", {})

    def test_nonexistent_input_raises_tdpro_error(self, tmp_path):
        """A nonexistent input path raises TdproError."""
        with pytest.raises(TdproError, match="not found"):
            convert_orders_csv(
                tmp_path / "nope.csv", tmp_path / "out.csv", {}
            )

    def test_empty_input_writes_header_only_output(self, tmp_path):
        """A header-only input produces a header-only output and returns 0."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [], columns=list(_order_row().keys())
        )
        out = tmp_path / "out.csv"
        n = convert_orders_csv(input_path, out, {})
        assert n == (0, 0)
        header, data = _read_output(out)
        assert header == TDPRO_COLUMNS
        assert data == []

    def test_atomic_write_leaves_no_tmp_file(self, tmp_path):
        """A successful write leaves no *.tmp sibling behind."""
        out = _convert(tmp_path, [_order_row()])
        assert out.exists()
        assert not (tmp_path / "out.csv.tmp").exists()


class TestCli:
    """Tests for the orders-to-tdpro CLI plumbing in cli.py."""

    def test_parser_multiplier_flag_accumulates(self):
        """Repeated -m flags accumulate as (root, value) pairs."""
        args = create_tdpro_parser().parse_args(
            ["f.csv", "-m", "ES=50", "-m", "MGC=10"]
        )
        assert args.multiplier == [("ES", "50"), ("MGC", "10")]

    @pytest.mark.parametrize(
        "bad", ["MES", "MES=abc", "MES=-1", "=5", "MES=nan", "MES=inf"]
    )
    def test_parser_rejects_bad_multiplier_forms(self, bad):
        """Malformed -m values are rejected by argparse with exit code 2."""
        with pytest.raises(SystemExit) as exc_info:
            create_tdpro_parser().parse_args(["f.csv", "-m", bad])
        assert exc_info.value.code == 2

    def test_default_output_naming(self, tmp_path):
        """Without -o, output is <stem>_tdpro.csv next to the input."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row()]
        )
        args = create_tdpro_parser().parse_args([str(input_path)])
        assert run_tdpro_mode(args) == 0
        assert (tmp_path / "orders_tdpro.csv").exists()

    def test_out_flag_is_honored(self, tmp_path):
        """-o writes to the given path instead of the default."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row()]
        )
        custom = tmp_path / "custom.csv"
        args = create_tdpro_parser().parse_args(
            [str(input_path), "-o", str(custom)]
        )
        assert run_tdpro_mode(args) == 0
        assert custom.exists()
        assert not (tmp_path / "orders_tdpro.csv").exists()

    def test_cli_multiplier_flag_supplies_unknown_root(self, tmp_path):
        """The CLI -m flag plumbs an unknown root's multiplier through."""
        leg = _leg(Underlying="ES", Symbol="ESH26")
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row(Legs=json.dumps([leg]))]
        )
        out = tmp_path / "out.csv"
        args = create_tdpro_parser().parse_args(
            [str(input_path), "-o", str(out), "-m", "ES=50"]
        )
        assert run_tdpro_mode(args) == 0
        _, data = _read_output(out)
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Multiplier"] == "50"

    def test_cli_multiplier_flag_lowercase_root_overrides_builtin(
        self, tmp_path
    ):
        """-m mes=10 (lowercase) normalizes to MES and overrides the built-in."""
        input_path = _write_orders_csv(tmp_path / "orders.csv", [_order_row()])
        out = tmp_path / "out.csv"
        args = create_tdpro_parser().parse_args(
            [str(input_path), "-o", str(out), "-m", "mes=10"]
        )
        assert run_tdpro_mode(args) == 0
        _, data = _read_output(out)
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Multiplier"] == "10"

    def test_cli_unknown_root_exits_1(self, tmp_path):
        """An unknown root without -m makes run_tdpro_mode return 1."""
        leg = _leg(Underlying="ZZZ", Symbol="ZZZH26")
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row(Legs=json.dumps([leg]))]
        )
        args = create_tdpro_parser().parse_args([str(input_path)])
        assert run_tdpro_mode(args) == 1

    def test_cli_out_same_as_input_exits_1_untouched(self, tmp_path):
        """-o pointing at the input file exits 1 and leaves it untouched."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row()]
        )
        original = input_path.read_bytes()
        args = create_tdpro_parser().parse_args(
            [str(input_path), "-o", str(input_path)]
        )
        assert run_tdpro_mode(args) == 1
        assert input_path.read_bytes() == original

    def test_cli_missing_column_exits_1(self, tmp_path):
        """A missing required column makes run_tdpro_mode return 1."""
        row = _order_row()
        del row["Currency"]
        input_path = _write_orders_csv(tmp_path / "orders.csv", [row])
        args = create_tdpro_parser().parse_args([str(input_path)])
        assert run_tdpro_mode(args) == 1

    def test_cli_nonexistent_input_exits_1(self, tmp_path):
        """A nonexistent input file makes run_tdpro_mode return 1."""
        args = create_tdpro_parser().parse_args([str(tmp_path / "nope.csv")])
        assert run_tdpro_mode(args) == 1

    def test_wrote_line_printed_on_success(self, tmp_path, capsys):
        """A successful run prints 'Wrote <path> (N trades)'."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [_order_row(), _order_row(OrderID="2")]
        )
        args = create_tdpro_parser().parse_args([str(input_path)])
        assert run_tdpro_mode(args) == 0
        out = capsys.readouterr().out
        assert "Wrote" in out
        assert "2 trades from 2 orders" in out

    def test_empty_input_via_cli_exits_0(self, tmp_path, capsys):
        """A header-only input writes header-only output and exits 0."""
        input_path = _write_orders_csv(
            tmp_path / "orders.csv", [], columns=list(_order_row().keys())
        )
        args = create_tdpro_parser().parse_args([str(input_path)])
        assert run_tdpro_mode(args) == 0
        assert "0 trades from 0 orders" in capsys.readouterr().out
        header, data = _read_output(tmp_path / "orders_tdpro.csv")
        assert header == TDPRO_COLUMNS
        assert data == []

    def test_main_tdpro_dispatch(self, tmp_path):
        """main_tdpro parses argv, calls convert_orders_csv, exits 0."""
        input_path = str(tmp_path / "orders.csv")
        with (
            patch.object(sys, "argv", ["orders-to-tdpro", input_path]),
            patch(
                "tradestation.tdpro.convert_orders_csv", return_value=(3, 1)
            ) as m_convert,
            pytest.raises(SystemExit) as exc_info,
        ):
            main_tdpro()
        assert exc_info.value.code == 0
        m_convert.assert_called_once()
        assert m_convert.call_args.args[0] == Path(input_path)

    def test_main_tdpro_propagates_error_exit_code(self, tmp_path):
        """main_tdpro exits 1 when convert_orders_csv raises TdproError."""
        with (
            patch.object(
                sys, "argv", ["orders-to-tdpro", str(tmp_path / "x.csv")]
            ),
            patch(
                "tradestation.tdpro.convert_orders_csv",
                side_effect=TdproError("boom"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main_tdpro()
        assert exc_info.value.code == 1

    def test_help_renders(self, capsys):
        """--help prints usage mentioning FILE and exits 0."""
        with pytest.raises(SystemExit) as exc_info:
            create_tdpro_parser().parse_args(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "FILE" in out
        assert "TradingDiary Pro" in out


class TestDecimalFormatting:
    """Spot checks on numeric pass-through used by the converter."""

    def test_multiplier_accepts_decimal_and_int_values(self, tmp_path):
        """multipliers values given as Decimal or int serialize plainly."""
        leg = _leg(Underlying="ES", Symbol="ESH26")
        rows = [_order_row(Legs=json.dumps([leg]))]
        out = _convert(tmp_path, rows, multipliers={"ES": Decimal("2.5")})
        _, data = _read_output(out)
        assert dict(zip(TDPRO_COLUMNS, data[0], strict=True))["Multiplier"] == "2.5"


def _ecb_csv(rates: dict[date, str]) -> str:
    """Build an ECB csvdata payload (TIME_PERIOD/OBS_VALUE) from a rate map."""
    lines = ["TIME_PERIOD,OBS_VALUE"]
    lines += [f"{day.isoformat()},{value}" for day, value in rates.items()]
    return "\n".join(lines) + "\n"


def _fx_response(text: str = "", status_code: int = 200,
                 headers: dict | None = None) -> Mock:
    """Build a fake requests.Response for the ECB endpoint."""
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    return resp


@pytest.fixture
def fx_cache_dir(tmp_path, monkeypatch) -> Path:
    """Point fx.FX_CACHE_DIR at a tmp dir so tests never touch the real cache."""
    cache_dir = tmp_path / "fxcache"
    monkeypatch.setattr(fx, "FX_CACHE_DIR", cache_dir)
    return cache_dir


def _seed_fx_cache(cache_dir: Path, currency: str,
                   rates: dict[date, str],
                   windows: list[tuple[date, date]] | None = None) -> Path:
    """Write a <CCY>.json cache file in the patched FX cache dir.

    ``windows=None`` writes a legacy file (rates only, no "windows"
    key); pass a list of ``(start, end)`` pairs for the v3 windowed
    format. No ``"version"`` key is written — seeded files exercise the
    legacy (missing-version) load path.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{currency}.json"
    payload: dict = {"rates": {day.isoformat(): v for day, v in rates.items()}}
    if windows is not None:
        payload["windows"] = [
            [start.isoformat(), end.isoformat()] for start, end in windows
        ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _berlin(year: int, month: int, day: int, hour: int = 18,
            minute: int = 0) -> datetime:
    """A tz-aware Europe/Berlin datetime for freezing tdpro's clock —
    tdpro derives its ``today`` (fetch end + warning gate) from
    ``datetime.now(ZoneInfo('Europe/Berlin'))``, which ``_freeze_now``
    patches."""
    return datetime(year, month, day, hour, minute, tzinfo=fx.BERLIN)


def _freeze_now(monkeypatch, moment: datetime) -> None:
    """Patch tradestation.tdpro.datetime so now() returns ``moment``.

    ``moment`` should be tz-aware (Europe/Berlin); a subclass keeps
    ``datetime.fromisoformat`` etc. working for the rest of tdpro.
    """
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(tdpro, "datetime", _FrozenDateTime)


def _expected_rate_cell(ecb_rate: str) -> str:
    """The exact Conversion rate cell for 1/ecb_rate quantized to 8dp."""
    rate = (Decimal(1) / Decimal(ecb_rate)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_UP
    )
    return format(rate, "f")


class TestFetchEcbDaily:
    """Unit tests for fx.fetch_ecb_daily (ECB CSV fetch + parse + retry)."""

    def test_parses_ecb_csv_to_date_decimal_map(self):
        """TIME_PERIOD/OBS_VALUE rows parse to {date: Decimal}."""
        csv_text = _ecb_csv({
            date(2026, 7, 9): "1.1418",
            date(2026, 7, 10): "1.1419",
        })
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(csv_text),
        ):
            rates = fx.fetch_ecb_daily(
                "USD", date(2026, 7, 9), date(2026, 7, 10)
            )
        assert rates == {
            date(2026, 7, 9): Decimal("1.1418"),
            date(2026, 7, 10): Decimal("1.1419"),
        }

    def test_404_names_currency_as_not_ecb_quoted(self):
        """A 404 means the currency is not an ECB-quoted series: the error
        names the currency and says so, without retrying."""
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response("oops", status_code=404),
            ) as mock_get,
            pytest.raises(
                fx.FxError,
                match="currency XXX is not an ECB-quoted daily "
                      "reference currency",
            ),
        ):
            fx.fetch_ecb_daily("XXX", date(2026, 7, 1), date(2026, 7, 2))
        assert mock_get.call_count == 1

    def test_non_200_non_404_raises_generic_fx_error(self):
        """Other non-retryable statuses keep the generic message."""
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response("oops", status_code=403),
            ) as mock_get,
            pytest.raises(fx.FxError, match="403"),
        ):
            fx.fetch_ecb_daily("USD", date(2026, 7, 1), date(2026, 7, 2))
        assert mock_get.call_count == 1

    def test_429_honors_retry_after_header(self):
        """A 429 sleeps Retry-After seconds, then retries successfully."""
        responses = [
            _fx_response(status_code=429, headers={"Retry-After": "7"}),
            _fx_response(_ecb_csv({date(2026, 7, 10): "1.1419"})),
        ]
        with (
            patch("tradestation.fx.requests.get", side_effect=responses),
            patch("tradestation.fx.time.sleep") as mock_sleep,
        ):
            rates = fx.fetch_ecb_daily(
                "USD", date(2026, 7, 10), date(2026, 7, 10)
            )
        assert rates == {date(2026, 7, 10): Decimal("1.1419")}
        mock_sleep.assert_called_once_with(7)

    def test_malformed_csv_raises_fx_error(self):
        """A 200 with unparseable CSV content raises FxError."""
        bad = "TIME_PERIOD,OBS_VALUE\nnot-a-date,1.2\n"
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(bad),
            ),
            pytest.raises(fx.FxError, match="Could not parse ECB CSV"),
        ):
            fx.fetch_ecb_daily("USD", date(2026, 7, 1), date(2026, 7, 2))


class TestRateOn:
    """Unit tests for fx.rate_on (last-published-rate fallback)."""

    def test_exact_date_hit(self):
        """A date present in the series returns that day's rate."""
        rates = {
            date(2026, 7, 9): Decimal("1.10"),
            date(2026, 7, 10): Decimal("1.20"),
        }
        assert fx.rate_on(rates, date(2026, 7, 10)) == Decimal("1.20")

    def test_weekend_falls_back_to_friday(self):
        """A Sunday lookup returns the previous Friday's published rate."""
        rates = {
            date(2026, 7, 9): Decimal("1.10"),   # Thursday
            date(2026, 7, 10): Decimal("1.20"),  # Friday
        }
        sunday = date(2026, 7, 12)
        assert sunday.weekday() == 6
        assert fx.rate_on(rates, sunday) == Decimal("1.20")

    def test_no_rate_on_or_before_raises_fx_error_naming_date(self):
        """A lookup before the first published rate raises FxError."""
        rates = {date(2026, 7, 10): Decimal("1.20")}
        with pytest.raises(fx.FxError, match="2026-07-09"):
            fx.rate_on(rates, date(2026, 7, 9))


class TestRateToBase:
    """Unit tests for fx.rate_to_base (cross-rate orientation)."""

    def test_usd_to_eur_is_reciprocal_of_published_rate(self):
        """cur=USD base=EUR yields 1/ecb(USD)."""
        rates = {"USD": {date(2026, 7, 10): Decimal("1.25")}}
        assert fx.rate_to_base(
            rates, "USD", "EUR", date(2026, 7, 10)
        ) == Decimal("0.8")

    def test_usd_to_gbp_is_ecb_cross_rate(self):
        """cur=USD base=GBP yields ecb(GBP)/ecb(USD)."""
        rates = {
            "USD": {date(2026, 7, 10): Decimal("1.25")},
            "GBP": {date(2026, 7, 10): Decimal("0.80")},
        }
        assert fx.rate_to_base(
            rates, "USD", "GBP", date(2026, 7, 10)
        ) == Decimal("0.64")

    def test_precision_is_exact_decimal(self):
        """The rate keeps full Decimal precision (no float artifacts)."""
        rates = {"USD": {date(2026, 7, 10): Decimal("1.1419")}}
        result = fx.rate_to_base(rates, "USD", "EUR", date(2026, 7, 10))
        assert result == Decimal(1) / Decimal("1.1419")


class TestFxDate:
    """Unit tests for fx.fx_date (berlin vs utc calendar date)."""

    def test_berlin_converts_summer_utc_2230_to_next_day(self):
        """22:30 UTC in July (CEST, UTC+2) is 00:30 the next day in Berlin."""
        dt = datetime(2026, 7, 13, 22, 30)
        assert fx.fx_date(dt, "berlin") == date(2026, 7, 14)

    def test_utc_keeps_the_recorded_date(self):
        """utc mode takes the naive datetime's date verbatim."""
        dt = datetime(2026, 7, 13, 22, 30)
        assert fx.fx_date(dt, "utc") == date(2026, 7, 13)

    def test_berlin_winter_offset_is_plus_one_hour(self):
        """23:30 UTC in January (CET, UTC+1) is 00:30 the next day in Berlin."""
        dt = datetime(2026, 1, 15, 23, 30)
        assert fx.fx_date(dt, "berlin") == date(2026, 1, 16)
        assert fx.fx_date(dt, "utc") == date(2026, 1, 15)


class TestEnsureRates:
    """Unit tests for fx.ensure_rates (window coverage + single fetch)."""

    def test_window_covering_needed_dates_does_not_fetch(
        self, fx_cache_dir
    ):
        """A needed date inside a recorded fetch window whose range
        contains a rate <= the date is covered — requests.get is never
        called."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 6): "1.10", date(2026, 7, 10): "1.20"},
            windows=[(date(2026, 7, 6), date(2026, 7, 10))],
        )
        with patch("tradestation.fx.requests.get") as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 8)}, today=date(2026, 7, 15)
            )
        mock_get.assert_not_called()
        assert fx.rate_on(rates, date(2026, 7, 8)) == Decimal("1.10")

    def test_weekend_covered_by_window_spanning_it_does_not_fetch(
        self, fx_cache_dir
    ):
        """A needed Sunday served by Friday's rate is covered without a
        fetch when a recorded window reaches the needed date (a window
        that only reached Friday could not prove the weekend empty;
        windows end on publication dates, so Monday's pub is what proves
        the weekend)."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 10): "1.20", date(2026, 7, 13): "1.30"},
            windows=[(date(2026, 7, 6), date(2026, 7, 13))],
        )
        sunday = date(2026, 7, 12)
        assert sunday.weekday() == 6
        with patch("tradestation.fx.requests.get") as mock_get:
            rates = fx.ensure_rates(
                "USD", {sunday}, today=date(2026, 7, 12)
            )
        mock_get.assert_not_called()
        assert fx.rate_on(rates, sunday) == Decimal("1.20")

    def test_window_not_reaching_needed_date_triggers_one_fetch(
        self, fx_cache_dir
    ):
        """A needed date past every recorded window's end is uncovered:
        exactly one fetch, then the merged series serves it."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 6): "1.10"},
            windows=[(date(2026, 7, 1), date(2026, 7, 6))],
        )
        fetched = _ecb_csv({date(2026, 7, 9): "1.30"})
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )
        assert mock_get.call_count == 1
        assert rates[date(2026, 7, 9)] == Decimal("1.30")
        assert rates[date(2026, 7, 6)] == Decimal("1.10")  # cache merged
        # the recorded windows merged into one covering range
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [["2026-06-25", "2026-07-09"]]

    def test_needed_range_beyond_cache_triggers_exactly_one_fetch(
        self, fx_cache_dir
    ):
        """An uncovered needed date causes exactly one fetch over
        [min(needed) - 14d, today], then the merged series serves it."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 6): "1.10"},
            windows=[(date(2026, 7, 1), date(2026, 7, 6))],
        )
        fetched = _ecb_csv({date(2026, 7, 20): "1.30"})
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 20)}, today=date(2026, 7, 20)
            )
        assert mock_get.call_count == 1
        # fetch window: min(needed) - 14d lookback margin through today
        assert mock_get.call_args.kwargs["params"]["startPeriod"] == (
            "2026-07-06"
        )
        assert mock_get.call_args.kwargs["params"]["endPeriod"] == (
            "2026-07-20"
        )
        assert rates[date(2026, 7, 20)] == Decimal("1.30")
        assert rates[date(2026, 7, 6)] == Decimal("1.10")  # cache merged

    def test_interior_window_hole_triggers_one_fetch(
        self, fx_cache_dir
    ):
        """Hole regression: rates exist in January and March with matching
        windows, but the needed February date sits in a fetched-never hole
        between the windows — the cache must NOT silently serve January's
        stale rate; it fetches once and serves February's real rate."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {
                date(2026, 1, 5): "1.01",
                date(2026, 1, 6): "1.02",
                date(2026, 1, 7): "1.03",
                date(2026, 1, 8): "1.04",
                date(2026, 1, 9): "1.05",
                date(2026, 3, 10): "1.31",
                date(2026, 3, 11): "1.32",
                date(2026, 3, 12): "1.33",
                date(2026, 3, 13): "1.34",
            },
            windows=[
                (date(2026, 1, 5), date(2026, 1, 9)),
                (date(2026, 2, 26), date(2026, 3, 13)),
            ],
        )
        needed = date(2026, 2, 20)
        # The ECB returns every publication in the requested range, so the
        # response also re-contains March's pubs — the recorded window
        # then extends to max(fetched) and merges with the March window.
        fetched = _ecb_csv({
            date(2026, 2, 19): "1.21",
            date(2026, 2, 20): "1.22",
            # continuous publications — a >14d hole in the merged series
            # would now split the recorded window (F1)
            date(2026, 2, 24): "1.24",
            date(2026, 3, 2): "1.30",
            date(2026, 3, 10): "1.31",
            date(2026, 3, 11): "1.32",
            date(2026, 3, 12): "1.33",
            date(2026, 3, 13): "1.34",
        })
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {needed}, today=date(2026, 3, 13)
            )
        assert mock_get.call_count == 1
        # fetch anchors at min(needed) - 14d and runs through today
        assert mock_get.call_args.kwargs["params"]["startPeriod"] == (
            "2026-02-06"
        )
        assert mock_get.call_args.kwargs["params"]["endPeriod"] == (
            "2026-03-13"
        )
        assert fx.rate_on(rates, needed) == Decimal("1.22")  # not 1.05 stale
        # the new window merged with the March window; January stays put
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [
            ["2026-01-05", "2026-01-09"],
            ["2026-02-06", "2026-03-13"],
        ]

    def test_window_starting_on_needed_date_with_no_rate_is_uncovered(
        self, fx_cache_dir
    ):
        """Pre-Easter-Monday straggler: the only window STARTS on the
        needed date and holds no rate <= it (Good Friday publishes
        nothing) — the true last-published rate precedes the window, so
        coverage cannot be proven and a fetch must happen."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 4, 6): "1.50"},
            windows=[(date(2026, 4, 3), date(2026, 4, 6))],
        )
        needed = date(2026, 4, 3)  # Good Friday — no publication
        fetched = _ecb_csv({date(2026, 4, 2): "1.49"})  # Holy Thursday
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {needed}, today=date(2026, 4, 6)
            )
        assert mock_get.call_count == 1
        assert fx.rate_on(rates, needed) == Decimal("1.49")

    def test_legacy_cache_without_windows_refetches_and_upgrades(
        self, fx_cache_dir
    ):
        """A v1 cache (rates only, no "windows") keeps its rates but
        proves nothing — the needed date triggers a fetch and the file is
        rewritten with a windows key."""
        _seed_fx_cache(
            fx_cache_dir, "USD", {date(2026, 7, 6): "1.10"}
        )  # no windows
        fetched = _ecb_csv({date(2026, 7, 6): "1.10", date(2026, 7, 9): "1.30"})
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )
        assert mock_get.call_count == 1
        assert rates[date(2026, 7, 9)] == Decimal("1.30")
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [["2026-06-25", "2026-07-09"]]

    @pytest.mark.parametrize("bad_value", ["-1.5", "0", "abc", "NaN"])
    def test_garbage_rate_value_treats_whole_cache_as_corrupt(
        self, fx_cache_dir, caplog, bad_value
    ):
        """A negative, zero, non-Decimal, or non-finite rate value
        invalidates the WHOLE cache (rates + windows): warning + refetch.
        Kills silently served negative rates and DivisionByZero."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 6): "1.10", date(2026, 7, 9): bad_value},
            windows=[(date(2026, 7, 1), date(2026, 7, 15))],
        )
        fetched = _ecb_csv({date(2026, 7, 9): "1.30"})
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.fx"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ) as mock_get,
        ):
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )
        assert mock_get.call_count == 1
        assert rates[date(2026, 7, 9)] == Decimal("1.30")
        assert any("corrupt FX cache" in r.message for r in caplog.records)

    def test_corrupt_cache_warns_and_is_treated_as_empty(
        self, fx_cache_dir, caplog
    ):
        """An unparseable cache file logs a warning and is refetched."""
        fx_cache_dir.mkdir(parents=True)
        (fx_cache_dir / "USD.json").write_text("not json", encoding="utf-8")
        fetched = _ecb_csv({date(2026, 7, 9): "1.30"})
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.fx"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ) as mock_get,
        ):
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )
        assert mock_get.call_count == 1
        assert rates[date(2026, 7, 9)] == Decimal("1.30")
        assert any("corrupt FX cache" in r.message for r in caplog.records)

    @pytest.mark.usefixtures("fx_cache_dir")
    def test_empty_ecb_response_raises_fx_error(self):
        """An ECB series with no observations for an unknown currency raises
        FxError."""
        empty_csv = "TIME_PERIOD,OBS_VALUE\n"
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(empty_csv),
            ),
            pytest.raises(fx.FxError, match="no rates for XXX"),
        ):
            fx.ensure_rates(
                "XXX", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )

    def test_cache_file_written_atomically_as_readable_json(
        self, fx_cache_dir
    ):
        """After a fetch the v3 cache (version + rates + windows) is
        valid JSON and no .tmp file remains."""
        fetched = _ecb_csv({date(2026, 7, 9): "1.30"})
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ):
            fx.ensure_rates(
                "USD", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )
        path = fx_cache_dir / "USD.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {
            "version": 3,
            "rates": {"2026-07-09": "1.30"},
            "windows": [["2026-06-25", "2026-07-09"]],
        }
        assert not list(fx_cache_dir.glob("*.tmp"))

    def test_fetch_records_window_ending_at_last_returned_publication(
        self, fx_cache_dir
    ):
        """Evidence window: the fetch REQUESTS through today but the
        response only publishes through Friday — the recorded window ends
        at the newest returned publication, and the needed date beyond it
        is provisional (served Friday's rate), not an error."""
        friday = date(2026, 7, 10)
        assert friday.weekday() == 4
        monday = date(2026, 7, 13)
        fetched = _ecb_csv({friday: "1.10"})
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {monday}, today=monday
            )
        assert mock_get.call_count == 1
        # request end still reaches today (ECB may already have it)
        assert mock_get.call_args.kwargs["params"]["endPeriod"] == "2026-07-13"
        # needed Monday uncovered but provisional (a rate <= d exists)
        # → no error, served the last published rate
        assert fx.rate_on(rates, monday) == Decimal("1.10")
        # the window ends at the last RETURNED publication, not today
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [["2026-06-29", "2026-07-10"]]

    def test_rerun_after_publication_refetches_and_serves_new_rate(
        self, fx_cache_dir
    ):
        """Delayed-publication regression (self-heal): a fetch whose
        response lacks today's pub records a window ending yesterday; a
        same-day re-run after publication performs a REAL refetch and
        serves the new rate."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 14): "1.10"},
            windows=[(date(2026, 7, 1), date(2026, 7, 14))],
        )
        fetched = _ecb_csv(
            {date(2026, 7, 14): "1.10", date(2026, 7, 15): "1.20"}
        )
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 15)}, today=date(2026, 7, 15)
            )
        assert mock_get.call_count == 1  # real refetch, not a cache hit
        assert rates[date(2026, 7, 15)] == Decimal("1.20")
        # window merge extended coverage through today's publication
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [["2026-07-01", "2026-07-15"]]

        # next-day run: the same needed date is now < today and covered by
        # the extended window — zero fetches, true rate served
        with patch("tradestation.fx.requests.get") as mock_get2:
            rates2 = fx.ensure_rates(
                "USD", {date(2026, 7, 15)}, today=date(2026, 7, 16)
            )
        mock_get2.assert_not_called()
        assert fx.rate_on(rates2, date(2026, 7, 15)) == Decimal("1.20")

    def test_window_ending_on_non_rate_date_is_dropped_and_refetched(
        self, fx_cache_dir, caplog
    ):
        """F1: a window whose end is NOT itself a published rate date is
        legacy-inflated or corrupt — dropped on load (rates kept, info
        logged), so the needed date is uncovered and exactly one refetch
        rebuilds an evidence-bounded window."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 10): "1.20", date(2026, 7, 14): "1.30"},
            windows=[(date(2026, 7, 6), date(2026, 7, 15))],
        )  # Jul 15 never published — the window overclaims it
        fetched = _ecb_csv({date(2026, 7, 14): "1.30"})
        with (
            caplog.at_level(logging.INFO, logger="tradestation.fx"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ) as mock_get,
        ):
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 15)}, today=date(2026, 7, 15)
            )
        assert mock_get.call_count == 1
        assert any(
            "Dropping overclaimed window" in r.message
            for r in caplog.records
        )
        # needed date stays provisional on the last published rate
        assert fx.rate_on(rates, date(2026, 7, 15)) == Decimal("1.30")
        # the overclaimed window is gone; the new window ends on the
        # newest publication the fetch actually returned
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [["2026-07-01", "2026-07-14"]]

    def test_legacy_overclaimed_window_end_beyond_last_rate_heals(
        self, fx_cache_dir
    ):
        """F1 migration: the real USD.json shape — rates through Friday
        2026-10-16 but a window claiming through Saturday 2026-10-17 (no
        publication possible). The window is dropped and one fetch heals
        the cache into an evidence-bounded window."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 10, 15): "1.15", date(2026, 10, 16): "1.16"},
            windows=[(date(2026, 10, 2), date(2026, 10, 17))],
        )
        fetched = _ecb_csv(
            {date(2026, 10, 15): "1.15", date(2026, 10, 16): "1.16"}
        )
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 10, 16)}, today=date(2026, 10, 17)
            )
        assert mock_get.call_count == 1  # one refetch, not a silent hit
        assert fx.rate_on(rates, date(2026, 10, 16)) == Decimal("1.16")
        # honest window: fetch start (min needed - 14d) through the last
        # returned publication — never the claimed Saturday
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [["2026-10-02", "2026-10-16"]]

    @pytest.mark.usefixtures("fx_cache_dir")
    def test_uncovered_needed_date_with_no_prior_rate_raises(self):
        """A needed date that stays uncovered after the fetch AND has no
        cached rate on or before it is a REAL HOLE → FxError naming
        currency + date."""
        fetched = _ecb_csv({date(2026, 7, 15): "1.30"})  # nothing <= Jul 8
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ),
            pytest.raises(fx.FxError) as exc_info,
        ):
            fx.ensure_rates(
                "USD", {date(2026, 7, 8)}, today=date(2026, 7, 15)
            )
        assert "USD" in str(exc_info.value)
        assert "2026-07-08" in str(exc_info.value)

    def test_fetch_window_splits_at_interior_rate_gap(
        self, fx_cache_dir
    ):
        """F1 writer side: a >14d hole between consecutive merged rates
        inside the fetched span is missing data, not a publication gap —
        the recorded window splits there so the hole is never claimed,
        and a needed date inside it stays uncovered-but-provisional."""
        needed = date(2026, 8, 1)   # Saturday, inside the hole
        today = date(2026, 9, 1)
        # fetch start = needed - 14d = 2026-07-18; the response leaves a
        # Jul 20 → Aug 10 hole (21 days) no window may claim
        fetched = _ecb_csv({
            date(2026, 7, 20): "1.10",
            date(2026, 8, 10): "1.20",
            date(2026, 9, 1): "1.30",
        })
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates("USD", {needed}, today=today)
        assert mock_get.call_count == 1
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [
            ["2026-07-18", "2026-07-20"],
            ["2026-08-10", "2026-08-10"],
            ["2026-09-01", "2026-09-01"],
        ]
        # needed date sits in the unclaimed hole: uncovered but a prior
        # rate exists -> provisional, served the last published rate
        assert not fx._is_covered(needed, rates, [
            (date(2026, 7, 18), date(2026, 7, 20)),
            (date(2026, 8, 10), date(2026, 8, 10)),
            (date(2026, 9, 1), date(2026, 9, 1)),
        ])
        assert fx.rate_on(rates, needed) == Decimal("1.10")

    def test_save_resplits_windows_merged_across_a_rate_hole(
        self, fx_cache_dir
    ):
        """Seam-adjacency regression: _merge_windows joins a new segment
        to an existing window ending exactly one day before the
        segment's start — a merge that would SPAN a >14d rate hole.
        _save_cache re-splits on the rate data itself, so the file never
        records a window claiming an unfetched hole."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 1, 5): "1.05", date(2026, 1, 9): "1.06"},
            windows=[(date(2026, 1, 5), date(2026, 1, 9))],
        )
        # needed Jan 24 -> fetch start Jan 10 — one day past the old
        # window's end, so the new segment [Jan-10, Jan-26] merges with
        # [Jan-5, Jan-9] into [Jan-5, Jan-26], spanning the 15-day
        # rate hole Jan-9 -> Jan-24
        fetched = _ecb_csv({
            date(2026, 1, 24): "1.20",
            date(2026, 1, 26): "1.25",
        })
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(fetched),
        ) as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 1, 24)}, today=date(2026, 1, 30)
            )
        assert mock_get.call_count == 1
        assert fx.rate_on(rates, date(2026, 1, 24)) == Decimal("1.20")
        # the saved windows are re-split at the hole: [Jan-5, Jan-9]
        # honest, [Jan-24, Jan-26] starts at its own first rate
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [
            ["2026-01-05", "2026-01-09"],
            ["2026-01-24", "2026-01-26"],
        ]
        # and a second load keeps both windows (no drop, no refetch)
        with patch("tradestation.fx.requests.get") as mock_get2:
            fx.ensure_rates(
                "USD", {date(2026, 1, 24)}, today=date(2026, 1, 30)
            )
        mock_get2.assert_not_called()

    @pytest.mark.usefixtures("fx_cache_dir")
    def test_needed_date_in_gap_with_no_prior_rate_raises(self):
        """F1 writer side, hole case: a needed date inside the split-out
        gap with NO cached rate <= it is a real hole -> FxError."""
        fetched = _ecb_csv({
            date(2026, 8, 10): "1.20",
            date(2026, 9, 1): "1.30",
        })
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ),
            pytest.raises(fx.FxError, match="2026-08-05"),
        ):
            fx.ensure_rates(
                "USD", {date(2026, 8, 5)}, today=date(2026, 9, 1)
            )

    def test_window_spanning_interior_rate_gap_is_dropped_and_refetched(
        self, fx_cache_dir, caplog
    ):
        """F1 loader side: a window spanning a >14d hole between
        consecutive cached rates claims missing data — dropped on load
        (rates kept, info logged), so the needed date refetches once.
        An honest window alongside it is untouched."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {
                date(2026, 6, 5): "1.05",
                date(2026, 7, 10): "1.10",
                date(2026, 8, 3): "1.20",
            },
            windows=[
                (date(2026, 6, 1), date(2026, 6, 5)),   # honest
                (date(2026, 7, 6), date(2026, 8, 3)),   # Jul10→Aug3 = 24d
            ],
        )
        needed = date(2026, 7, 21)  # inside the dishonest window's hole
        fetched = _ecb_csv({
            date(2026, 7, 21): "1.15",
            date(2026, 8, 3): "1.20",
        })
        with (
            caplog.at_level(logging.INFO, logger="tradestation.fx"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ) as mock_get,
        ):
            rates = fx.ensure_rates(
                "USD", {needed}, today=date(2026, 8, 3)
            )
        assert mock_get.call_count == 1
        assert any(
            "interior rate gap" in r.message for r in caplog.records
        )
        assert fx.rate_on(rates, needed) == Decimal("1.15")
        # the honest window survives untouched; the refetch records one
        # window (Jul 21 → Aug 3 is 13 days — under the gap limit)
        data = json.loads((fx_cache_dir / "USD.json").read_text())
        assert data["windows"] == [
            ["2026-06-01", "2026-06-05"],
            ["2026-07-07", "2026-08-03"],
        ]

    def test_cache_without_version_key_still_loads(self, fx_cache_dir):
        """F3: a legacy cache with no "version" key loads under the same
        rules — covered needed dates hit the cache without a fetch."""
        _seed_fx_cache(
            fx_cache_dir, "USD",
            {date(2026, 7, 6): "1.10", date(2026, 7, 10): "1.20"},
            windows=[(date(2026, 7, 6), date(2026, 7, 10))],
        )  # no "version" key — legacy
        with patch("tradestation.fx.requests.get") as mock_get:
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 8)}, today=date(2026, 7, 15)
            )
        mock_get.assert_not_called()
        assert fx.rate_on(rates, date(2026, 7, 8)) == Decimal("1.10")

    def test_window_pair_with_wrong_length_is_whole_cache_corrupt(
        self, fx_cache_dir, caplog
    ):
        """A windows entry that is not a 2-element pair is malformed like
        any other corrupt shape: warning + whole cache treated as empty
        + refetch."""
        cache_dir = fx_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / "USD.json").write_text(json.dumps({
            "version": 3,
            "rates": {"2026-07-09": "1.30"},
            "windows": [["2026-07-01", "2026-07-09", "extra"]],
        }), encoding="utf-8")
        fetched = _ecb_csv({date(2026, 7, 9): "1.30"})
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.fx"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(fetched),
            ) as mock_get,
        ):
            rates = fx.ensure_rates(
                "USD", {date(2026, 7, 9)}, today=date(2026, 7, 9)
            )
        assert mock_get.call_count == 1
        assert rates[date(2026, 7, 9)] == Decimal("1.30")
        assert any("corrupt FX cache" in r.message for r in caplog.records)


class TestEcbPublicationPossible:
    """Unit tests for fx.ecb_publication_possible (TARGET working days)."""

    @pytest.mark.parametrize("d", [
        date(2026, 1, 1),    # New Year's Day (Thursday)
        date(2026, 4, 3),    # Good Friday
        date(2026, 4, 6),    # Easter Monday
        date(2026, 5, 1),    # Labour Day (Friday)
        date(2026, 12, 25),  # Christmas Day (Friday)
        date(2026, 12, 26),  # Boxing Day — also a Saturday
        date(2026, 7, 18),   # plain Saturday
        date(2026, 7, 19),   # plain Sunday
    ])
    def test_no_publication_possible_returns_false(self, d):
        assert not fx.ecb_publication_possible(d)

    @pytest.mark.parametrize("d", [
        date(2026, 7, 15),   # plain Wednesday
        date(2026, 4, 7),    # Tuesday after Easter
        date(2026, 12, 24),  # Christmas Eve IS a TARGET working day
    ])
    def test_target_working_day_returns_true(self, d):
        assert fx.ecb_publication_possible(d)

    @pytest.mark.parametrize("year, expected", [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
    ])
    def test_easter_sunday_pins_known_dates(self, year, expected):
        """Pin the Anonymous Gregorian computus against known Easter
        Sundays — Good Friday/Easter Monday derive from it."""
        assert fx._easter_sunday(year) == expected


class TestMergeWindows:
    """Pins for fx._merge_windows' contiguity rule (F5)."""

    def test_one_missing_day_gap_does_not_merge(self):
        """[Jan1-5] + [Jan7-10]: Jan 6 was never fetched — merging would
        fabricate coverage, so the two windows must stay separate."""
        windows = [
            (date(2026, 1, 1), date(2026, 1, 5)),
            (date(2026, 1, 7), date(2026, 1, 10)),
        ]
        assert fx._merge_windows(windows) == windows

    def test_contiguous_windows_merge(self):
        """[Jan1-5] + [Jan6-10]: zero missing days → merged into one."""
        assert fx._merge_windows([
            (date(2026, 1, 1), date(2026, 1, 5)),
            (date(2026, 1, 6), date(2026, 1, 10)),
        ]) == [(date(2026, 1, 1), date(2026, 1, 10))]


@pytest.mark.usefixtures("fx_cache_dir")
class TestConvertFx:
    """Integration tests for convert_orders_csv with fx_base set."""

    def _convert_fx(self, tmp_path, rows, fx_base="EUR",
                    fx_date_mode="berlin", out_name="out.csv") -> Path:
        input_path = _write_orders_csv(tmp_path / "orders.csv", rows)
        output_path = tmp_path / out_name
        convert_orders_csv(
            input_path, output_path, {},
            fx_base=fx_base, fx_date_mode=fx_date_mode,
        )
        return output_path

    def test_fx_eur_emits_reciprocal_ecb_rate_quantized_8dp(
        self, tmp_path
    ):
        """--fx EUR fills Conversion rate with 1/ecb(USD) at 8 decimals."""
        csv_text = _ecb_csv({date(2026, 2, 6): "1.1419"})
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(csv_text),
        ):
            out = self._convert_fx(tmp_path, [_order_row()])
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == _expected_rate_cell("1.1419")

    def test_row_with_currency_equal_to_fx_base_gets_empty_cell(
        self, tmp_path
    ):
        """A row whose Currency already is the base gets an empty cell."""
        rows = [_order_row(Currency="EUR")]
        with patch("tradestation.fx.requests.get") as mock_get:
            out = self._convert_fx(tmp_path, rows)
        mock_get.assert_not_called()  # EUR needs no series at all
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Currency"] == "EUR"
        assert trade["Conversion rate"] == ""

    def test_two_fills_on_different_days_get_different_rates(
        self, tmp_path
    ):
        """Each fill is converted at its own date's ECB rate."""
        csv_text = _ecb_csv({
            date(2026, 2, 5): "1.10",
            date(2026, 2, 6): "1.20",
        })
        rows = [
            _order_row(OrderID="d1", datetime="2026-02-05T10:00:00"),
            _order_row(OrderID="d2", datetime="2026-02-06T10:00:00"),
        ]
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(csv_text),
        ):
            out = self._convert_fx(tmp_path, rows)
        _, data = _read_output(out)
        rates = [dict(zip(TDPRO_COLUMNS, r, strict=True))["Conversion rate"]
                 for r in data]
        assert rates[0] == _expected_rate_cell("1.10")
        assert rates[1] == _expected_rate_cell("1.20")
        assert rates[0] != rates[1]

    def test_fx_date_mode_berlin_vs_utc_boundary_fill(
        self, tmp_path
    ):
        """A fill at 2026-07-12T22:30 UTC (Sunday) looks up Monday's rate
        under berlin and Friday's under utc."""
        csv_text = _ecb_csv({
            date(2026, 7, 10): "1.10",  # Friday
            date(2026, 7, 13): "1.30",  # Monday
        })
        rows = [_order_row(datetime="2026-07-12T22:30:00")]
        with patch(
            "tradestation.fx.requests.get",
            return_value=_fx_response(csv_text),
        ):
            out_berlin = self._convert_fx(
                tmp_path, rows, fx_date_mode="berlin", out_name="berlin.csv"
            )
            out_utc = self._convert_fx(
                tmp_path, rows, fx_date_mode="utc", out_name="utc.csv"
            )
        berlin_rate = dict(
            zip(TDPRO_COLUMNS, _read_output(out_berlin)[1][0], strict=True)
        )["Conversion rate"]
        utc_rate = dict(
            zip(TDPRO_COLUMNS, _read_output(out_utc)[1][0], strict=True)
        )["Conversion rate"]
        assert berlin_rate == _expected_rate_cell("1.30")
        assert utc_rate == _expected_rate_cell("1.10")

    def test_missing_rate_for_needed_date_raises_tdpro_error(
        self, tmp_path
    ):
        """A needed date with no ECB rate within 14 days before it raises
        TdproError naming the currency AND the date."""
        csv_text = _ecb_csv({date(2026, 7, 7): "1.10"})  # day after the fill
        rows = [_order_row(datetime="2026-07-06T10:00:00")]
        input_path = _write_orders_csv(tmp_path / "orders.csv", rows)
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
            pytest.raises(TdproError) as exc_info,
        ):
            convert_orders_csv(
                input_path, tmp_path / "out.csv", {}, fx_base="EUR"
            )
        assert "USD" in str(exc_info.value)
        assert "2026-07-06" in str(exc_info.value)

    def test_fx_base_none_leaves_column_empty(self, tmp_path):
        """Without fx_base the Conversion rate column stays empty."""
        out = _convert(tmp_path, [_order_row()])
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == ""

    def test_blank_currency_under_fx_raises_tdpro_error_naming_order_id(
        self, tmp_path
    ):
        """An executed row with a blank Currency under --fx fails loud —
        a silent empty rate cell would resurrect the stale-rate bug."""
        rows = [
            _order_row(OrderID="blank1", Currency="  "),
            _order_row(OrderID="blank2", Currency=""),
        ]
        input_path = _write_orders_csv(tmp_path / "orders.csv", rows)
        with (
            patch("tradestation.fx.requests.get") as mock_get,
            pytest.raises(TdproError) as exc_info,
        ):
            convert_orders_csv(
                input_path, tmp_path / "out.csv", {}, fx_base="EUR"
            )
        assert "blank1" in str(exc_info.value)
        assert "blank2" in str(exc_info.value)
        mock_get.assert_not_called()  # fail before any ECB request

    def test_blank_currency_without_fx_stays_silent_empty(self, tmp_path):
        """Without --fx, a blank Currency behaves exactly as before: the row
        converts with empty Currency and Conversion rate cells."""
        rows = [_order_row(OrderID="b1", Currency="")]
        out = _convert(tmp_path, rows)
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["TransactionID"] == "b1-1"
        assert trade["Currency"] == ""
        assert trade["Conversion rate"] == ""

    def test_fx_error_surfaces_as_tdpro_error(self, tmp_path):
        """An FxError from the fx layer (empty ECB series) is re-raised as
        TdproError."""
        with (
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response("TIME_PERIOD,OBS_VALUE\n"),
            ),
            pytest.raises(TdproError, match="no rates"),
        ):
            self._convert_fx(tmp_path, [_order_row()])

    def test_same_day_fill_served_by_earlier_rate_warns_provisional(
        self, tmp_path, caplog, monkeypatch
    ):
        """A weekday fill needing TODAY's rate but served by yesterday's
        warns (hedged — names the currency, notes the day may not be an
        ECB publication day)."""
        _freeze_now(monkeypatch, _berlin(2026, 7, 15, 10))  # Wednesday 10:00
        assert date(2026, 7, 15).weekday() < 5
        csv_text = _ecb_csv({date(2026, 7, 14): "1.10"})
        rows = [_order_row(datetime="2026-07-15T10:00:00")]
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.tdpro"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
        ):
            out = self._convert_fx(
                tmp_path, rows, fx_date_mode="utc"
            )
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == _expected_rate_cell("1.10")
        warnings = [
            r.message for r in caplog.records
            if "no ECB rate for" in r.message
        ]
        assert len(warnings) == 1
        assert warnings[0].startswith("USD:")
        assert "2026-07-15" in warnings[0]
        assert "2026-07-14" in warnings[0]
        assert "if 2026-07-15 is an ECB publication day" in warnings[0]
        assert "16:00 CET" in warnings[0]

    def test_needed_saturday_at_or_after_today_does_not_warn(
        self, tmp_path, caplog, monkeypatch
    ):
        """F2: a Saturday needed date ≥ today is served the last published
        rate with NO warning — the ECB never publishes on weekends, so the
        served rate IS final."""
        _freeze_now(monkeypatch, _berlin(2026, 7, 18, 10))  # Saturday 10:00
        saturday = date(2026, 7, 18)
        assert saturday.weekday() == 5
        csv_text = _ecb_csv({date(2026, 7, 17): "1.10"})  # Friday
        rows = [_order_row(datetime="2026-07-18T10:00:00")]
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.tdpro"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
        ):
            out = self._convert_fx(
                tmp_path, rows, fx_date_mode="utc"
            )
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == _expected_rate_cell("1.10")
        assert not any(
            "no ECB rate for" in r.message for r in caplog.records
        )

    @pytest.mark.parametrize("closed_day, served_day", [
        (date(2026, 1, 1), date(2025, 12, 31)),   # New Year's Day
        (date(2026, 4, 3), date(2026, 4, 2)),     # Good Friday
        (date(2026, 4, 6), date(2026, 4, 2)),     # Easter Monday
        (date(2026, 5, 1), date(2026, 4, 30)),    # Labour Day
        (date(2026, 12, 25), date(2026, 12, 24)), # Christmas Day
        (date(2026, 12, 26), date(2026, 12, 24)), # Boxing Day (Saturday)
    ])
    def test_target_closing_day_at_or_after_today_does_not_warn(
        self, tmp_path, caplog, monkeypatch, closed_day, served_day
    ):
        """F2: a needed date on a TARGET closing day >= today can never
        have an ECB publication — the last published rate is final, so it
        is served with NO provisional warning."""
        _freeze_now(
            monkeypatch,
            _berlin(closed_day.year, closed_day.month, closed_day.day, 10),
        )
        csv_text = _ecb_csv({served_day: "1.10"})
        rows = [_order_row(datetime=f"{closed_day.isoformat()}T10:00:00")]
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.tdpro"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
        ):
            out = self._convert_fx(tmp_path, rows, fx_date_mode="utc")
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == _expected_rate_cell("1.10")
        assert not any(
            "no ECB rate for" in r.message for r in caplog.records
        )

    def test_past_weekday_holiday_stays_silent(
        self, tmp_path, caplog, monkeypatch
    ):
        """F2 edge: a needed date that is a weekday TARGET holiday in the
        PAST (d < today) never warns — the gate only fires at all for
        d >= today."""
        _freeze_now(monkeypatch, _berlin(2026, 7, 15, 10))
        may_day = date(2026, 5, 1)  # Friday, TARGET-closed, in the past
        assert may_day.weekday() == 4
        csv_text = _ecb_csv({date(2026, 4, 30): "1.10"})
        rows = [_order_row(datetime="2026-05-01T10:00:00")]
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.tdpro"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
        ):
            out = self._convert_fx(tmp_path, rows, fx_date_mode="utc")
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == _expected_rate_cell("1.10")
        assert not any(
            "no ECB rate for" in r.message for r in caplog.records
        )

    def test_cross_rate_provisional_warns_per_currency(
        self, tmp_path, caplog, monkeypatch
    ):
        """F3: --fx GBP with USD rows — the provisional warning names each
        currency leg (dedup key includes currency), so the user knows both
        the USD and GBP series are provisional."""
        _freeze_now(monkeypatch, _berlin(2026, 7, 15, 10))  # Wednesday 10:00
        csv_text = _ecb_csv({date(2026, 7, 14): "1.10"})
        rows = [_order_row(Currency="USD", datetime="2026-07-15T10:00:00")]
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.tdpro"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
        ):
            out = self._convert_fx(
                tmp_path, rows, fx_base="GBP", fx_date_mode="utc"
            )
        _, data = _read_output(out)
        assert len(data) == 1
        warnings = [
            r.message for r in caplog.records
            if "no ECB rate for" in r.message
        ]
        # one warning per series leg: USD (row currency) AND GBP (base)
        assert any(w.startswith("USD:") for w in warnings)
        assert any(w.startswith("GBP:") for w in warnings)

    def test_past_saturday_served_by_friday_does_not_warn(
        self, tmp_path, caplog
    ):
        """A past weekend fallback is final — no provisional warning."""
        saturday = date(2026, 1, 10)
        assert saturday.weekday() == 5
        csv_text = _ecb_csv({date(2026, 1, 9): "1.10"})  # Friday
        rows = [_order_row(datetime="2026-01-10T10:00:00")]
        with (
            caplog.at_level(logging.WARNING, logger="tradestation.tdpro"),
            patch(
                "tradestation.fx.requests.get",
                return_value=_fx_response(csv_text),
            ),
        ):
            out = self._convert_fx(
                tmp_path, rows, fx_date_mode="utc"
            )
        _, data = _read_output(out)
        trade = dict(zip(TDPRO_COLUMNS, data[0], strict=True))
        assert trade["Conversion rate"] == _expected_rate_cell("1.10")
        assert not any(
            "no ECB rate for" in r.message for r in caplog.records
        )


class TestCliFx:
    """Tests for the --fx / --fx-date CLI plumbing."""

    def test_fx_flag_normalizes_to_uppercase(self):
        """--fx eur parses to 'EUR'."""
        args = create_tdpro_parser().parse_args(["f.csv", "--fx", "eur"])
        assert args.fx == "EUR"

    @pytest.mark.parametrize("bad", ["1X", "EURO"])
    def test_fx_flag_rejects_non_three_letter_codes(self, bad):
        """--fx 1X and --fx EURO are rejected by argparse with exit code 2."""
        with pytest.raises(SystemExit) as exc_info:
            create_tdpro_parser().parse_args(["f.csv", "--fx", bad])
        assert exc_info.value.code == 2

    def test_fx_date_without_fx_exits_2(self, tmp_path):
        """--fx-date without --fx is a usage error (exit 2)."""
        with (
            patch.object(
                sys, "argv",
                ["orders-to-tdpro", str(tmp_path / "x.csv"),
                 "--fx-date", "utc"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main_tdpro()
        assert exc_info.value.code == 2

    def test_fx_and_fx_date_flow_to_convert_orders_csv(self, tmp_path):
        """--fx EUR --fx-date berlin reaches convert_orders_csv as
        fx_base/fx_date_mode kwargs."""
        input_path = str(tmp_path / "orders.csv")
        with (
            patch.object(
                sys, "argv",
                ["orders-to-tdpro", input_path, "--fx", "EUR",
                 "--fx-date", "berlin"],
            ),
            patch(
                "tradestation.tdpro.convert_orders_csv", return_value=(0, 0)
            ) as m_convert,
            pytest.raises(SystemExit) as exc_info,
        ):
            main_tdpro()
        assert exc_info.value.code == 0
        assert m_convert.call_args.kwargs["fx_base"] == "EUR"
        assert m_convert.call_args.kwargs["fx_date_mode"] == "berlin"

    def test_help_shows_fx_flags(self, capsys):
        """--help mentions both --fx and --fx-date."""
        with pytest.raises(SystemExit) as exc_info:
            create_tdpro_parser().parse_args(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--fx" in out
        assert "--fx-date" in out
