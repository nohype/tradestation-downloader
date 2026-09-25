"""Fetch symbol metadata from TradeStation API and derive session times from downloaded data."""

import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .auth import TradeStationAuth
from .config import ConfigurationError, load_config
from .storage import create_storage, detect_storage_format

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tradestation.com/v3"

_WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

EXCHANGE_TIMEZONES = {
    "CME": "America/Chicago",
    "CBOT": "America/Chicago",
    "COMEX": "America/New_York",
    "NYMEX": "America/New_York",
    "CBOEF": "America/Chicago",
    "NYSE": "America/New_York",
    "NASDAQ": "America/New_York",
    "AMEX": "America/New_York",
    "ARCX": "America/New_York",
    "ICE": "America/New_York",
    "ICEUS": "America/New_York",
    "IFEU": "Europe/London",
    "ICE FUTURES EUROPE": "Europe/London",
    "EUREX": "Europe/Berlin",
    "EUREX_US": "America/Chicago",
}


def get_exchange_timezone(exchange: str | None) -> str | None:
    """Return the IANA timezone for an exchange, or None if unknown."""
    if not exchange:
        return None
    return EXCHANGE_TIMEZONES.get(exchange.upper())


def convert_df_timezone(df: pd.DataFrame, to_tz: str) -> pd.DataFrame:
    """Convert a DataFrame's datetime index/column to a target timezone (DST-aware per bar)."""
    if df.empty:
        return df
    result = df.copy()
    if isinstance(result.index, pd.DatetimeIndex):
        result.index = result.index.tz_localize("UTC").tz_convert(ZoneInfo(to_tz)).tz_localize(None)
    elif "datetime" in result.columns:
        result["datetime"] = (
            pd.to_datetime(result["datetime"])
            .dt.tz_localize("UTC")
            .dt.tz_convert(ZoneInfo(to_tz))
            .dt.tz_localize(None)
        )
    return result


def select_standard_time_bars(df: pd.DataFrame, tz: str | None) -> pd.DataFrame:
    """Return the subset of ``df`` whose bars fall in the exchange's standard (winter) time.

    Used so session derivation does not collapse DST-shifted maintenance gaps via a
    cross-season minute-of-day union. If winter bars exist they are returned; otherwise
    the DST (summer) bars are returned (still a single, gap-stable season); if ``tz`` is
    None or the frame is empty/unclassifiable, ``df`` is returned unchanged.
    """
    if tz is None or df.empty:
        return df

    if isinstance(df.index, pd.DatetimeIndex):
        idx = df.index
    elif "datetime" in df.columns:
        idx = pd.DatetimeIndex(pd.to_datetime(df["datetime"]))
    else:
        return df

    local_aware = idx.tz_localize("UTC").tz_convert(ZoneInfo(tz))
    # ponytail: this pandas build lacks Series.dt.dst(), so we use Timestamp.dst() per bar.
    # Upgrade path: revert to vectorized `local_aware.to_series().dt.dst()` when available.
    dst = local_aware.map(lambda t: t.dst())
    winter_mask = dst == pd.Timedelta(0)

    if winter_mask.any():
        return df[winter_mask]
    if (~winter_mask).any():
        return df[~winter_mask]
    return df


def fetch_symbol_details(auth: TradeStationAuth, symbols: list[str]) -> list[dict]:
    """Fetch symbol details from the TradeStation API in batches of 50."""
    details: list[dict] = []
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(symbols), 50):
        batch = symbols[i : i + 50]
        symbol_str = ",".join(batch)
        url = f"{BASE_URL}/marketdata/symbols/{symbol_str}"
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.error("Failed to fetch symbol details for %s: %s", symbol_str, e)
            continue
        if response.status_code != 200:
            logger.error(
                "Symbol details request failed for %s: %d %s",
                symbol_str,
                response.status_code,
                response.text,
            )
            continue
        try:
            data = response.json()
        except ValueError as e:
            logger.error("Invalid JSON in symbol details for %s: %s", symbol_str, e)
            continue
        details.extend(data.get("Symbols", []))
        for error in data.get("Errors", []):
            logger.error(
                "Symbol %s details error: %s - %s",
                error.get("Symbol"),
                error.get("Error"),
                error.get("Message"),
            )
        returned_symbols = {s.get("Symbol") for s in data.get("Symbols", [])} | {
            e.get("Symbol") for e in data.get("Errors", [])
        }
        for missing in set(batch) - returned_symbols:
            logger.error("No symbol details returned for %s", missing)
        if i + 50 < len(symbols):
            time.sleep(0.2)
    return details


def fetch_quote_snapshots(auth: TradeStationAuth, symbols: list[str]) -> list[dict]:
    """Fetch quote snapshots from the TradeStation API in batches of 100."""
    quotes: list[dict] = []
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(symbols), 100):
        batch = symbols[i : i + 100]
        symbol_str = ",".join(batch)
        url = f"{BASE_URL}/marketdata/quotes/{symbol_str}"
        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            logger.error("Failed to fetch quote snapshots for %s: %s", symbol_str, e)
            continue
        if response.status_code != 200:
            logger.error(
                "Quote snapshot request failed for %s: %d %s",
                symbol_str,
                response.status_code,
                response.text,
            )
            continue
        try:
            data = response.json()
        except ValueError as e:
            logger.error("Invalid JSON in quote snapshots for %s: %s", symbol_str, e)
            continue
        quotes.extend(data.get("Quotes", []))
        if i + 100 < len(symbols):
            time.sleep(0.2)
    return quotes


def derive_session_times(df: pd.DataFrame) -> dict:
    """Derive session start/end times for each weekday from bar data."""
    if isinstance(df.index, pd.DatetimeIndex):
        series = df.index.to_series().reset_index(drop=True)
    elif "datetime" in df.columns:
        series = pd.to_datetime(df["datetime"]).reset_index(drop=True)
    else:
        return {}

    if series.empty:
        return {}

    dayofweek = series.dt.dayofweek
    times = series.dt.time

    session_times = {}
    for day in range(7):
        mask = dayofweek == day
        if not mask.any():
            continue
        day_times = times[mask]
        session_times[_WEEKDAY_NAMES[day]] = {
            "start": day_times.min().strftime("%H:%M"),
            "end": day_times.max().strftime("%H:%M"),
            "bar_count": int(mask.sum()),
        }
    return session_times


def derive_sessions(df: pd.DataFrame, gap_threshold_minutes: int = 15) -> list[dict]:
    """Derive contiguous trading session blocks from bar data."""
    if isinstance(df.index, pd.DatetimeIndex):
        series = df.index.to_series().reset_index(drop=True)
    elif "datetime" in df.columns:
        series = pd.to_datetime(df["datetime"]).reset_index(drop=True)
    else:
        return []

    if series.empty:
        return []

    minutes = set((series.dt.hour * 60 + series.dt.minute).astype(int).tolist())
    if not minutes:
        return []

    if len(minutes) == 1440:
        return [{"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}]

    sorted_minutes = sorted(minutes)
    gaps = []
    for i in range(len(sorted_minutes)):
        current = sorted_minutes[i]
        next_minute = sorted_minutes[(i + 1) % len(sorted_minutes)]
        gap_size = (next_minute - current - 1) % 1440
        if gap_size > gap_threshold_minutes:
            gaps.append((current, next_minute))

    if not gaps:
        return [{"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}]

    gaps.sort(key=lambda gap: gap[0])
    raw_sessions = []
    for i in range(len(gaps)):
        start_minute = gaps[i][1]
        end_minute = gaps[(i + 1) % len(gaps)][0]
        spans_midnight = start_minute > end_minute
        if spans_midnight:
            duration_minutes = 1440 - start_minute + end_minute + 1
        else:
            duration_minutes = end_minute - start_minute + 1
        raw_sessions.append(
            (start_minute, end_minute, spans_midnight, round(duration_minutes / 60.0, 2))
        )

    raw_sessions.sort(key=lambda session: session[0])
    return [
        {
            "start": f"{start // 60:02d}:{start % 60:02d}",
            "end": f"{end // 60:02d}:{end % 60:02d}",
            "duration_hours": duration,
            "spans_midnight": spans,
        }
        for start, end, spans, duration in raw_sessions
    ]


def run_metadata(config_path: str = "config.yaml") -> int:
    """Fetch metadata from TradeStation API and save to metadata.json."""
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

    symbols = storage.list_symbols()
    if not symbols:
        print("No downloaded symbols found in data directory.", file=sys.stderr)
        return 1

    try:
        auth = TradeStationAuth(
            config.client_id,
            config.client_secret,
            config.refresh_token,
        )
        # Symbols are queried exactly as stored (no rewriting/probing).
        details = fetch_symbol_details(auth, symbols)
        quotes = fetch_quote_snapshots(auth, symbols)
    except Exception as e:
        logger.error("Failed to fetch metadata from TradeStation API: %s", e)
        return 1

    details_by_symbol = {d.get("Symbol"): d for d in details}
    quotes_by_symbol = {q.get("Symbol"): q for q in quotes}

    try:
        symbol_metadata = {}
        for symbol in symbols:
            df = storage.load(symbol)
            first = storage.get_first_timestamp(symbol)
            last = storage.get_last_timestamp(symbol)

            api_data = details_by_symbol.get(symbol)
            exchange = api_data.get("Exchange") if api_data else None
            tz = get_exchange_timezone(exchange)

            if tz is None and exchange is not None and exchange.upper().startswith("ICE"):
                logger.error(
                    "Symbol %s exchange '%s' is not in EXCHANGE_TIMEZONES; "
                    "timezone and local sessions will be null.",
                    symbol,
                    exchange,
                )

            if df is not None and not df.empty:
                canon_df = select_standard_time_bars(df, tz)
                session_times_utc = derive_session_times(canon_df)
                sessions_utc = derive_sessions(canon_df)
                if tz:
                    canon_df_local = convert_df_timezone(canon_df, tz)
                    session_times = derive_session_times(canon_df_local)
                    sessions = derive_sessions(canon_df_local)
                else:
                    session_times = session_times_utc
                    sessions = sessions_utc
            else:
                sessions_utc = []
                session_times_utc = {}
                sessions = []
                session_times = {}

            symbol_metadata[symbol] = {
                "symbol": symbol,
                "api": api_data,
                "quote": quotes_by_symbol.get(symbol),
                "downloaded_data": {
                    "first_timestamp": first.strftime("%Y-%m-%dT%H:%M:%S") if first else None,
                    "last_timestamp": last.strftime("%Y-%m-%dT%H:%M:%S") if last else None,
                    "total_bars": len(df) if df is not None else 0,
                    "exchange_timezone": tz,
                    "session_times": session_times,
                    "session_times_utc": session_times_utc,
                    "sessions": sessions,
                    "sessions_utc": sessions_utc,
                },
            }

        output = {
            "generated_at": datetime.now(UTC)
            .replace(tzinfo=None)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data_dir": str(data_dir),
            "timezone": "UTC",
            "symbols": symbol_metadata,
        }

        json_str = json.dumps(output, indent=2, default=str)
        print(json_str)

        output_path = data_dir / "metadata.json"
        output_path.write_text(json_str, encoding="utf-8")
    except Exception as e:
        logger.error("Unexpected error while building metadata: %s", e)
        return 1

    return 0
