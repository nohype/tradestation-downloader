# Research: Futures Symbol → Exchange → Timezone Mapping Audit

**Project:** `D:\nohype-tradestation-downloader` (TradeStation Historical Data Downloader)
**Mode:** Read-only investigation. No code was modified.
**Date:** 2026-07-15
**Trigger:** User reports `@NG` (Natural Gas) is traded at NYMEX, so its timezone should be
New York (Eastern / `America/New_York`), not Chicago. User suspects more misattributions.

---

## 1. Where the mapping lives

### 1a. Exchange → timezone mapping (the thing that is wrong)

**File:** `tradestation/metadata.py`
**Lines:** 32–44
**Structure:** Module-level dict literal `EXCHANGE_TIMEZONES`, keyed by TradeStation API
`Exchange` string → IANA timezone. Lookup helper `get_exchange_timezone()` at lines 47–51
does a case-insensitive `dict.get` and returns `None` for unknown/missing exchanges.

```python
# tradestation/metadata.py:32-44
EXCHANGE_TIMEZONES = {
    "CME": "America/Chicago",
    "CBOT": "America/Chicago",
    "COMEX": "America/Chicago",
    "NYMEX": "America/Chicago",
    "NYSE": "America/New_York",
    "NASDAQ": "America/New_York",
    "AMEX": "America/New_York",
    "ARCX": "America/New_York",
    "ICE": "America/New_York",
    "EUREX": "Europe/Berlin",
    "EUREX_US": "America/Chicago",
}
```

### 1b. Symbol → exchange linkage (not hardcoded — comes from the API)

There is **no** hardcoded symbol→exchange table. For each symbol the code reads the
`Exchange` field from the TradeStation `/marketdata/symbols` API response, then maps it
through `EXCHANGE_TIMEZONES`:

- `tradestation/metadata.py:289` — `exchange = api_data.get("Exchange") if api_data else None`
- `tradestation/metadata.py:290` — `tz = get_exchange_timezone(exchange)`
- `tradestation/metadata.py:307` — writes `"exchange_timezone": tz` into `metadata.json`

So the **timezone a symbol gets is determined entirely by (a) the API's `Exchange` string
and (b) the `EXCHANGE_TIMEZONES` dict.** Wrong timezones therefore come from either a wrong
dict value (e.g. NYMEX→Chicago) or a missing dict key (the API returns an exchange string
the dict doesn't know, yielding `tz=None`).

### 1c. Source of actual API `Exchange` values used in this audit

Pulled from the two existing `metadata.json` files (both identical for the 24 downloaded
symbols) via `tradestation/metadata.py` `run_metadata()`:

- `old_data/minute_data/metadata.json`
- `data/metadata.json`

Observed API `Exchange` strings (24 symbols): `CME`, `CBOT`, `NYMEX`, `COMEX`, `CBOEF`.
`CBOEF` is **not** in `EXCHANGE_TIMEZONES` → those symbols get `tz=None` today.

### 1d. Symbol catalog

`DEFAULT_SYMBOLS` is defined at `tradestation/models.py:86–172` (a dict of category →
list of symbols, 48 symbols total). The user-facing list is mirrored in
`config.yaml.template:31–115`. Both lists are identical in symbol set.

---

## 2. Exchange → timezone audit (the 11 dict entries)

Convention applied (per user + authoritative sources): the timezone follows the
**exchange's named location**, not the CME Group corporate HQ. NYMEX/COMEX = New York
(Eastern); CME/CBOT = Chicago (Central). This is explicitly confirmed by CME Group's own
data FAQ: *"Crude Oil is a NYMEX product and timestamps will be in EST. Corn is a CBOT
product and timestamps from the RTH session will be in CST."*

| # | Dict key | Current TZ (metadata.py) | Correct TZ | Status | Source |
|---|----------|--------------------------|------------|--------|--------|
| 1 | `CME` | `America/Chicago` | `America/Chicago` | OK | CME HQ 20 S. Wacker Dr, Chicago (cmegroup.com IR; Wikipedia CME) |
| 2 | `CBOT` | `America/Chicago` | `America/Chicago` | OK | CBOT Chicago Loop (cbot.com; Wikipedia CBOT); CME FAQ: CBOT=CST |
| 3 | `COMEX` | `America/Chicago` | `America/New_York` | **WRONG** | COMEX in Manhattan; markethours.io = America/New_York; CME FAQ: NYMEX/COMEX=EST; CFTC filing 300 Vesey St, NY |
| 4 | `NYMEX` | `America/Chicago` | `America/New_York` | **WRONG** | NYMEX HQ One North End Ave, Manhattan (Wikipedia NYMEX); CME FAQ: Crude Oil NYMEX=EST; markethours.io=America/New_York |
| 5 | `NYSE` | `America/New_York` | `America/New_York` | OK | NYSE, New York, Eastern |
| 6 | `NASDAQ` | `America/New_York` | `America/New_York` | OK | Nasdaq, New York, Eastern |
| 7 | `AMEX` | `America/New_York` | `America/New_York` | OK | NYSE American (AMEX), New York, Eastern |
| 8 | `ARCX` | `America/New_York` | `America/New_York` | OK | NYSE Arca, New York, US Eastern (tradinghours.com MIC ARCX; NYSE Arca spec "US Eastern Time") |
| 9 | `ICE` | `America/New_York` | `America/New_York` (for ICE Futures **U.S.**) / `Europe/London` (for ICE Futures **Europe**) | **PARTIAL / RISK** | ICE Futures U.S. office 1345 Ave of the Americas, NYC, SEC filing "time in New York"; ICE Futures Europe is London-based, FCA-regulated (Europe/London). A single `"ICE"` key cannot represent both. |
| 10 | `EUREX` | `Europe/Berlin` | `Europe/Berlin` (CET/CEST) | OK | Eurex trading hours in CET; Eurex Frankfurt AG, Eschborn, Germany (eurex.com) |
| 11 | `EUREX_US` | `America/Chicago` | `America/Chicago` (unverified, no symbol uses it) | OK* | Eurex US is defunct; no `DEFAULT_SYMBOLS` entry maps here. America/Chicago plausible for a US CME-campus venture. *Unverified — no live impact. |

### Missing dict keys (API returns these but the dict has no entry)

| API `Exchange` string | Correct TZ | Why needed | Status | Source |
|-----------------------|------------|------------|--------|--------|
| `CBOEF` | `America/Chicago` | `@VX` (VIX futures) API returns `CBOEF`; currently → `tz=None` | **MISSING (WRONG)** | CFTC CFE rulebook defines "Chicago time" = Central; metadata.json confirms `@VX` → `CBOEF` |
| `IFEU` / ICE-Futures-Europe identifier (TBD) | `Europe/London` | `@BRN` (Brent) is ICE Futures Europe (London). If API returns `"ICE"` it wrongly gets New York; if it returns `"IFEU"` it gets `None`. Either way wrong. | **MISSING / RISK** | ICE Futures Europe is London-based (ice.com/futures-europe/regulation); Brent = ICE Futures Europe |
| `MGEX` (if ever returned) | `America/Chicago` | Minneapolis = Central. Not currently needed (`@W` is CBOT per API). | optional | MGEX rulebook: "all times … Central Time"; Minneapolis, MN |

> **Dict-key vs API-string caveat:** This audit only has live API `Exchange` strings for
> the 24 downloaded symbols (CME/CBOT/NYMEX/COMEX/CBOEF). For ICE-listed symbols that are
> not yet downloaded (`@KC @SB @CT @CC @OJ @DX @BRN`), the exact API `Exchange` string is
> **not confirmed**. The dict only contains `"ICE"`. If TradeStation returns `"IFUS"` /
> `"ICEUS"` for U.S. softs, those would also fall through to `tz=None` today. The downstream
> designer should verify the API string for at least one ICE symbol and add the exact key(s).

---

## 3. Full symbol audit table (all 48 `DEFAULT_SYMBOLS`)

"Exchange (API)" = value actually returned by TradeStation API where the symbol has been
downloaded (confirmed via `metadata.json`); otherwise inferred from TradeStation's own
symbology/fees pages and exchange product pages (marked `inferred`). "Current TZ" = what
`EXCHANGE_TIMEZONES` produces today given that exchange; "Correct TZ" = the user-honored
exchange-location convention verified against authoritative sources.

| Symbol | Category | Exchange (API) | Current TZ | Correct Exchange | Correct TZ | Status | Source |
|--------|----------|----------------|------------|------------------|------------|--------|--------|
| `@ES` | index | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json; cmegroup IR |
| `@NQ` | index | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |
| `@YM` | index | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS fees: YM=CBOT |
| `@RTY` | index | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |
| `@EMD` | index | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |
| `@SMC` | index | CME (inferred) | America/Chicago | CME | America/Chicago | OK | E-mini S&P SmallCap = CME |
| `@MES` | micro_index | CME (inferred) | America/Chicago | CME | America/Chicago | OK | Micro E-mini = CME |
| `@MNQ` | micro_index | CME (inferred) | America/Chicago | CME | America/Chicago | OK | Micro E-mini = CME |
| `@MYM` | micro_index | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | TS fees: MYM=CBOT |
| `@M2K` | micro_index | CME (inferred) | America/Chicago | CME | America/Chicago | OK | Micro Russell = CME |
| `@CL` | energy | NYMEX (confirmed) | America/Chicago | NYMEX | America/New_York | **WRONG** | metadata.json; TS symbology CL=NYMEX; CME FAQ NYMEX=EST |
| `@NG` | energy | NYMEX (confirmed) | America/Chicago | NYMEX | America/New_York | **WRONG** | metadata.json; TS symbology NG=NYMEX; CME FAQ NYMEX=EST |
| `@RB` | energy | NYMEX (confirmed) | America/Chicago | NYMEX | America/New_York | **WRONG** | metadata.json; RBOB = NYMEX |
| `@HO` | energy | NYMEX (confirmed) | America/Chicago | NYMEX | America/New_York | **WRONG** | metadata.json; TS symbology HO=NYMEX |
| `@BRN` | energy | ICE Futures Europe (inferred) | America/New_York (if API=="ICE") / None (if API=="IFEU") | ICE Futures Europe | Europe/London | **WRONG** | ICE Brent = ICE Futures Europe, London (ice.com/brent-crude; ice.com/futures-europe) |
| `@MCL` | micro_energy | NYMEX (inferred) | America/Chicago | NYMEX | America/New_York | **WRONG** | Micro Crude = NYMEX |
| `@MNG` | micro_energy | NYMEX (inferred) | America/Chicago | NYMEX | America/New_York | **WRONG** | Micro Henry Hub NatGas = NYMEX |
| `@GC` | metals | COMEX (confirmed) | America/Chicago | COMEX | America/New_York | **WRONG** | metadata.json; Gold=COMEX (markethours.io America/New_York) |
| `@SI` | metals | COMEX (confirmed) | America/Chicago | COMEX | America/New_York | **WRONG** | metadata.json; Silver=COMEX |
| `@HG` | metals | COMEX (confirmed) | America/Chicago | COMEX | America/New_York | **WRONG** | metadata.json; Copper=COMEX |
| `@PL` | metals | NYMEX (confirmed) | America/Chicago | NYMEX | America/New_York | **WRONG** | metadata.json; Platinum=NYMEX (CME metals: NYMEX 105; delivery NY/NJ/DE) |
| `@PA` | metals | NYMEX (inferred) | America/Chicago | NYMEX | America/New_York | **WRONG** | Palladium=NYMEX (cmegroup.com palladium contractSpecs; delivery NY/NJ/DE) |
| `@MGC` | micro_metals | COMEX (inferred) | America/Chicago | COMEX | America/New_York | **WRONG** | Micro Gold = COMEX |
| `@SIL` | micro_metals | COMEX (inferred) | America/Chicago | COMEX | America/New_York | **WRONG** | Micro Silver = COMEX |
| `@MHG` | micro_metals | COMEX (inferred) | America/Chicago | COMEX | America/New_York | **WRONG** | Micro Copper = COMEX |
| `@US` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | TS symbology: 30Y Bond US=CBOT |
| `@TY` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | TS symbology: 10Y Note TY=CBOT |
| `@FV` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | TS symbology: 5Y Note FV=CBOT |
| `@TU` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | TS symbology: 2Y Note TU=CBOT |
| `@UB` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | Ultra Bond = CBOT |
| `@TEN` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | Ultra 10Y = CBOT |
| `@TWE` | treasuries | CBOT (inferred) | America/Chicago | CBOT | America/Chicago | OK | 20Y Bond = CBOT |
| `@C` | grains | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS symbology C=CBOT |
| `@S` | grains | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS symbology S=CBOT |
| `@W` | grains | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS symbology W=CBOT |
| `@KW` | grains | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS fees: KW=CBOT (KC Wheat routed as CBOT) |
| `@BO` | grains | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS symbology BO=CBOT |
| `@SM` | grains | CBOT (confirmed) | America/Chicago | CBOT | America/Chicago | OK | metadata.json; TS symbology SM=CBOT |
| `@KC` | softs | ICE Futures U.S. (inferred) | America/New_York (if API=="ICE") | ICE Futures U.S. | America/New_York | OK* | ICE: KC=Coffee C, IFUS (ice.com products) |
| `@SB` | softs | ICE Futures U.S. (inferred) | America/New_York (if API=="ICE") | ICE Futures U.S. | America/New_York | OK* | ICE: SB=Sugar No.11, IFUS |
| `@CT` | softs | ICE Futures U.S. (inferred) | America/New_York (if API=="ICE") | ICE Futures U.S. | America/New_York | OK* | ICE: CT=Cotton No.2, IFUS |
| `@CC` | softs | ICE Futures U.S. (inferred) | America/New_York (if API=="ICE") | ICE Futures U.S. | America/New_York | OK* | ICE: CC=Cocoa (NY cocoa), IFUS (London Cocoa is a separate contract) |
| `@OJ` | softs | ICE Futures U.S. (inferred) | America/New_York (if API=="ICE") | ICE Futures U.S. | America/New_York | OK* | ICE: OJ=FCOJ-A, IFUS |
| `@LBR` | softs | CME (inferred) | America/Chicago | CME | America/Chicago | OK | CME Lumber (cmegroup.com lumber; delivered Chicago Switching District) |
| `@LC` | meats | CME (inferred) | America/Chicago | CME | America/Chicago | OK | Live Cattle = CME |
| `@LH` | meats | CME (inferred) | America/Chicago | CME | America/Chicago | OK | Lean Hogs = CME |
| `@FC` | meats | CME (inferred) | America/Chicago | CME | America/Chicago | OK | Feeder Cattle = CME |
| `@EC` | currencies | CME (inferred) | America/Chicago | CME | America/Chicago | OK | TS symbology: Euro EC=CME |
| `@JY` | currencies | CME (inferred) | America/Chicago | CME | America/Chicago | OK | TS symbology: JY=CME |
| `@BP` | currencies | CME (inferred) | America/Chicago | CME | America/Chicago | OK | TS symbology: BP=CME |
| `@AD` | currencies | CME (inferred) | America/Chicago | CME | America/Chicago | OK | TS symbology: AD=CME |
| `@CD` | currencies | CME (inferred) | America/Chicago | CME | America/Chicago | OK | TS symbology: CD=CME |
| `@SF` | currencies | CME (inferred) | America/Chicago | CME | America/Chicago | OK | TS symbology: SF=CME |
| `@DX` | currencies | ICE Futures U.S. (inferred) | America/New_York (if API=="ICE") | ICE Futures U.S. | America/New_York | OK* | ICE: DX=US Dollar Index, IFUS |
| `@VX` | volatility | CBOEF (confirmed) | None (MISSING) | CBOE Futures Exchange (CFE) | America/Chicago | **WRONG (missing key)** | metadata.json: @VX→CBOEF; CFTC CFE rulebook: "Chicago time"=Central |
| `@BTC` | crypto | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |
| `@ETH` | crypto | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |
| `@MBT` | crypto | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |
| `@MET` | crypto | CME (confirmed) | America/Chicago | CME | America/Chicago | OK | metadata.json |

`OK*` = the timezone is correct **only if** the TradeStation API returns `"ICE"` for that
symbol. If the API returns `"IFUS"`/`"ICEUS"` (unconfirmed), these would also be MISSING
(`tz=None`). See the dict-key caveat in §2.

---

## 4. Wrong entries — explicit list of symbols needing correction

### 4a. Confirmed wrong (have live API data) — 9 symbols

| Symbol | Before (exchange → tz) | After (exchange → tz) |
|--------|------------------------|------------------------|
| `@NG` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@CL` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@RB` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@HO` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@PL` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@GC` | COMEX → America/Chicago | COMEX → America/New_York |
| `@SI` | COMEX → America/Chicago | COMEX → America/New_York |
| `@HG` | COMEX → America/Chicago | COMEX → America/New_York |
| `@VX` | CBOEF → None (missing key) | CBOEF → America/Chicago |

### 4b. Wrong by inference (not yet downloaded; same dict defect applies) — 7 symbols

| Symbol | Before (exchange → tz) | After (exchange → tz) |
|--------|------------------------|------------------------|
| `@MCL` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@MNG` | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@PA`  | NYMEX → America/Chicago | NYMEX → America/New_York |
| `@MGC` | COMEX → America/Chicago | COMEX → America/New_York |
| `@SIL` | COMEX → America/Chicago | COMEX → America/New_York |
| `@MHG` | COMEX → America/Chicago | COMEX → America/New_York |
| `@BRN` | ICE → America/New_York (or None if API=="IFEU") | ICE Futures Europe → Europe/London |

### 4c. Root-cause dict edits that fix the bulk of the above

The 14 NYMEX/COMEX symbols (4a energy/metals + 4b micro/PA) are fixed by just **two**
dict value changes. `@VX` needs one **new** key. `@BRN` needs an ICE-Futures-Europe key.

```
metadata.py:35   "COMEX": "America/Chicago",     ->  "COMEX": "America/New_York",
metadata.py:36   "NYMEX": "America/Chicago",     ->  "NYMEX": "America/New_York",
# add new key for CBOE Futures Exchange (VIX):
                 "CBOEF": "America/Chicago",
# add/adjust ICE Futures Europe key for Brent (verify exact API string):
                 "IFEU":  "Europe/London",   # (or whatever the API returns for @BRN)
```

**Total symbols needing correction: 16** (9 confirmed + 7 inferred).
**Root-cause dict defects: 3 wrong/missing entries** (NYMEX value, COMEX value, CBOEF key)
**+ 1 partial/risk** (ICE cannot represent ICE Futures Europe / London).

---

## 5. Missing entries (symbols referenced by code with no mapping)

"Missing" = the symbol's API `Exchange` string is not present in `EXCHANGE_TIMEZONES`, so
`get_exchange_timezone()` returns `None` and `metadata.json` gets `exchange_timezone: null`.

1. **`@VX` → `CBOEF`** (CONFIRMED missing, live data). `metadata.json` shows
   `@VX exchange=CBOEF tz=None`. CBOE Futures Exchange is in Chicago → must be
   `America/Chicago`. **Add `"CBOEF": "America/Chicago"`.**

2. **`@BRN` → ICE Futures Europe** (RISK, likely missing or wrong). Brent trades on
   ICE Futures Europe (London). If the API returns `"ICE"` it is wrongly mapped to
   New York; if it returns `"IFEU"`/`"IFE"` it is missing (None). Either way it must be
   `Europe/London`. Needs API-string confirmation + a dedicated key.

3. **(Potential) `@KC @SB @CT @CC @OJ @DX` → ICE Futures U.S.** (RISK). These are
   ICE Futures U.S. (New York) products. If the API returns `"ICE"` they are currently OK;
   if it returns `"IFUS"`/`"ICEUS"` they are MISSING today. Needs API-string confirmation.
   Correct TZ is `America/New_York` for all six.

No `DEFAULT_SYMBOLS` entry maps to MGEX, KCBT, EUREX, or EUREX_US today, so those keys are
not strictly required (EUREX/EUREX_US already exist; MGEX optional at `America/Chicago`).

---

## 6. Notes for the downstream designer

- **Single source of truth:** all timezone outcomes flow through `EXCHANGE_TIMEZONES`
  (`tradestation/metadata.py:32-44`) + the API `Exchange` field (`metadata.py:289-290`).
  Fixing the dict fixes every symbol at once; no per-symbol edits are needed for the
  NYMEX/COMEX cases.
- **Honor user convention:** NYMEX/COMEX = `America/New_York`; CME/CBOT = `America/Chicago`.
  This is verified by CME Group's own FAQ (NYMEX/COMEX timestamps in EST, CBOT in CST) and
  by each exchange's headquarters location (NYMEX/COMEX in Manhattan; CME/CBOT in Chicago).
- **Verify API strings before finalizing ICE entries.** Run `metadata` for at least
  `@KC` (ICE Futures U.S.) and `@BRN` (ICE Futures Europe) to capture the exact `Exchange`
  string the API returns, then add the precise key(s). A single `"ICE"` key is insufficient
  because ICE Futures Europe is London, not New York.
- **`@VX` currently silently loses its timezone** (`tz=None`) because `CBOEF` is unknown —
  `metadata.json` shows `exchange_timezone: null` for it. Adding `"CBOEF": "America/Chicago"`
  fixes this without touching anything else.
- **Existing tests encode the wrong values.** `tests/test_metadata.py` asserts
  `CME → America/Chicago` (line ~434, `test_run_metadata_full_flow`) — that one stays valid.
  Any test asserting NYMEX/COMEX → America/Chicago (if added later) must be updated to
  America/New_York. The current test suite only exercises `CME`, so the NYMEX/COMEX fix will
  not break existing tests, but a new test should be added covering NYMEX/COMEX → New_York
  and CBOEF → Chicago.

---

## 7. Sources (authoritative URLs consulted)

**CME Group / CBOT / NYMEX / COMEX — location & timezone**
- CME Group data FAQ (NYMEX/COMEX=EST, CBOT=CST): https://www.cmegroup.com/market-data/files/FAQ_TS.pdf
- Chicago Board of Trade (Chicago, IL): https://cbot.com/ and https://en.wikipedia.org/wiki/Chicago_Board_of_Trade
- Chicago Mercantile Exchange (20 S. Wacker Dr, Chicago): https://www.cmegroup.com/investor-relations/investor-faqs.html and https://en.wikipedia.org/wiki/Chicago_Mercantile_Exchange
- New York Mercantile Exchange (One North End Ave, Manhattan): https://en.wikipedia.org/wiki/New_York_Mercantile_Exchange and https://en.wikipedia.org/wiki/One_North_End_Avenue
- COMEX timezone = America/New_York: https://markethours.io/market/comex and https://markethours.io/is-comex-open-today
- COMEX precious/base metals segment of NYMEX (New York): https://twelvedata.com/mic/xcec
- CFTC COMEX filing (300 Vesey St, New York, NY): https://www.cftc.gov/filings/orgrules/rule061517comexdcm001.pdf
- CBOT timezone = America/Chicago: https://markethours.io/market/cbot
- CME Group metals (Platinum/Palladium = NYMEX; delivery NY/NJ/DE): https://www.cmegroup.com/markets/metals/metals-product-guide.html and https://www.cmegroup.com/markets/metals/precious/palladium.contractSpecs.html and https://www.cmegroup.com/trading/metals/files/precious-metals-physical-delivery-process-2025.pdf
- CME Lumber (LBR, Chicago Switching District, CME Globex): https://www.cmegroup.com/markets/agriculture/lumber-and-softs/lumber.html

**CBOE Futures Exchange (CFE / CBOEF) — Chicago**
- CFTC CFE rulebook defining "Chicago time" = Central: https://www.cftc.gov/filings/orgrules/rules0912245976.pdf and https://www.cftc.gov/filings/orgrules/rule111215cfedcm001.pdf
- Cboe VIX futures trading hours (Chicago time): https://cdn.cboe.com/resources/global-trading-hours.pdf and https://www.cboe.com/en/tradable-products/vix/vix-futures/specifications/

**ICE — Futures U.S. (New York) vs Futures Europe (London)**
- ICE Futures U.S. New York office (1345 Ave of the Americas, NYC): https://www.ice.com/publicdocs/futures_us/member_notices/ICE_Futures_US_New_York_Office_Relocation_20241101.pdf and https://www.ice.com/futures-us/membership
- ICE Futures U.S. SEC filing ("a reference to a time … is a reference to time in New York"): https://www.sec.gov/Archives/edgar/data/1820302/000119312521303985/d219325dex1010.htm
- ICE Futures Europe (London-based, FCA-regulated): https://www.ice.com/futures-europe/regulation and https://www.ice.com/futures-europe and https://find-and-update.company-information.service.gov.uk/company/01528617
- ICE Brent Crude (ICE Futures Europe, London time): https://www.ice.com/brent-crude and https://www.ice.com/products/219/Brent-Crude-Futures/
- ICE product list (CC/KC/CT/OJ/SB/DX = IFUS = ICE Futures U.S.): https://www.ice.com/products/?filter=ifus
- ICE Futures U.S. regular trading hours: https://www.ice.com/publicdocs/futures_us/ICE_Futures_US_Regular_Trading_Hours.pdf

**EUREX — CET (Europe/Berlin)**
- Eurex trading hours (CET): https://www.eurex.com/ex-en/trade/trading-hours and https://www.eurexchange.com/resource/blob/4873184/d03af1201fbd640ba30e29ae5359c371/data/tradingcalendar_2026_en.pdf
- Eurex Frankfurt AG (Eschborn, Germany): per Eurex trading calendar 2026 imprint

**MGEX — Minneapolis, Central**
- MGEX rulebook (Minneapolis, "all times … Central Time"): https://www.miaxglobal.com/sites/default/files/job-files/2024-02-20-MGEXRulebook_0.pdf and https://www.cftc.gov/filings/orgrules/rule103119mgechdco001.pdf

**NYSE Arca (ARCX) — New York, Eastern**
- ARCX MIC (New York): https://www.tradinghours.com/mic/s/arcx
- NYSE Arca spec ("All times are US Eastern Time"): https://www.nyse.com/publicdocs/nyse/data/ArcaBook_Client_Specification.pdf

**TradeStation symbol → exchange references**
- TradeStation futures symbology comparison (CME/CBOT/NYMEX assignments): https://help.tradestation.com/10_00/eng/TradeStationHelp/symbology/futures_symbology_comparison.htm
- TradeStation futures symbol reference: https://help.tradestation.com/10_00/eng/TradeStationHelp/symbology/ts_futures_symbol_reference.htm
- TradeStation exchange & clearing fees (per-symbol Exchange column: YM/MYM/C/W/KW/S/BO/SM=CBOT, etc.): https://www.tradestation.com/pricing/exchange-execution-and-clearing-fees/

**In-repo evidence (API Exchange strings)**
- `old_data/minute_data/metadata.json` and `data/metadata.json` — actual `Exchange` field
  per symbol (24 symbols): CME, CBOT, NYMEX, COMEX, CBOEF. (`@VX` → CBOEF → tz=null.)
