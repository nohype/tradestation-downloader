"""Shared CSV formatting and parsing for plain and TradeStation CSV outputs."""

from datetime import datetime
from pathlib import Path

import pandas as pd

_PLAIN_HEADER = '"Date","Time","Open","High","Low","Close"'
_PLAIN_COLUMNS = ["Date", "Time", "Open", "High", "Low", "Close"]

_PLAIN_RENAME = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Up": "up_volume",
    "Down": "down_volume",
    "Upticks": "up_ticks",
    "Downticks": "down_ticks",
    "Vol": "volume",
    "OpenInterest": "open_interest",
    "TotalTicks": "total_ticks",
}

_TS_HEADER = '"Date","Time","Open","High","Low","Close","Volume"'
_TS_COLUMNS = ["Date", "Time", "Open", "High", "Low", "Close", "Volume"]

_TS_RENAME = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "OpenInt": "open_interest",
}


def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure a datetime column and add formatted Date/Time columns."""
    if isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index(names=["datetime"])

    df["Date"] = df["datetime"].dt.strftime("%m/%d/%Y")
    df["Time"] = df["datetime"].dt.strftime("%H:%M")
    return df


def build_plain_csv_output(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
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


def _parse_csv_datetime(date: str, time: str) -> datetime:
    """Parse MM/DD/YYYY and HH:MM into a naive datetime."""
    return datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M")


def parse_plain_csv(path: Path) -> pd.DataFrame | None:
    """Parse a standard CSV .txt file back into the internal DataFrame format."""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        return None

    if df.empty:
        return None

    df["datetime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"], format="%m/%d/%Y %H:%M"
    )
    df = df.drop(columns=["Date", "Time"])
    df = df.rename(columns=_PLAIN_RENAME)

    numeric_cols = [
        "open", "high", "low", "close", "volume",
        "up_volume", "down_volume", "up_ticks", "down_ticks",
        "open_interest", "total_ticks",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            if col in ("open_interest", "total_ticks"):
                df[col] = df[col].astype("Int64")

    # Move datetime to the first column to match the internal format.
    cols = ["datetime"] + [c for c in df.columns if c != "datetime"]
    df = df[cols]

    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    return df.reset_index(drop=True)


def build_ts_csv_output(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
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


def parse_ts_csv(path: Path, timezone: str | None = None) -> pd.DataFrame | None:
    """Parse a TradeStation ASCII .txt file back into the internal DataFrame format.

    The file is expected to be in exchange-local time; if ``timezone`` is
    supplied the parsed datetime is converted from that timezone to UTC.
    """
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        return None

    if df.empty:
        return None

    df["datetime"] = pd.to_datetime(
        df["Date"] + " " + df["Time"], format="%m/%d/%Y %H:%M"
    )
    df = df.drop(columns=["Date", "Time"])
    df = df.rename(columns=_TS_RENAME)

    if timezone is not None:
        from zoneinfo import ZoneInfo
        df["datetime"] = (
            df["datetime"]
            .dt.tz_localize(ZoneInfo(timezone))
            .dt.tz_convert("UTC")
            .dt.tz_localize(None)
        )

    numeric_cols = ["open", "high", "low", "close", "volume", "open_interest"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
            if col == "open_interest":
                df[col] = df[col].astype("Int64")

    # Move datetime to the first column to match the internal format.
    cols = ["datetime"] + [c for c in df.columns if c != "datetime"]
    df = df[cols]

    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    return df.reset_index(drop=True)
