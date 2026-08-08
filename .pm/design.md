# Design: fix `sessions`/`sessions_utc` mirror-image bug + exchange→timezone misattributions

Self-contained design doc. The implementer reads **this file plus `tradestation/metadata.py` and `tests/test_metadata.py` only**. All root-cause context is inlined below with exact file:line touchpoints.

Two bugs, both in `tradestation/metadata.py`:
- **Bug 1 — `sessions_utc` loses gaps that `sessions` keeps.**
- **Bug 2 — `EXCHANGE_TIMEZONES` misattributes NYMEX/COMEX, is missing `CBOEF`, and cannot represent ICE Futures Europe.**

---

## 0. Root-cause context (inline, for the implementer)

### Bug 1 root cause
`derive_sessions()` (`metadata.py:172-226`) infers contiguous session blocks by building **one global set of minute-of-day values across ALL dates** and cutting wherever a gap exceeds `gap_threshold_minutes` (default 15):

```python
# metadata.py:184
minutes = set((series.dt.hour * 60 + series.dt.minute).astype(int).tolist())
# metadata.py:188
if len(minutes) == 1440:
    return [{"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}]
```

The maintenance pause of a futures session is at a **stable LOCAL minute-of-day**, so in the exchange-local DataFrame the gap is always in the same place and is detected. In UTC the same pause lands at **different UTC minute-of-day values in winter vs. summer** (DST shifts the offset by 1h). The **union** of winter + summer UTC minute-of-day values fills the entire 24h circle → `len(minutes) == 1440` → `derive_sessions()` returns a single 24h block and **the gap disappears**.

`run_metadata()` (`metadata.py:282-294`) builds the two fields from **two independent derivations on different data**:
```python
session_times_utc = derive_session_times(df)      # metadata.py:282  -- raw UTC df, ALL dates
sessions_utc      = derive_sessions(df)           # metadata.py:283
...
df_local = convert_df_timezone(df, tz)            # metadata.py:292
session_times = derive_session_times(df_local)    # metadata.py:293  -- local df, ALL dates
sessions      = derive_sessions(df_local)         # metadata.py:294
```
So `sessions` keeps the gap (local is stable), `sessions_utc` loses it (UTC union collapses). They are not mirror images.

Worked example (`@ES`, CME, `America/Chicago`, session 17:00→16:00 CT next day, maintenance 16:00–17:00 CT):
- Local union (all dates): `{0..960, 1020..1439}` → gap 16:00→17:00 (59 min) → **1 block `{17:00, 16:00, 23.02h, spans_midnight:true}`**. ✓ gap preserved.
- Winter UTC (CST, UTC−6): pause 22:00–23:00 UTC → `{0..1320, 1380..1439}`.
- Summer UTC (CDT, UTC−5): pause 21:00–22:00 UTC → `{0..1260, 1320..1439}`.
- UTC union (winter ∪ summer) = `{0..1439}` → 1440 → **1 block `{00:00, 23:59, 24.0h, spans_midnight:false}`**. ✗ gap lost.

> **Key insight to encode:** the bug is the *cross-DST-season* union. Within a single season the minute-of-day set is stable and the gap survives. The fix must prevent the winter+summer union, **not** add manual offset arithmetic.

### Bug 2 root cause
`EXCHANGE_TIMEZONES` (`metadata.py:32-44`) maps `NYMEX` and `COMEX` to `America/Chicago`; both are New York exchanges → must be `America/New_York` (CME Group FAQ: NYMEX/COMEX timestamps in EST, CBOT in CST). `CBOEF` (CBOE Futures Exchange, returned by the API for `@VX`) is **missing** → `@VX` gets `tz=None`. A single `"ICE"` key cannot represent ICE Futures Europe (London, `Europe/London`) — `@BRN` (Brent) needs its own key. `get_exchange_timezone()` (`metadata.py:47-51`) does a case-insensitive `dict.get(exchange.upper())`, so **dict keys must be stored in UPPERCASE** to match.

---

## A. `sessions` / `sessions_utc` fix

### A.1 Approach chosen: **(b) restrict derivation to a single DST season — winter/standard preferred** (applied in the caller `run_metadata`, via one small helper)

The user's binding constraint 3 states the canonical `sessions_utc` "is satisfied naturally by deriving from real winter UTC bars, which need no manual conversion." That sanctions approach (b). Evaluation:

- **(a) Derive per-date, take modal pattern.** Fixes the union collapse inside `derive_sessions` without needing `tz`, and handles unknown-tz. But: modal picks the *majority* season (could be summer → violates winter-canonical), needs pattern-equality voting logic (~30 lines), and does not fit `derive_session_times` (a per-weekday min/max, not a gap detector — per-date modal is the wrong shape for it). Two different mechanisms for two functions = less clean.
- **(b) Restrict to one season (winter preferred, summer fallback) before deriving.** ~8 lines, reuses `derive_sessions`/`derive_session_times` **unchanged**, fixes the root cause (no cross-season union) for both functions with one uniform mechanism, guarantees winter-canonical when winter bars exist, and `sessions_utc` is produced from **real UTC bars** (constraint 2). Chosen.
- **(c) Derive structure once in local time, then read corresponding winter UTC bars via row correspondence.** Truest "single structure" but needs local-window→UTC-row mapping with wraparound handling; materially more complex than (b) and exceeds "smallest working diff."

**Why (b) best satisfies the constraints (≤3 sentences):** It is the smallest diff that removes the root cause (the cross-season union) while keeping `sessions_utc` derived purely from real UTC timestamps (constraint 2) and reflecting standard-time offsets when winter data exists (constraint 3). Both `sessions` and `sessions_utc` are derived from the **same canonical row-set** viewed in two timezones, so they are mirror images by construction (constraint 1) from a single derivation path (constraint 4). It reuses both existing derivation functions untouched, minimising regression risk.

### A.2 The new helper (the only new code)

Insert **one** small function after `convert_df_timezone()` (~after `metadata.py:70`):

```python
def select_standard_time_bars(df: pd.DataFrame, tz: str | None) -> pd.DataFrame:
    """Return the subset of ``df`` whose bars fall in the exchange's standard (winter) time.

    Used so session derivation does not collapse DST-shifted maintenance gaps via a
    cross-season minute-of-day union. If winter bars exist they are returned; otherwise
    the DST (summer) bars are returned (still a single, gap-stable season); if ``tz`` is
    None or the frame is empty/unclassifiable, ``df`` is returned unchanged.
    """
```

**Return shape:** a row-subset of `df` (same columns, same index type), containing only standard-time bars; or `df` unchanged when no classification is possible.

**Behaviour spec (illustrative — not production code):**
- If `tz is None` or `df` is empty or has neither a `DatetimeIndex` nor a `"datetime"` column → return `df`.
- Obtain the naive-UTC index/`datetime` series the same way `convert_df_timezone` does (`metadata.py:59-69`): `DatetimeIndex` via `df.index`, else `pd.DatetimeIndex(pd.to_datetime(df["datetime"]))`.
- `local_aware = idx.tz_localize("UTC").tz_convert(ZoneInfo(tz))` (every UTC instant maps to exactly one local instant, so no DST ambiguity/nonexistent-time issues).
- `dst = local_aware.to_series().dt.dst()` → a `Timedelta` per bar (`0` in winter/standard, non-zero in summer/DST). (pandas ≥ 2.0 per `pyproject.toml`.)
- `winter_mask = (dst == pd.Timedelta(0)).to_numpy()`. If `winter_mask.any()` → return `df[winter_mask]`. Else if `(~winter_mask).any()` → return `df[~winter_mask]` (summer fallback). Else return `df`.
- Boolean-mask indexing is positional and aligns with `df`'s rows because the mask is built from `df`'s own index/column.

> This is a **selection** of real UTC bars, not offset arithmetic. `sessions_utc` timestamps still come from `derive_sessions()` reading the UTC `HH:MM` of those bars (constraint 2 ✓).

### A.3 `derive_sessions()` and `derive_session_times()` — **NO internal change**

Both functions stay exactly as-is. Their contract becomes: *"given a DataFrame, derive blocks/min-max from the minute-of-day union; the caller is responsible for ensuring the frame is DST-consistent when gap preservation matters."* The caller (`run_metadata`) ensures that by feeding the canonical single-season subset.

### A.4 `derive_session_times()` — same DST-collapse bug? **Yes, same root cause; fixed symmetrically by the same mechanism (no internal change).**

`derive_session_times()` (`metadata.py:143-169`) takes per-weekday `min`/`max` of `series.dt.time` across **all dates**. For a non-wraparound session (e.g. equities 09:30–16:00) the cross-season mix makes `min` pick the summer extreme and `max` pick the winter extreme, stretching the window wrongly (e.g. UTC `13:30`–`21:00` instead of the standard-time `14:30`–`21:00`). It is the **same root cause** (mixing seasons in a global per-time-of-day aggregation), not the "gap disappears" variant. The fix is the **same**: feed it the single-season canonical frame (A.5). No internal edit to `derive_session_times` is required. (For wraparound sessions the per-weekday min/max is inherently degenerate `00:00`/`23:59` regardless of DST; after the fix it is at least *consistent* between local and UTC. The `sessions` field remains the authoritative gap representation; `session_times` is a coarse secondary field.)

### A.5 `run_metadata()` call-site change (`metadata.py:277-313`) — single derivation path, mirror by construction

**Reorder** so `tz` is known **before** deriving (today `tz` is computed at `metadata.py:290`, after the UTC derivation at 282-283). Replace the block at `metadata.py:278-297` with (illustrative):

```python
df = storage.load(symbol)
first = storage.get_first_timestamp(symbol)
last  = storage.get_last_timestamp(symbol)

api_data = details_by_symbol.get(symbol)                      # moved up from 288
exchange = api_data.get("Exchange") if api_data else None     # moved up from 289
tz = get_exchange_timezone(exchange)                          # moved up from 290

if df is not None and not df.empty:
    canon_df = select_standard_time_bars(df, tz)              # winter-pref, summer-fallback, all-if-no-tz
    sessions_utc       = derive_sessions(canon_df)            # real UTC bars, single season
    session_times_utc  = derive_session_times(canon_df)
    if tz:
        canon_df_local = convert_df_timezone(canon_df, tz)   # SAME rows, local view
        sessions       = derive_sessions(canon_df_local)
        session_times  = derive_session_times(canon_df_local)
    else:
        session_times  = session_times_utc
        sessions       = sessions_utc
else:
    sessions_utc = session_times_utc = []
    session_times_utc = {}
    sessions = session_times = ([] if False else [])          # see note: use [] / {} per field
    # sessions = []; session_times = {}
```
> Correct the illustrative else-branch to: `sessions_utc, sessions = [], []`; `session_times_utc, session_times = {}, {}`.

**Why this is a single derivation path / single source of truth:** one canonical row-set (`canon_df`) is selected once; `sessions_utc` is derived from it (UTC view) and `sessions` from `convert_df_timezone(canon_df, tz)` (local view of the *same rows*). Same rows + same algorithm + a **constant** offset (canonical season ⇒ no DST mix ⇒ constant offset) ⇒ identical block count, gap count, `duration_hours`, `spans_midnight`; only the `HH:MM` timestamps differ by the constant standard offset. **Mirror image by construction.** This replaces today's two independent derivations on different data.

**Unchanged fields (do NOT filter these):** `total_bars` (`metadata.py:306`, uses `len(df)` — full download size), `first_timestamp`/`last_timestamp` (`metadata.py:304-305`, full range). The canonical subset is used **only** for session derivation.

**Edge cases:**
- **No winter data (symbol's data is all summer):** `select_standard_time_bars` returns the summer subset (single season, gap-stable). `sessions_utc` then reflects summer UTC offsets — the best available from real bars with no manual conversion. Constraint 3 is honoured *when winter data exists*; summer is the graceful fallback.
- **Unknown `tz` (`exchange` not in `EXCHANGE_TIMEZONES`):** `select_standard_time_bars` returns `df` unchanged; `sessions = sessions_utc` (both from full `df`). They are **trivially mirror-identical** (equal). If `df` spans DST the gap may collapse in both — acceptable because (a) mirror-image (constraint 1) still holds, (b) gap preservation requires a known tz, (c) after Bug 2 this is rare (only `@BRN` if its API string is unconfirmed). Documented limitation, not a regression (today these symbols already get `tz=None`).

---

## B. Exchange→timezone fix

### B.1 Exact `EXCHANGE_TIMEZONES` edits (`metadata.py:32-44`)

Keys must be **UPPERCASE** (`get_exchange_timezone` does `exchange.upper()`). Before → after:

```python
EXCHANGE_TIMEZONES = {
    "CME": "America/Chicago",
    "CBOT": "America/Chicago",
    "COMEX": "America/New_York",          # was America/Chicago  (FIXED)
    "NYMEX": "America/New_York",          # was America/Chicago  (FIXED)
    "CBOEF": "America/Chicago",           # ADDED  (CBOE Futures Exchange, @VX)
    "NYSE": "America/New_York",
    "NASDAQ": "America/New_York",
    "AMEX": "America/New_York",
    "ARCX": "America/New_York",
    "ICE": "America/New_York",            # kept as ICE Futures U.S. fallback
    "IFEU": "Europe/London",              # ADDED  (ICE Futures Europe, @BRN) -- confirm API string
    "ICE FUTURES EUROPE": "Europe/London",# ADDED  (uppercase form; .upper() lookup) -- confirm API string
    "EUREX": "Europe/Berlin",
    "EUREX_US": "America/Chicago",
}
```

Net: **2 value changes** (`COMEX`, `NYMEX`), **3 keys added** (`CBOEF`, `IFEU`, `ICE FUTURES EUROPE`). `MGEX`→`America/Chicago` is **optional, not required** (no `DEFAULT_SYMBOLS` entry maps to it today); may be added for completeness but is out of scope for the minimal fix.

### B.2 `@BRN` / ICE Futures Europe — confirmation + safe default

The exact TradeStation API `Exchange` string for `@BRN` is **not confirmed** (it is not among the 24 downloaded symbols in the existing `metadata.json`; the research only confirmed `CME/CBOT/NYMEX/COMEX/CBOEF`).

**Implementer must confirm** the API string by one of:
1. Run `fetch_symbol_details(auth, ["@BRN"])` against the live API and read `["Exchange"]` (requires valid credentials in `config.yaml`), **or**
2. Check any existing `metadata.json` / parquet fixture that already contains `@BRN`, **or**
3. Check TradeStation symbology/fees docs.

**Once confirmed:** add the API string's **UPPERCASE** form as a dict key → `"Europe/London"`. Remove the speculative keys that do not match.

**Safe default if it cannot be confirmed (ship-ready):** keep **both** `"IFEU"` and `"ICE FUTURES EUROPE"` → `"Europe/London"` (covers the two most likely distinct identifiers), and keep `"ICE"` → `"America/New_York"` as the ICE Futures U.S. fallback. This is the state in §B.1.

**⚠ Critical risk (see §E):** if the API returns the **same** string `"ICE"` for both ICE Futures U.S. (NY) and ICE Futures Europe (London), the exchange-string→tz mapping is **fundamentally ambiguous** and `@BRN` would be mis-attributed to New York. In that case a per-symbol override is required — that **exceeds the minimal fix** and needs PM/user sign-off. The implementer should escalate rather than guess.

### B.3 `get_exchange_timezone()` (`metadata.py:47-51`) — **NO change**

It already does `exchange.upper()` then `EXCHANGE_TIMEZONES.get(...)`. New uppercase keys are matched correctly. Confirmed: no change needed.

---

## C. Test plan (Red phase — failing tests first)

All tests extend `tests/test_metadata.py`. Add a module-level fixture builder and the cases below. TDD: write these **before** the fix; they must fail on the current code (Red), pass after (Green).

### C.1 Shared fixture: a DST-spanning UTC DataFrame with a known maintenance gap

Add to `tests/test_metadata.py` (near `_create_sample_df` / `TestDeriveSessions`):

```python
def _dst_spanning_session_df():
    """1-min bars for 3 winter days + 3 summer days; CME session 17:00->16:00 CT,
    maintenance pause 16:00-17:00 CT.

    Winter (CST, UTC-6): pause 22:00-23:00 UTC -> bars 00:00-22:00 and 23:00-23:59 each day.
    Summer (CDT, UTC-5): pause 21:00-22:00 UTC -> bars 00:00-21:00 and 22:00-23:59 each day.
    Feeding the FULL frame to derive_sessions() today collapses to a 24h block (the bug).
    """
    ts = []
    for d in ("2024-01-08", "2024-01-09", "2024-01-10"):        # winter
        ts += pd.date_range(f"{d} 00:00", f"{d} 22:00", freq="1min").tolist()
        ts += pd.date_range(f"{d} 23:00", f"{d} 23:59", freq="1min").tolist()
    for d in ("2024-07-08", "2024-07-09", "2024-07-10"):        # summer
        ts += pd.date_range(f"{d} 00:00", f"{d} 21:00", freq="1min").tolist()
        ts += pd.date_range(f"{d} 22:00", f"{d} 23:59", freq="1min").tolist()
    n = len(ts)
    return pd.DataFrame({
        "datetime": pd.DatetimeIndex(ts),
        "open": [100.0]*n, "high": [101.0]*n, "low": [99.0]*n,
        "close": [100.5]*n, "volume": [1000]*n,
    })
```

Expected post-fix values:
- `derive_sessions(select_standard_time_bars(df, "America/Chicago"))` → `[{start:"23:00", end:"22:00", duration_hours:23.02, spans_midnight:True}]` (winter pause 22:00–23:00 UTC).
- `derive_sessions(convert_df_timezone(select_standard_time_bars(df, "America/Chicago"), "America/Chicago"))` → `[{start:"17:00", end:"16:00", duration_hours:23.02, spans_midnight:True}]` (local pause 16:00–17:00 CT).
- Today, `derive_sessions(df)` (full, mixed seasons) → `[{start:"00:00", end:"23:59", duration_hours:24.0, spans_midnight:False}]` (the bug).

### C.2 New unit tests (Red)

| # | Test name (in `TestDeriveSessions` / new class) | Asserts | Why it fails today |
|---|---|---|---|
| 1 | `test_select_standard_time_bars_prefers_winter` | `select_standard_time_bars(_dst_spanning_session_df(), "America/Chicago")` contains only Jan (winter) timestamps; no Jul timestamps; row count == winter bar count. | `select_standard_time_bars` does not exist → `ImportError`. |
| 2 | `test_derive_sessions_preserves_gap_on_dst_spanning_utc` | `r = derive_sessions(select_standard_time_bars(df, "America/Chicago"))`; `len(r)==1`; `r[0]["spans_midnight"] is True`; `r[0]["start"]=="23:00"`; `r[0]["end"]=="22:00"`; `r[0]["duration_hours"]==23.02`; **not** the 24h block. | Helper missing → `ImportError`. (And `derive_sessions(df)` directly still collapses today.) |
| 3 | `test_derive_sessions_collapses_on_mixed_seasons_today` (characterization, kept) | `derive_sessions(_dst_spanning_session_df()) == [{00:00, 23:59, 24.0, False}]` — documents the bug. | **Passes** today (kept as a regression guard for the low-level contract). |
| 4 | `test_sessions_utc_and_sessions_are_mirror_images` (integration; see C.3) | `len(sessions)==len(sessions_utc)`; for each block `spans_midnight` and `duration_hours` match; `sessions`=`{17:00,16:00,...}`, `sessions_utc`=`{23:00,22:00,...}`. | Today `sessions_utc` is the 24h block → `spans_midnight` False / 24.0h ≠ local → mirror fails. |
| 5 | `test_nymex_and_comex_map_to_new_york` | `get_exchange_timezone("NYMEX")=="America/New_York"`; `get_exchange_timezone("COMEX")=="America/New_York"`. | Today both return `"America/Chicago"`. |
| 6 | `test_cboef_maps_to_chicago` | `get_exchange_timezone("CBOEF")=="America/Chicago"`; also `get_exchange_timezone("cboef")=="America/Chicago"` (case-insensitive). | `CBOEF` key missing → returns `None`. |
| 7 | `test_ice_futures_europe_maps_to_london` | `get_exchange_timezone("IFEU")=="Europe/London"` and `get_exchange_timezone("ICE FUTURES EUROPE")=="Europe/London"`; `get_exchange_timezone("ice futures europe")=="Europe/London"`. (Adjust to the confirmed API string — see §B.2.) | Keys missing → `None`. |
| 8 | `test_ice_us_still_new_york` (regression guard) | `get_exchange_timezone("ICE")=="America/New_York"`. | Passes today; kept to ensure the NY fallback is not broken. |

A `derive_session_times` mirror assertion (recommended, in `TestDeriveSessionTimes`): for a **non-wraparound** DST-spanning fixture (e.g. 09:30–16:00 NY session, winter + summer bars), assert `session_times_utc` (via `select_standard_time_bars`) equals the standard-time shift of `session_times` (e.g. local `{09:30,16:00}` → UTC winter `{14:30,21:00}`), and that the un-filtered `derive_session_times(df)` is *wrong* (`13:30`–`21:00`, the stretched mix). This pins the symmetric fix for §A.4.

### C.3 Integration test updates (existing tests that assert old values)

Two integration tests recompute expected values directly and must be updated to the new single-source derivation:

- **`test_run_metadata_full_flow`** (`test_metadata.py:395-453`) and **`test_sessions_in_run_metadata_output`** (`test_metadata.py:604-640`).

They currently do:
```python
expected_sessions       = derive_sessions(convert_df_timezone(df, "America/Chicago"))   # ALL rows
expected_sessions_utc   = derive_sessions(df)                                           # ALL rows
```
After the fix, `run_metadata` derives from the canonical subset. For their **current 6-bar January fixture** (all winter) `canon_df == df`, so the values are unchanged and these tests still pass — **but** the expected-computation must be updated to reflect the new path and to stay correct if the fixture ever spans DST:

```python
canon   = select_standard_time_bars(df, "America/Chicago")                       # new
expected_sessions     = derive_sessions(convert_df_timezone(canon, "America/Chicago"))
expected_sessions_utc = derive_sessions(canon)
expected_session_times     = derive_session_times(convert_df_timezone(canon, "America/Chicago"))
expected_session_times_utc = derive_session_times(canon)
# plus mirror assertions:
assert len(dd["sessions"]) == len(dd["sessions_utc"])
for a, b in zip(dd["sessions"], dd["sessions_utc"]):
    assert a["spans_midnight"] == b["spans_midnight"]
    assert a["duration_hours"] == b["duration_hours"]
```
Add `select_standard_time_bars` to the import list at `test_metadata.py:11-19`.

**Add a new DST-spanning integration test** (the real exercise of the bug, = test #4 above): save `_dst_spanning_session_df()` via `SingleFileStorage` (extend `_save_symbol` or add `_save_dst_spanning_symbol`), run `run_metadata` with `Exchange="CME"`, and assert the mirror-image properties and the exact `sessions`/`sessions_utc` block values from C.1. This test **fails today** (24h `sessions_utc`) and **passes after**.

**Unaffected existing test:** `test_sessions_per_bar_dst_correct` (`test_metadata.py:710-719`) calls `derive_sessions(convert_df_timezone(df, "America/Chicago"))` directly (not via `run_metadata`/`select_standard_time_bars`) on a tiny close-only frame — `derive_sessions` is unchanged, so this test stays green. No edit.

---

## D. Implementation plan (Green phase) — ordered, minimal

1. **`EXCHANGE_TIMEZONES`** (`metadata.py:32-44`): change `COMEX`→`America/New_York`, `NYMEX`→`America/New_York`; add `"CBOEF": "America/Chicago"`, `"IFEU": "Europe/London"`, `"ICE FUTURES EUROPE": "Europe/London"` (adjust ICE-Europe keys per §B.2 once the `@BRN` API string is confirmed). Keep `"ICE": "America/New_York"`.
2. **Add `select_standard_time_bars(df, tz)`** after `convert_df_timezone()` (~`metadata.py:71`), per §A.2.
3. **Rewire `run_metadata`** (`metadata.py:278-297`) per §A.5: compute `tz` first; select `canon_df = select_standard_time_bars(df, tz)`; derive `sessions_utc`/`session_times_utc` from `canon_df`; derive `sessions`/`session_times` from `convert_df_timezone(canon_df, tz)` (same rows); keep the `tz is None` → `sessions = sessions_utc` fallback. **Do not** change `total_bars`/`first_timestamp`/`last_timestamp` (still from full `df`).
4. **`get_exchange_timezone`** — no change.
5. **`derive_sessions` / `derive_session_times`** — no change.
6. Update the two integration tests' expected-computations + imports per §C.3; add the DST-spanning fixture + new tests per §C.2.

**Existing tests that break and must be updated:** only the two integration tests' expected-value lines noted above (and only their *expected-computation expressions*, not their assertions, since the all-winter fixture yields identical values). All other existing unit tests stay green because `derive_sessions`/`derive_session_times`/`get_exchange_timezone` behaviour for already-correct inputs is unchanged.

### Lint / typecheck / test commands (from `pyproject.toml`)
- Ruff config: `line-length=100`, `target-version="py310"`, selects `E,W,F,I,B,C4,UP,ARG,SIM`; ignores `E501,B008`. No mypy configured.
- Lint: `uv run ruff check tradestation tests` (fix any `ARG`/`SIM`/import issues in the new helper).
- Format check: `uv run ruff format --check .`
- Tests: `uv run pytest tests/test_metadata.py` (full: `uv run pytest`; `addopts` already includes `-v --cov`).

---

## E. Risks / open questions / sign-off triggers

1. **`@BRN` API `Exchange` string (BLOCKING for the ICE-Europe key).** Must be confirmed per §B.2. **If the API returns `"ICE"` for both ICE Futures U.S. and ICE Futures Europe**, the mapping is ambiguous and a per-symbol override table is required — **this exceeds the minimal fix and needs PM/user sign-off.** Escalate; do not guess.
2. **ICE Futures U.S. softs (`@KC @SB @CT @CC @OJ @DX`).** The research marks these `OK*`: correct **only if** the API returns `"ICE"`. If the API returns `"IFUS"`/`"ICEUS"` they are missing today (`tz=None`). The implementer should confirm at least one (`@KC`) when credentials are available and add the exact uppercase key → `America/New_York` if needed. (Not blocking for the confirmed bugs; flagged.)
3. **`derive_session_times` downstream usage.** Grep confirms `run_metadata` (`metadata.py:282-294`) is the **only** production caller of `derive_sessions`/`derive_session_times`; all other references are tests. The fix is caller-side, so no other production code is affected. (The `.aim` memory notes a `sessions_per_bar` concept exists only as a test, `test_metadata.py:710-719` — not a production caller.) Implementer should re-grep `derive_sessions|derive_session_times|select_standard_time` after editing to be safe.
4. **Unknown-tz gap loss.** With `tz=None`, `sessions = sessions_utc` from the full `df`; if it spans DST the gap collapses in **both** (still mirror-identical, constraint 1 holds). Gap preservation needs a known tz. Acceptable post-Bug-2 (rare). Documented; no sign-off needed.
5. **Summer-only data.** `sessions_utc` then reflects summer (CDT) offsets, not standard-time. This is the graceful fallback when no winter bars exist; constraint 3 is honoured whenever winter data exists. No sign-off needed (falls out of the user-sanctioned "derive from real winter bars" with a best-available fallback).
6. **No decision in this design exceeds the minimal fix except** the potential per-symbol ICE override in item 1, which is explicitly flagged for sign-off. The `MGEX` key is explicitly out of scope.
