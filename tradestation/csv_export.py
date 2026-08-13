"""Export downloaded parquet data to CSV .txt files."""

import logging
import sys
from pathlib import Path

import pandas as pd

from .config import ConfigurationError, load_config
from .models import validate_symbol
from .storage import create_storage, detect_storage_format

logger = logging.getLogger(__name__)


def _normalize_symbol(symbol: str) -> str:
    """Normalize a symbol to its @-prefixed form."""
    return f"@{symbol.lstrip('@')}"


def run_export_csv(config_path: str, symbols: list[str] | None = None) -> int:
    """Export downloaded parquet data to CSV .txt files. Returns 0 success, 1 error."""
    try:
        config = load_config(config_path)
    except ConfigurationError as e:
        logger.error(str(e))
        return 1

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
        return 1

    if symbols is not None:
        for requested in symbols:
            validate_symbol(requested)

    available = storage.list_symbols()
    if not available:
        logger.error("No data found in %s", data_dir)
        print(f"No data found in {data_dir}", file=sys.stderr)
        return 1

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
                return 1
            targets.append(normalized_available[normalized])

    output_dir = data_dir.parent / "plain_data"
    output_dir.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    for symbol in targets:
        df = storage.load(symbol)
        if df is None or df.empty:
            logger.warning("No data for %s; skipping", symbol)
            continue

        if isinstance(df.index, pd.DatetimeIndex):
            df = df.reset_index(names=["datetime"])

        df["Date"] = df["datetime"].dt.strftime("%m/%d/%Y")
        df["Time"] = df["datetime"].dt.strftime("%H:%M")

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

        for col in ["open_interest", "total_ticks"]:
            if col in df.columns:
                output_columns.append(col)

        out_df = df.rename(columns=rename_map)[output_columns]

        path = output_dir / f"{symbol}.txt"
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write('"' + '","'.join(output_columns) + '"\r\n')
            out_df.to_csv(f, header=False, index=False, lineterminator="\r\n")

        logger.info("Exported %s: %d rows -> %s", symbol, len(out_df), path)
        total_rows += len(out_df)

    print(f"CSV export complete: {len(targets)} symbols, {total_rows} rows -> {output_dir}")
    return 0

