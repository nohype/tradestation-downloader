"""Download TradeStation order history into a per-account Parquet cache and CSV export.

Self-contained orders feature module (``tradestation-download orders``): resolves
the brokerage account, incrementally fetches historical and current orders, and
writes a CSV export derived from the persisted cache.
"""

import json
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from .auth import AuthenticationError, TradeStationAuth
from .models import DownloadConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tradestation.com/v3"
ORDERS_ROOT = Path("orders")           # cwd-relative; config.data_dir ignored (PM ruling)
PAGE_SIZE = 600
DETAIL_BATCH_SIZE = 50
RETENTION_DAYS = 729                   # conservative vs API ~730d (PM ruling 3)
CURRENT_FRESHNESS = timedelta(minutes=5)
OVERLAP = timedelta(days=2)

ORDER_COLUMNS = [
    "datetime", "AccountID", "OrderID", "OrderType", "OpenedDateTime",
    "ClosedDateTime", "Duration", "GoodTillDate", "LimitPrice", "FilledPrice",
    "PriceUsedForBuyingPower", "CommissionFee", "Currency", "ConversionRate",
    "Routing", "RejectReason", "ShowOnlyQuantity", "Spread", "GroupName",
    "AdvancedOptions", "Status", "StatusDescription", "StopPrice",
    "UnbundledRouteFee", "TrailingStop", "ConditionalOrders", "Legs",
    "MarketActivationRules", "TimeActivationRules",
]  # canonical 29 columns, fixed order; observed extras appended sorted

# API-documented top-level order fields — diagnostic basis for extra-field detection
DOCUMENTED_FIELDS = frozenset(ORDER_COLUMNS) - {"datetime"}

DATETIME_COLUMNS = ["datetime", "OpenedDateTime", "ClosedDateTime", "GoodTillDate"]
NESTED_COLUMNS = [
    "TrailingStop", "ConditionalOrders", "Legs",
    "MarketActivationRules", "TimeActivationRules",
]

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class OrdersError(Exception):
    """Raised when an orders API call, cache operation, or export fails."""


class AccountResolutionError(Exception):
    """Raised when the brokerage account cannot be selected (domain failure)."""


@dataclass
class Coverage:
    """One contiguous covered interval of fetched historical orders.

    Attributes:
        since: Earliest successfully fetched ``since`` boundary
        through: UTC instant the historical listing was complete to
        completed_at: Completion time of that full pagination
    """

    since: date
    through: datetime
    completed_at: datetime


@dataclass
class FetchPlan:
    """Fetch plan for a single orders run, produced by ``_build_fetch_plan``.

    Attributes:
        historical_since: ``since`` boundary for the historical traversal,
            or None on a zero-call plan
        fetch_current_orders: Whether to fetch the ``/orders`` open snapshot
        reconcile_open_ids: Cached open OrderIDs to reconcile by ID
        reason: Human-readable explanation of the plan
    """

    historical_since: date | None
    fetch_current_orders: bool
    reconcile_open_ids: list[str]
    reason: str


def _validate_account_id(account_id: str) -> str:
    """Validate an account ID for use in cache paths; return it unchanged.

    Rejects empty IDs, ``.``/``..``, and anything outside ``[A-Za-z0-9_.-]``
    so ``orders/cache/<account_id>/`` cannot escape the cache root.
    """
    if not account_id or account_id in (".", "..") or not _ACCOUNT_ID_RE.match(account_id):
        raise OrdersError(f"Invalid account ID: {account_id!r}")
    return account_id


def _account_cache_dir(account_id: str) -> Path:
    """Return the cache directory for an account: ``orders/cache/<account_id>``."""
    return ORDERS_ROOT / "cache" / account_id


def _api_get(
    auth: TradeStationAuth,
    url: str,
    params: dict | None = None,
    *,
    max_retries: int,
    retry: int = 0,
    auth_retried: bool = False,
) -> dict[str, Any]:
    """GET a TradeStation API endpoint with retry/backoff; raise on terminal failure.

    Mirrors ``downloader._api_request`` semantics but never returns None — a
    failed request must not be mistaken for an empty page:

    - 429 → honor ``Retry-After`` (else 60s), up to ``max_retries``
    - 401 → invalidate the token and retry exactly once, then
      ``AuthenticationError``
    - 5xx → exponential backoff ``2**retry``, up to ``max_retries``, then
      ``OrdersError`` with status and a bounded body excerpt
    - other ``requests.RequestException`` → exponential backoff ``2**retry``,
      then ``OrdersError``
    - 404 → ``OrdersError`` (account or resource not found)
    - other non-2xx → ``OrdersError`` with status and a bounded body excerpt
    - invalid JSON → ``OrdersError``

    On HTTP 200, each entry in the payload's ``Errors[]`` list is logged as a
    warning. ``OrdersError`` is raised only when account-level errors leave no
    usable ``Orders``/``NextToken``/``Accounts`` payload.
    """
    headers = {
        "Authorization": f"Bearer {auth.get_access_token()}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=60)

        if resp.status_code == 429:
            if retry >= max_retries:
                raise OrdersError(f"Rate limited after {max_retries} retries: {url}")
            try:
                wait = int(resp.headers.get("Retry-After", 60))
            except (TypeError, ValueError):
                wait = 60
                logger.debug(
                    "Malformed Retry-After header %r; using %ds default",
                    resp.headers.get("Retry-After"), wait,
                )
            rate_headers = {
                k: v for k, v in resp.headers.items() if k.startswith("X-RateLimit-")
            }
            logger.warning(
                "Rate limited, waiting %ds... (%s)", wait, rate_headers or "no X-RateLimit headers"
            )
            time.sleep(wait)
            return _api_get(auth, url, params, max_retries=max_retries, retry=retry + 1)

        if resp.status_code == 401:
            if auth_retried:
                raise AuthenticationError(
                    "Authentication failed: access token rejected by TradeStation API"
                )
            logger.info("Token rejected, refreshing and retrying once...")
            auth.invalidate()
            return _api_get(
                auth, url, params,
                max_retries=max_retries, retry=retry + 1, auth_retried=True,
            )

        if resp.status_code == 404:
            raise OrdersError(f"Account or resource not found (404): {url}")

        if resp.status_code >= 500:
            if retry >= max_retries:
                raise OrdersError(
                    f"API request failed ({resp.status_code}) for {url} "
                    f"after {max_retries} retries: {resp.text[:200]}"
                )
            wait = 2 ** retry
            logger.warning(
                "Server error %d for %s. Retrying in %ds...",
                resp.status_code, url, wait,
            )
            time.sleep(wait)
            return _api_get(auth, url, params, max_retries=max_retries, retry=retry + 1)

        if not resp.ok:
            raise OrdersError(
                f"API request failed ({resp.status_code}) for {url}: {resp.text[:200]}"
            )

    except requests.RequestException as e:
        if retry < max_retries:
            wait = 2 ** retry
            logger.warning("Request failed: %s. Retrying in %ds...", e, wait)
            time.sleep(wait)
            return _api_get(auth, url, params, max_retries=max_retries, retry=retry + 1)
        raise OrdersError(f"Request failed after {max_retries} retries: {e}") from e

    try:
        data = resp.json()
    except ValueError as e:
        raise OrdersError(f"Invalid JSON response from {url}: {e}") from e

    errors = data.get("Errors") if isinstance(data, dict) else None
    if errors:
        for err in errors:
            logger.warning("API reported error for %s: %s", url, err)
        if not any(data.get(key) for key in ("Orders", "NextToken", "Accounts")):
            raise OrdersError(f"API returned errors with no usable payload for {url}: {errors}")

    return data


def _format_account_table(accounts: list[dict]) -> str:
    """Format accounts as a compact table for error/log messages.

    Columns include AccountID plus the identifying fields the API returns
    (Alias, AccountType, Status, Currency); fields absent from every account
    are omitted.
    """
    columns = ["AccountID", "Alias", "AccountType", "Status", "Currency"]
    present = [c for c in columns if c == "AccountID" or any(a.get(c) for a in accounts)]

    lines = [" | ".join(present)]
    lines.append("-+-".join("-" * len(c) for c in present))
    for account in accounts:
        lines.append(" | ".join(str(account.get(c) or "") for c in present))
    return "\n".join(lines)


def _describe_account(account: dict) -> str:
    """Build the parenthetical description for the 'Selected account' log line."""
    parts = [str(account[k]) for k in ("AccountType", "Status") if account.get(k)]
    return f" ({', '.join(parts)})" if parts else ""


def resolve_account_id(auth: TradeStationAuth, requested: str | None) -> str:
    """Resolve the account ID to use, auto-selecting when there is exactly one.

    Args:
        auth: TradeStationAuth instance (single OAuth session shared with the run)
        requested: Account ID from ``-a/--account``, or None for auto-select

    Returns:
        The selected account ID

    Raises:
        AccountResolutionError: No accounts returned; ``requested`` not among the
            accounts; or multiple accounts with no ``requested`` ID
        AuthenticationError: Propagated unchanged from the API call
        OrdersError: Propagated unchanged from the API call
    """
    data = _api_get(auth, f"{BASE_URL}/brokerage/accounts", {}, max_retries=3)
    accounts = data.get("Accounts") or []

    if not accounts:
        raise AccountResolutionError("No brokerage accounts returned by the TradeStation API")

    by_id = {str(a.get("AccountID") or ""): a for a in accounts}

    if requested is not None:
        if requested not in by_id:
            raise AccountResolutionError(
                f"Account {requested!r} not found. Available accounts:\n"
                f"{_format_account_table(accounts)}"
            )
        selected = by_id[requested]
    elif len(accounts) == 1:
        selected = accounts[0]
    else:
        raise AccountResolutionError(
            "Multiple brokerage accounts found; use -a/--account to select one.\n"
            f"{_format_account_table(accounts)}"
        )

    account_id = str(selected.get("AccountID") or "")
    logger.info("Selected account %s%s", account_id, _describe_account(selected))
    return account_id


# ---------------------------------------------------------------------------
# Part B — data layer: frame construction, cache/coverage persistence,
# fetch planning, merge, and CSV export.
# ---------------------------------------------------------------------------


def _json_cell(value: Any) -> Any:
    """Serialize dict/list cell values to canonical compact JSON; pass through the rest."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return value


def _orders_to_dataframe(orders: list[dict]) -> pd.DataFrame:
    """Build a canonical-schema DataFrame from raw order dicts.

    ``datetime`` = ``ClosedDateTime`` if present else ``OpenedDateTime``.
    DATETIME_COLUMNS are parsed via ``pd.to_datetime(..., utc=True,
    errors="coerce").dt.tz_convert(None)``; non-empty raw values that coerce to
    NaT log a warning naming the OrderID and keep the row. NESTED_COLUMNS and
    any dict/list-typed extra fields are serialized to compact JSON; absent
    values stay null. Every non-datetime column is cast to ``pd.StringDtype()``
    and columns are reindexed to ``ORDER_COLUMNS + sorted(extras)`` where
    extras are observed top-level keys not in DOCUMENTED_FIELDS.
    """
    rows = []
    extras: set[str] = set()
    for order in orders:
        observed_extra = set(order) - set(DOCUMENTED_FIELDS)
        if observed_extra:
            logger.debug(
                "Order %s has undocumented fields: %s",
                order.get("OrderID"),
                sorted(observed_extra),
            )
            extras |= observed_extra
        row = dict(order)
        closed = order.get("ClosedDateTime")
        row["datetime"] = closed if closed is not None else order.get("OpenedDateTime")
        rows.append(row)

    columns = ORDER_COLUMNS + sorted(extras)
    df = pd.DataFrame(rows).reindex(columns=columns)

    for col in NESTED_COLUMNS + sorted(extras):
        df[col] = df[col].map(_json_cell)

    for col in DATETIME_COLUMNS:
        raw = df[col]
        converted = pd.to_datetime(raw, utc=True, errors="coerce")
        if converted.dt.tz is not None:
            converted = converted.dt.tz_convert(None)
        bad = converted.isna() & raw.notna() & (raw != "")
        for idx in df.index[bad]:
            logger.warning(
                "Order %s: could not parse %s=%r; storing NaT",
                df.at[idx, "OrderID"], col, raw.at[idx],
            )
        df[col] = converted

    for col in df.columns:
        if col not in DATETIME_COLUMNS:
            df[col] = df[col].astype(pd.StringDtype())

    return df.reindex(columns=columns)


def _empty_orders_dataframe() -> pd.DataFrame:
    """Return an empty frame with the canonical 29-column orders schema."""
    return pd.DataFrame({
        col: pd.Series(dtype="datetime64[ns]" if col in DATETIME_COLUMNS else "string")
        for col in ORDER_COLUMNS
    })


def _load_cache(cache_dir: Path) -> pd.DataFrame:
    """Load ``orders.parquet`` from ``cache_dir``; return an empty frame if absent.

    Raises:
        OrdersError: The parquet file exists but cannot be read.
    """
    path = cache_dir / "orders.parquet"
    if not path.exists():
        return _empty_orders_dataframe()
    try:
        return pd.read_parquet(path)
    except Exception as e:
        raise OrdersError(
            f"Order cache {path} is unreadable; rerun with --full to rebuild ({e})"
        ) from e


def _save_cache(cache_dir: Path, df: pd.DataFrame) -> None:
    """Write ``orders.parquet`` atomically (tmp file + ``Path.replace``)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "orders.parquet"
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        df.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _parse_coverage_datetime(value: str) -> datetime:
    """Parse a ``Z``-suffixed ISO datetime from coverage.json to naive UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _load_coverage(cache_dir: Path, account_id: str) -> Coverage | None:
    """Load ``coverage.json``; return None (+ warning) if missing/corrupt/mismatched."""
    path = cache_dir / "coverage.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("account_id") != account_id:
            raise ValueError(f"coverage is for account {data.get('account_id')!r}")
        return Coverage(
            since=date.fromisoformat(data["historical_since"]),
            through=_parse_coverage_datetime(data["historical_through"]),
            completed_at=_parse_coverage_datetime(data["completed_at"]),
        )
    except Exception as e:
        logger.warning("Ignoring unreadable coverage metadata %s: %s", path, e)
        return None


def _save_coverage(cache_dir: Path, coverage: Coverage, account_id: str) -> None:
    """Write ``coverage.json`` atomically (tmp file + ``Path.replace``)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "account_id": account_id,
        "historical_since": coverage.since.isoformat(),
        "historical_through": coverage.through.isoformat() + "Z",
        "completed_at": coverage.completed_at.isoformat() + "Z",
    }
    path = cache_dir / "coverage.json"
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _build_fetch_plan(
    requested_start: date,
    requested_end: date,
    today: date,
    now: datetime,
    coverage: Coverage | None,
    cached_open_ids: list[str],
    *,
    include_open: bool,
) -> FetchPlan:
    """Compute the fetch plan per the §5 decision table.

    Definitions: ``earliest_supported = today - RETENTION_DAYS``;
    ``R0 = max(requested_start, earliest_supported)``; ``R1x = requested_end +
    1 day`` — the request's exclusive upper bound at 00:00 UTC. Both zero-call
    rules (default mode only) are checked FIRST and short-circuit to a plan
    with all fetch fields off. Otherwise ``historical_since`` is ``R0`` (no
    coverage or backfill) or ``max(earliest_supported, coverage.through.date()
    - OVERLAP)``, ``fetch_current_orders = include_open or (R1x > today)``,
    and the cached open IDs are reconciled.

    Comparison conventions: ``R1x`` and ``today`` are compared as dates;
    ``R1x <= CT`` is evaluated as ``R1x <= coverage.through.date()`` (the
    covered top is an instant whose date bounds any covered exclusive bound);
    ``now - coverage.completed_at`` is a datetime subtraction. The clamped-R0
    warning is emitted by ``run_orders``, not here.
    """
    earliest_supported = today - timedelta(days=RETENTION_DAYS)
    r0 = max(requested_start, earliest_supported)
    r1x = requested_end + timedelta(days=1)

    if coverage is not None:
        base_covered = r0 >= coverage.since

        if not include_open:
            # Zero-call rules first — a hit makes no brokerage call at all.
            # Deliberate asymmetry (plan §3 PM note): the past-only rule also
            # requires empty ``cached_open_ids`` (it doubles as forced
            # reconciliation), while the today-spanning rule does not — fresh
            # coverage makes immediate reconciliation low-value there.
            if (
                base_covered
                and r1x <= today
                and r1x <= coverage.through.date()
                and not cached_open_ids
            ):
                return FetchPlan(
                    historical_since=None,
                    fetch_current_orders=False,
                    reconcile_open_ids=[],
                    reason="zero-call: past-only request fully covered by cache",
                )
            if (
                base_covered
                and r1x > today
                and now - coverage.completed_at <= CURRENT_FRESHNESS
            ):
                return FetchPlan(
                    historical_since=None,
                    fetch_current_orders=False,
                    reconcile_open_ids=[],
                    reason="zero-call: today-spanning request within fresh coverage window",
                )

        if r0 < coverage.since:
            historical_since = r0
            reason = "backfill: requested start precedes covered interval"
        else:
            historical_since = max(earliest_supported, coverage.through.date() - OVERLAP)
            reason = "refresh: extend covered interval from overlap boundary"
    else:
        historical_since = r0
        reason = "no coverage: full historical traversal"

    return FetchPlan(
        historical_since=historical_since,
        fetch_current_orders=include_open or (r1x > today),
        reconcile_open_ids=list(cached_open_ids),
        reason=reason,
    )


def _merge_orders(cached: pd.DataFrame, fetched: list[pd.DataFrame]) -> pd.DataFrame:
    """Merge fetched frames into the cached frame per §5 precedence.

    ``_fetch_seq`` is 0 for cached rows and increases per fetched frame. Among
    rows sharing a non-null ``OrderID``: (a) non-null ClosedDateTime beats
    null; (b) same class → greater ``_fetch_seq``; (c) same frame → last
    occurrence wins. Null-OrderID rows are kept (with a warning) and deduped
    only on exact full-row duplicates (``keep="last"``). Columns are reindexed
    to ``ORDER_COLUMNS + sorted(all observed extras)``, rows are sorted by
    ``["datetime", "OrderID"]`` (stable, NaN last), and ``_fetch_seq`` dropped.
    """
    frames = [cached.assign(_fetch_seq=0)]
    frames += [frame.assign(_fetch_seq=i + 1) for i, frame in enumerate(fetched)]

    extras = sorted(
        set().union(*(set(f.columns) for f in frames)) - set(ORDER_COLUMNS) - {"_fetch_seq"}
    )
    all_columns = ORDER_COLUMNS + extras + ["_fetch_seq"]

    def _reindex_to_schema(f: pd.DataFrame) -> pd.DataFrame:
        """Reindex to ``all_columns`` with explicit schema dtypes.

        Columns absent from ``f`` come in all-NA; casting each column to its
        canonical dtype (``datetime64[ns]`` for DATETIME_COLUMNS, ``string``
        otherwise) keeps pandas' deprecated all-NA dtype inference out of the
        concat and guarantees no object-dtype column reaches parquet.
        """
        f = f.reindex(columns=all_columns)
        for col in f.columns:
            if col == "_fetch_seq":
                continue
            f[col] = f[col].astype(
                "datetime64[ns]" if col in DATETIME_COLUMNS else pd.StringDtype()
            )
        return f

    nonempty = [_reindex_to_schema(f) for f in frames if len(f)]
    if not nonempty:
        empty = _empty_orders_dataframe().reindex(columns=ORDER_COLUMNS + extras)
        for col in extras:
            empty[col] = empty[col].astype(pd.StringDtype())
        return empty
    merged = pd.concat(nonempty, ignore_index=True)

    has_id = merged["OrderID"].notna()

    null_rows = merged[~has_id]
    if not null_rows.empty:
        logger.warning(
            "Keeping %d order rows with null OrderID (no ID-based dedupe possible)",
            len(null_rows),
        )
        null_rows = null_rows.drop_duplicates(keep="last")

    id_rows = merged[has_id].copy()
    # Sort ascending by precedence keys and keep="last": closed-beats-open via
    # _closed_rank, newer frame via _fetch_seq, last-in-frame via position.
    id_rows["_closed_rank"] = id_rows["ClosedDateTime"].notna().astype(int)
    id_rows["_pos"] = range(len(id_rows))
    id_rows = id_rows.sort_values(
        ["_closed_rank", "_fetch_seq", "_pos"], kind="stable"
    ).drop_duplicates("OrderID", keep="last")
    id_rows = id_rows.drop(columns=["_closed_rank", "_pos"])

    merged = pd.concat([id_rows, null_rows], ignore_index=True)
    merged = merged.sort_values(
        ["datetime", "OrderID"], na_position="last", kind="stable"
    )
    return merged.drop(columns=["_fetch_seq"]).reindex(
        columns=ORDER_COLUMNS + extras
    ).reset_index(drop=True)


def _write_orders_csv(
    cache_df: pd.DataFrame,
    start: date,
    end: date,
    output_path: Path,
    *,
    include_open: bool,
) -> int:
    """Write the CSV export for inclusive date range [start, end]; return row count.

    Filters on explicit ``pd.Timestamp`` bounds (``[start 00:00, end+1d
    00:00)``), drops open rows unless ``include_open``, re-sorts defensively,
    and writes atomically via a ``*.tmp`` sibling + ``Path.replace``.
    """
    # `start`/`end` are INCLUSIVE calendar DATES; the comparison bound is the
    # EXCLUSIVE timestamp one day past `end` (so the whole end day is included)
    start_ts = pd.Timestamp(start)
    end_exclusive = pd.Timestamp(end) + pd.Timedelta(days=1)
    filtered = cache_df[(cache_df["datetime"] >= start_ts) & (cache_df["datetime"] < end_exclusive)]
    if not include_open:
        filtered = filtered[filtered["ClosedDateTime"].notna()]
    filtered = filtered.sort_values(
        ["datetime", "OrderID"], kind="stable", na_position="last"
    )

    tmp_path = output_path.with_name(output_path.name + ".tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        filtered.to_csv(tmp_path, index=False, header=True, encoding="utf-8",
                        lineterminator="\n", date_format="%Y-%m-%dT%H:%M:%S.%f")
        tmp_path.replace(output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return len(filtered)


# ---------------------------------------------------------------------------
# Part C — API fetchers and orchestration.
# ---------------------------------------------------------------------------


def _fetch_orders_pages(
    auth: TradeStationAuth,
    url: str,
    params: dict,
    *,
    max_retries: int,
    rate_limit_delay: float,
    on_page: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Paginate an orders endpoint; return the concatenated ``Orders`` list.

    Per §5 pagination rules: ``params`` are repeated on every page with the
    returned ``nextToken`` cursor added; a missing/null/empty ``NextToken``
    ends the traversal; a token identical to the one just used raises
    ``OrdersError`` (infinite-loop guard); ``rate_limit_delay`` is slept
    BETWEEN pages only. ``on_page`` (if given) is invoked per page with that
    page's order list — absent ``Orders`` counts as an empty page.
    """
    orders: list[dict] = []
    token: str | None = None
    page = 0
    while True:
        if token is not None:
            time.sleep(rate_limit_delay)
        page += 1
        logger.info("Fetching orders page %d (%d/page)", page, PAGE_SIZE)
        page_params = dict(params)
        if token is not None:
            page_params["nextToken"] = token
        data = _api_get(auth, url, page_params, max_retries=max_retries)
        page_orders = data.get("Orders") or []
        orders.extend(page_orders)
        if on_page is not None:
            on_page(page_orders)
        next_token = data.get("NextToken")
        if not next_token:
            return orders
        if next_token == token:
            raise OrdersError(
                f"API returned identical NextToken on page {page}; "
                f"aborting pagination for {url}"
            )
        token = next_token


def _fetch_historical_orders(
    auth: TradeStationAuth,
    account_id: str,
    since: date,
    *,
    max_retries: int,
    rate_limit_delay: float,
    on_page: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Fetch closed orders from ``/historicalorders``, paging from ``since``."""
    url = f"{BASE_URL}/brokerage/accounts/{account_id}/historicalorders"
    params = {"since": since.isoformat(), "pageSize": PAGE_SIZE}
    return _fetch_orders_pages(
        auth, url, params,
        max_retries=max_retries, rate_limit_delay=rate_limit_delay,
        on_page=on_page,
    )


def _fetch_current_orders(
    auth: TradeStationAuth,
    account_id: str,
    *,
    max_retries: int,
    rate_limit_delay: float,
    on_page: Callable[[list[dict]], None] | None = None,
) -> list[dict]:
    """Fetch today's + open orders from ``/orders`` (no ``since`` param)."""
    url = f"{BASE_URL}/brokerage/accounts/{account_id}/orders"
    params = {"pageSize": PAGE_SIZE}
    return _fetch_orders_pages(
        auth, url, params,
        max_retries=max_retries, rate_limit_delay=rate_limit_delay,
        on_page=on_page,
    )


def _fetch_historical_order_ids(
    auth: TradeStationAuth,
    account_id: str,
    order_ids: list[str],
    since: date,
    *,
    max_retries: int,
    rate_limit_delay: float,  # noqa: ARG001 - signature parity; single call, caller paces
) -> list[dict]:
    """Fetch specific orders by ID from ``/historicalorders/{orderIds}``.

    Single unpaginated call (§2): ``orderIds`` is a comma-separated path
    segment of at most 50 IDs; ``since`` is required. Returns ``Orders``
    or an empty list.
    """
    url = (
        f"{BASE_URL}/brokerage/accounts/{account_id}"
        f"/historicalorders/{','.join(order_ids)}"
    )
    data = _api_get(
        auth, url, {"since": since.isoformat()}, max_retries=max_retries
    )
    return data.get("Orders") or []


def run_orders(
    config: DownloadConfig,
    auth: TradeStationAuth,
    account_id: str,
    start: date,
    end: date,
    *,
    full: bool,
    include_open: bool,
    output_path: Path,
) -> int:
    """Orchestrate one orders run per §5; return the process exit code.

    Fetches per the ``FetchPlan`` (historical traversal → coverage.json →
    open-order reconciliation → ``/orders`` snapshot), merging and saving
    the parquet cache per page so an interruption never loses fetched data.
    On success the CSV is derived from the PERSISTED cache and ``0`` is
    returned; on ``OrdersError``/``AuthenticationError`` the error is logged
    and ``1`` returned WITHOUT writing the CSV.
    """
    _validate_account_id(account_id)
    if end < start:
        logger.error("--start (%s) is after --end (%s)", start.isoformat(), end.isoformat())
        return 1

    now = datetime.now(UTC).replace(tzinfo=None)
    today = now.date()
    earliest = today - timedelta(days=RETENTION_DAYS)
    if start < earliest:
        logger.warning(
            "TradeStation does not provide order data before %s; "
            "using that as the start boundary",
            earliest.isoformat(),
        )
    requested_start = max(start, earliest)

    cache_dir = _account_cache_dir(account_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)  # fail-fast on bad -o

    if full and cache_dir.exists():
        logger.info("Full download requested: removing existing cache %s", cache_dir)
        shutil.rmtree(cache_dir)

    cache = _load_cache(cache_dir)
    coverage = _load_coverage(cache_dir, account_id)
    cached_open_ids = cache.loc[
        cache["ClosedDateTime"].isna() & cache["OrderID"].notna(), "OrderID"
    ].tolist()

    plan = _build_fetch_plan(
        requested_start, end, today, now, coverage, cached_open_ids,
        include_open=include_open,
    )
    makes_calls = (
        plan.historical_since is not None
        or plan.fetch_current_orders
        or bool(plan.reconcile_open_ids)
    )
    if makes_calls:
        logger.info("%s", plan.reason)
    else:
        logger.info(
            "Orders cache is fresh for %s to %s; skipping API download.",
            start.isoformat(), end.isoformat(),
        )

    max_retries = config.max_retries
    rate_limit_delay = config.rate_limit_delay
    new_updated = 0

    def merge_page(page_orders: list[dict]) -> None:
        """Convert one fetched page, merge into the cache frame, persist."""
        nonlocal cache, new_updated
        frame = _orders_to_dataframe(page_orders)
        cache = _merge_orders(cache, [frame])
        _save_cache(cache_dir, cache)
        new_updated += len(frame)

    def merge_current_page(page_orders: list[dict]) -> None:
        """Per-page merge for /orders; default mode drops opens pre-merge."""
        nonlocal cache, new_updated
        frame = _orders_to_dataframe(page_orders)
        if not include_open:
            frame = frame[frame["ClosedDateTime"].notna()]
        cache = _merge_orders(cache, [frame])
        _save_cache(cache_dir, cache)
        new_updated += len(frame)

    try:
        if plan.historical_since is not None:
            _fetch_historical_orders(
                auth, account_id, plan.historical_since,
                max_retries=max_retries, rate_limit_delay=rate_limit_delay,
                on_page=merge_page,
            )
            # Coverage is established the instant the traversal completes; a
            # later failure must not force a full re-traversal next run.
            completed = datetime.now(UTC).replace(tzinfo=None)
            new_coverage = Coverage(
                since=plan.historical_since
                if coverage is None
                else min(coverage.since, plan.historical_since),
                through=completed,
                completed_at=completed,
            )
            _save_coverage(cache_dir, new_coverage, account_id)

        if plan.reconcile_open_ids:
            open_rows = cache.loc[
                cache["OrderID"].isin(plan.reconcile_open_ids), "OpenedDateTime"
            ].dropna()
            if not open_rows.empty and open_rows.min().date() < earliest:
                logger.warning(
                    "Some cached open orders were opened before %s; their final "
                    "state may be unverifiable once they close",
                    earliest.isoformat(),
                )
        for i in range(0, len(plan.reconcile_open_ids), DETAIL_BATCH_SIZE):
            if i:
                time.sleep(rate_limit_delay)
            batch = [str(oid) for oid in plan.reconcile_open_ids[i:i + DETAIL_BATCH_SIZE]]
            opened = cache.loc[
                cache["OrderID"].isin(batch), "OpenedDateTime"
            ].dropna()
            batch_since = (
                earliest if opened.empty else max(earliest, opened.min().date())
            )
            fetched = _fetch_historical_order_ids(
                auth, account_id, batch, batch_since,
                max_retries=max_retries, rate_limit_delay=rate_limit_delay,
            )
            frame = _orders_to_dataframe(fetched)
            cache = _merge_orders(cache, [frame])
            _save_cache(cache_dir, cache)
            new_updated += len(frame)

        if plan.fetch_current_orders:
            _fetch_current_orders(
                auth, account_id,
                max_retries=max_retries, rate_limit_delay=rate_limit_delay,
                on_page=merge_current_page,
            )

        if makes_calls:
            logger.info(
                "Cache: %s orders total, %s new/updated",
                f"{len(cache):,}", f"{new_updated:,}",
            )
    except KeyboardInterrupt:
        # Pages merged so far are safe; coverage exists only if the
        # historical pagination fully completed. Re-raise → CLI returns 130.
        raise
    except (OrdersError, AuthenticationError) as e:
        logger.error(str(e))
        return 1

    df = _load_cache(cache_dir)  # CSV derives from PERSISTED cache
    n = _write_orders_csv(df, start, end, output_path, include_open=include_open)
    print(f"Wrote {output_path} ({n:,} orders)")
    return 0
