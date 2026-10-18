# TradeStation Historical Data Downloader

Automated download of 1-minute futures data from TradeStation API with incremental updates, rate limiting, and Parquet storage.

## Features

- **OAuth2 Authentication** - Automatic token refresh
- **Incremental Updates** - Only downloads new bars since last run
- **Parallel Downloads** - Up to 4 concurrent symbol downloads (configurable)
- **Rate Limiting** - Respects API limits with configurable delays
- **Parquet Storage** - Fast, compressed columnar format
- **Resume Capability** - Interrupted downloads can be resumed
- **All US Futures** - Pre-configured list of ~50 continuous contracts

## Quick Start

### 1. Install

```bash
# From PyPI (recommended)
pip install tradestation-downloader

# Or from source using uv
pip install uv
uv sync

# Or standard pip from source
pip install -e .

# For development (includes pytest, ruff)
pip install -e ".[dev]"
```

### 2. Get TradeStation API Credentials

1. Go to [TradeStation Developer Portal](https://developer.tradestation.com/)
2. Create an application
3. Get your `client_id`, `client_secret`, and `refresh_token`

### 3. Configure

Option A - Interactive setup (recommended):
```bash
tradestation-auth
```

Option B - Manual setup:
```bash
cp config.yaml.template config.yaml
# Edit config.yaml with your credentials
```

### 4. Run

After install, CLI commands are available:

```bash
# Download all configured symbols (incremental)
tradestation-download

# Download specific symbols only
tradestation-download -s "@ES" "@NQ" "@CL"

# Full download (ignore existing data)
tradestation-download --full

# Use daily or monthly partitioned storage
tradestation-download --storage-format daily
tradestation-download --storage-format monthly

# Use different compression (default: zstd)
tradestation-download --compression snappy

# List all default symbols
tradestation-download --list-symbols

# List symbol categories
tradestation-download --list-categories

# Download specific category
tradestation-download --category index

# Save without datetime index (raw format)
tradestation-download -s @ES --no-datetime-index

# Use more parallel workers for faster downloads (default: 4)
tradestation-download -w 8

# Sequential download (no parallelism)
tradestation-download -w 1

# Check whether each symbol's front contract should roll to the next
tradestation-download --rollcheck

# Roll check with a custom days-to-expiry/first-notice trigger (default: 6)
tradestation-download --rollcheck --roll-days 10
```

#### Rollover check output

`--rollcheck` prints one row per symbol and recommends `ROLL -> <next>` or
`keep <current>`. A roll is recommended when the next contract's open interest
or volume exceeds the current contract's, or when the current contract's
deadline is within `--roll-days` days (default: 6).

The `EXP/FND` column shows the deadline: the earlier of the contract's
**expiration date `{ED}`** and **first notice date `{FND}`** (the day after
which long holders may be assigned physical delivery — important for
physically-delivered contracts like CL or GC). If one date is unavailable —
e.g. cash-settled contracts have no first notice date — the other is used.
`Days` counts down to that deadline.

```
Symbol | Current        | Next           | OI next/cur | Vol next/cur | EXP/FND          | Days | Action
-------+----------------+----------------+-------------+--------------+------------------+------+-------------
@ES    | ESZ25 Dec 2025 | ESH26 Mar 2026 | 10.0%       | 2.0%         | 2025-12-19 {ED}  | 30   | keep ESZ25
@CL    | CLZ25 Dec 2025 | CLF26 Jan 2026 | 45.0%       | 60.0%        | 2025-11-20 {FND} | 5    | ROLL -> CLF26
```

> **Note:** On Windows, the `@` symbol has special meaning in CMD/PowerShell.
> Always quote symbols: `"@ES"` instead of `@ES`.

Or run directly with Python:

```bash
python tradestation_downloader.py
python tradestation_downloader.py -s "@ES" "@NQ" "@CL"
```

## Configuration

Edit `config.yaml`:

```yaml
tradestation:
  client_id: "YOUR_CLIENT_ID"
  client_secret: "YOUR_CLIENT_SECRET"
  refresh_token: "YOUR_REFRESH_TOKEN"

data_dir: "./data"
start_date: "2007-01-01"
interval: 1
unit: "Minute"
storage_format: "single"  # single, daily, or monthly
compression: "zstd"       # zstd, snappy, gzip, lz4, or none
max_workers: 4            # parallel download workers (1 = sequential)

symbols:
  - "@ES"    # E-mini S&P 500
  - "@NQ"    # E-mini Nasdaq 100
  # ... add more symbols
```

## Output Format

Data is saved as Parquet files in the `data_dir`. Structure depends on storage format:

**Single file (with datetime index):**
```
data/
├── @ES_index_1_1min.parquet
├── @NQ_index_1_1min.parquet
└── @CL_index_1_1min.parquet
```

**Monthly partitioned (default with datetime index):**
```
data/
├── @ES_index_1/
│   ├── year_month=2024-01/data-0.parquet
│   ├── year_month=2024-02/data-0.parquet
│   └── ...
└── @NQ_index_1/
    └── ...
```

Each file contains:

| Column   | Type     | Description              |
|----------|----------|--------------------------|
| datetime | datetime | Bar timestamp (UTC)      |
| open     | float    | Opening price            |
| high     | float    | High price               |
| low      | float    | Low price                |
| close    | float    | Closing price            |
| volume   | int      | Total volume             |

## DateTime Index Mode

By default, data is saved with `datetime` as the DataFrame index, enabling pandas time-series operations like `resample()` and time-based slicing. This adds an `_index_1` suffix to folder names.

**Default (recommended):**
```bash
tradestation-download -s @ES
```
→ Creates `@ES_index_1/` folder with datetime as index

**Raw format (datetime as column):**
```bash
tradestation-download -s @ES --no-datetime-index
```
→ Creates `@ES/` folder with datetime as regular column

**Why use datetime index?**
- `df.resample('5min')` works directly
- Time slicing: `df['2024-01':'2024-06']`
- Compatible with pandas time-series functions
- Matches common conventions for OHLCV data

**Folder structure with index (default):**
```
data/
├── @ES_index_1/
│   ├── year_month=2024-01/data-0.parquet
│   └── year_month=2024-02/data-0.parquet
└── @NQ_index_1/
    └── ...
```

## Orders Mode

Download historical orders for the logged-in TradeStation account to a CSV file. `orders` must be the first argument after the program name; only the `tradestation:` credentials section of `config.yaml` is used (symbols, `data_dir`, storage format, etc. are ignored). Orders are cached incrementally as Parquet under `orders/cache/<account_id>/`, so repeat runs only download not-yet-cached data.

```bash
tradestation-download orders                          # all history, auto account
tradestation-download orders -s 2024-01-01 -e 2025-01-31
tradestation-download orders -s 2024 -e 2024-06       # year / year-month forms
tradestation-download orders --full -o my_orders.csv
tradestation-download orders -a 12345678 -s 2025-01-01 -e latest
tradestation-download orders --include-open            # also include still-open orders
```

| Flag | Long | Default | Description |
|------|------|---------|-------------|
| `-c` | `--config` | `config.yaml` | Path to configuration file |
| | `--full` | off | Discard existing local order cache and re-download everything |
| | `--include-open` | off | Include open orders (default: only closed orders are downloaded) |
| `-s` | `--start` | `earliest` (today − 729 days) | Start date: `YYYY-MM-DD`, `YYYY-MM`, `YYYY`, or `earliest` |
| `-e` | `--end` | `latest` (today) | End date: `YYYY-MM-DD`, `YYYY-MM`, `YYYY`, or `latest` |
| `-o` | `--out` | `orders/<account>_<start>_<end>.csv` | Output CSV path |
| `-a` | `--account` | auto (single account) | TradeStation account ID (required with multiple accounts) |
| `-v` | `--verbose` | off | Enable verbose (debug) logging |

**Output and cache layout:**

```
orders/
├── cache/
│   └── <account_id>/
│       ├── orders.parquet        # incremental order cache
│       └── coverage.json         # which span has been fetched
└── <account_id>_<start>_<end>.csv   # e.g. 12345678_2025-01-01_2026-10-17.csv
```

The CSV is generated from the cache for the requested `--start`/`--end` span and always replaced. Note the API only provides roughly the last 730 days of order history.

### Converting orders to TradingDiary Pro

`orders-to-tdpro` converts an orders CSV into the 17-column format TradingDiary Pro's Excel Importer expects — futures only. Each executed leg becomes one trade row: buys are positive quantities, sells are negative. Contract multipliers come from a small built-in table (CME-verified: MES=5, MNG=1000, 1OZ=1) and can be added or overridden with `-m ROOT=VALUE`; an unknown underlying root fails with an error naming the missing roots. `Conversion rate` is left empty so TD Pro applies its own FX rate, unless `--fx BASE` is given (see below). Order datetimes are written as naive UTC (as stored in the orders CSV) — TD Pro will show them as-is, so users in other timezones should be aware.

```bash
uv run orders-to-tdpro orders/<account_id>_<start>_<end>.csv
uv run orders-to-tdpro orders/orders.csv -o tdpro_import.csv
uv run orders-to-tdpro orders/orders.csv -m ES=50 -m MGC=10
uv run orders-to-tdpro orders/orders.csv --fx EUR
```

| Flag | Long | Default | Description |
|------|------|---------|-------------|
| `-o` | `--out` | `<input_stem>_tdpro.csv` next to input | Output CSV path |
| `-m` | `--multiplier` | built-in table only | Add/override a contract point value as `ROOT=VALUE`; repeatable |
| | `--fx` | off (column empty) | Fill Conversion rate with ECB daily reference rates to `BASE` (e.g. `EUR`) |
| | `--fx-date` | `berlin` | Calendar date for the ECB rate lookup: `berlin` = German-local fill date (tax-relevant), `utc` = recorded date. Requires `--fx` |
| `-v` | `--verbose` | off | Enable verbose (debug) logging |

`--fx EUR` writes the official **ECB Euro-Referenzkurs** per leg date into `Conversion rate` — the per-leg conversion mandated for German tax reporting (R 34c Abs. 1 EStR / § 20 Abs. 4 Satz 1 EStG). The ECB publishes around **16:00 CET** on TARGET working days. The cache records each fetch's coverage only through the newest publication the ECB actually returned — a run before (or during a delayed) publication never claims the unpublished day, so re-running after the publication lands refetches once and picks up the same-day rate. A fill needing a rate that isn't published yet gets the last published rate plus a warning to re-run later. Rate dates are the fill's German-local (Europe/Berlin) calendar date by default — e.g. fills late Sunday evening UTC count as Monday fills during CEST but remain Sunday-dated during CET; `--fx-date utc` uses the recorded UTC date instead. Weekend/holiday dates resolve to the last previously published rate.

Rates cache to `orders/fx_cache/<CCY>.json` — human-inspectable files recording both the published rates and the fetched date windows that prove coverage. A currency is only re-fetched when its cached windows cannot cover the needed dates; deleting a currency's file forces a clean re-fetch.

`-m` values are not persisted — repeat the flag on each run that needs them.

## Loading Data

```python
import pandas as pd

# Load single symbol (monthly partitioned with datetime index)
df = pd.read_parquet("data/@ES_index_1")
print(df.head())

# Resample to 5-minute bars (works because datetime is the index)
df_5min = df.resample('5min').agg({
    'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
}).dropna()

# Time-based slicing
df_2024 = df['2024-01':'2024-12']

# Load single file format
df = pd.read_parquet("data/ES_1min.parquet")
```

## Python API

```bash
pip install tradestation-downloader
```

Use programmatically in your project:

```python
from tradestation import TradeStationDownloader, DownloadConfig, StorageFormat, Compression

config = DownloadConfig(
    client_id="your_client_id",
    client_secret="your_client_secret",
    refresh_token="your_refresh_token",
    symbols=["@ES"],
    data_dir="./data",
    start_date="2020-01-01",
    storage_format=StorageFormat.SINGLE,
    compression=Compression.ZSTD,  # or SNAPPY, GZIP, LZ4, NONE
    max_workers=4,  # parallel downloads (1 = sequential)
)

downloader = TradeStationDownloader(config)
data = downloader.download_all()

# Access download statistics
stats = downloader.stats
print(f"Downloaded {stats.bars_downloaded} bars")
print(f"Processed {stats.symbols_processed} symbols")
print(f"Errors: {stats.errors}")

# Use the data
es_df = data["@ES"]
print(es_df.head())
```

Or load from config file:

```python
from tradestation import TradeStationDownloader, load_config

config = load_config("config.yaml")
config.symbols = ["@ES", "@NQ"]  # Override symbols

downloader = TradeStationDownloader(config)
downloader.download_all()
```

## Scheduling Daily Updates

### Linux/Mac (cron)

```bash
# Edit crontab
crontab -e

# Add line to run daily at 6 AM
0 6 * * * cd /path/to/tradestation_downloader && python tradestation_downloader.py >> logs/download.log 2>&1
```

### Windows (Task Scheduler)

1. Open Task Scheduler
2. Create Basic Task
3. Set trigger: Daily at 6:00 AM
4. Action: Start a program
5. Program: `python`
6. Arguments: `C:\path\to\tradestation_downloader.py`
7. Start in: `C:\path\to\tradestation_downloader`

## Data Validation

```python
import pandas as pd
from pathlib import Path

data_dir = Path("./data")

for f in data_dir.glob("*_1min.parquet"):
    df = pd.read_parquet(f)
    symbol = f.stem.replace("_1min", "")
    
    print(f"{symbol}:")
    print(f"  Date range: {df['datetime'].min()} to {df['datetime'].max()}")
    print(f"  Total bars: {len(df):,}")
    print(f"  Missing dates: {df['datetime'].diff().gt(pd.Timedelta(minutes=2)).sum()}")
    print()
```

## Troubleshooting

### "401 Unauthorized" Error

Your refresh token may have expired. Run `tradestation-auth` to get a new one.

### "429 Rate Limited" Error

The script handles this automatically, but if persistent:
- Reduce `max_workers` (e.g., `-w 2`)
- Increase `rate_limit_delay` in config
- Reduce number of symbols per run

TradeStation allows 500 requests per 5-minute window for bar chart data. The default settings (4 workers, 0.2s delay) stay well within these limits.

### Missing Data

Some symbols may not have data going back to 2007. Check TradeStation's data availability for specific contracts.

### Large Download Size

1-minute data from 2007 is ~500MB-1GB per symbol. Total for all US futures: ~30-50GB.

## Default Symbols

Run `tradestation-download --list-symbols` to see all default symbols:

- **Equity Index**: @ES, @NQ, @YM, @RTY, @MES, @MNQ, etc.
- **Energy**: @CL, @NG, @RB, @HO, @BRN
- **Metals**: @GC, @SI, @HG, @PL, @PA
- **Treasury**: @US, @TY, @FV, @TU, @UB, @TEN, @TWE
- **Grains**: @C, @S, @W, @KW, @BO, @SM
- **Softs**: @KC, @SB, @CT, @CC, @OJ, @LBR
- **Meats**: @LC, @LH, @FC
- **Currency**: @EC, @JY, @BP, @AD, @CD, @SF, @DX
- **Volatility**: @VX
- **Crypto**: @BTC, @ETH, @MBT, @MET

## License

MIT License - Free to use and modify.

## Support

For TradeStation API issues: [TradeStation Developer Support](https://developer.tradestation.com/)
