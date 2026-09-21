"""Build and update TradeStation third-party ``attributes.ini`` for --export-ts-csv.

The file lives in the ``plain_data`` directory next to the exported ``*_ts.txt``
files and follows the format of ``example_attributes.INI``: one comma-separated
row per exported symbol, CRLF line endings, DESCRIPTION as the only quoted field.
"""

import csv
import io
import json
import logging
from pathlib import Path

import pandas as pd

from .metadata import derive_sessions, get_exchange_timezone

logger = logging.getLogger(__name__)

ATTRIBUTES_HEADER = (
    "SYMBOL,CATEGORY,DATE FORMAT,EXCHANGE,PRICE SCALE,MINIMUM MOVEMENT,"
    "BIG POINT VALUE,SESSION 1 START TIME,SESSION 1 END TIME,SESSION 1 DAYS,"
    "DESCRIPTION,SESSION 2 START TIME,SESSION 2 END TIME,SESSION 2 DAYS,"
    "OPTION TYPE,STRIKE PRICE,DAILY LIMIT,MARGIN,EXPIRATION DATE,LOCALE"
)
ATTRIBUTES_FIELDS = ATTRIBUTES_HEADER.split(",")

# SESSION 1 DAYS letters in canonical order (Sunday first, matching the example).
_DAY_LETTERS = "UMTWRFS"
_WEEKDAY_TO_LETTER = {6: "U", 0: "M", 1: "T", 2: "W", 3: "R", 4: "F", 5: "S"}

_SESSION_GAP_MINUTES = 15


def ini_symbol(symbol: str) -> str:
    """SYMBOL field: the export filename without extension and without '@' (@ES -> ES_ts)."""
    return f"{symbol.lstrip('@')}_ts"


def load_metadata(data_dir: Path) -> dict | None:
    """Load <data_dir>/metadata.json, returning None if missing or unreadable."""
    path = Path(data_dir) / "metadata.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.error("Failed to read %s: %s", path, e)
        return None
    if not isinstance(data, dict):
        logger.error("Unexpected structure in %s", path)
        return None
    return data


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _scale_denominator(price_format: dict) -> int | None:
    """Denominator D for a PRICE SCALE of 1/D, derived from api.PriceFormat."""
    fmt = price_format.get("Format")
    if fmt == "Decimal":
        decimals = _int_or_none(price_format.get("Decimals"))
        return None if decimals is None else 10**decimals
    if fmt in ("Fraction", "SubFraction"):
        return _int_or_none(price_format.get("Fraction"))
    return None


def _minimum_movement(price_format: dict, denominator: int) -> int | None:
    """MINIMUM MOVEMENT: tick Increment expressed in price-scale units (1-65000)."""
    increment = _float_or_none(price_format.get("Increment"))
    if increment is None:
        return None
    movement = int(round(increment * denominator))
    if not 1 <= movement <= 65000:
        return None
    return movement


def _big_point_value(price_format: dict) -> str | None:
    """BIG POINT VALUE: PointValue always formatted with exactly 2 decimal
    places (e.g. 50.00); TradeStation rejects a 0-decimal value on import."""
    point_value = _float_or_none(price_format.get("PointValue"))
    if point_value is None:
        return None
    return f"{point_value:.2f}"


def entry_timezone(entry: dict) -> str | None:
    """Exchange timezone from downloaded_data, falling back to the Exchange mapping."""
    downloaded = entry.get("downloaded_data") or {}
    tz = downloaded.get("exchange_timezone")
    if tz:
        return tz
    api = entry.get("api") or {}
    return get_exchange_timezone(api.get("Exchange"))


def _entry_sufficient(entry) -> bool:
    """True when a metadata.json symbol entry can populate a complete INI row."""
    if not isinstance(entry, dict):
        return False
    api = entry.get("api")
    if not isinstance(api, dict):
        return False
    if not api.get("AssetType") or not api.get("Exchange"):
        return False
    price_format = api.get("PriceFormat")
    if not isinstance(price_format, dict):
        return False
    denominator = _scale_denominator(price_format)
    if denominator is None:
        return False
    if _minimum_movement(price_format, denominator) is None:
        return False
    if _big_point_value(price_format) is None:
        return False
    return entry_timezone(entry) is not None


def metadata_sufficient(metadata: dict | None, symbols: list[str]) -> bool:
    """True when metadata.json has complete data for every requested symbol."""
    if not metadata:
        return False
    entries = metadata.get("symbols") or {}
    return all(_entry_sufficient(entries.get(symbol)) for symbol in symbols)


def ensure_metadata(config_path: str, data_dir: Path, symbols: list[str]) -> dict | None:
    """Return metadata.json contents, regenerating via --metadata when insufficient.

    Returns None when regeneration fails or metadata.json is still unreadable.
    """
    metadata = load_metadata(data_dir)
    if metadata_sufficient(metadata, symbols):
        return metadata

    # Imported lazily so patching tradestation.metadata.run_metadata works.
    from .metadata import run_metadata

    logger.info("metadata.json missing or incomplete; regenerating via --metadata")
    if run_metadata(config_path) != 0:
        logger.error("Metadata regeneration failed")
        return None
    metadata = load_metadata(data_dir)
    if metadata is None:
        logger.error("metadata.json still missing after regeneration")
        return None
    if not metadata_sufficient(metadata, symbols):
        entries = metadata.get("symbols") or {}
        insufficient = [
            symbol for symbol in symbols if not _entry_sufficient(entries.get(symbol))
        ]
        logger.error(
            "metadata.json still insufficient after regeneration for: %s",
            ", ".join(insufficient),
        )
        return None
    return metadata


def _datetime_series(df: pd.DataFrame) -> pd.Series | None:
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index.to_series().reset_index(drop=True)
    if "datetime" in df.columns:
        return pd.to_datetime(df["datetime"]).reset_index(drop=True)
    return None


def _hhmm(minute_of_day: int) -> str:
    minute_of_day %= 1440
    return f"{minute_of_day // 60:02d}{minute_of_day % 60:02d}"


def derive_regular_session(df_local: pd.DataFrame) -> tuple[str, str, str] | None:
    """Derive (start HHMM, end HHMM, days) for the regular session.

    ``df_local`` must already be converted to exchange-local time. The regular
    session is the longest occupied minute-of-day block (the overnight Globex
    block); days are the weekdays on which a session opens, in UMTWRFS order.
    """
    sessions = derive_sessions(df_local, gap_threshold_minutes=_SESSION_GAP_MINUTES)
    if not sessions:
        return None
    regular = max(sessions, key=lambda session: session["duration_hours"])

    start_h, start_m = regular["start"].split(":")
    end_h, end_m = regular["end"].split(":")
    start_minute = int(start_h) * 60 + int(start_m)
    end_minute = int(end_h) * 60 + int(end_m)

    series = _datetime_series(df_local)
    if series is None or series.empty:
        return None
    bar_minutes = series.dt.hour * 60 + series.dt.minute
    weekdays = {_WEEKDAY_TO_LETTER[d] for d in series[bar_minutes == start_minute].dt.dayofweek}
    if not weekdays:
        return None
    days = "".join(letter for letter in _DAY_LETTERS if letter in weekdays)

    # A first occupied minute of XX:01 (the first bar after a 60-minute halt,
    # e.g. the 17:01 Globex reopen) is written as the round session open XX00.
    if start_minute % 60 == 1:
        start_minute -= 1
    return _hhmm(start_minute), _hhmm(end_minute), days


def build_row(symbol: str, api: dict, df_local: pd.DataFrame) -> dict | None:
    """Build a complete attributes.ini row, or None when data is insufficient."""
    price_format = api.get("PriceFormat")
    if not isinstance(price_format, dict):
        return None
    denominator = _scale_denominator(price_format)
    if denominator is None:
        return None
    movement = _minimum_movement(price_format, denominator)
    if movement is None:
        return None
    point_value = _big_point_value(price_format)
    if point_value is None:
        return None
    session = derive_regular_session(df_local)
    if session is None:
        return None
    start, end, days = session

    description = api.get("Description") or symbol.lstrip("@")
    return {
        "SYMBOL": ini_symbol(symbol),
        "CATEGORY": str(api.get("AssetType") or ""),
        "DATE FORMAT": "MM/DD/YYYY",
        "EXCHANGE": str(api.get("Exchange") or ""),
        "PRICE SCALE": "1" if denominator == 1 else f"1/{denominator}",
        "MINIMUM MOVEMENT": str(movement),
        "BIG POINT VALUE": point_value,
        "SESSION 1 START TIME": start,
        "SESSION 1 END TIME": end,
        "SESSION 1 DAYS": days,
        "DESCRIPTION": str(description)[:50],
        "LOCALE": "0x409",
    }


def _serialize_row(row: dict) -> str:
    fields = []
    for field in ATTRIBUTES_FIELDS:
        value = str(row.get(field, ""))
        if field == "DESCRIPTION":
            value = '"' + value.replace('"', '""') + '"'
        fields.append(value)
    return ",".join(fields)


def _read_rows(path: Path) -> tuple[list[str], dict[str, dict]]:
    """Read an existing attributes.ini into (symbol order, symbol -> row).

    Rows are remapped by field name so files with a different header keep their
    data on a best-effort basis; unknown fields are dropped, missing ones empty.
    """
    order: list[str] = []
    rows: dict[str, dict] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("Could not read %s (%s); recreating", path, e)
        return order, rows

    parsed = [record for record in csv.reader(io.StringIO(text)) if record]
    if not parsed:
        return order, rows

    old_header = [field.strip() for field in parsed[0]]
    for record in parsed[1:]:
        old_row = dict(zip(old_header, record, strict=False))
        row = {field: old_row.get(field, "") for field in ATTRIBUTES_FIELDS}
        key = row.get("SYMBOL") or (record[0] if record else "")
        if not key:
            continue
        row["SYMBOL"] = key
        if key not in rows:
            order.append(key)
        rows[key] = row
    return order, rows


def upsert_attributes_ini(path: Path, rows: dict[str, dict]) -> None:
    """Insert or update rows keyed by SYMBOL, leaving other entries unchanged."""
    order, existing = _read_rows(path) if path.exists() else ([], {})
    for key, row in rows.items():
        if key not in existing:
            order.append(key)
        existing[key] = row

    lines = [ATTRIBUTES_HEADER] + [_serialize_row(existing[key]) for key in order]
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    logger.info("Updated %s (%d rows)", path, len(order))
