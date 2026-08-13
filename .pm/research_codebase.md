# TradeStation Downloader — Codebase Research Report

This is a READ-ONLY investigation of the TradeStation downloader project at
`D:\nohype-tradestation-downloader`. All claims cite file paths and line numbers.

Repository layout (relevant files):

```
tradestation/
├── __init__.py        # package exports, __version__ = "1.0.7"
├── auth.py            # OAuth2 token handling
├── auth_setup.py      # interactive OAuth2 setup wizard
├── cli.py             # argparse CLI (download / metadata / export-csv)
├── config.py          # YAML config loading -> DownloadConfig
├── csv_export.py      # parquet -> CSV (.txt) export
├── downloader.py      # TradeStationDownloader: API fetch + bar parsing
├── metadata.py        # --metadata feature: symbol details + session derivation
├── models.py          # StorageFormat/Compression enums, DownloadConfig, DEFAULT_SYMBOLS
└── storage.py         # StorageBackend ABC + Single/Daily/Monthly implementations
tests/
├── conftest.py
├── test_csv_export.py
├── test_csv_export_up_down.py
├── test_downloader_volume_fields.py
├── test_metadata.py
├── test_models.py
├── test_security_fixes.py
└── test_storage.py
```

---

## 1. Bar data fetch (`tradestation/downloader.py`)

### Endpoint

`TradeStationDownloader._api_request` (`tradestation/downloader.py:248-300`) issues a
GET to the TradeStation v3 barcharts endpoint:

```python
url = f"{self.BASE_URL}/marketdata/barcharts/{symbol}"
```

`BASE_URL = "https://api.tradestation.com/v3"` (`tradestation/downloader.py:69`).

So the full endpoint is `GET https://api.tradestation.com/v3/marketdata/barcharts/{symbol}`.

### Query params

Built at `tradestation/downloader.py:257-262`:

```python
params = {
    "interval": self.config.interval,      # default 1 (DownloadConfig.interval)
    "unit": self.config.unit,              # default "Minute" (DownloadConfig.unit)
    "barsback": barsback or self.config.max_bars_per_request,  # default 57600
    "lastdate": last_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
}
```

Headers (`tradestation/downloader.py:263-266`):

```python
headers = {
    "Authorization": f"Bearer {self._auth.get_access_token()}",
    "Content-Type": "application/json",
}
```

Defaults come from `DownloadConfig` (`tradestation/models.py:55-57`):
`interval=1`, `unit="Minute"`, `max_bars_per_request=57600` (~40 days of 1-min bars).

### Retry / auth behavior

`_api_request` (`tradestation/downloader.py:268-300`):
- HTTP 429 → reads `Retry-After` header, sleeps, retries up to `max_retries`.
- HTTP 401 → refreshes token once (`self._auth.invalidate()` + retry); a second 401 raises `AuthenticationError`.
- Other `requests.exceptions.RequestException` → exponential backoff (`2 ** retry`).

### Pagination / batching loop

`_fetch_bars` (`tradestation/downloader.py:219-246`) walks backwards in time:

```python
while current_end > start_date:
    barsback = self._calc_barsback(start_date, current_end)
    data = self._api_request(symbol, current_end, barsback=barsback)
    if not data or "Bars" not in data or not data["Bars"]:
        break
    bars = data["Bars"]
    all_bars.extend(bars)
    ...
    oldest = pd.to_datetime(bars[0]["TimeStamp"]).replace(tzinfo=None)
    if oldest <= start_date:
        break
    current_end = oldest - timedelta(minutes=1)
    time.sleep(self.config.rate_limit_delay)
```

`_calc_barsback` (`tradestation/downloader.py:214-217`) computes minutes between
`start_date` and `current_end`, clamped to `[1, max_bars_per_request]`.

The last (incomplete) bar is dropped at `tradestation/downloader.py:246`:
`return df.iloc[:-1] if len(df) > 0 else df`.

### Response parsing — the per-bar record / DataFrame columns

The API returns JSON with a top-level `"Bars"` array; each bar is a dict such as
(see the sample fixture in `tests/test_downloader_volume_fields.py:9-50`):

```python
{
    "High": "6370", "Low": "6368.75", "Open": "6369.75", "Close": "6370",
    "TimeStamp": "2025-02-05T11:56:00Z", "TotalVolume": "213",
    "DownTicks": 71, "DownVolume": 103, "UpTicks": 91, "UpVolume": 110,
    "OpenInterest": "0", "IsRealtime": False, "IsEndOfHistory": False,
    "TotalTicks": 162, "UnchangedTicks": 0, "UnchangedVolume": 0,
    "Epoch": 1738756560000, "BarStatus": "Closed",
}
```

The column mapping and output column list are module-level constants
(`tradestation/downloader.py:23-36`):

```python
# Column mapping from API response to output
_COLUMN_MAP = {
    "TimeStamp": "datetime",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "TotalVolume": "volume",
    "UpVolume": "up_volume",
    "DownVolume": "down_volume",
    "UpTicks": "up_ticks",
    "DownTicks": "down_ticks",
}
_OUTPUT_COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "up_volume", "down_volume", "up_ticks", "down_ticks"]
```

The actual conversion is `_bars_to_dataframe` (`tradestation/downloader.py:302-323`):

```python
@staticmethod
def _bars_to_dataframe(bars: list[dict], start_date: datetime) -> pd.DataFrame:
    """Convert API bars to DataFrame."""
    if not bars:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    df = pd.DataFrame(bars)
    df["TimeStamp"] = pd.to_datetime(df["TimeStamp"])
    if df["TimeStamp"].dt.tz is not None:
        df["TimeStamp"] = df["TimeStamp"].dt.tz_convert(None)

    df = df.rename(columns=_COLUMN_MAP)
    df = df[[c for c in _OUTPUT_COLUMNS if c in df.columns]]

    # Convert OHLCV to numeric types
    for col in ["open", "high", "low", "close", "volume", "up_volume", "down_volume", "up_ticks", "down_ticks"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("datetime").drop_duplicates(subset=["datetime"], keep="last")
    df = df[df["datetime"] >= start_date]
    return df.reset_index(drop=True)
```

Key points:
- Only the 10 fields in `_COLUMN_MAP` are extracted from each bar. All other API
  fields (`OpenInterest`, `IsRealtime`, `IsEndOfHistory`, `TotalTicks`,
  `UnchangedTicks`, `UnchangedVolume`, `Epoch`, `BarStatus`) are dropped via the
  `df = df[[c for c in _OUTPUT_COLUMNS if c in df.columns]]` projection.
- `TimeStamp` is parsed to datetime and tz-stripped to naive (UTC) datetime.
- All 9 numeric columns are coerced via `pd.to_numeric(..., errors="coerce")`.
- Rows are sorted by `datetime`, de-duplicated on `datetime` keeping last, and
  filtered to `>= start_date`.
- When bars lack the up/down fields, `_OUTPUT_COLUMNS` still declares them, but
  the projection `if c in df.columns` means only present columns survive — so old
  API responses yield only the 6 base columns (see test
  `test_bars_without_up_down_volume`, `tests/test_downloader_volume_fields.py:97-122`).

---

## 2. Current parquet schema

### Trace: API response -> parsed record -> DataFrame -> parquet write

1. **API response** bar dict → fields listed in section 1.
2. **`_bars_to_dataframe`** (`tradestation/downloader.py:302-323`) renames via
   `_COLUMN_MAP` and projects to `_OUTPUT_COLUMNS`, producing a DataFrame with
   columns (order): `datetime, open, high, low, close, volume, up_volume,
   down_volume, up_ticks, down_ticks` (the up/down columns only appear when the
   API supplied them).
3. **`StorageBackend.append` / `save`** (`tradestation/storage.py`) calls
   `_prepare_dataframe` (`tradestation/storage.py:17-38`), which:
   - Converts `datetime` to datetime dtype, strips tz.
   - Sorts by `datetime`, drops duplicate datetimes (keep last).
   - If `datetime_index=True` (default, `DownloadConfig.datetime_index`,
     `tradestation/models.py:63`): sets `datetime` as the index.
   - Otherwise keeps `datetime` as a column.
4. **Parquet write** via `df.to_parquet(...)`:
   - `SingleFileStorage.save` (`tradestation/storage.py:128-130`):
     `df.to_parquet(self._get_filepath(symbol), index=self.datetime_index, compression=self.compression)`.
   - `DailyPartitionedStorage.save` (`tradestation/storage.py:173-180`) and
     `MonthlyPartitionedStorage.save` (`tradestation/storage.py:285-292`):
     group by date/month, optionally set datetime index, then `to_parquet`.

### Columns & dtypes in the parquet files

Verified by reading actual files with pandas:

**Current `data/@ES_index_1_1min.parquet`** (datetime index mode, new schema):

```
index:  datetime  datetime64[ns]
columns:
  open         float64
  high         float64
  low          float64
  close        float64
  volume         int64
  up_volume      int64
  down_volume    int64
  up_ticks       int64
  down_ticks     int64
```

**Legacy `old_data/minute_data/@ES_index_1_1min.parquet`** (old schema, only base columns):

```
index:  datetime  datetime64[ns]
columns:
  open    float64
  high    float64
  low     float64
  close   float64
  volume    int64
```

So the **current schema** (with `datetime_index=True`, the default) is:

| Column / index | dtype          | Source API field |
|----------------|----------------|------------------|
| `datetime` (index) | datetime64[ns] (naive UTC) | `TimeStamp` |
| `open`         | float64        | `Open`           |
| `high`         | float64        | `High`           |
| `low`          | float64        | `Low`            |
| `close`        | float64        | `Close`          |
| `volume`       | int64          | `TotalVolume`    |
| `up_volume`    | int64          | `UpVolume`       |
| `down_volume`  | int64          | `DownVolume`     |
| `up_ticks`     | int64          | `UpTicks`        |
| `down_ticks`   | int64          | `DownTicks`      |

Note: dtypes observed in real files are `int64` for the volume/tick columns
because the source values were integers; `_bars_to_dataframe` only enforces
`pd.to_numeric` (which can yield float64 if any value is non-integer). With
`datetime_index=False`, `datetime` becomes a regular `datetime64[ns]` column
instead of the index.

The README's "Output Format" table (`README.md:150-159`) is **out of date** — it
only lists `datetime, open, high, low, close, volume` and does not mention the
up/down volume and tick columns.

---

## 3. CSV export

**Yes, CSV export exists.** It is a dedicated module and a CLI flag.

### Where

- Module: `tradestation/csv_export.py` (117 lines).
- CLI flag: `--export-csv` in `tradestation/cli.py:99-103`:

```python
parser.add_argument(
    "--export-csv",
    action="store_true",
    help="Export downloaded data to CSV (.txt) files in a 'plain_data' sibling directory, then exit",
)
```

### How it is invoked

In `run_download` (`tradestation/cli.py:155-157`):

```python
if args.export_csv:
    from .csv_export import run_export_csv
    return run_export_csv(args.config, args.symbols)
```

So `tradestation-download --export-csv [-s SYMBOL ...]` loads `config.yaml`,
detects the storage format from `data_dir`, loads each symbol's parquet data via
the storage backend, and writes `.txt` CSV files. It does **not** download new
data first — it only exports what is already on disk.

### How it works (`tradestation/csv_export.py:21-116`)

1. `load_config(config_path)` (`csv_export.py:24`).
2. `detect_storage_format(data_dir)` (`csv_export.py:32`) — falls back to
   `config.storage_format` on exception.
3. `create_storage(...)` (`csv_export.py:37-42`).
4. `storage.list_symbols()` (`csv_export.py:51`); if none, exit 1.
5. If `symbols` given, normalize `@`-prefix (`_normalize_symbol`,
   `csv_export.py:16-18`) and match against available; missing symbol → exit 1.
6. Output dir = `data_dir.parent / "plain_data"` (`csv_export.py:70-71`) — a
   sibling of the data directory (e.g. `./plain_data/@ES.txt`).
7. For each symbol: `storage.load(symbol)`; if DatetimeIndex, reset to a
   `datetime` column (`csv_export.py:80-81`).
8. Derive `Date` (`%m/%d/%Y`) and `Time` (`%H:%M`) columns from `datetime`
   (`csv_export.py:83-84`).
9. Build output columns / rename map (`csv_export.py:86-103`):

```python
output_columns = ["Date", "Time", "Open", "High", "Low", "Close"]
rename_map = {
    "open": "Open", "high": "High", "low": "Low", "close": "Close",
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
```

So the CSV schema is **conditional**:
- If both `up_volume` and `down_volume` exist → columns
  `Date, Time, Open, High, Low, Close, Up, Down` (+ `Upticks, Downticks` if
  `up_ticks`/`down_ticks` also present). The aggregate `volume`/`Vol` column is
  **dropped** in this branch.
- Else if `volume` exists (legacy data) → columns
  `Date, Time, Open, High, Low, Close, Vol`.

10. Write header manually with quoted column names and CRLF
    (`csv_export.py:108-110`):

```python
f.write('"' + '","'.join(output_columns) + '"\r\n')
out_df.to_csv(f, header=False, index=False, lineterminator="\r\n")
```

Data rows are **not** quoted (only the header is). Line endings are CRLF.

### Tests

- `tests/test_csv_export.py` — covers the legacy `Vol` path: CSV format
  correctness, all three storage backends, export-all vs specific symbols,
  `@`-prefix normalization, missing-symbol error, empty data dir, path
  traversal rejection, output directory location, CRLF endings, and
  `datetime_index=False`. Expected header:
  `'"Date","Time","Open","High","Low","Close","Vol"'`
  (`tests/test_csv_export.py:80`).
- `tests/test_csv_export_up_down.py` — covers the up/down volume + ticks path:
  header `'"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks"'`
  (`tests/test_csv_export_up_down.py:26`), up/down without ticks, fallback to
  `Vol`, per-row value correctness, all storage backends.

### README / CLAUDE.md mentions

- `CLAUDE.md` does **not** mention CSV export (the project-structure section
  lists only `downloader.py`, `storage.py`, etc., and omits `csv_export.py`).
- `README.md` does **not** document `--export-csv` in its CLI examples section.
  So the feature is implemented and tested but undocumented in the README.

---

## 4. Storage backends (`tradestation/storage.py`)

### Common prep: `_prepare_dataframe` (`storage.py:17-38`)

All backends run data through `_prepare_dataframe(df, datetime_index)` before
writing. It:
- Handles a DatetimeIndex-only frame (resets/sorts/dedupes on the index).
- Otherwise converts the `datetime` column to datetime, strips tz, sorts,
  dedupes on `datetime` (keep last), and either sets `datetime` as index
  (`datetime_index=True`) or resets the index.

It does **not** impose a column schema — it preserves whatever non-datetime
columns the DataFrame has.

### `SingleFileStorage` (`storage.py:121-148`)

- File path: `data_dir / f"{folder}_1min.parquet"` where
  `folder = f"{symbol}_index_1"` if `datetime_index` else `symbol`
  (`storage.py:51-53`, `124-126`).
- `save` (`storage.py:128-130`): `_prepare_dataframe` then
  `df.to_parquet(filepath, index=self.datetime_index, compression=self.compression)`.
- `load` (`storage.py:132-140`): `pd.read_parquet` then `_prepare_dataframe`.
- `list_symbols` (`storage.py:142-144`): globs `*_1min.parquet`, strips
  `_index_1_1min` / `_1min` suffixes.
- Inherits default `append` (`storage.py:99-118`): load all, concat, dedupe on
  `datetime` keep last, sort, save.

### `DailyPartitionedStorage` (`storage.py:151-263`)

- Partition path: `data_dir/{folder}/year=YYYY/month=MM/day=DD/{folder}.parquet`
  (`storage.py:157-165`).
- `save` (`storage.py:173-180`): prep with `datetime_index=False` (keep datetime
  as a column for groupby), group by `df["datetime"].dt.date`, optionally set
  datetime index per group, write each group to its partition file.
- `load` (`storage.py:182-191`): concat all partition parquet files, then prep.
- Overrides `get_last_timestamp` / `get_first_timestamp`
  (`storage.py:204-236`) to read only the latest/earliest partition file.
- Overrides `append` (`storage.py:238-262`): only updates affected partitions —
  for each date group, if the partition file exists, read it, concat with the
  new group, dedupe on `datetime` keep last, sort, then write.

### `MonthlyPartitionedStorage` (`storage.py:265-374`)

- Partition path: `data_dir/{folder}/year_month=YYYY-MM/data-0.parquet`
  (`storage.py:271-277`).
- `save` (`storage.py:285-292`): group by `df["datetime"].dt.to_period("M")`,
  write each month.
- `load` (`storage.py:294-303`): concat all `year_month=*/*.parquet`.
- Overrides `get_last_timestamp` / `get_first_timestamp`
  (`storage.py:316-348`) to read only the latest/earliest partition.
- Overrides `append` (`storage.py:350-374`): same per-partition merge strategy
  as Daily, but grouped by month.

### Schema imposition?

**No explicit schema anywhere.** None of the backends declare a list of
columns or a pyarrow schema. They write whatever columns the incoming
DataFrame has (after `_prepare_dataframe`, which only manages the
`datetime` column/index). The only column-level logic is:
- `_prepare_dataframe` requires a `datetime` column (or DatetimeIndex) for
  sorting/dedup/grouping.
- Partitioned `save`/`append` group by `df["datetime"].dt.date` /
  `.dt.to_period("M")`, again requiring `datetime`.

**Would adding new columns "just work"?** Yes, for storage. If a new column is
added in `_bars_to_dataframe` (i.e. added to `_COLUMN_MAP` and `_OUTPUT_COLUMNS`)
and produced as a DataFrame column, all three backends will persist it via
`to_parquet` without any code change — the column will appear in the parquet
schema. The merge in `append` uses `pd.concat` + `drop_duplicates(subset=["datetime"])`,
which is column-agnostic: it concatenates all columns from both frames. The
only caveat is **mixed-schema merges**: if an existing partition/file has the
old column set and a new fetch has additional columns, `pd.concat` will
introduce `NaN` for the missing columns in the old rows (pandas aligns on
column names). This already happens today for the up/down volume migration and
works without error.

`detect_storage_format` (`storage.py:394-411`) inspects only directory
structure (`year=*/month=*/day=*`, `year_month=*`, etc.), not column schemas.

---

## 5. Models / config — explicit bar column enumeration

### `tradestation/models.py`

- `StorageFormat` enum (`models.py:9-23`): SINGLE / DAILY / MONTHLY.
- `Compression` enum (`models.py:26-42`): ZSTD / SNAPPY / GZIP / LZ4 / NONE.
- `DownloadConfig` dataclass (`models.py:45-70`): holds connection + storage
  settings. **No bar-field/column enumeration.** The only field related to bar
  shape is `interval=1`, `unit="Minute"`, `max_bars_per_request=57600`.
- `validate_symbol` (`models.py:73-82`): path/URL injection guard.
- `DEFAULT_SYMBOLS` (`models.py:86-172`) and helpers `get_all_symbols` /
  `get_symbols_by_category`: symbol lists only, no bar columns.

### `tradestation/config.py`

- `load_config` / `_parse_config` (`config.py:16-92`): reads YAML into
  `DownloadConfig`. **No bar-column enumeration.** It does not set or validate
  any column list.

### Where bar columns ARE enumerated

The **only** place bar columns are explicitly enumerated is in
`tradestation/downloader.py`:

- `_COLUMN_MAP` (`downloader.py:24-35`) — API field name → output column name.
- `_OUTPUT_COLUMNS` (`downloader.py:36`) — the ordered list of output columns.

And the CSV export module has its own separate, conditional column lists in
`tradestation/csv_export.py:86-103` (see section 3).

There is no shared "bar schema" constant imported by both downloader and
csv_export — each module defines its own column names independently.

---

## 6. Tests

Test infrastructure: **pytest** with `pytest-cov`
(`pyproject.toml:42-49`, `[tool.pytest.ini_options]` at `pyproject.toml:85-87`:
`testpaths = ["tests"]`, `addopts = "-v --cov=tradestation --cov-report=term-missing"`).
Shared fixture `temp_data_dir` (a `TemporaryDirectory` Path) in
`tests/conftest.py:9-13`. `tests/__init__.py` is empty (package marker).

Test files:

| File | Lines | Covers |
|------|-------|--------|
| `tests/conftest.py` | 13 | Shared `temp_data_dir` fixture. |
| `tests/test_models.py` | 80 | `StorageFormat` enum values/`from_string` (incl. invalid), `DownloadConfig` defaults + string→enum conversion, `get_all_symbols`/`get_symbols_by_category` (incl. invalid category), `DEFAULT_SYMBOLS` category set. |
| `tests/test_storage.py` | 173 | `SingleFileStorage` save/load (asserts columns `["open","high","low","close","volume"]` and `index.name=="datetime"`), load nonexistent, list_symbols, file size; `DailyPartitionedStorage` save/load + partition dir creation; `MonthlyPartitionedStorage` save/load + partition dir creation; `create_storage` factory; `detect_storage_format` for single/daily/monthly/empty. |
| `tests/test_downloader_volume_fields.py` | 131 | `_bars_to_dataframe` with up/down volume + ticks: exact column order `["datetime","open","high","low","close","volume","up_volume","down_volume","up_ticks","down_ticks"]`, value correctness, aggregate `volume` retained, `up+down==total` invariant, bars without up/down fields yield only base columns (and asserts the new fields are declared in `_OUTPUT_COLUMNS`), numeric dtype check. Uses realistic `SAMPLE_BARS` fixture mirroring the API response. |
| `tests/test_csv_export.py` | 285 | Legacy `Vol` CSV export path: header `'"Date","Time","Open","High","Low","Close","Vol"'`, row count, date/time/OHLCV values, no data quoting, all three storage backends, export-all vs specific symbols, `@`-prefix normalization (`BO` and `@BO`), missing-symbol error (exit 1), empty data dir (exit 1), path-traversal rejection, output dir is `plain_data` sibling, CRLF line endings, `datetime_index=False` mode. |
| `tests/test_csv_export_up_down.py` | 287 | Up/down CSV export path: header `'"Date","Time","Open","High","Low","Close","Up","Down","Upticks","Downticks"'`, `Vol` dropped when Up/Down present, up/down without ticks, fallback to `Vol` for legacy data, per-row value correctness, all three storage backends (exact full-file CSV equality). |
| `tests/test_metadata.py` | 1085 | `derive_session_times` (basic, multi-day, overnight/midnight, empty, DatetimeIndex, datetime column, excludes empty weekdays, DST-mirror bug); `fetch_symbol_details` (single batch, 50-symbol batching, 500 error, per-symbol error logging); `fetch_quote_snapshots` (single batch, 100-symbol batching, 500 error); `get_first_timestamp` for all backends; `run_metadata` full flow (writes `metadata.json`, prints JSON), no-symbols error, API error, DST-spanning mirror, unmapped-ICE warning, mapped ICEUS no-warning; `derive_sessions` (empty, around-the-clock, single block, spans midnight, two blocks, real-world wheat, small gaps ignored, index vs column, DST collapse characterization); `select_standard_time_bars` (prefers winter/standard time); `get_exchange_timezone` (known/unknown/case-insensitive, NYMEX/COMEX→New York, CBOEF→Chicago). |
| `tests/test_security_fixes.py` | 307 | `validate_symbol`: valid symbols, path traversal rejection, URL injection rejection, control chars, non-string, `..` substring. `_api_request` 401: refresh-once-then-succeed, persistent 401 raises after one retry, `max_retries=0` fails immediately. `_api_request` 429: retries then succeeds, persistent stops after `max_retries`. OAuth `CallbackHandler` state matching/mismatch, unique state per authorization. `exchange_code_for_tokens` timeout=30. |

Coverage is configured with branch coverage
(`pyproject.toml:89-91`, `[tool.coverage.run] source=["tradestation"], branch=true`).
A `.coverage` file and `.pytest_cache` exist, indicating tests have been run.

---

## 7. Incremental update logic

### Deciding what to fetch

`TradeStationDownloader.download_symbol` (`downloader.py:185-212`) calls
`_get_download_start` (`downloader.py:167-183`):

```python
def _get_download_start(self, symbol: str, incremental: bool) -> tuple[datetime, bool]:
    config_start = datetime.strptime(self.config.start_date, "%Y-%m-%d")
    if not incremental:
        return config_start, False
    last_timestamp = self._storage.get_last_timestamp(symbol)
    if last_timestamp is None:
        return config_start, False
    return last_timestamp, True  # Re-fetch last bar to ensure completeness
```

- If `--full` (incremental=False) → start from `config.start_date`.
- Else → ask the storage backend for the last timestamp
  (`get_last_timestamp`). If none, start from `config.start_date`. If present,
  **re-fetch starting at the last existing timestamp** (the comment says
  "Re-fetch last bar to ensure completeness") — so the last bar is re-downloaded
  and merged.

`get_last_timestamp` (`storage.py:71-83` default; overridden for Daily at
`storage.py:204-219` and Monthly at `storage.py:316-331`) reads only the
latest partition for partitioned backends (an optimization), or loads the whole
file for `SingleFileStorage`.

### Merging / appending new bars

`download_symbol` calls `self._storage.append(symbol, new_df)`
(`downloader.py:207`).

- **Default `StorageBackend.append`** (`storage.py:99-118`, used by
  `SingleFileStorage`): loads existing, resets index to a `datetime` column on
  both frames, `pd.concat([existing_df, new_df], ignore_index=True)`,
  `drop_duplicates(subset=["datetime"], keep="last")`, `sort_values("datetime")`,
  then `save`.

- **`DailyPartitionedStorage.append`** (`storage.py:238-262`): only updates
  affected day partitions. For each date group in `new_df`, if the partition
  file exists, read it, concat, dedupe on `datetime` keep last, sort, write.

- **`MonthlyPartitionedStorage.append`** (`storage.py:350-374`): same strategy
  per month partition.

### Does it rely on a specific schema?

**No.** The merge logic is column-agnostic:
- Dedup key is always `subset=["datetime"]` — only the `datetime` column is
  semantically required.
- `pd.concat` aligns columns by name. If `new_df` has extra columns the
  existing file lacks (or vice versa), the missing columns become `NaN` in the
  rows that lack them — no error, no schema comparison, no column-order check.
- There is no code that compares `existing_df.columns` to `new_df.columns` or
  to `_OUTPUT_COLUMNS` during append.

So incremental updates "just work" when new columns are added, with the
caveat that **old rows will have `NaN`/null for the new columns** until those
partitions/files are fully rewritten (e.g. via `--full`). The partitioned
backends mitigate this per-partition: only the partition(s) touched by the new
fetch get re-merged, so old partitions remain with the old column set. A full
re-download (`--full`) is the way to backfill new columns across all history.

---

## Summary of key facts for implementers

1. **Bar fields are defined in exactly one place**: `_COLUMN_MAP` and
   `_OUTPUT_COLUMNS` at `tradestation/downloader.py:24-36`. To add a new bar
   column, add it to both (and ensure the API field name maps correctly).
2. **Storage is schema-less**: all three backends write whatever columns the
   DataFrame has; adding columns requires no storage changes.
3. **Incremental append is column-agnostic** (`pd.concat` + dedupe on
   `datetime`); new columns will be `NaN` in old rows/partitions that aren't
   re-fetched.
4. **CSV export has its own conditional column logic** at
   `tradestation/csv_export.py:86-103` — it is NOT driven by
   `_OUTPUT_COLUMNS`. Adding a new bar column will NOT automatically appear in
   CSV export; the export's `if "col" in df.columns` branches must be updated
   separately.
5. **Parquet schema (current, datetime_index=True)**: index `datetime`
   (datetime64[ns]), columns `open, high, low, close` (float64) and `volume,
   up_volume, down_volume, up_ticks, down_ticks` (int64). Legacy files lack the
   four up/down columns.
6. **README/CLAUDE.md are partially out of date**: they don't mention the
   up/down volume/tick columns, `csv_export.py`, or the `--export-csv` flag.
7. **Tests are comprehensive** (pytest + branch coverage) and include TDD-style
   "Red phase" tests that pin the exact expected column order and CSV header
   strings — any schema change should update these tests in lockstep.
