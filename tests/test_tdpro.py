"""Tests for tradestation.tdpro (orders CSV -> TradingDiary Pro converter)."""

import csv
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

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
