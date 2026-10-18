"""Convert a tradestation-downloader orders CSV to TradingDiary Pro import format.

Self-contained converter module (``orders-to-tdpro``): reads an orders CSV
export produced by ``tradestation-download orders``, maps every executed leg
to a futures trade row in TradingDiary Pro's 17-column Excel Importer format,
and writes the result atomically next to the input (``<stem>_tdpro.csv``).
"""

import csv
import hashlib
import json
import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from . import fx
from .models import base_symbol

logger = logging.getLogger(__name__)

TDPRO_COLUMNS = [
    "TransactionID", "DateTime", "Ticker", "Description", "Quantity",
    "Price", "Commission", "Asset Category", "Multiplier", "Currency",
    "Conversion rate", "Expiration Date", "Call or Put", "Strike", "Note",
    "Exchange", "Tag(s)",
]  # canonical 17 columns, fixed order, per TD Pro Excel Importer docs

REQUIRED_INPUT_COLUMNS = [
    "datetime", "OrderID", "Legs", "FilledPrice",
    "CommissionFee", "Currency", "ConversionRate",
]  # ConversionRate is required for validation but intentionally not emitted —
# TD Pro applies its own FX rate (see plan §3)

# CME-verified contract point values (Amount = Price x Quantity x Multiplier)
FUTURES_MULTIPLIERS = {"MES": 5, "MNG": 1000, "1OZ": 1}


class TdproError(Exception):
    """Raised when an orders CSV cannot be converted to TD Pro format."""


def _decimal_str(value: Decimal) -> str:
    """Format a Decimal as a plain decimal string (no exponent, no float artifacts)."""
    return format(value, "f")


def _parse_exec_quantity(value: Any) -> int | None:
    """Parse a leg's ``ExecQuantity``; ``None`` when unparseable or negative."""
    try:
        qty = int(str(value))
    except (TypeError, ValueError):
        return None
    return qty if qty >= 0 else None


def _is_executed_leg(leg: Any) -> bool:
    """True when the leg is a FUTURE leg with a positive ExecQuantity."""
    # Deliberately no Status check: ExecQuantity > 0 is the fill predicate, so
    # partial fills are captured regardless of status — do not "fix" by
    # requiring Status == "FLL".
    if not isinstance(leg, dict) or leg.get("AssetType") != "FUTURE":
        return False
    qty = _parse_exec_quantity(leg.get("ExecQuantity"))
    return qty is not None and qty > 0


def _format_datetime(raw: str) -> str | None:
    """Reformat a ``YYYY-MM-DDTHH:MM:SS[.ffffff]`` timestamp to ``YYYY-MM-DD HH:MM:SS``."""
    try:
        return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _format_expiration(raw: Any) -> str:
    """Reformat an ISO ``ExpirationDate`` to ``YYYY-MM-DD``; empty when unusable."""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _underlying_root(leg: dict) -> str:
    """Return the leg's ``Underlying``, falling back to ``base_symbol(Symbol)``."""
    underlying = str(leg.get("Underlying") or "").strip()
    if underlying:
        return underlying
    return base_symbol(str(leg.get("Symbol") or ""))


def _parse_commission(raw: Any, order_id: str) -> Decimal | None:
    """Parse ``CommissionFee``; ``None`` (with a warning) when unusable.

    Commission is optional in TD Pro, so a rejected fee — unparseable,
    non-finite, |fee| > 1e9, or nonzero with adjusted() < -18 — leaves the
    Commission cell empty rather than dropping the fill.
    """
    text = str(raw or "").strip()
    fee: Decimal | None = None
    if text:
        try:
            fee = Decimal(text)
        except ArithmeticError:
            fee = None
    if (
        fee is None
        or not fee.is_finite()
        or fee.copy_abs() > Decimal("1e9")  # copy_abs: abs() can Overflow
        or (fee != 0 and fee.adjusted() < -18)
    ):
        logger.warning(
            "Order %s: unusable CommissionFee %r, leaving Commission empty",
            order_id, raw,
        )
        return None
    if fee == 0:
        # Canonicalize zero (e.g. "0e-999999999") so downstream formatting
        # emits "0" instead of a string with a million zeros.
        fee = Decimal("0")
    return fee


def _transaction_id(order_id: str, row: pd.Series, leg: dict,
                    leg_index: int) -> str:
    """TD Pro TransactionID: ``{OrderID}-{leg_index}`` normally; when OrderID
    is empty, a deterministic ``ts-<digest>-{leg_index}`` over the whole leg
    plus the row's datetime/CommissionFee/AccountID, so TD Pro's dedup still
    works and only byte-identical fills share an ID."""
    if order_id:
        return f"{order_id}-{leg_index}"
    digest = hashlib.sha1("|".join([
        json.dumps(leg, sort_keys=True),
        str(row["datetime"]),
        str(row["CommissionFee"]),
        str(row.get("AccountID") or ""),
    ]).encode()).hexdigest()[:12]
    return f"ts-{digest}-{leg_index}"


def _make_rate_lookup(
    fx_base: str,
    rates_by_currency: dict[str, dict],
    fx_date_mode: str,
    today: date,
):
    """Build the ``(currency, fill_datetime) -> rate | None`` lookup used for
    the Conversion rate column. ``None`` means the cell stays empty
    (currency == base). FX failures surface as ``TdproError`` prefixed with
    the row's currency.

    When the needed date is today (or later) but only an earlier rate was
    published, a WARNING is logged once per distinct
    (currency, needed_date, served_date) triple — the same-day rate may
    appear after the ECB's ~16:00 CET publication. The warning is
    suppressed on dates where an ECB publication is impossible —
    weekends and TARGET closing days (see
    ``fx.ecb_publication_possible``) — because the served rate IS final
    then. Past-date fallbacks (weekends, holidays) are final and never
    warn.

    This pairs with ``fx.ensure_rates``' provisional rule: uncovered
    needed dates with a cached rate <= d are provisional and served by
    the last-published fallback — this warning is their user-facing
    signal — while uncovered dates with NO rate <= d are already
    ``FxError``s upstream and never reach this lookup.
    """
    warned: set[tuple[str, date, date]] = set()

    def rate_lookup(currency: str, dt: datetime) -> Decimal | None:
        if currency == fx_base:
            return None
        try:
            d = fx.fx_date(dt, fx_date_mode)
            if d >= today and fx.ecb_publication_possible(d):
                for ccy in ({currency, fx_base} - {"EUR"}):
                    served = fx.last_published_on(rates_by_currency[ccy], d)
                    if served < d and (ccy, d, served) not in warned:
                        warned.add((ccy, d, served))
                        logger.warning(
                            "%s: no ECB rate for %s yet — used last "
                            "published rate (%s); if %s is an ECB "
                            "publication day, re-run after ~16:00 CET for "
                            "the same-day rate",
                            ccy, d, served, d,
                        )
            return fx.rate_to_base(
                rates_by_currency, currency, fx_base, d,
            )
        except fx.FxError as e:
            raise TdproError(f"{currency}: {e}") from e

    return rate_lookup


def _order_rows(
    row: pd.Series,
    legs: list,
    multipliers: dict[str, Decimal],
    rate_lookup: Any = None,
) -> tuple[list[list[str]], str, int]:
    """Map one order's executed legs to TD Pro rows.

    Precondition: ``multipliers`` must contain every executed leg's
    underlying root — enforced by the pre-scan in ``convert_orders_csv``.

    ``rate_lookup`` (when the ``--fx`` feature is on) maps
    ``(currency, fill_datetime)`` to the ECB-derived conversion rate, or
    ``None`` when the cell should stay empty (currency == base).

    Returns ``(rows, status, bad_legs)`` where status is ``"ok"``,
    ``"non_executed"``, or ``"bad"``. Problem legs/orders are logged, skipped,
    and reflected in the status/leg count; only ``TdproError`` (an
    unresolvable FX rate) propagates.
    """
    order_id = str(row["OrderID"]).strip()

    executed = []
    skipped = 0
    for idx, leg in enumerate(legs, start=1):
        if _is_executed_leg(leg):
            executed.append((idx, leg))
            continue
        if not isinstance(leg, dict):
            logger.warning(
                "Order %s leg %d: malformed leg %r, skipping leg",
                order_id, idx, leg,
            )
            skipped += 1
            continue
        qty = _parse_exec_quantity(leg.get("ExecQuantity"))
        if qty is None:
            logger.warning(
                "Order %s leg %d: invalid ExecQuantity %r, skipping leg",
                order_id, idx, leg.get("ExecQuantity"),
            )
            skipped += 1
        elif qty > 0:
            logger.warning(
                "Order %s leg %d: AssetType %r is not FUTURE, skipping leg",
                order_id, idx, leg.get("AssetType"),
            )
            skipped += 1
        # qty == 0 → non-executed leg, skipped silently
    if not executed:
        # non-executed order — skipped silently, counted by caller
        return [], "bad" if skipped else "non_executed", skipped

    trade_dt = _format_datetime(str(row["datetime"]))
    if trade_dt is None:
        logger.warning(
            "Order %s: could not parse datetime %r, skipping row",
            order_id, row["datetime"],
        )
        return [], "bad", skipped

    currency = str(row["Currency"]).strip()
    # trade_dt guarantees this parses — same source for the DateTime column
    dt_obj = datetime.fromisoformat(str(row["datetime"]))

    # _is_executed_leg already guarantees these parse to positive ints
    total_qty = sum(_parse_exec_quantity(leg["ExecQuantity"]) for _, leg in executed)
    fee = _parse_commission(row["CommissionFee"], order_id)
    if not order_id:
        logger.warning(
            "Order at %s has an empty OrderID: deriving TransactionID from a "
            "digest of the fill fields",
            row["datetime"],
        )

    rows = []
    for leg_index, leg in executed:
        qty = _parse_exec_quantity(leg["ExecQuantity"])
        side = leg.get("BuyOrSell")
        if side == "Buy":
            signed_qty = qty
        elif side == "Sell":
            signed_qty = -qty
        else:
            logger.warning(
                "Order %s leg %d: unexpected BuyOrSell %r, skipping leg",
                order_id, leg_index, side,
            )
            skipped += 1
            continue
        price = str(leg.get("ExecutionPrice") or "").strip() or str(row["FilledPrice"]).strip()
        if not price:
            logger.warning(
                "Order %s leg %d: no ExecutionPrice or FilledPrice, skipping leg",
                order_id, leg_index,
            )
            skipped += 1
            continue
        try:
            commission = (
                "" if fee is None
                else _decimal_str(fee * Decimal(qty) / Decimal(total_qty))
            )
        except ArithmeticError:
            # Backstop: extreme values (e.g. a pathological ExecQuantity)
            # can overflow the decimal context during the split — leave the
            # Commission cell empty rather than dropping the fill.
            logger.warning(
                "Order %s: could not compute commission share, "
                "leaving Commission empty",
                order_id,
            )
            commission = ""
        multiplier = multipliers[_underlying_root(leg)]
        conversion = ""
        if rate_lookup is not None and currency:
            rate = rate_lookup(currency, dt_obj)
            if rate is not None:
                conversion = _decimal_str(
                    rate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
                )
        rows.append([
            _transaction_id(order_id, row, leg, leg_index),
            trade_dt,
            str(leg.get("Symbol") or ""),
            "",  # Description — defaults to Ticker in TD Pro
            str(signed_qty),
            price,
            commission,
            "Futures",
            _decimal_str(multiplier),
            currency,
            conversion,  # ECB rate when --fx is on; else TD Pro's own FX rate applies
            _format_expiration(leg.get("ExpirationDate")),
            "",  # Call or Put — unused for Futures
            "",  # Strike — unused for Futures
            "",  # Note
            "",  # Exchange
            "",  # Tag(s)
        ])
    return rows, "ok" if rows else "bad", skipped


def convert_orders_csv(
    input_path: Path,
    output_path: Path,
    multipliers: dict[str, Decimal | str | int],
    fx_base: str | None = None,
    fx_date_mode: str = "berlin",
) -> tuple[int, int]:
    """Convert an orders CSV to a TradingDiary Pro import CSV.

    Args:
        input_path: Orders CSV produced by ``tradestation-download orders``
        output_path: Destination CSV path (written atomically)
        multipliers: Extra/override contract point values keyed by root
        fx_base: When set, fill ``Conversion rate`` with ECB daily reference
            rates converting each row's currency to this base (e.g. ``EUR``)
        fx_date_mode: ``"berlin"`` (German-local fill date) or ``"utc"`` —
            the calendar date used for the ECB rate lookup

    Returns:
        ``(trades, orders)`` — trade rows written and total order rows processed

    Raises:
        TdproError: Input missing/unreadable, required columns absent, an
            executed leg's underlying root has no known multiplier, an
            executed row has a blank Currency under ``fx_base``, or the fx
            layer fails — any ``fx.FxError`` (fetch failure, unwritable
            cache, no rate covering a needed date) is re-raised as
            ``TdproError`` naming the currency
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    if input_path.resolve() in (output_path.resolve(), tmp_path.resolve()):
        raise TdproError(
            f"Output path is the same as the input file: {input_path}"
        )
    if not input_path.is_file():
        raise TdproError(f"Input file not found: {input_path}")
    try:
        df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    except Exception as e:
        raise TdproError(f"Could not read {input_path}: {e}") from e

    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in df.columns]
    if missing:
        raise TdproError(
            f"{input_path} is missing required columns {missing} — "
            "is this a tradestation-downloader orders CSV?"
        )

    effective = {root: Decimal(str(value)) for root, value in FUTURES_MULTIPLIERS.items()}
    effective.update({root: Decimal(str(value)) for root, value in multipliers.items()})

    # Fail fast: every executed leg's underlying root must have a multiplier.
    missing_roots: set[str] = set()
    parsed_legs: list[list[dict] | None] = []
    fx_needed: dict[str, set] = {}  # currency -> needed rate dates (--fx only)
    blank_currency_orders: list[str] = []  # executed rows w/ blank Currency (--fx only)
    for _, row in df.iterrows():
        try:
            legs = json.loads(row["Legs"])
        except (TypeError, ValueError):
            parsed_legs.append(None)
            continue
        if not isinstance(legs, list):
            parsed_legs.append(None)
            continue
        parsed_legs.append(legs)
        executed_any = False
        for leg in legs:
            if _is_executed_leg(leg):
                executed_any = True
                root = _underlying_root(leg)
                if root not in effective:
                    missing_roots.add(root)
        if fx_base is not None and executed_any:
            currency = str(row["Currency"]).strip()
            if not currency:
                # Fail loud: a blank currency under --fx would otherwise emit
                # an empty Conversion rate cell and silently resurrect the
                # stale-rate bug the column exists to fix.
                order_id = str(row["OrderID"]).strip()
                blank_currency_orders.append(
                    order_id or f"order at {row['datetime']}"
                )
                continue
            try:
                dt = datetime.fromisoformat(str(row["datetime"]))
            except ValueError:
                dt = None  # row warns+skips in the real pass
            if currency != fx_base and dt is not None:
                fx_needed.setdefault(currency, set()).add(
                    fx.fx_date(dt, fx_date_mode)
                )
    if missing_roots:
        roots = ", ".join(sorted(missing_roots))
        raise TdproError(
            f"No multiplier known for underlying root(s): {roots}. "
            "Add them with -m ROOT=VALUE (e.g. -m ES=50)."
        )
    if blank_currency_orders:
        raise TdproError(
            "Blank Currency on executed order(s) under --fx: "
            f"{', '.join(blank_currency_orders)}. Fix the orders CSV or "
            "rerun without --fx (TD Pro then applies its own FX rate)."
        )

    rate_lookup = None
    if fx_base is not None:
        # A non-EUR base needs its own series for rate_to_base; EUR is 1 by
        # definition and ECB-quoted currencies only need their own series.
        all_dates = set().union(*fx_needed.values()) if fx_needed else set()
        if fx_base != "EUR" and all_dates:
            fx_needed.setdefault(fx_base, set()).update(all_dates)
        rates_by_currency = {}
        # The ECB publication calendar is CET: "today" is the Europe/Berlin
        # date — the fetch end for ensure_rates and the threshold for the
        # provisional-rate warning gate.
        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        for currency in sorted(fx_needed):
            if currency == "EUR":
                continue
            try:
                # ensure_rates errors already name the currency
                rates_by_currency[currency] = fx.ensure_rates(
                    currency, fx_needed[currency], today=today
                )
            except fx.FxError as e:
                raise TdproError(str(e)) from e
        rate_lookup = _make_rate_lookup(
            fx_base, rates_by_currency, fx_date_mode, today
        )

    trade_rows: list[list[str]] = []
    skipped_bad_rows = skipped_legs = skipped_non_executed = 0
    for (_, row), legs in zip(df.iterrows(), parsed_legs, strict=True):
        if legs is None:
            logger.warning(
                "Order %s: malformed Legs JSON, skipping row", row["OrderID"]
            )
            skipped_bad_rows += 1
            continue
        rows, status, bad_legs = _order_rows(row, legs, effective, rate_lookup)
        if status == "non_executed":
            skipped_non_executed += 1
        elif status == "bad":
            skipped_bad_rows += 1
        skipped_legs += bad_legs
        trade_rows.extend(rows)

    if skipped_non_executed:
        logger.info("Skipped %d non-executed orders", skipped_non_executed)
    if skipped_bad_rows or skipped_legs:
        logger.warning(
            "Skipped %d unconvertible orders and %d bad legs",
            skipped_bad_rows, skipped_legs,
        )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(TDPRO_COLUMNS)
            writer.writerows(trade_rows)
        tmp_path.replace(output_path)
    except OSError as e:
        tmp_path.unlink(missing_ok=True)
        raise TdproError(
            f"Could not write {output_path}: {e} — "
            "is the file open in another program?"
        ) from e
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return len(trade_rows), len(df)
