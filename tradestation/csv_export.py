"""Export downloaded parquet data to CSV .txt files."""

import logging
import sys
from pathlib import Path

import pandas as pd

from . import attributes_ini
from .config import ConfigurationError, load_config
from .metadata import convert_df_timezone
from .models import validate_symbol
from .storage import create_storage, detect_storage_format

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """Normalize a symbol to its @-prefixed form."""
    return f"@{symbol.lstrip('@')}"


def _setup_export(config_path: str, symbols: list[str] | None):
    """Load config, create storage, and resolve target symbols.

    Returns (storage, output_dir, targets, data_dir) or None on error.
    """
    try:
        config = load_config(config_path)
    except ConfigurationError as e:
        logger.error(str(e))
        return None

    data_dir = Path(config.data_dir)

    try:
        storage_format = detect_storage_format(data_dir)
    except Exception:
        storage_format = config.storage_format

    try:
        storage = create_storage(
            storage_format,
            data_dir,
            compression=config.compression.value,
            datetime_index=config.datetime_index,
        )
    except Exception as e:
        logger.error("Failed to create storage: %s", e)
        return None

    if symbols is not None:
        for requested in symbols:
            validate_symbol(requested)

    available = storage.list_symbols()
    if not available:
        logger.error("No data found in %s", data_dir)
        print(f"No data found in {data_dir}", file=sys.stderr)
        return None

    if symbols is None:
        targets = available
    else:
        normalized_available = {_normalize_symbol(s): s for s in available}
        targets = []
        for requested in symbols:
            normalized = _normalize_symbol(requested)
            if normalized not in normalized_available:
                logger.error("No data found for symbol: %s", requested)
                print(f"No data found for symbol: {requested}", file=sys.stderr)
                return None
            targets.append(normalized_available[normalized])

    output_dir = data_dir.parent / "plain_data"
    output_dir.mkdir(parents=True, exist_ok=True)

    return storage, output_dir, targets, data_dir


def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure a datetime column and add formatted Date/Time columns."""
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index(names=["datetime"])

    df["Date"] = df["datetime"].dt.strftime("%m/%d/%Y")
    df["Time"] = df["datetime"].dt.strftime("%H:%M")
    return df


def _build_csv_output(df: pd.DataFrame):
    """Build (out_df, output_columns) for the standard CSV export format."""
    df = _prepare_frame(df)

    output_columns = ["Date", "Time", "Open", "High", "Low", "Close"]
    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
    }
    if "up_volume" in df.columns and "down_volume" in df.columns:
        rename_map["up_volume"] = "Up"
        rename_map["down_volume"] = "Down"
        output_columns.extend(["Up", "Down"])
        if "up_ticks" in df.columns and "down_ticks" in df.columns:
            rename_map["up_ticks"] = "Upticks"
            rename_map["down_ticks"] = "Downticks"
            output_columns.extend(["Upticks", "Downticks"])
    elif "volume" in df.columns:
        rename_map["volume"] = "Vol"
        output_columns.append("Vol")

    if "open_interest" in df.columns:
        rename_map["open_interest"] = "OpenInterest"
        output_columns.append("OpenInterest")

    if "total_ticks" in df.columns:
        rename_map["total_ticks"] = "TotalTicks"
        output_columns.append("TotalTicks")

    return df.rename(columns=rename_map)[output_columns], output_columns


def _build_ts_csv_output(df: pd.DataFrame):
    """Build (out_df, output_columns) for the TradeStation ASCII format."""
    df = _prepare_frame(df)

    output_columns = ["Date", "Time", "Open", "High", "Low", "Close", "Volume"]
    rename_map = {
        "open": "Open",
        "high": "High",
        "low": "Low",
        "close": "Close",
    }
    if "up_volume" in df.columns and "down_volume" in df.columns:
        df["Volume"] = (df["up_volume"].fillna(0) + df["down_volume"].fillna(0)).astype("Int64")
    elif "volume" in df.columns:
        df["Volume"] = df["volume"].fillna(0).astype("Int64")
    else:
        df["Volume"] = 0

    if "open_interest" in df.columns:
        rename_map["open_interest"] = "OpenInt"
        output_columns.append("OpenInt")

    return df.rename(columns=rename_map)[output_columns], output_columns


def _run_export(config_path: str, symbols: list[str] | None, build_output, filename, label: str) -> int:
    """Shared export loop. Returns 0 success, 1 error."""
    setup = _setup_export(config_path, symbols)
    if setup is None:
        return 1
    storage, output_dir, targets, _data_dir = setup

    total_rows = 0
    for symbol in targets:
        df = storage.load(symbol)
        if df is None or df.empty:
            logger.warning("No data for %s; skipping", symbol)
            continue

        out_df, output_columns = build_output(df)

        path = output_dir / filename(symbol)
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write('"' + '","'.join(output_columns) + '"\r\n')
            out_df.to_csv(f, header=False, index=False, lineterminator="\r\n")

        logger.info("Exported %s: %d rows -> %s", symbol, len(out_df), path)
        total_rows += len(out_df)

    print(f"{label} export complete: {len(targets)} symbols, {total_rows} rows -> {output_dir}")
    return 0


def run_export_csv(config_path: str, symbols: list[str] | None = None) -> int:
    """Export downloaded parquet data to CSV .txt files. Returns 0 success, 1 error."""
    return _run_export(
        config_path,
        symbols,
        _build_csv_output,
        lambda symbol: f"{symbol}.txt",
        "CSV",
    )


def run_export_ts_csv(config_path: str, symbols: list[str] | None = None) -> int:
    """Export to TradeStation third-party ASCII format (_ts.txt). Returns 0 success, 1 error.

    Date/Time are written in the exchange timezone (matching SESSION 1 in
    attributes.ini); a row per exported symbol is upserted into
    plain_data/attributes.ini. metadata.json is regenerated via --metadata when
    it is missing or lacks data needed for the exported symbols.
    """
    setup = _setup_export(config_path, symbols)
    if setup is None:
        return 1
    storage, output_dir, targets, data_dir = setup

    metadata = attributes_ini.ensure_metadata(config_path, data_dir, targets)
    if metadata is None:
        return 1

    total_rows = 0
    ini_rows: dict[str, dict] = {}
    failed = False
    for symbol in targets:
        df = storage.load(symbol)
        if df is None or df.empty:
            logger.warning("No data for %s; skipping", symbol)
            continue

        entry = attributes_ini.find_metadata_entry(metadata, symbol) or {}
        api = entry.get("api") if isinstance(entry.get("api"), dict) else None
        timezone = attributes_ini.entry_timezone(entry)
        if api is None or timezone is None:
            logger.error(
                "Insufficient metadata for %s; cannot export TradeStation CSV", symbol
            )
            failed = True
            continue

        local_df = convert_df_timezone(df, timezone)
        out_df, output_columns = _build_ts_csv_output(local_df)

        path = output_dir / f"{symbol}_ts.txt"
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write('"' + '","'.join(output_columns) + '"\r\n')
            out_df.to_csv(f, header=False, index=False, lineterminator="\r\n")

        logger.info("Exported %s: %d rows -> %s", symbol, len(out_df), path)
        total_rows += len(out_df)

        row = attributes_ini.build_row(symbol, api, local_df)
        if row is None:
            logger.error("Cannot build attributes.ini row for %s", symbol)
            failed = True
        else:
            ini_rows[row["SYMBOL"]] = row

    if ini_rows:
        attributes_ini.upsert_attributes_ini(output_dir / "attributes.ini", ini_rows)

    if failed:
        print(
            f"TS CSV export failed: {len(ini_rows)}/{len(targets)} symbols complete "
            f"-> {output_dir}",
            file=sys.stderr,
        )
    else:
        print(
            f"TS CSV export complete: {len(targets)} symbols, "
            f"{total_rows} rows -> {output_dir}"
        )
    return 1 if failed else 0

