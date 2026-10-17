# Orders Mode — Implementation Plan

This document is self-contained: it incorporates the verbatim requirement, PM rulings (noted inline), approved design decisions, and the API facts verified against the live TradeStation OpenAPI 3.0.3 spec (identical to the repo's `ts_api_openapi.json`).

---

## 1. Overview & requirement summary

Add an `orders` mode to the existing `tradestation-download` CLI. `orders` MUST be the first argument after the program name; it switches the tool into a mode that downloads historical orders for the currently logged-in TradeStation account to a CSV file.

Key requirements:

- Only the `tradestation:` credentials section of the config is used; everything else (symbols, `data_dir`, storage format, `start_date`, compression, etc.) is ignored.
- Flags: `-c/--config`, `--full`, `-s/--start`, `-e/--end`, `-o/--out`, `--include-open`, plus PM-approved `-a/--account` and repo-consistent `-v/--verbose`.
- Orders are cached in `orders/cache/<account_id>/` as Parquet; incremental runs only download not-yet-cached data; chronological order MUST be preserved.
- `--full` deletes the cache and re-downloads.
- CSV is generated in a second step from the cache for the requested `[--start, --end]` span and always replaced.
- CSV + cache contain ALL fields the API returns for historical orders (canonical 29-column schema + observed extra fields, §6); sparse fields are always columns, null when absent.
- Every order gets a `datetime` column = `ClosedDateTime` if closed else `OpenedDateTime`.
- By default ONLY closed orders are pulled. An optional `--include-open` flag also includes open orders (via `GET .../orders`); in default mode `/orders` is fetched only when the span includes today (§10.3) and no zero-call rule holds (§5), and its open rows are dropped before merge, so open orders never enter the cache from a default-mode run (§5).
- Prefer v3 API; request-efficient; best UX; DRY/YAGNI.

### Data flow

```
tradestation-download orders [flags]
        │
        ▼
tradestation/cli.py  main_download() ──sys.argv[1]=="orders"──► main_orders()
        │                                                          │
        │                                            create_orders_parser()
        │                                            run_orders_mode(parser, args)
        │                                                          │
        │        config.load_credentials() ◄─── config.yaml (tradestation: only)
        │                                                          │
        │        TradeStationAuth(client_id, client_secret, refresh_token)
        │                                                          │
        │        orders.resolve_account_id(auth, args.account)
        │              │ GET /v3/brokerage/accounts                │
        │              ▼                                           │
        │        account_id ──► default output_path if -o absent   │
        │                                                          ▼
        └────────────────────────────► orders.run_orders(config, auth, account_id,
                                                         start, end,
                                                         full=..., include_open=...,
                                                         output_path=...)
                                             │
              ┌──────────────────────────────┼──────────────────────────────┐
              ▼                              ▼                              ▼
   GET .../historicalorders         GET .../orders              GET .../historicalorders/{ids}
   (closed orders, since=,          (open + today's orders;     (open-order reconciliation,
    pageSize=600, nextToken)         fetch policy §5)             batches of ≤50 IDs)
              │                              │                              │
              └──────────────┬───────────────┴───────────────┬──────────────┘
                             ▼                               │
                    merge + dedupe + sort                    │
                             ▼                               │
              orders/cache/<account_id>/orders.parquet (atomic tmp+replace)
              orders/cache/<account_id>/coverage.json  (only after full pagination)
                             │
                             ▼
              filter [start, end+1d) → sort → output_path CSV (atomic, LF)
              print: Wrote orders/<account>_<start>_<end>.csv (N orders)
```

---

## 2. Verified API facts

(From `.planning/api-facts.md`, verified against the live TradeStation OpenAPI 3.0.3 spec; repo `ts_api_openapi.json` is verbatim identical.)

### Endpoints (v3, base `https://api.tradestation.com`)

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/v3/brokerage/accounts` | `{Accounts: [Account]}` | no params; one call lists the user's accounts |
| GET | `/v3/brokerage/accounts/{account_id}/historicalorders` | `{Orders, Errors, NextToken?}` | **closed orders only**, sorted DESC by close time. `since` query param **required**, limited to ~730 days back; `pageSize` 1–600 (default 600); `nextToken` cursor (1-hour lifetime, absent when done) |
| GET | `/v3/brokerage/accounts/{account_id}/historicalorders/{orderIds}` | `{Orders, Errors}` | `orderIds` = comma-separated, 1–50; `since` required; no pagination/filters |
| GET | `/v3/brokerage/accounts/{account_id}/orders` | `{Orders, Errors, NextToken?}` | today's orders + open orders; no `since`; same pagination |

- `Orders` + `Errors` may co-occur (partial success): log Errors, keep Orders.
- HTTP errors → `{Error, Message}` with `Error ∈ {BadRequest, Unauthorized, Forbidden, TooManyRequests, InternalServerError, NotImplemented, ServiceUnavailable, GatewayTimeout}`.

### Rate limits & auth

- "Order Details" + "Accounts" categories: **320 req / rolling 5-min window**. 429 → `{"Error":"TooManyRequests"}` + `X-RateLimit-*` headers; `Retry-After` is not documented — keep the existing code's pattern (read `Retry-After`, default 60s).
- OAuth: `POST https://signin.tradestation.com/oauth/token`, `grant_type=refresh_token`; access token ~20 min. Existing `TradeStationAuth` (`tradestation/auth.py`) handles this entirely.
- Scope `ReadAccount` covers the brokerage GET endpoints and is **already** in the auth flow's scope string: `auth_setup.py:98` has `"scope": "openid profile MarketData ReadAccount offline_access"`. **No auth/scope changes needed** (PM ruling 7).

### Unverified → empirical checks (§10)

- `since` filter semantics: open-time vs close-time — ambiguous in spec.
- Exact 730-day boundary inclusivity → plan uses today−729 conservative clamp (PM ruling 3).
- Whether `/historicalorders` includes orders closed TODAY.
- Whether subsequent pages must repeat `since`/`pageSize` alongside `nextToken` (default: repeat all params).
- Post-close revisions of order fields; undocumented extra top-level fields.

---

## 3. Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Dispatch | `sys.argv[1:2] == ["orders"]` check at top of `main_download()` (`cli.py:321`) | `orders` must be first arg; works for console script and `python tradestation_downloader.py`; no new entry point needed (`pyproject.toml:52`) |
| Module layout | Single new `tradestation/orders.py` | Matches `metadata.py`/`rollcheck.py` precedent (self-contained feature modules with own `BASE_URL` and `run_*` entry); cache logic inseparable from fetch coverage |
| Cache | One `orders.parquet` + `coverage.json` per account under `orders/cache/<account_id>/` | API limited to ~730 days; open orders move chronologically when they close; single file makes OrderID updates trivial |
| Orders root | `ORDERS_ROOT = Path("orders")` — cwd-relative literal; `config.data_dir` NOT consulted | PM ruling: "everything else ignored"; requirement specifies `orders/cache/<account_id>` literally |
| Default output path | `Path("orders") / f"{account_id}_{start}_{end}.csv"` (e.g. `orders/12345678_2025-01-01_2026-10-17.csv`) — computed by the **CLI**, passed as required `output_path` | PM ruling 1 overrides design-data's `orders/<account_id>/orders_*.csv` sketch |
| Nested API fields (`TrailingStop`, `ConditionalOrders`, `Legs`, `MarketActivationRules`, `TimeActivationRules`) | Canonical compact JSON-string columns: `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`; absent → null not `"null"` | One cache row = one order; OrderID stays the dedupe key; lossless |
| Scalar fields | Keep API string values as nullable `string` dtype (prices/quantities/fees are JSON strings in v3) | No precision loss; exact API representation |
| Dedupe | By `OrderID`: closed-beats-open, then later `_fetch_seq`, then last-in-batch | Prevents `/orders` open snapshot from clobbering a closed historical row |
| Sort | Ascending `["datetime", "OrderID"]`, stable, `na_position="last"` | PM ruling 4 (ascending chronological; requirement: chronological order MUST be preserved) |
| `-a/--account` | Extra flag (PM ruling 1); auto-select when exactly one account; otherwise print account list and exit 1 | Requirement doesn't cover multi-account logins |
| `earliest` | `date.today() - 729 days` (conservative vs ~730-day API limit, pending live check §10.2) | PM ruling 3 |
| `latest` | `date.today()` | PM ruling 3 |
| CSV line endings | `lineterminator="\n"` (LF) | Standard cross-platform CSV; `csv_export.py` CRLF is its own TradeStation-ASCII format, not relevant |
| Writes | Atomic: `*.tmp` then `Path.replace()` for parquet, coverage.json, and CSV | Crash safety; bars code doesn't do this but orders design mandates it |
| Config loader | `load_credentials()` in `config.py` returning `DownloadConfig` with only the three credentials set | Reuses `TradeStationAuth` contract and gives sane `rate_limit_delay`/`max_retries` defaults |
| `_api_get` helper | Duplicated inside `orders.py` (per-module precedent: `rollcheck.py:54-72`), raising `OrdersError` on terminal failure | Unlike downloader `_api_request` (`downloader.py:298-350`) which returns `None` — orders must NOT treat a failed request as an empty page (would corrupt coverage) |
| `--include-open` | `action="store_true"` flag (default off); default mode = only closed orders | PM ruling: `/orders` is the sole open-order source; default mode fetches it only when the span includes today and drops open rows before merge, so opens never enter the cache from a default-mode run |
| Default-mode CSV predicate | `ClosedDateTime.notna()` required in addition to the `datetime` bounds | Cached opens written by earlier `--include-open` runs must not leak into a default-mode CSV |

### PM notes (resolved conflicts / conservative choices)

- **Output path** — design-data.md sketched `orders/<account_id>/orders_*.csv`; PM ruling forces `orders/<account_id>_<start>_<end>.csv`. Applied.
- **`run_orders` signature** — design-data had `output_path: Path | None = None`; PM ruling makes `output_path` required (CLI always computes it). The CLI constructs ONE `TradeStationAuth` and shares it between `resolve_account_id` and `run_orders` (single OAuth session, no redundant token refresh). Applied: `run_orders(config, auth, account_id, start, end, *, full, include_open, output_path) -> int` (the `include_open` keyword arg was added by the later include-open ruling).
- **`earliest`/`latest` on wrong flag** — rejected with `ArgumentTypeError` (exit 2).
- **Coverage-vs-request edge**: on a today-spanning request, if coverage is fresh (<5 min) but stale open orders are cached, still skip API calls (fresh coverage makes immediate re-reconciliation a low-value trade-off — the today-spanning zero-call rule intentionally does not require `cached_open_ids` empty). For past-only requests the rule is stricter (`cached_open_ids` must be empty) so cached opens always trigger by-ID reconciliation rather than a blind skip. This is the literal reading of design-data §5.2 rule 4 and is the conservative choice. (Both zero-call rules apply to default mode only — `--include-open` runs always fetch `/orders` and never take the zero-call path; §5.)
- **`__init__.py`** — do NOT export orders API (see §4 file list, justified there).

---

## 4. File-by-file changes

### 4.1 `tradestation/cli.py` (modify)

New imports at top: `calendar`, `re`, `from datetime import date, timedelta`, `from pathlib import Path`, and `from .config import load_credentials` (added to the existing `from .config import ConfigurationError, load_config` line → keep both names imported).

**a) Dispatch in `main_download()`** — replace current body (`cli.py:321-329`):

```python
def main_download() -> None:
    """Entry point for download CLI; also dispatches 'orders' mode."""
    if sys.argv[1:2] == ["orders"]:
        sys.exit(main_orders())

    parser = create_download_parser()
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    sys.exit(run_download(args))
```

**b) `main_orders()`** — placed directly after `main_download()`:

```python
def main_orders() -> int:
    """Entry point for the 'orders' sub-mode."""
    parser = create_orders_parser()
    args = parser.parse_args(sys.argv[2:])

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    return run_orders_mode(parser, args)
```

**c) `create_orders_parser()`** — next to `create_download_parser()`. `prog="tradestation-download orders"`, `RawDescriptionHelpFormatter`, epilog with examples. Flags (see §7 for the table): `-c/--config` (default `config.yaml`), `--full`, `-s/--start` (`type=_make_orders_date_type("start")`, `default="earliest"`), `-e/--end` (`type=_make_orders_date_type("end")`, `default="latest"`), `-o/--out` (default `None`), `--include-open` (`action="store_true"`, help `"Include open orders (default: only closed orders are downloaded)"`), `-a/--account` (default `None`), `-v/--verbose`.

**Important argparse detail (PM ruling 6, verified):** argparse applies the `type` callable to string defaults too (only non-string defaults bypass conversion). So `default="earliest"` / `default="latest"` **do** pass through `_make_orders_date_type(...)` at parse time, yielding `date` objects even when the flag is omitted. The date type factory MUST therefore accept and resolve the literals `"earliest"`/`"latest"`, not just `YYYY-...` forms.

**d) `_make_orders_date_type(flag)`** — full sketch (design-cli §3, verbatim-approved):

```python
def _make_orders_date_type(flag: str):
    """Return an argparse type callable for --start or --end dates."""

    def parse(value: str) -> date:
        lowered = value.lower()
        if lowered == "earliest":
            if flag != "start":
                raise argparse.ArgumentTypeError(
                    f"'earliest' is only valid for --start, got {value!r}"
                )
            return date.today() - timedelta(days=729)
        if lowered == "latest":
            if flag != "end":
                raise argparse.ArgumentTypeError(
                    f"'latest' is only valid for --end, got {value!r}"
                )
            return date.today()
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.fullmatch(r"(\d{4})-(\d{2})", value)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            if flag == "start":
                return date(year, month, 1)
            return date(year, month, calendar.monthrange(year, month)[1])
        m = re.fullmatch(r"(\d{4})", value)
        if m:
            year = int(m.group(1))
            return date(year, 1, 1) if flag == "start" else date(year, 12, 31)
        valid = "YYYY-MM-DD, YYYY-MM, YYYY" + (
            ", or 'earliest'" if flag == "start" else ", or 'latest'"
        )
        raise argparse.ArgumentTypeError(
            f"invalid {flag} date {value!r}. Expected one of: {valid}"
        )

    return parse
```

Note: `date(y, m, d)` raises `ValueError` (not `ArgumentTypeError`) for e.g. `2024-13-01` — argparse catches `ValueError`/`TypeError`/`ArgumentTypeError` identically at the type-call boundary, so no extra handling needed.

**e) `run_orders_mode(parser, args) -> int`** — lazy-imports (precedent `cli.py:227-238`):

```python
def run_orders_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    from .orders import (
        AccountResolutionError, OrdersError, resolve_account_id, run_orders,
    )

    start: date = args.start
    end: date = args.end
    if start > end:
        parser.error(
            f"--start ({start.isoformat()}) must be on or before "
            f"--end ({end.isoformat()})"
        )

    try:
        config = load_credentials(args.config)
    except ConfigurationError as e:
        logger.error(str(e))
        return 1

    from .auth import AuthenticationError, TradeStationAuth
    auth = TradeStationAuth(
        config.client_id, config.client_secret, config.refresh_token
    )

    try:
        account_id = resolve_account_id(auth, args.account)
    except (AccountResolutionError, AuthenticationError, OrdersError) as e:
        logger.error(str(e))
        return 1

    if args.out:
        output_path = Path(args.out)
    else:
        output_path = Path("orders") / f"{account_id}_{start.isoformat()}_{end.isoformat()}.csv"
    logger.info("Date range: %s to %s", start.isoformat(), end.isoformat())
    if args.include_open:
        logger.info("Including open orders")
    try:
        return run_orders(
            config, auth, account_id, start, end,
            full=args.full, include_open=args.include_open,
            output_path=output_path,
        )
    except KeyboardInterrupt:
        logger.info("\nOrders download interrupted by user")
        return 130
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        if args.verbose:
            raise
        return 1
```

(`auth` is constructed exactly once here and shared by `resolve_account_id` and `run_orders` — one OAuth session, no redundant token refresh. `resolve_account_id` performs the accounts call through orders.py's `_api_get`, so its transport failures surface as `OrdersError`/`AuthenticationError`, both caught above; `AccountResolutionError` covers only domain failures.)

### 4.2 `tradestation/config.py` (modify)

**a) Extract `_parse_tradestation_credentials(data) -> tuple[str, str, str]`** from the top of `_parse_config` (`config.py:49-57`), adding the `isinstance` guards from design-cli §4:

```python
def _parse_tradestation_credentials(data: dict | None) -> tuple[str, str, str]:
    """Validate and return credentials from the 'tradestation' section."""
    if not isinstance(data, dict) or "tradestation" not in data:
        raise ConfigurationError("Missing 'tradestation' section in config")
    ts_config = data["tradestation"]
    if not isinstance(ts_config, dict):
        raise ConfigurationError("'tradestation' section must be a mapping")
    required_fields = ["client_id", "client_secret", "refresh_token"]
    missing = [f for f in required_fields if f not in ts_config]
    if missing:
        raise ConfigurationError(
            f"Missing required fields in tradestation config: {missing}"
        )
    return (ts_config["client_id"], ts_config["client_secret"], ts_config["refresh_token"])
```

`_parse_config` then starts with `client_id, client_secret, refresh_token = _parse_tradestation_credentials(data)` and uses those locals in the `DownloadConfig(...)` call (replacing `ts_config[...]`).

**b) New `load_credentials(config_path="config.yaml") -> DownloadConfig`** — same file-existence/YAML boilerplate as `load_config` (`config.py:29-43`), then:

```python
client_id, client_secret, refresh_token = _parse_tradestation_credentials(data)
return DownloadConfig(
    client_id=client_id, client_secret=client_secret, refresh_token=refresh_token
)
```

All other fields keep `DownloadConfig` defaults and the user's market-data settings are never validated or read — satisfying "everything else ignored".

### 4.3 `tradestation/orders.py` (NEW module)

Imports: `json`, `logging`, `re`, `shutil`, `time`, `from dataclasses import dataclass`, `from datetime import date, datetime, timedelta`, `from pathlib import Path`, `from typing import Any, Callable`, `import pandas as pd`, `import requests`, `from .auth import AuthenticationError, TradeStationAuth`, `from .models import DownloadConfig`.

Constants:

```python
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
]  # canonical 29 columns, fixed order; observed extras appended sorted (§6)

# API-documented top-level order fields — diagnostic basis for extra-field detection
DOCUMENTED_FIELDS = frozenset(ORDER_COLUMNS) - {"datetime"}

DATETIME_COLUMNS = ["datetime", "OpenedDateTime", "ClosedDateTime", "GoodTillDate"]
NESTED_COLUMNS = ["TrailingStop", "ConditionalOrders", "Legs",
                  "MarketActivationRules", "TimeActivationRules"]
```

Exceptions/dataclasses:

```python
class OrdersError(Exception): ...
class AccountResolutionError(Exception): ...

@dataclass
class Coverage:
    since: date            # earliest successfully fetched `since` boundary
    through: datetime      # UTC instant the historical listing was complete to
    completed_at: datetime # completion time of that full pagination

@dataclass
class FetchPlan:
    historical_since: date | None
    fetch_current_orders: bool
    reconcile_open_ids: list[str]
    reason: str
```

Key signatures (design-data §2, adjusted for PM rulings):

```python
def _validate_account_id(account_id: str) -> str           # ^[A-Za-z0-9_.-]+$, reject "."/".."/empty
def _account_cache_dir(account_id: str) -> Path            # ORDERS_ROOT / "cache" / account_id
def _api_get(auth, url, params, *, max_retries, retry=0) -> dict
def _fetch_historical_orders(auth, account_id, since, *, max_retries, rate_limit_delay,
                             on_page: Callable[[list[dict]], None] | None = None) -> list[dict]
def _fetch_current_orders(auth, account_id, *, max_retries, rate_limit_delay) -> list[dict]
def _fetch_historical_order_ids(auth, account_id, order_ids, since, *,
                                max_retries, rate_limit_delay) -> list[dict]
def _orders_to_dataframe(orders: list[dict]) -> pd.DataFrame
def _empty_orders_dataframe() -> pd.DataFrame
def _load_cache(cache_dir) -> pd.DataFrame                 # OrdersError w/ "--full" hint if unreadable
def _save_cache(cache_dir, df) -> None                     # tmp + replace
def _load_coverage(cache_dir: Path, account_id: str) -> Coverage | None
                                                       # corrupt/mismatch → None + warn;
                                                       # stored account_id must equal passed account_id
                                                       # (mismatched-account metadata → coverage = empty)
def _save_coverage(cache_dir, coverage) -> None            # tmp + replace
def _build_fetch_plan(requested_start, requested_end, today, now,
                      coverage, cached_open_ids, *, include_open) -> FetchPlan
def _merge_orders(cached: pd.DataFrame, fetched: list[pd.DataFrame]) -> pd.DataFrame
def _write_orders_csv(cache_df, start, end, output_path, *, include_open) -> int
                                                       # returns row count
def resolve_account_id(auth: TradeStationAuth, requested: str | None) -> str
def run_orders(config, auth, account_id, start, end, *,
               full, include_open, output_path) -> int
```

`_api_get` behavior (mirrors `downloader.py:318-350` but raises instead of returning `None`): bearer token + `Content-Type: application/json`; `requests.get(..., timeout=60)`; 429 → `Retry-After` or 60s, ≤`max_retries`, log `X-RateLimit-*`; 401 → `auth.invalidate()` + retry exactly once → `AuthenticationError`; other `RequestException` → exp backoff `2**retry` then `OrdersError`; 404 → account-specific `OrdersError`; other non-2xx → `OrdersError` with status + bounded body; invalid JSON → `OrdersError`. On HTTP 200, log each `Errors[]` entry; raise `OrdersError` only for account-level errors with no usable `Orders`/`NextToken`.

`resolve_account_id` — full logic in design-cli §5. The accounts call goes through the module's `_api_get` (`_api_get(auth, f"{BASE_URL}/brokerage/accounts", {}, max_retries=...)`) — same 401 invalidate+retry-once, 429 `Retry-After`, exponential backoff, 60s timeout as every other call; NO raw `requests.get`/`raise_for_status` here. Exception contract: `AuthenticationError`/`OrdersError` from `_api_get` propagate unchanged; `AccountResolutionError` is raised only for domain failures — 0 accounts returned; `requested` ID not among the accounts (error includes `_format_account_table`); multiple accounts with no `requested` (error includes table + "Use -a/--account"). 1 account → auto-select + `logger.info`. `run_orders_mode` catches `(AccountResolutionError, AuthenticationError, OrdersError)` → log + exit 1 (§4.1e).

`run_orders` orchestration pseudocode — see §5.

### 4.4 `.gitignore` (modify)

Add `orders/` to the "Data files" block (after `plain_data/`, line 26). Rationale: orders root contains both the local cache and generated CSVs — same treatment as `data/`/`plain_data/`. (PM ruling 5.)

### 4.5 `README.md` (modify)

Add an "Orders mode" usage section: brief description, the example invocations from §7, flag table, output/cache path explanation. Follow the existing README's command-table style.

### 4.6 `CLAUDE.md` (modify)

- Commands block: add `tradestation-download orders [-s START] [-e END] [-a ACCOUNT] [-o OUT] [--full] [--include-open]` examples.
- Project Structure: add `orders.py` line ("Orders mode: fetch, incremental parquet cache, CSV export").

### 4.7 `tradestation/__init__.py` — **no change (decision)**

Do not export `run_orders`/`resolve_account_id`. Justification: orders mode is a CLI feature invoked via `main_download` dispatch; existing `__init__.py` exports only the market-data library API (downloader/storage/auth/config). `metadata.py` and `rollcheck.py` — the closest precedent features — are also not exported there. YAGNI; keeps the public surface minimal.

---

## 5. Incremental fetch algorithm

### Coverage model

`coverage.json` records one contiguous covered interval `[historical_since 00:00 UTC, historical_through]`:

- `historical_since: date` — earliest `since` whose pagination fully completed.
- `historical_through: datetime` — UTC instant the listing was complete to.
- `completed_at: datetime` — when that full pagination finished.

Coverage is NEVER inferred from min/max order timestamps (empty days, zero-order spans, `datetime` shifts when opens close all make row-derived coverage unreliable). Corrupt/missing/mismatched-account metadata → coverage = empty; existing rows still merge, but no fetch is skipped based on them.

The zero-call path uses **two explicit rules**, not a single comparison, and exists only in default mode (`include_open=False`): in `--include-open` mode the `/orders` fetch is unconditional (it is the sole source of open orders), so every include-open run makes ≥1 API call and never satisfies a zero-call rule:

- A **past-only** request (exclusive upper bound `R1x ≤ today 00:00 UTC`) is fully covered when `R1x ≤ CT` and no unresolved open orders are cached.
- A **today-spanning** request (`R1x > today 00:00 UTC`) can never satisfy `bound ≤ CT` for long — `CT` is a past instant while the effective top is `now`. Its coverage top is inherently "as of `CT`"; instead the **freshness** of that coverage governs: skip all calls iff `now − completed_at ≤ CURRENT_FRESHNESS`. Do NOT compare a `min(bound, now)` cap to `CT` — `min(R1x, now) = now > CT` on every later run, which would make the rule permanently false.

### `since` decision table (`_build_fetch_plan`)

Definitions:

- `C0 = coverage.since`, `CT = coverage.through` — covered interval `[C0 00:00 UTC, CT]`.
- `R0 = max(requested_start, earliest_supported)` where `earliest_supported = today - 729d`.
- `R1x = requested_end + 1 day` at 00:00 UTC — the request's **exclusive** upper bound.
- `base_covered = R0 >= C0`.
- **past-only zero-call** = `NOT include_open` AND `base_covered` AND `R1x ≤ today 00:00 UTC` AND `R1x ≤ CT` AND `cached_open_ids` empty.
- **today-spanning zero-call** = `NOT include_open` AND `base_covered` AND `R1x > today 00:00 UTC` AND `now − completed_at ≤ CURRENT_FRESHNESS` (coverage top is inherently "as of `CT`"; freshness governs — `CT` is not compared to `now`).
- `refresh_since = max(earliest_supported, CT.date() - OVERLAP)`.
- **Evaluation order**: the two zero-call rules are checked FIRST. When either holds, `_build_fetch_plan` short-circuits to `FetchPlan(historical_since=None, fetch_current_orders=False, reconcile_open_ids=[], reason="zero-call: ...")` — no brokerage call at all, including `/orders`. The `fetch_current_orders = include_open or request_includes_today` formula (below) applies ONLY on non-zero-call paths.

| Case | Condition | Historical action |
|---|---|---|
| a | No coverage | `historical_since = R0` |
| b | `R0 < C0` (backfill) | `historical_since = R0` — the API has no upper bound, so one traversal from `R0` through present covers the backfill AND refreshes the top (no separate top-refresh case needed) |
| c | `base_covered` and neither zero-call rule holds (past-only top beyond `CT`, stale coverage on a today-spanning request, or unresolved opens on a past-only request) | `historical_since = refresh_since` |
| d | **past-only zero-call** OR **today-spanning zero-call** (definitions above) | No API calls (zero-call path): `historical_since=None`, `fetch_current_orders=False`, `reconcile_open_ids=[]` |

If `requested_start < earliest_supported`: warn once ("TradeStation does not provide data before ..."), clamp all `since` to `earliest_supported`, metadata records the clamped boundary.

### `/orders` (current) fetch policy

On non-zero-call paths the policy is a single formula (it is evaluated only when neither zero-call rule holds — a zero-call plan already has `fetch_current_orders=False`):

```python
fetch_current_orders = include_open or request_includes_today
# request_includes_today = R1x > today 00:00 UTC
```

- **`--include-open` mode**: `/orders` is ALWAYS fetched — it is the sole source of open orders, and even a past-only span can contain orders opened in-range that are still open. Include-open runs therefore always make ≥1 API call; the zero-API-call path exists only in default mode.
- **Default mode** (`include_open=False`): `/orders` is fetched ONLY when the requested span includes today — required because it is unverified whether `/historicalorders` covers orders closed *today* (§10.3) — and NOT fetched for past-only spans. Any open rows (null `ClosedDateTime`) returned by that fetch are DROPPED before merge — open orders never enter the cache from a default-mode run, keeping "only closed orders are pulled" literally true.

Separately, when a zero-call rule holds (possible only in default mode) no brokerage call is made at all: for today-spanning requests fresh coverage justifies skipping even with unresolved opens cached (they were just reconciled); for past-only requests `cached_open_ids` empty is part of the rule.

`reconcile_open_ids` = cached open `OrderID`s — computed from the cache exactly as before and run in BOTH modes: the by-ID endpoint only returns closed orders, and reconciliation is needed whenever a cache contains opens (which only `--include-open` runs or earlier include-open runs can produce), regardless of the current run's mode.

### Open-order reconciliation

Before requests: `cached_open_ids = cache.loc[cache["ClosedDateTime"].isna() & cache["OrderID"].notna(), "OrderID"]`. Partition into ≤50-ID batches; per batch call `/historicalorders/{ids}` with `batch_since = max(earliest_supported, min(cached OpenedDateTime dates in batch, default=earliest_supported))`. Covers both `since` semantics (close-time → recent close returned; open-time → original open date supplied). Orders absent from all responses: keep last-known cached row (never delete — `/orders` excludes old closed orders; partial errors and timing gaps are transient). Warn once if an open order's `OpenedDateTime` precedes the retention boundary.

### Merge precedence (`_merge_orders`)

Assign monotonically increasing `_fetch_seq` per fetched batch (cached rows = 0). For duplicate `OrderID`:

1. Non-null `ClosedDateTime` beats null `ClosedDateTime`.
2. Same class → greater `_fetch_seq` wins.
3. Same batch → last API occurrence wins.

Null-`OrderID` rows: keep + warn; dedupe only exact full-row duplicates (`keep="last"`). Then `sort_values(["datetime", "OrderID"], na_position="last", kind="stable")`, drop `_fetch_seq`.

### Orchestration pseudocode (`run_orders`)

```text
run_orders(config, auth, account_id, start, end, *, full, include_open, output_path) -> int:
    validate account_id regex; reject end < start
    now = naive UTC now; today = now.date(); earliest = today - 729d
    if start < earliest: warn once
    requested_start = max(start, earliest)
    cache_dir = ORDERS_ROOT/"cache"/account_id
    output_path.parent.mkdir(parents=True, exist_ok=True)   # fail-fast: a bad/unwritable
                                                            # -o fails here, before any API work

    if full and cache_dir exists: shutil.rmtree(cache_dir)
    cache = _load_cache(cache_dir)          # or typed empty frame
    coverage = _load_coverage(cache_dir, account_id)    # or None (mismatched-account → None)
    cached_open_ids = open OrderIDs in cache
    plan = _build_fetch_plan(requested_start, end, today, now,
                             coverage, cached_open_ids, include_open=include_open)
    # a zero-call plan carries ALL fetch fields off — the three guarded blocks
    # below (historical / reconcile / /orders) are each skipped, so the run
    # makes no brokerage call at all; it goes straight to CSV export

    # `auth` is the single TradeStationAuth constructed by the CLI — do NOT build another
    try:
        if plan.historical_since is not None:
            _fetch_historical_orders(..., since=plan.historical_since,
                on_page=lambda orders: merge+_save_cache atomically per page)
            # write coverage.json NOW — historical_since/through are established the
            # instant the traversal completes; a later failure (reconciliation,
            # /orders) must not force a full re-traversal next run
            new_coverage.since = plan.historical_since if no old coverage
                                 else min(old.since, plan.historical_since)
            new_coverage.through = instant the traversal completed; completed_at = same
            _save_coverage(cache_dir, new_coverage)   # AFTER parquet persisted
        for batch in chunks(plan.reconcile_open_ids, 50):
            fetched = _fetch_historical_order_ids(..., batch_since)
            merge + _save_cache; sleep rate_limit_delay between batches
        if plan.fetch_current_orders:
            _fetch_current_orders(...)   # pageSize=600, nextToken loop,
                                         # merge+_save_cache per page;
                                         # when include_open is False, drop rows with
                                         # ClosedDateTime.isna() from each fetched page
                                         # BEFORE merging (default mode never caches opens)
    except KeyboardInterrupt:
        # pages already merged are safe; coverage written only if the historical
        # pagination fully completed; re-raise → CLI returns 130
        raise
    except (OrdersError, AuthenticationError, ...) as e:
        log; return 1    # do NOT write CSV

    df = _load_cache(cache_dir)     # CSV derives from PERSISTED cache
    n = _write_orders_csv(df, start, end, output_path, include_open=include_open)
    print(f"Wrote {output_path} ({n:,} orders)")
    return 0
```

Ordering invariant: parquet per-page save → coverage.json immediately after the historical pagination completes (before by-ID reconciliation and `/orders`) → CSV last. Crash between steps is always safe (extra rows w/o coverage → re-fetch + dedupe; never claimed coverage for unpersisted data; `historical_through`/`completed_at` record the instant that traversal completed).

**Accepted tradeoff**: coverage.json is persisted right after historical pagination, before reconciliation and `/orders`. If a later phase fails, the run still exits 1 (the failure is visible to the user and no CSV is written), but coverage now claims a top fresher than what the cache actually holds. A subsequent fresh-window (≤5 min) default-mode today-spanning run would then take the zero-call path and could emit a CSV missing the rows the failed phase would have added — though in practice the failed phase's rows were never persisted either, so the CSV is missing the same data it was missing before the failed run, and the next non-fresh run refetches them normally. The 5-min freshness window bounds this staleness; we accept it rather than deferring the coverage write, because deferring would force a full re-traversal on every reconciliation/`/orders` failure (the far more common and costlier failure mode).

Mixed-mode note: opens cached by earlier `--include-open` runs survive in the cache. Default-mode runs exclude them from the CSV via the `ClosedDateTime.notna()` predicate (§6) and still reconcile them toward their closed state whenever cached opens exist (`reconcile_open_ids` runs in both modes). A default-mode `--full` run wipes the whole cache including opens — acceptable and documented.

### Pagination rules

`params = {"since": since.isoformat(), "pageSize": 600}`; repeat `since`+`pageSize` and add returned `nextToken` on subsequent pages (default per api-facts; verified in §10.5). Stop on missing/null/empty `NextToken`; identical repeated token → `OrdersError` (infinite-loop guard); `time.sleep(rate_limit_delay)` between pages only; tokens never persisted; sequential pages only.

---

## 6. Cache & CSV schema

### ORDER_COLUMNS — canonical 29, first and in this fixed order, for BOTH Parquet and CSV

| # | Column | dtype | # | Column | dtype |
|---|---|---|---|---|---|
| 1 | `datetime` | datetime64[ns] tz-naive UTC | 16 | `RejectReason` | string |
| 2 | `AccountID` | string | 17 | `ShowOnlyQuantity` | string |
| 3 | `OrderID` | string | 18 | `Spread` | string |
| 4 | `OrderType` | string | 19 | `GroupName` | string |
| 5 | `OpenedDateTime` | datetime64[ns] | 20 | `AdvancedOptions` | string |
| 6 | `ClosedDateTime` | datetime64[ns] | 21 | `Status` | string |
| 7 | `Duration` | string | 22 | `StatusDescription` | string |
| 8 | `GoodTillDate` | datetime64[ns] | 23 | `StopPrice` | string |
| 9 | `LimitPrice` | string | 24 | `UnbundledRouteFee` | string |
| 10 | `FilledPrice` | string | 25 | `TrailingStop` | string (JSON) |
| 11 | `PriceUsedForBuyingPower` | string | 26 | `ConditionalOrders` | string (JSON) |
| 12 | `CommissionFee` | string | 27 | `Legs` | string (JSON) |
| 13 | `Currency` | string | 28 | `MarketActivationRules` | string (JSON) |
| 14 | `ConversionRate` | string | 29 | `TimeActivationRules` | string (JSON) |
| 15 | `Routing` | string | | | |

`datetime` = `ClosedDateTime` if present else `OpenedDateTime`. Datetimes via `pd.to_datetime(..., utc=True, errors="coerce").dt.tz_convert(None)`; non-empty values → `NaT` log a warning with `OrderID`; row retained (excluded from CSV naturally). After constructing the frame, `_orders_to_dataframe` casts every non-datetime column to `pd.StringDtype()` (nullable UTF-8) — parquet must never receive `object`/mixed columns — then reindexes to the evolved schema (below).

### Schema evolution (observed extra fields)

The canonical 29 `ORDER_COLUMNS` always come first, in fixed order, and are always unioned into the frame (sparse documented fields become null columns). Additionally observed **top-level order fields** not in the canonical list are appended in deterministic sorted order on every merge:

```python
columns = ORDER_COLUMNS + sorted(extras)
```

- Dict/list-typed extras → canonical compact JSON string (same `json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` rule as `NESTED_COLUMNS`); scalar extras → nullable `string`.
- Previously seen extra columns persist across merges even when absent from the latest response — the cache schema is the **union of all fields ever observed** over the cache's lifetime (`_merge_orders` reindexes to `ORDER_COLUMNS + sorted(all observed extras)`, cached ∪ fetched).
- CSV exports the cache's complete evolved schema (canonical 29 first, extras sorted).
- `_orders_to_dataframe` keeps the diagnostic `logger.debug` of `set(order) - set(DOCUMENTED_FIELDS)` so newly observed extras are visible in verbose runs.

### CSV export (`_write_orders_csv`)

Filter on explicit `pd.Timestamp` bounds — comparing a `datetime64[ns]` Series to `datetime.date` raises `Invalid comparison` on modern pandas — apply the open-order predicate (default mode never exports opens: the cache may already hold open rows written by earlier `--include-open` runs and they must not leak into a default-mode CSV), then re-sort defensively so CSV row order never depends on cache-read order:

```python
# `start`/`end` are INCLUSIVE calendar DATES; the comparison bound is the
# EXCLUSIVE timestamp one day past `end` (so the whole end day is included)
start_ts = pd.Timestamp(start)
end_exclusive = pd.Timestamp(end) + pd.Timedelta(days=1)
filtered = df[(df["datetime"] >= start_ts) & (df["datetime"] < end_exclusive)]
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
```

`_write_orders_csv` owns `output_path.parent.mkdir(parents=True, exist_ok=True)` and removes the tmp file on failure — a failed export never destroys an existing CSV (atomic write, same rule as parquet/coverage writes).

### `coverage.json`

```json
{
  "schema_version": 1,
  "account_id": "12345678",
  "historical_since": "2025-01-01",
  "historical_through": "2026-01-13T14:22:31.123456Z",
  "completed_at": "2026-01-13T14:22:31.123456Z"
}
```

### File layout

```text
orders/
├── cache/
│   └── <account_id>/
│       ├── orders.parquet        # canonical 29 + observed extras, sorted asc by datetime/OrderID
│       └── coverage.json
└── <account_id>_<start>_<end>.csv   # e.g. 12345678_2025-01-01_2026-10-17.csv
```

---

## 7. UX spec

### Flags

| short | long | dest | default | help summary |
|---|---|---|---|---|
| `-c` | `--config` | `config` | `config.yaml` | Path to configuration file |
| | `--full` | `full` | `False` | Discard existing local order cache and re-download everything |
| | `--include-open` | `include_open` | `False` | Include open orders (default: only closed orders are downloaded) |
| `-s` | `--start` | `start` | `"earliest"` → `today−729d` | `YYYY-MM-DD`, `YYYY-MM`, `YYYY`, or `earliest` |
| `-e` | `--end` | `end` | `"latest"` → `today` | `YYYY-MM-DD`, `YYYY-MM`, `YYYY`, or `latest` |
| `-o` | `--out` | `out` | `None` → `orders/<acct>_<start>_<end>.csv` | Output CSV path |
| `-a` | `--account` | `account` | `None` (auto if 1 account) | TradeStation account ID (required with multiple accounts) |
| `-v` | `--verbose` | `verbose` | `False` | Enable verbose (debug) logging |

### Examples

```
tradestation-download orders                          # all history, auto account
tradestation-download orders -s 2024-01-01 -e 2025-01-31
tradestation-download orders -s 2024 -e 2024-06       # year / year-month forms
tradestation-download orders --full -o my_orders.csv
tradestation-download orders -a 12345678 -s 2025-01-01 -e latest
tradestation-download orders --include-open            # also include still-open orders
```

### Message flow

| Step | Channel | Example |
|---|---|---|
| Date range | log INFO | `Date range: 2024-01-01 to 2025-12-31` |
| Open-order mode | log INFO | `Including open orders` |
| Account selection | log INFO | `Selected account 12345678 (Futures, Active)` |
| Paging | log INFO | `Fetching orders page 1 (600/page)` |
| Cache stats | log INFO | `Cache: 1,234 orders total, 89 new/updated` |
| Fresh-cache skip | log INFO | `Orders cache is fresh for ...; skipping API download.` |
| Final result | **print** | `Wrote orders/12345678_2024-01-01_2025-12-31.csv (1,234 orders)` |

### Errors / exit codes

| Scenario | Handling | Exit |
|---|---|---|
| Config missing / bad YAML / missing creds | `ConfigurationError` → `logger.error` | 1 |
| Bad date format / `earliest`/`latest` on wrong flag | `ArgumentTypeError` | 2 |
| `--start > --end` | `parser.error` | 2 |
| No accounts / multi-account without `-a` / `-a` not found | `AccountResolutionError` → `logger.error` (message includes account table) | 1 |
| Auth/refresh failure | `AuthenticationError` → `logger.error` | 1 |
| API failure during fetch or account resolution | `OrdersError` → `logger.error` (account resolution goes through `_api_get`) | 1 |
| Corrupt parquet cache | `OrdersError`: "...unreadable; rerun with --full to rebuild" | 1 |
| KeyboardInterrupt | `logger.info` | 130 |
| Unexpected | `logger.error`; re-raise under `-v` | 1 |

---

## 8. Date parsing spec

| Flag | Input | Resolves to |
|---|---|---|
| `--start` | `YYYY-MM-DD` | exact date |
| `--start` | `YYYY-MM` | first day of month |
| `--start` | `YYYY` | Jan 1 |
| `--start` | `earliest` | `date.today() − 729d` |
| `--end` | `YYYY-MM-DD` | exact date |
| `--end` | `YYYY-MM` | last day of month (`calendar.monthrange`) |
| `--end` | `YYYY` | Dec 31 |
| `--end` | `latest` | `date.today()` |

- `earliest` valid only on `--start`; `latest` only on `--end` → `ArgumentTypeError` (exit 2).
- Strict zero-padded grammar via `re.fullmatch`; `2024-1-1` rejected.
- String defaults `"earliest"`/`"latest"` pass through `type` at parse time (argparse converts string defaults) — defaults therefore resolve dynamically at each invocation.
- All values are `datetime.date` (local tz); data layer converts to API `since` ISO date / UTC instants.
- `start > end` → `parser.error` → exit 2.

---

## 9. Testing plan

New files, matching repo pytest conventions (`tests/test_cli.py`: `_make_args`/`_make_config` helpers, class-grouped tests, `patch`, `temp_data_dir`, fake `Mock(status_code, json, headers)`, patch `time.sleep`, fixed `now`/`today` — patch `date.today`/`datetime.now` or inject for determinism).

### `tests/test_orders_cli.py`

- `TestDispatch`: `["prog","orders",...]` routes to orders path; `["prog","--help"]` still download help; `["prog","orders","--help"]` orders help; `["prog","foo"]` → exit 2.
- `TestOrdersDateParsing`: full `YYYY-MM-DD`; `YYYY` → Jan1/Dec31; `YYYY-02` leap year (2024-02-29); omitted flags → today−729/today; `2024-13-01`, `not-a-date`, `2024-1-1` → SystemExit 2; `-s latest` / `-e earliest` → 2.
- `TestStartEndValidation`: start > end → SystemExit 2.
- `TestOrdersRunMode`: default output_path `orders/ACC123_2024-01-01_2024-02-01.csv`; `-o` override + parent mkdir; multi-account error → 1; `-a` verification path → `run_orders` gets `"ACC123"`; config missing → 1; `-v` parses; exactly ONE `TradeStationAuth` is constructed — the same instance passed to `resolve_account_id` is passed to `run_orders` (patch `TradeStationAuth` once and assert call count + identity); `OrdersError`/`AuthenticationError` from `resolve_account_id` → exit 1.
- `TestIncludeOpenFlag`: `--include-open` parses (`args.include_open is True`, `store_true` semantics — no value consumed); absent → `False`; flag forwards as `include_open=` kwarg to `run_orders`; `logger.info("Including open orders")` emitted only when set.

### `tests/test_orders.py`

- `TestHistoricalPagination`: 2-page token flow (assert `since`+`pageSize=600`+token), 3-page delays, empty first page, repeated-token → `OrdersError`, token absent/None/"" all terminate, token never in coverage.json.
- `TestOrdersApiGet`: 429 with/without/malformed `Retry-After`; 401→invalidate→200; double-401 → `AuthenticationError`; exp backoff; exhausted retries → `OrdersError`, no coverage advance; 404 account error; invalid JSON.
- `TestOrderNormalization`: canonical 29 cols first in fixed order; missing fields null; scalars stay strings; ISO → tz-naive UTC; `datetime` prefers Closed; open → Opened; bad timestamps → `NaT` row kept; nested → sorted-key compact JSON, absent → null; undocumented extra → appended after canonical 29 in sorted order (dict extra → compact JSON, scalar extra → string); previously seen extra persists as null column across a merge where it's absent.
- `TestFetchPlanning` (fixed `now`): cases a–d of §5 table incl. both zero-call rules — past-only (`R1x ≤ today 00:00`, `R1x ≤ CT`, no cached opens → skip) and today-spanning (`CT=12:00`, `now=12:01` <5min after `completed_at`, request includes today → zero brokerage calls; `now−completed_at` >5min → refresh path). Both zero-call cases assert ALL three fields off — `historical_since is None`, `fetch_current_orders is False`, `reconcile_open_ids == []` — so the plan makes no brokerage call at all (the zero-call check short-circuits before the `/orders` formula, which would otherwise be `True` on a today-spanning request). 729d clamp + one warning; corrupt metadata → empty coverage; coverage metadata on an empty (zero-row) cache still enables the zero-call skip. `fetch_current_orders` truth table on non-zero-call paths, per mode: default + past-only → `False` (no `/orders` fetch); default + today-spanning with stale/absent coverage → `True`; `include_open=True` → `True` unconditionally (past-only span, empty cache, fresh coverage — all still fetch).
- `TestIncludeOpenZeroCall`: pure zero-API-call path exists only in default mode — a run that would be zero-call in default mode (fresh coverage, today-spanning) still calls `/orders` when `include_open=True` (≥1 call).
- `TestIncludeOpenMergeAndCsv`: default-mode `/orders` fetch — open rows (null `ClosedDateTime`) dropped before merge, closed rows kept (cache gains no opens); `include_open` run keeps opens in cache AND CSV (`datetime` = `OpenedDateTime`), including orders opened in-range on a past-only span that are still open; default-mode CSV excludes opens already cached by an earlier `--include-open` run (`ClosedDateTime.notna()` predicate) while still exporting closed rows.
- `TestOrderMerge`: closed beats open both directions; dedupe today's order across both endpoints; tie-break `OrderID`; `NaT` last; missing `OrderID` kept + warn.
- `TestOpenOrderReconciliation`: old-open-now-closed via by-ID with open-date `since`; still-open via `/orders`; vanished → cached row kept; 51 IDs → batches 50+1; per-ID error partial; pre-retention open → clamp + warn.
- `TestFullOrdersDownload` / `TestInterruptedDownload`: `rmtree` of whole cache dir; interrupt after page 1 → parquet has page, coverage unchanged, CSV untouched; rerun dedupes.
- `TestOrdersCsvExport`: `[start, end+1d)` boundary cases (midnight-start included, next-midnight excluded); `date` bounds vs `datetime64[ns]` column (no Invalid comparison); deterministic sort independent of input row order; overwrite; empty → canonical 29 headers (+ evolved extras when present); nested JSON parses; CSV read from saved parquet; failed `to_csv` → tmp removed, pre-existing CSV untouched.
- `TestOrdersResponseErrors`: `Orders`+`Errors` partial → keep + warn + success; account error no orders → 1; details partial.

---

## 10. Live-API verification checklist

| # | Question | Cheap verification |
|---|---|---|
| 1 | `since` filters on open vs close time; midnight inclusivity? | Pick an order opened day A, closed day B; request `since` = A, A+1, B, B+1 on list + by-ID endpoints; record appearances. |
| 2 | Exact 730-day boundary? | Send `since` = today−729/−730/−731; record status+message. If −730 accepted, change `RETENTION_DAYS` to 730 + regression test. |
| 3 | Does `/historicalorders` include today's closed orders? | Close a small same-day order; query both endpoints; compare OrderIDs; repeat after a few minutes. |
| 4 | By-ID endpoint on a still-open ID: row, empty, or error? | Query one known open OrderID via `/historicalorders/{id}`; compare with `/orders`. |
| 5 | Must page 2+ repeat `since`/`pageSize` with `nextToken`? | Find >600-row span; try full params (default) then token-only; record. |
| 6 | Undocumented top-level fields? | Debug-log `set(order) - set(DOCUMENTED_FIELDS)` over several responses; observed extras are auto-appended per the §6 schema-evolution policy — verify the sorted-append and JSON/string typing on real data. |
| 7 | Post-close field revisions? | Snapshot closed IDs; re-query after 1 and several days; widen `OVERLAP` if revisions beyond 2 days are common. |

---

## 11. Implementation order

1. **`config.py` refactor** — `_parse_tradestation_credentials` + `load_credentials`; refactor `_parse_config`. No dependencies; verifiable by existing `test_config.py`.
2. **`orders.py` skeleton** — constants, exceptions, `Coverage`/`FetchPlan`, `_validate_account_id`, `_api_get`, `resolve_account_id` (+ tests for these).
3. **`orders.py` core** — `_orders_to_dataframe`, cache/coverage I/O, `_merge_orders`, `_build_fetch_plan`, `_write_orders_csv`, fetch trio, `run_orders` (+ `test_orders.py`).
4. **`cli.py` wiring** — `_make_orders_date_type`, `create_orders_parser`, `run_orders_mode`, `main_orders`, dispatch (+ `test_orders_cli.py`).
5. **`.gitignore`** — `orders/` entry.
6. **Docs** — README orders section, CLAUDE.md commands/structure lines.
7. **Live verification** (§10) — adjust `RETENTION_DAYS`/params if needed.

Dependencies: step 3 needs step 2; step 4 needs 1+3; 5–6 independent; 7 last.

---

## 12. Risks & open questions

| Risk / open question | Mitigation |
|---|---|
| `since` semantics ambiguous (open vs close time) | By-ID reconciliation makes both semantics safe; §10.1 confirms; keep reconciliation regardless |
| 730-day boundary inclusivity | Conservative 729 clamp; §10.2 may relax to 730 |
| Orders opened >730d ago, still open → unverifiable final state after close | Preserve last-known row + warn once (design-data §12.1); cannot manufacture withheld data |
| Single-file parquet rewrite scales O(total) per page | Acceptable for ≤730-day order history; revisit with partitioned storage only if observed slow (YAGNI) |
| Post-close revisions older than 2-day overlap missed | `--full` escape hatch; §10.7 checks whether overlap needs widening |
| Coverage freshness window (5 min) is a heuristic | Matches the documented rolling rate-limit window; prevents accidental-rerun API storms while keeping opens reasonably fresh |
| `Retry-After` not documented for these endpoints | Keep existing pattern: use header if present, else 60s; optionally prefer `X-RateLimit-Reset` when present |
| Two sequential `_api_get` definitions diverge across modules | Accepted codebase convention (rollcheck duplicates); document behavior parity in docstring |
| Open rows cached by earlier `--include-open` runs leak into default-mode output | Two independent guards: per-page drop before merge (default mode never adds opens) and the `ClosedDateTime.notna()` CSV predicate (excludes already-cached opens); default-mode `--full` wipes the whole cache incl. opens — acceptable and documented (§5 mixed-mode note) |

---

## 13. Acceptance checklist — requirement → plan section

| Requirement bullet | Where addressed |
|---|---|
| `orders` command MUST be first argument | §3 dispatch; §4.1a `sys.argv[1:2]` check; §9 dispatch tests |
| Purpose: historical orders → CSV for logged-in account | §1; §5 orchestration |
| Only account/client info from config; everything else ignored | §3 config loader decision; §4.2 `load_credentials`; `ORDERS_ROOT` literal (PM ruling) |
| `-c`/`--config` flag | §7 flag table; §4.1c |
| `--full` discards cache | §4.1c; §5 pseudocode (`rmtree`); §9 `TestFullOrdersDownload` |
| `-s`/`--start` formats (day/month-year/`earliest`) | §8 grammar; §4.1d factory |
| `-e`/`--end` formats (month/year → LAST day; `latest`) | §8 grammar; §4.1d |
| `-o`/`--out`, default `orders/<account_id>_<start>_<end>.csv` | §4.1e path computation; PM ruling 1 applied; §9 test 5–6 |
| Cache at `orders/cache/<account_id>` parquet; only new data downloaded | §6 layout; §5 incremental algorithm/decision table |
| Correct chronological order MUST be preserved | §3 sort decision; `_merge_orders` asc sort; §9 merge/export tests |
| `--full` re-downloads always | §5 pseudocode + §7 errors |
| CSV generated in second step from cached data for requested span | §5 materialization (`_load_cache` then `_write_orders_csv`); §6 filtering — `start`/`end` inclusive dates, compared as `[start 00:00, end+1d 00:00)` timestamps |
| Output CSV always replaced | `_write_orders_csv` tmp+replace overwrite; §9 export test 6 |
| Cache+CSV contain ALL API fields; sparse → always columns, null absent | §6 schema evolution: canonical 29 first + `sorted(extras)` union persisted across merges; CSV exports evolved schema; §9 normalization tests |
| `datetime` column = Closed else Opened | §6 schema note; §9 normalization tests 5–6 |
| Multi-account: auto-select one; else list + exit 1 + `-a` flag (PM) | §3 account decision; §4.3 `resolve_account_id`; §7 errors |
| Orders root literal `orders/`, `data_dir` ignored (PM) | §3; `ORDERS_ROOT = Path("orders")`; §4.3 |
| `earliest` = today−729 (PM) | §3; `RETENTION_DAYS`; §8 |
| Ascending sort, tie-break `OrderID` (PM) | §3; §5 merge |
| Open orders included via `GET .../orders`, `datetime` = `OpenedDateTime` (PM) | §5 `/orders` policy + reconciliation; §6 — applies to `--include-open` mode |
| Only closed orders by default; `--include-open` includes open orders | §1 summary; §3 flag/predicate decisions; §4.1c flag, §4.1e forwarding; §4.3 signatures; §5 `fetch_current_orders` formula + merge drop + mixed-mode note; §6 CSV predicate; §7 flag table/example/log; §9 `TestIncludeOpen*` |
| `.gitignore` gains `orders/` (PM) | §4.4 |
| `ReadAccount` scope already present; no auth changes (PM) | §2 auth note (`auth_setup.py:98`) |
| Prefer v3; request-efficient; Lean UX; DRY/YAGNI | §2 v3 endpoints; §5 request-efficiency (zero-call path, 50-ID batches, 600/page); §3 decisions |
