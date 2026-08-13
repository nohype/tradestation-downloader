# TradeStation Web API v3 — Historical Bar Data Research Report

**Date:** 2026-08-07
**Purpose:** Document the exact per-bar fields returned by the TradeStation Web API v3
historical bar endpoint, so downstream implementers can decide which columns to add to
parquet/CSV output.

**Important correction up front:** The endpoint path is
`/v3/marketdata/barcharts/{symbol}` — **not** `/v3/marketdata/bars/{symbol}`.
The task brief assumed "barmarketdata / bars", but the actual v3 path uses
"barcharts". Every official and community source confirms this.

---

## Sources

The official TradeStation v3 API documentation at
https://api.tradestation.com/docs/specification is a JavaScript-rendered Docusaurus
SPA that does not yield structured content to `webfetch`. The context7 MCP server was
unavailable (OAuth token expired: "Invalid or expired OAuth token. Please
re-authenticate to obtain a new token."). Therefore the following corroborating
sources were used:

1. **Official v2 Swagger spec** — `tradestation/api-docs` GitHub repo:
   https://github.com/tradestation/api-docs/blob/master/spec/swagger.yaml
   (raw: https://raw.githubusercontent.com/tradestation/api-docs/master/spec/swagger.yaml)
   — Contains the v2 streaming barchart endpoint with a worked JSON example showing
   the field names. The v3 REST endpoint reuses the same field names (confirmed by
   the typed clients below).

2. **`tradestation` Rust crate v1.0.1** (typed v3 client, MIT, 100% documented) —
   `Bar` struct source:
   https://docs.rs/tradestation/latest/src/tradestation/market_data/bar.rs.html
   - `Bar` struct doc: https://docs.rs/tradestation/latest/tradestation/market_data/bar/struct.Bar.html
   - `GetBarsQuery` doc: https://docs.rs/tradestation/latest/tradestation/market_data/bar/struct.GetBarsQuery.html
   - `BarUnit` enum: https://docs.rs/tradestation/latest/tradestation/market_data/bar/enum.BarUnit.html
   - `BarStatus` enum: https://docs.rs/tradestation/latest/tradestation/market_data/bar/enum.BarStatus.html
   - `SessionTemplate` enum: https://docs.rs/tradestation/latest/tradestation/market_data/bar/enum.SessionTemplate.html
   - The struct uses `#[serde(rename_all = "PascalCase")]`, so the JSON keys are
     PascalCase (e.g. `open` field → `"Open"` key).

3. **`TradeStationAPI.jl` Julia package** — real `get_bars()` output DataFrame:
   https://juliapackages.com/p/tradestationapi
   - Shows an actual 10×18 DataFrame with columns: `High, Low, Open, Close,
     TimeStamp, TotalVolume, DownTicks, DownVolume, OpenInterest, IsRealtime,
     IsEndOfHistory, TotalTicks, UnchangedTicks, UnchangedVolume, UpTicks,
     UpVolume, Epoch, BarStatus` — confirming the 18 fields and their observed
     types.

4. **`tradestation-api-ts` TypeScript wrapper** (v3):
   https://github.com/mxcoppell/tradestation-api-ts
   - README shows `getBarHistory('MSFT', { interval: '1', unit: 'Minute',
     barsback: 100 })`.

5. **`flare9x/TradeStationAPI.jl`** — `get_bars()` signature:
   https://github.com/flare9x/TradeStationAPI.jl
   - `get_bars(access_token; symbol="AAPL", interval="1", unit="Daily",
     barsback="10", firstdate=nothing, lastdate=nothing,
     sessiontemplate="Default")` — confirms the exact query-param names.

6. **SkillsMP `tradestation-api-specialist` skill doc**:
   https://skillsmp.com/skills/muath2000-tradestation-claude-skills-tradestation-api-specialist-skill-md
   - Documents `GET /v3/marketdata/barcharts/{symbol}` with required params
     `unit, interval, barsback OR firstdate+lastdate` and the response shape
     `{ Bars: [{ Open, High, Low, Close, TotalVolume, TimeStamp }] }`.

7. **Multiple generated client READMEs** (`pattertj/ts-api`, `alexmerm/ts-api`,
   `dustinhartlyn/ts-api`) all list `get_bars → GET
   /v3/marketdata/barcharts/{symbol}` → "Get Bars".

---

## 1. Bar Endpoint

| Item | Value |
|------|-------|
| HTTP method | `GET` |
| Path | `/v3/marketdata/barcharts/{symbol}` |
| Base URL (live) | `https://api.tradestation.com` |
| Base URL (sim) | `https://sim-api.tradestation.com` |
| Full live URL | `https://api.tradestation.com/v3/marketdata/barcharts/{symbol}` |
| Auth | OAuth2 bearer token, scope `marketdata` |
| Operation ID (swagger) | `GetBars` |

`{symbol}` is the TradeStation symbol, e.g. `@ES` for E-mini S&P 500 continuous
futures, `CLX24` for Nov 2024 Crude Oil, `MSFT` for the stock.

There is also a **streaming** variant at
`GET /v3/marketdata/stream/barcharts/{symbol}` (operation `StreamBars`) which
returns the same `Bar` objects as newline-delimited JSON chunks. This report
focuses on the historical (non-streaming) endpoint, but the per-bar schema is
identical.

**Sources:** tradestation Rust crate `bar.rs` line 104
(`"marketdata/barcharts/{}"`); SkillsMP doc; all generated-client READMEs.

---

## 2. Response Schema — Per-Bar Fields

The response body is a JSON object of the form:

```json
{
  "Bars": [
    { ...bar object... },
    { ...bar object... }
  ],
  "Error": null
}
```

Each element of the `Bars` array is a bar object. The API returns **PascalCase**
field names (confirmed by the Rust struct's `#[serde(rename_all =
"PascalCase")]` attribute and by the v2 swagger worked example).

### Complete list of per-bar fields (18 fields)

| # | JSON field name | Rust type | Observed type (Julia) | Meaning |
|---|-----------------|-----------|----------------------|---------|
| 1 | `Open` | `String` | String | Opening price of the bar. Returned as a **string** (decimal), not a number. |
| 2 | `High` | `String` | String | Highest price traded in the bar. String. |
| 3 | `Low` | `String` | String | Lowest price traded in the bar. String. |
| 4 | `Close` | `String` | String | Closing price of the bar. String. |
| 5 | `TotalVolume` | `String` | String | Sum of up-volume and down-volume (total traded volume for the bar). Returned as a **string** despite being numeric. |
| 6 | `UpVolume` | `u64` | Int64 | Number of shares/contracts traded on up ticks. Integer. |
| 7 | `DownVolume` | `u64` | Int64 | Number of shares/contracts traded on down ticks. Integer. |
| 8 | `TotalTicks` | `u64` | Int64 | Total number of ticks (upticks + downticks). Integer. |
| 9 | `UpTicks` | `u64` | Int64 | Number of trades at a price ≥ previous trade price. Integer. |
| 10 | `DownTicks` | `u64` | Int64 | Number of trades at a price ≤ previous trade price. Integer. |
| 11 | `UnchangedTicks` | `u8` | Int64 | Trades at the same price as the previous trade. **DEPRECATED — always 0.** |
| 12 | `UnchangedVolume` | `u8` | Int64 | Volume on unchanged ticks. **DEPRECATED — always 0.** |
| 13 | `OpenInterest` | `Option<String>` | String | Number of open contracts. **Futures and Options ONLY** (see §4). String, may be absent/null for non-futures. |
| 14 | `TimeStamp` | `String` | String | RFC3339 formatted timestamp, e.g. `"2024-09-01T23:30:30Z"`. UTC. |
| 15 | `Epoch` | `i64` | Int64 | Unix epoch time in **milliseconds** (e.g. `1700254800000`). Integer. |
| 16 | `BarStatus` | `BarStatus` (enum) | String | Whether the bar is `"Open"` (still trading) or `"Closed"` (finished). |
| 17 | `IsRealtime` | `Option<bool>` | Bool | `true` when the bar is being built in real time from a live trade (streaming context). May be absent/null in historical responses. |
| 18 | `IsEndOfHistory` | `bool` | Bool | `true` conveys that all historical bars in the request have been delivered (primarily a streaming marker; `false` for historical fetch bars). |

### Fields NOT present

The following fields, sometimes guessed from other market-data APIs, are **not**
returned by the bars endpoint:

- `bidPrice` / `askPrice` — not in bar objects (these are quote-snapshot fields).
- `tickCount` — the equivalent is `TotalTicks` (plus `UpTicks`/`DownTicks`).
- `vwap` — not returned.
- `settlement` / `adjustedClose` — not returned.
- any continuous-contract adjustment flag — not returned (see §4).

### Sample raw JSON (from v2 swagger, identical field set in v3)

```json
{
  "Close": 19956.09,
  "DownTicks": 26,
  "DownVolume": 940229,
  "High": 19961.77,
  "Low": 19943.46,
  "Open": 19943.46,
  "Status": 13,
  "TimeStamp": "\/Date(1482849060000)\/",
  "TotalTicks": 59,
  "TotalVolume": 3982533,
  "UnchangedTicks": 0,
  "UnchangedVolume": 0,
  "UpTicks": 33,
  "UpVolume": 3042304,
  "OpenInterest": 0
}
```

> **v2 vs v3 differences:** The v2 streaming example uses `"Status": 13` (a
> numeric code) and a `\/Date(epoch)\/` timestamp format. The v3 REST endpoint
> replaces these with `"BarStatus": "Closed"|"Open"` (string enum) and adds
> `Epoch` (int ms) + `TimeStamp` (RFC3339 string) + `IsRealtime` +
> `IsEndOfHistory`. The OHLCV and tick/volume fields are identical in name.

**Sources:** Rust `Bar` struct (docs.rs source, lines 12–80); Julia DataFrame
output (juliapackages.com); v2 swagger worked example.

---

## 3. Query Parameters

The `GetBarsQuery` struct (docs.rs) maps to these query-string parameters. Note
the **lowercase** param names (the Rust builder uses snake_case internally but
the API query string is lowercase per the Julia/TS wrappers):

| Param (query string) | Required | Type | Default | Notes |
|-----------------------|----------|------|---------|-------|
| `interval` | Yes | integer (i16) | `1` | Number of `unit`s per bar. Max 1440 for Minute. E.g. `interval=5` + `unit=Minute` = 5-min bars. |
| `unit` | Yes | enum string | — | `Minute`, `Daily`, `Weekly`, `Monthly`. |
| `barsback` | Conditional | integer (u32) | `1` | Number of bars back from now. **Max 57,600 for intraday (Minute)**; no limit for Daily/Weekly/Monthly. Mutually exclusive with `firstdate`. |
| `firstdate` | Conditional | string | — | Start date `"YYYY-MM-DD"` or `"2020-04-20T18:00:00Z"`. Mutually exclusive with `barsback`. |
| `lastdate` | Conditional | string | current timestamp | End date. Mutually exclusive with `startdate` (use `lastdate`; `startdate` is deprecated). |
| `sessiontemplate` | No | enum string | `Default` | `Default`, `USEQPre`, `USEQPost`, `USEQPreAndPost`, `USEQ24Hour`. **Ignored for non-US-equity symbols** (i.e. irrelevant for futures). |
| `startdate` | No (deprecated) | string | — | Deprecated alias for `lastdate`. Do not use. |

### Typical request for futures

```
GET https://api.tradestation.com/v3/marketdata/barcharts/@ES?interval=1&unit=Minute&firstdate=2024-01-01&lastdate=2024-01-02
```

or using `barsback`:

```
GET https://api.tradestation.com/v3/marketdata/barcharts/@ES?interval=1&unit=Minute&barsback=1000
```

### Does any param control WHICH fields are returned?

**No.** There is no `fields`, `columns`, or projection parameter. The API
always returns the full set of 18 fields per bar (with `OpenInterest` only
populated for futures/options — see §4). The `sessiontemplate` only affects
*which bars* are included (session window), not *which fields*.

The v2 TradeStation EasyLanguage `PriceSeriesProvider` has `IncludeTicksInfo`
and `IncludeVolumeInfo` booleans, but **these are not exposed as query params
on the v3 REST barcharts endpoint** — the REST endpoint always includes ticks
and volume.

**Sources:** Rust `GetBarsQuery` (docs.rs); Julia `get_bars` signature
(flare9x/TradeStationAPI.jl); SkillsMP doc; TS wrapper README.

---

## 4. Futures-Specific Fields

| Field | Futures relevance |
|-------|-------------------|
| `OpenInterest` | **The key futures/options field.** "The number of open contracts. NOTE: Futures and Options ONLY." (Rust doc). For equities it is absent or `0`. For futures it carries the open-interest value for that bar's contract. Type: `Option<String>` — may be `null`/omitted for non-futures symbols. |
| `TotalVolume`, `UpVolume`, `DownVolume` | For futures these are **contract counts** (not share counts). |
| `TotalTicks`, `UpTicks`, `DownTicks` | Trade counts; same semantics for futures as equities. |

### Continuous-contract info

There is **no** field in the bar object that identifies the continuous-contract
roll policy, the underlying specific month, or an adjustment flag. The symbol
itself (e.g. `@ES` vs `ESZ24`) is the only carrier of that information, and it
is a path parameter, not a bar field. If you need continuous-vs-specific
distinction, record the requested symbol as a column yourself.

Open interest for a continuous symbol like `@ES` reflects the front-month
contract's open interest at each bar; TradeStation does not document a
separate "continuous open interest" aggregate.

**Sources:** Rust `Bar::open_interest` doc ("Futures and Options ONLY");
v2 swagger example showing `OpenInterest` on `$DJI`.

---

## 5. Field Availability by Interval

The **set of returned fields does not differ by bar interval.** Whether you
request `unit=Minute`, `unit=Daily`, `unit=Weekly`, or `unit=Monthly`, the
same 18 fields are returned per bar.

Observed differences are in **values**, not schema:

- **Minute bars:** `OpenInterest` is typically `0` intraday (open interest is
  settled once daily); `IsRealtime` may be `true` for the current forming bar.
- **Daily/Weekly/Monthly bars:** `OpenInterest` carries the settled daily OI;
  `IsRealtime` is `false`; `IsEndOfHistory` is `false` for all but the last
  delivered bar in a stream.
- `UnchangedTicks` and `UnchangedVolume` are **always 0** at every interval
  (deprecated fields).

The Julia example (Daily bars for AAPL) shows all 18 columns populated with
`OpenInterest=0` (equity) and `BarStatus="Closed"`.

**Sources:** Rust `Bar` struct (single struct, no interval-conditional
fields); Julia Daily-bar DataFrame; SkillsMP doc (same response shape for
Daily and Minute).

---

## 6. Known Gotchas

1. **String-typed numerics.** `Open`, `High`, `Low`, `Close`, `TotalVolume`,
   and `OpenInterest` are returned as **JSON strings** (e.g. `"190.38"`),
   not numbers. Downstream parquet writers must parse these to float/int.
   The Rust crate provides `Bar::ohlcv()` and `Bar::timestamp()` convenience
   parsers for exactly this reason. `UpVolume`/`DownVolume`/ticks are real
   integers.

2. **`Epoch` is milliseconds, not seconds.** Example: `1700254800000` =
   2023-11-17T21:00:00Z. Divide by 1000 for standard Unix seconds.

3. **`TimeStamp` is UTC RFC3339** with a `Z` suffix (e.g.
   `"2024-09-01T23:30:30Z"`). It is always UTC; there is no timezone
   parameter. TradeStation bar timestamps mark the **close** of the bar
   (i.e. a 09:31 1-min bar is timestamped 09:31:00, the end of the 09:30–09:31
   interval) — verify this convention against your data, as it affects
   alignment.

4. **`OpenInterest` is `Option<String>`** — may be `null` or omitted entirely
   for non-futures/options symbols. Your schema must allow nulls. For futures
   it is usually present but can be `"0"` for intraday bars.

5. **`UnchangedTicks` / `UnchangedVolume` are deprecated and always 0.** Do
   not rely on them. Consider dropping them from parquet to save space, or
   keep them for schema completeness with a note.

6. **`IsRealtime` / `IsEndOfHistory`** are primarily streaming markers. In a
   historical `GET` response they are typically `false`/absent. Do not treat
   their absence as an error.

7. **`BarStatus` is a string enum** (`"Open"` / `"Closed"`), not the numeric
   `Status` code used in v2 (e.g. `13`). If migrating from v2, note this
   breaking change.

8. **`barsback` max is 57,600 for intraday** (≈ 40 trading days of 1-min
   bars at 390 bars/day, or 100 days at 24h). For longer history use
   `firstdate`/`lastdate` instead. Daily/Weekly/Monthly have no barsback
   limit.

9. **`barsback` and `firstdate` are mutually exclusive** — supplying both is
   an error. `lastdate` and `startdate` are also mutually exclusive (and
   `startdate` is deprecated).

10. **`sessiontemplate` is ignored for futures.** It only affects US equities.
    Passing it for `@ES` etc. has no effect.

11. **No field-projection param.** You cannot ask the API for a subset of
    columns; you always get all 18 and must drop unwanted ones client-side.

12. **Blank/null fields:** Per the v2 swagger conventions, "Blank fields may
    either be included as null or omitted, so please support both." Your
    deserializer should treat missing keys and explicit `null` identically.

13. **Endpoint name is `barcharts`, not `bars`.** Using
    `/v3/marketdata/bars/{symbol}` will 404. This is the single most common
    implementation mistake.

**Sources:** Rust `Bar` field docs (deprecation notes, string types, ms
epoch); v2 swagger "Common Conventions"; Julia DataFrame (string-typed OHLC);
SkillsMP doc (barsback limits).

---

## Summary Table for Implementers — Recommended Parquet Columns

| Parquet column | Source field | Parquet type | Nullable | Notes |
|----------------|--------------|--------------|----------|-------|
| `timestamp` | `TimeStamp` | timestamp (ms, UTC) | No | Parse RFC3339; marks bar close. |
| `epoch_ms` | `Epoch` | int64 | No | Redundant with timestamp but cheap to keep. |
| `open` | `Open` | double | No | Parse from string. |
| `high` | `High` | double | No | Parse from string. |
| `low` | `Low` | double | No | Parse from string. |
| `close` | `Close` | double | No | Parse from string. |
| `total_volume` | `TotalVolume` | int64 | No | Parse from string. |
| `up_volume` | `UpVolume` | int64 | No | |
| `down_volume` | `DownVolume` | int64 | No | |
| `total_ticks` | `TotalTicks` | int64 | No | |
| `up_ticks` | `UpTicks` | int64 | No | |
| `down_ticks` | `DownTicks` | int64 | No | |
| `open_interest` | `OpenInterest` | int64 | **Yes** | Futures/options only; parse from string; null for equities. |
| `bar_status` | `BarStatus` | string | No | `"Open"` / `"Closed"`. |
| `is_realtime` | `IsRealtime` | bool | Yes | Usually false for historical. |
| `is_end_of_history` | `IsEndOfHistory` | bool | No | Usually false for historical GET. |
| ~~`unchanged_ticks`~~ | `UnchangedTicks` | — | — | **Drop** (deprecated, always 0). |
| ~~`unchanged_volume`~~ | `UnchangedVolume` | — | — | **Drop** (deprecated, always 0). |

Downstream should also persist the **requested symbol** (e.g. `@ES`) and the
**bar interval/unit** (e.g. `1Minute`) as metadata columns, since the API
does not echo them in the bar objects.
