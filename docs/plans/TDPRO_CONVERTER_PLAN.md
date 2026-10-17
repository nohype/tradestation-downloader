# orders-to-tdpro — Implementation Plan

Companion converter: turns an orders CSV export (produced by
`tradestation-download orders`) into the CSV format TradingDiary Pro's
Excel Importer expects, for **Futures only**.

Requirement: `uv run orders-to-tdpro <file_path>` converts the orders CSV at
`<file_path>` into a TradingDiary Pro import file. DRY, YAGNI, simple.

---

## 1. Verified facts (evidence, not assumptions)

### Input (orders CSV — real file `orders/<account_id>_<start>_<end>.csv`, 346 rows)

- 29 columns; relevant ones: `datetime`, `OrderID`, `Status`, `Legs`,
  `FilledPrice`, `CommissionFee`, `Currency`, `ConversionRate`.
- `Legs` is a JSON array; every observed order has exactly 1 leg.
  Leg fields: `AssetType` ("FUTURE"), `BuyOrSell` ("Buy"/"Sell"),
  `ExecQuantity` (string int), `ExecutionPrice` (string),
  `ExpirationDate` ("2026-03-20T00:00:00Z"), `Symbol` ("MESH26"),
  `Underlying` ("MES"), plus OpenOrClose/QuantityOrdered/QuantityRemaining.
- Statuses: FLL (filled), OUT, EXP, REJ. Verified: a row has any
  `ExecQuantity > 0` **iff** Status == FLL (in this dataset). Semantically
  correct filter = legs with `ExecQuantity > 0` regardless of status
  (covers partial fills if they ever occur).
- `ExecutionPrice` == `FilledPrice` on all filled rows.
- `CommissionFee` is a per-order total (0.5 per contract observed:
  qty 1 → 0.5, qty 2 → 1.0).
- `Currency` = USD everywhere; `ConversionRate` = "1".
- `OrderID` unique per row.
- `datetime` = naive UTC, `YYYY-MM-DDTHH:MM:SS.ffffff`.

### Output (TradingDiary Pro Excel Importer — docs/tdpro_csv_format.txt + webhelp.tradingdiarypro.com)

- 17 columns in this exact order (header spellings from the doc list):
  `TransactionID`, `DateTime`, `Ticker`, `Description`, `Quantity`,
  `Price`, `Commission`, `Asset Category`, `Multiplier`, `Currency`,
  `Conversion rate`, `Expiration Date`, `Call or Put`, `Strike`, `Note`,
  `Exchange`, `Tag(s)`.
- **Buy = positive Quantity, Sell = negative** — confirmed by official docs.
- Each row = one raw execution/fill; TD Pro FIFO-matches opens/closes itself.
- `Multiplier` = contract point value ("Amount = Price × Quantity ×
  Multiplier"). Verified CME values: MES=5, MNG=1000, 1OZ=1.
- Unrecognized tickers auto-create instruments on import; the Ticker string
  just needs to be unique & consistent per contract.
- `Description` defaults to Ticker when empty; `Conversion rate` only used
  when trade currency ≠ base currency; `TransactionID` used for
  duplicate-detection.

### Date/decimal formats

Not documented anywhere. PM ruling (documented choice, not an assumption):
`DateTime` → `YYYY-MM-DD HH:MM:SS`; `Expiration Date` → `YYYY-MM-DD`;
plain `.` decimal separator throughout.

---

## 2. Design

New self-contained feature module `tradestation/tdpro.py` (mirrors
`orders.py`'s "self-contained feature module" convention), a thin parser +
entry point in `cli.py`, and a `[project.scripts]` entry so
`uv run orders-to-tdpro <file>` works — same pattern as
`tradestation-download` / `tradestation-auth`.

```
uv run orders-to-tdpro orders/<account_id>_<start>_<end>.csv
        │
        ▼
cli.py  main_tdpro() → create_tdpro_parser()
        │
        ▼
tdpro.convert_orders_csv(input_path, output_path, multipliers)
        │  read → filter fills → map rows → atomic write
        ▼
orders/<account_id>_<start>_<end>_tdpro.csv
```

### CLI

```
orders-to-tdpro FILE [-o OUT] [-m ROOT=VALUE ...] [-v]
```

- `FILE` positional: orders CSV produced by `tradestation-download orders`.
- `-o/--out FILE`: output path. Default: `<input_stem>_tdpro.csv` beside input.
- `-m/--multiplier ROOT=VALUE`: repeatable; adds/overrides point values
  (e.g. `-m ES=50`). Parsed strictly; bad form or non-positive → argparse error.
- `-v/--verbose`: debug logging (repo convention).

### Conversion rules (per executed leg → one output row)

| TD Pro column | Source / rule |
|---|---|
| TransactionID | `{OrderID}-{leg_index}` (leg_index = 1-based position in Legs array; deterministic, unique, stable for re-import dedup) |
| DateTime | row `datetime`, reformatted `YYYY-MM-DD HH:MM:SS` |
| Ticker | leg `Symbol` verbatim (e.g. `MESH26`) |
| Description | empty (defaults to Ticker) |
| Quantity | `+ExecQuantity` for Buy, `-ExecQuantity` for Sell |
| Price | leg `ExecutionPrice`; fallback row `FilledPrice` |
| Commission | `CommissionFee × (leg ExecQuantity / order total ExecQuantity)` — whole fee for single-leg orders; proportional for multi-leg |
| Asset Category | `Futures` |
| Multiplier | `FUTURES_MULTIPLIERS[underlying]`, override via `-m` |
| Currency | row `Currency` |
| Conversion rate | **empty** — see §3 |
| Expiration Date | leg `ExpirationDate` → `YYYY-MM-DD` |
| Call or Put | empty (unused for Futures) |
| Strike | empty (unused for Futures) |
| Note | empty |
| Exchange | empty |
| Tag(s) | empty |

Multiplier key = leg `Underlying`; if absent, fall back to
`models.base_symbol(leg Symbol)` (strips month-code/year → root). Any
underlying with no known multiplier → **error exit 1** naming all missing
roots and showing the exact `-m ROOT=VALUE` syntax to fix it (fail fast,
self-documenting — Lean UX).

`FUTURES_MULTIPLIERS` (module constant, only CME-verified values):
`{"MES": 5, "MNG": 1000, "1OZ": 1}`.

### Validation & error handling

- Input must exist and contain columns: `datetime`, `OrderID`, `Legs`,
  `FilledPrice`, `CommissionFee`, `Currency`, `ConversionRate` → else exit 1
  with a clear message ("is this a tradestation-downloader orders CSV?").
- Malformed `Legs` JSON → `logger.warning`, skip row, count it.
- Executed leg with no usable price (no `ExecutionPrice`, no `FilledPrice`)
  → warning, skip leg, count it.
- Unparseable `datetime` on an otherwise-executed row → warning, skip.
- Non-executed orders (all legs `ExecQuantity` == 0) → skipped silently,
  counted in summary.
- Atomic write: `*.tmp` sibling + `Path.replace` (repo convention).
- Success line (mirrors orders' `Wrote {path} ({n:,} orders)`):
  `Wrote {out} ({n:,} trades from {m:,} orders)` — and if anything was
  skipped, a preceding `logger.warning` count.
- 0 trades → still writes header-only CSV, prints the Wrote line with 0.
- Exit codes: 0 ok; 1 input/validation error; 130 KeyboardInterrupt
  (repo convention).

### Read/parse notes

- `pd.read_csv(..., dtype=str, keep_default_na=False)` — all strings,
  empty stays `""` (matches how the orders CSV was probed; avoids NaN soup).
- `Legs` parsed per row with `json.loads`.
- Numbers: `ExecQuantity` via `int(str)`; prices/commission via
  `Decimal`? — No: keep it simple, pass numeric strings through to the CSV
  verbatim where possible (price verbatim, commission formatted without
  float artifacts via `Decimal`). PM ruling: use `Decimal` for the
  commission arithmetic (money math, avoids 0.1+0.2 artifacts); prices are
  copied verbatim.
- Output CSV: utf-8, `lineterminator="\n"` (repo convention).

---

## 3. Rulings & rejected alternatives

- **Conversion rate left empty.** Source value is always `1` (= account is
  USD-based at TradeStation). Writing `1` into TD Pro would be *wrong* if
  the user's TD Pro base currency differs from the trade currency; empty
  lets TD Pro use its own FX rate. Documented in help/README.
- **Commission split proportional to ExecQuantity** across executed legs.
  All observed orders are single-leg (gets whole fee); proportional split is
  the only allocation that stays correct for multi-leg without inventing
  semantics.
- **Unusable CommissionFee → empty Commission, row still emitted.** When the
  fee is empty, unparseable, or non-finite (`NaN`/`Infinity`), the fill row is
  still written with the Commission cell left empty plus a `logger.warning`
  naming the OrderID and raw value. Commission is optional in TD Pro, and
  dropping the whole fill would corrupt FIFO position matching far worse than
  a missing fee — qty/price/side are known-good.
- **Empty OrderID → digest TransactionID.** `orders.py` can emit null-OrderID
  rows; `{OrderID}-{leg_index}` would collapse them all to `"-1"` and TD Pro's
  TransactionID dedup would silently drop trades. Instead the converter emits
  `ts-<sha1[:12]>-{leg_index}` over `json.dumps(leg)|datetime|CommissionFee|
  AccountID` — deterministic, collision-resistant (identical fills share an
  ID — acceptable, they are indistinguishable anyway) — plus a
  `logger.warning`.
- **No open-order handling needed** — legs with `ExecQuantity=0` never
  produce rows regardless of status.
- **Only FUTURE legs convert.** A leg with `AssetType` other than `"FUTURE"`
  is skipped with a `logger.warning` naming the OrderID and AssetType, and
  counted as a bad/skipped row — never silently converted as Futures.
- **`Symbol` verbatim as Ticker** (not `Underlying`); the contract code is
  the unique instrument identifier; `Expiration Date` reinforces identity.
- **No config.yaml dependency.** The converter is a pure file tool; `-m`
  covers unknown roots. A config section would couple the tools for no
  benefit (YAGNI).
- **No batch/glob input** — spec is one file (YAGNI).
- **Entry via `[project.scripts]`** (`orders-to-tdpro =
  "tradestation.cli:main_tdpro"`), not a loose script file: matches repo
  pattern and makes `uv run orders-to-tdpro` work after `uv sync`.

## 4. Test plan (`tests/test_tdpro.py`, pytest, repo style)

- Golden conversion of a small synthetic orders CSV → expected 17-col CSV
  (buy → +qty, sell → −qty, fee, multiplier, expiry, TransactionID).
- Non-executed rows (OUT/EXP/REJ, ExecQuantity 0) produce no output rows.
- `CommissionFee` split across a multi-leg order.
- `-m` override adds an unknown root; unknown root without `-m` → exit 1.
- Malformed Legs / missing price / bad datetime → warnings + skip, still 0.
- Missing required column → exit 1.
- Default `-o` naming; `-o` honored; atomic output.
- Empty (header-only) input → header-only output, exit 0.
- CLI: parser shape, `main_tdpro` dispatch, `--help` text.
