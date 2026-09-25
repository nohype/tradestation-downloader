"""Check whether each symbol's front futures contract should roll to the next contract."""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import requests

from .auth import TradeStationAuth
from .metadata import fetch_symbol_details, resolve_api_symbols
from .models import DownloadConfig

logger = logging.getLogger(__name__)

BASE_URL_V3 = "https://api.tradestation.com/v3"
BASE_URL_V2 = "https://api.tradestation.com/v2"

NA = "n/a"

# Default for --roll-days: roll when the front contract expires this soon.
DEFAULT_ROLL_DAYS = 6


class RollcheckError(Exception):
    """Raised when a roll-check API call fails."""


@dataclass
class RollcheckRow:
    """Roll-check result for a single symbol."""

    symbol: str
    current: str | None = None
    next: str | None = None
    current_oi: int | None = None
    current_vol: int | None = None
    next_oi: int | None = None
    next_vol: int | None = None
    rollover: str = "no"
    warning: bool = False
    warnings: list[str] = field(default_factory=list)
    day: date | None = None
    current_expiry: date | None = None
    next_expiry: date | None = None
    days_to_expiry: int | None = None


def _api_get(auth: TradeStationAuth, url: str, params: dict | None = None):
    """GET a TradeStation API endpoint; on 401 invalidate the token and retry once."""
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 401:
            auth.invalidate()
            headers["Authorization"] = f"Bearer {auth.get_access_token()}"
            response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            raise RollcheckError(f"{response.status_code} {response.text}")
        return response.json()
    except requests.RequestException as e:
        raise RollcheckError(str(e)) from e
    except ValueError as e:
        raise RollcheckError(f"invalid JSON response: {e}") from e


def _parse_datetime(value) -> datetime | None:
    """Parse an API timestamp, returning None on failure.

    Handles both ISO 8601 (e.g. bar ``TimeStamp`` fields) and the legacy
    ASP.NET JSON date format ``/Date(<epoch_ms>)/`` used by the v2
    symbol-search endpoint's ``ExpirationDate``.
    """
    if not value:
        return None
    match = re.fullmatch(r"/Date\((\d+)\)/", str(value))
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_int(value) -> int | None:
    """Parse a string/int volume or open-interest figure."""
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return None


def resolve_roots(auth: TradeStationAuth, symbols: list[str]) -> dict[str, str]:
    """Determine the base contract root for each symbol.

    Prefers the symbol-details endpoint's ``Root`` field; falls back to
    stripping the leading ``@`` and any ``=...`` suffix.
    """
    api_symbols = resolve_api_symbols(auth, symbols)
    details = fetch_symbol_details(auth, list(api_symbols.values()))
    details_by_symbol = {d.get("Symbol"): d for d in details}
    roots = {}
    for symbol in symbols:
        detail = details_by_symbol.get(api_symbols.get(symbol, symbol)) or {}
        root = detail.get("Root")
        if not root:
            root = symbol.lstrip("@").split("=", 1)[0]
            logger.debug("No Root in symbol details for %s; using %s", symbol, root)
        roots[symbol] = root
    return roots


def fetch_contract_chain(auth: TradeStationAuth, root: str) -> list[tuple[str, datetime]]:
    """Return real contracts for a root sorted by expiration date.

    Filters out continuous (@...) and custom (=...) symbols, dotted
    session-variant contracts (e.g. ESZ26.D), and contracts that expired
    before today.
    """
    url = f"{BASE_URL_V2}/data/symbols/search/R={root}&C=Future&FT=Electronic"
    data = _api_get(auth, url)
    if isinstance(data, dict):
        items = data.get("Symbols")
        if items is None:
            items = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        items = data or []
    today = date.today()
    contracts = []
    for item in items:
        name = item.get("Name") or ""
        if name.startswith("@") or "=" in name or "." in name:
            continue
        expiry = _parse_datetime(item.get("ExpirationDate"))
        if expiry is None or expiry.date() < today:
            continue
        contracts.append((name, expiry))
    contracts.sort(key=lambda c: c[1])
    return contracts


def fetch_daily_bars(auth: TradeStationAuth, contract: str) -> list[dict]:
    """Fetch the most recent daily bars for a contract."""
    url = f"{BASE_URL_V3}/marketdata/barcharts/{contract}"
    params = {"unit": "Daily", "interval": 1, "barsback": 10}
    data = _api_get(auth, url, params=params)
    return data.get("Bars") or []


def last_closed_bar(bars: list[dict]) -> dict | None:
    """Return the last bar with BarStatus == 'Closed'."""
    for bar in reversed(bars):
        if bar.get("BarStatus") == "Closed":
            return bar
    return None


def bar_for_day(bars: list[dict], timestamp: datetime) -> dict | None:
    """Find the completed bar matching a timestamp exactly, else the same calendar date.

    Only bars with BarStatus == 'Closed' are considered; a non-Closed match is
    treated as missing data.
    """
    closed = [bar for bar in bars if bar.get("BarStatus") == "Closed"]
    for bar in closed:
        if _parse_datetime(bar.get("TimeStamp")) == timestamp:
            return bar
    for bar in closed:
        ts = _parse_datetime(bar.get("TimeStamp"))
        if ts is not None and ts.date() == timestamp.date():
            return bar
    return None


def check_symbol(
    auth: TradeStationAuth,
    symbol: str,
    root: str,
    roll_days: int = DEFAULT_ROLL_DAYS,
) -> RollcheckRow:
    """Run the roll check for one symbol."""
    row = RollcheckRow(symbol=symbol)
    try:
        chain = fetch_contract_chain(auth, root)
    except RollcheckError as e:
        row.warning = True
        row.warnings.append(f"{symbol}: contract chain lookup failed for root {root}: {e}")
        return row

    if len(chain) < 2:
        row.warning = True
        if chain:
            row.current = chain[0][0]
        row.warnings.append(
            f"{symbol}: contract chain for root {root} has fewer than 2 contracts "
            f"({len(chain)} found)"
        )
        return row

    row.current, row.next = chain[0][0], chain[1][0]
    row.current_expiry = chain[0][1].date()
    row.next_expiry = chain[1][1].date()
    row.days_to_expiry = (row.current_expiry - date.today()).days
    if row.days_to_expiry <= roll_days:
        row.rollover = "YES"

    try:
        current_bars = fetch_daily_bars(auth, row.current)
    except RollcheckError as e:
        row.warning = True
        row.warnings.append(f"{symbol}: failed to fetch daily bars for {row.current}: {e}")
        return row

    try:
        next_bars = fetch_daily_bars(auth, row.next)
    except RollcheckError as e:
        row.warning = True
        row.warnings.append(f"{symbol}: failed to fetch daily bars for {row.next}: {e}")
        return row

    cur_bar = last_closed_bar(current_bars)
    if cur_bar is None or _parse_datetime(cur_bar.get("TimeStamp")) is None:
        row.warning = True
        row.warnings.append(f"{symbol}: no completed daily bar for current contract {row.current}")
        if next_bars:
            next_bar = last_closed_bar(next_bars)
            if next_bar is not None:
                row.next_oi = _parse_int(next_bar.get("OpenInterest"))
                row.next_vol = _parse_int(next_bar.get("TotalVolume"))
        return row

    cur_ts = _parse_datetime(cur_bar["TimeStamp"])
    row.day = cur_ts.date()
    row.current_oi = _parse_int(cur_bar.get("OpenInterest"))
    row.current_vol = _parse_int(cur_bar.get("TotalVolume"))

    next_bar = bar_for_day(next_bars, cur_ts)
    if next_bar is None:
        row.warning = True
        row.warnings.append(
            f"{symbol}: next contract {row.next} has no bar for {row.day} "
            "(last completed day); next figures shown as n/a"
        )
        return row

    row.next_oi = _parse_int(next_bar.get("OpenInterest"))
    row.next_vol = _parse_int(next_bar.get("TotalVolume"))

    oi_higher = (
        row.next_oi is not None and row.current_oi is not None and row.next_oi > row.current_oi
    )
    vol_higher = (
        row.next_vol is not None and row.current_vol is not None and row.next_vol > row.current_vol
    )
    if oi_higher or vol_higher:
        row.rollover = "YES"
    return row


_MONTH_CODES = "FGHJKMNQUVXZ"
_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def describe_contract(name: str | None, expiry: date | None) -> str:
    """Describe a contract as '<Mon YYYY>' (e.g. 'Nov 2026').

    Prefers the contract symbol's month code, which is the contract month even
    when expiration falls in the prior month (e.g. CL); falls back to the
    expiration date, then n/a.
    """
    if name:
        match = re.fullmatch(r"[A-Z]+([FGHJKMNQUVXZ])(\d{1,2})", name)
        if match:
            month = _MONTH_NAMES[_MONTH_CODES.index(match.group(1))]
            return f"{month} {2000 + int(match.group(2))}"
    if expiry is not None:
        return expiry.strftime("%b %Y")
    return NA


def _fmt_ratio(next_value: int | None, current_value: int | None) -> str:
    """Format next/current as a percentage with 1 decimal."""
    if next_value is None or current_value is None:
        return NA
    if current_value == 0:
        return "inf" if next_value > 0 else NA
    return f"{next_value / current_value * 100:.1f}%"


def _print_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str] | None = None,
) -> None:
    """Print a plain ASCII table with | separators.

    aligns: per-column '<' (left, default) or '>' (right) alignment.
    """
    if aligns is None:
        aligns = ["<"] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cell: str, i: int) -> str:
        return cell.rjust(widths[i]) if aligns[i] == ">" else cell.ljust(widths[i])

    print(" | ".join(fmt(h, i) for i, h in enumerate(headers)))
    print("-+-".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print(" | ".join(fmt(cell, i) for i, cell in enumerate(row)))


def run_rollcheck(config: DownloadConfig) -> int:
    """Check whether each symbol's front contract should roll to the next, then exit."""
    symbols = config.symbols
    if not symbols:
        logger.error("No symbols configured")
        return 1

    try:
        auth = TradeStationAuth(
            config.client_id,
            config.client_secret,
            config.refresh_token,
        )
        roots = resolve_roots(auth, symbols)
    except Exception as e:
        logger.error("Failed to resolve contract roots: %s", e)
        return 1

    rows = []
    for i, symbol in enumerate(symbols):
        try:
            rows.append(check_symbol(auth, symbol, roots[symbol], config.roll_days))
        except Exception as e:
            logger.error("Roll check failed for %s: %s", symbol, e)
            row = RollcheckRow(symbol=symbol, warning=True)
            row.warnings.append(f"{symbol}: roll check failed: {e}")
            rows.append(row)
        if i < len(symbols) - 1:
            time.sleep(config.rate_limit_delay)

    if not rows:
        return 1

    # Header: run date, data day (when all resolved symbols agree), the rule
    # with its days-to-expiry threshold, and the headline result.
    days = [r.day for r in rows if r.day is not None]
    roll_count = sum(1 for r in rows if r.rollover == "YES")
    print(f"Rollover check - {date.today().isoformat()}")
    if days:
        data_day = days[0].isoformat() if len(set(days)) == 1 else "varies by symbol"
        print(f"Data day: {data_day}")
    print(
        f"Roll rule: next OI or Vol higher, or current contract expires "
        f"within {config.roll_days} days"
    )
    print(
        f"Roll recommended: {roll_count} of {len(rows)} "
        f"symbol{'' if len(rows) == 1 else 's'}"
    )
    print()

    table = []
    for row in rows:
        current_cell = (
            f"{row.current} {describe_contract(row.current, row.current_expiry)}"
            if row.current
            else NA
        )
        next_cell = (
            f"{row.next} {describe_contract(row.next, row.next_expiry)}" if row.next else NA
        )
        if row.rollover == "YES":
            action = f"ROLL -> {row.next}" if row.next else "ROLL"
        elif row.current:
            action = f"keep {row.current}"
        else:
            action = NA
        table.append(
            [
                row.symbol + (" (!)" if row.warning else ""),
                current_cell,
                next_cell,
                _fmt_ratio(row.next_oi, row.current_oi),
                _fmt_ratio(row.next_vol, row.current_vol),
                row.current_expiry.isoformat() if row.current_expiry else NA,
                str(row.days_to_expiry) if row.days_to_expiry is not None else NA,
                action,
            ]
        )

    _print_table(
        ["Symbol", "Current", "Next", "OI next/cur", "Vol next/cur", "Expires", "Days", "Action"],
        table,
        aligns=["<", "<", "<", ">", ">", "<", ">", "<"],
    )

    all_warnings = [w for row in rows for w in row.warnings]
    if all_warnings:
        print()
        print("Warnings")
        print("=" * 60)
        for warning in all_warnings:
            print(f"  (!) {warning}")

    processed = sum(1 for row in rows if row.current is not None or row.day is not None)
    return 0 if processed > 0 else 1
