# `sessions` / `sessions_utc` inconsistency research

## Locations

| Site | Purpose |
|------|---------|
| `tradestation_downloader.py:19-22` | Entry-point wrapper; imports and runs `main_download`. |
| `tradestation/cli.py:95-98` | `--metadata` argument definition. |
| `tradestation/cli.py:151-153` | `--metadata` branch that lazily imports and calls `run_metadata`. |
| `tradestation/metadata.py:229-329` | `run_metadata()` — builds `metadata.json`. |
| `tradestation/metadata.py:32-44` | `EXCHANGE_TIMEZONES` — hard-coded exchange -> IANA timezone map. |
| `tradestation/metadata.py:47-51` | `get_exchange_timezone()` — lookup helper. |
| `tradestation/metadata.py:54-70` | `convert_df_timezone()` — per-bar UTC -> local-time conversion. |
| `tradestation/metadata.py:143-169` | `derive_session_times()` — per-weekday min/max times. |
| `tradestation/metadata.py:172-226` | `derive_sessions()` — contiguous session-block derivation from minute-of-day union. |
| `tradestation/metadata.py:299-312` | JSON assembly of the `downloaded_data` block. |
| `tradestation/downloader.py:302-323` | `_bars_to_dataframe()` — stores timestamps as naive UTC. |
| `tradestation/storage.py:17-38` | `_prepare_dataframe()` — loads timestamps as naive, timezone-stripped. |

## Data flow

1. CLI flag `--metadata` is defined at `tradestation/cli.py:95-98`.
2. `run_download()` sees `args.metadata` and calls `run_metadata(args.config)` at `tradestation/cli.py:151-153`.
3. `run_metadata()` at `tradestation/metadata.py:229` does:
   - loads config,
   - detects/ creates the storage backend,
   - lists symbols via `storage.list_symbols()` at line 255,
   - fetches symbol details and quotes from the TradeStation API.
4. For each symbol it loads the stored bars:

   ```python
   df = storage.load(symbol)            # metadata.py:278
   first = storage.get_first_timestamp(symbol)
   last = storage.get_last_timestamp(symbol)
   ```

   `df` contains **naive UTC** 1-minute bar timestamps (the downloader strips the `Z` in `downloader.py:307-311`; storage normalizes to naive in `storage.py:33-34`).
5. UTC session fields are produced **directly from `df`**:

   ```python
   session_times_utc = derive_session_times(df)   # metadata.py:282
   sessions_utc = derive_sessions(df)              # metadata.py:283
   ```
6. The exchange is taken from the API symbol detail:

   ```python
   api_data = details_by_symbol.get(symbol)
   exchange = api_data.get("Exchange") if api_data else None
   tz = get_exchange_timezone(exchange)            # metadata.py:288-290
   ```

   `get_exchange_timezone()` looks up the exchange in `EXCHANGE_TIMEZONES` at `metadata.py:47-51`.
7. If a timezone is known, the UTC DataFrame is converted to exchange-local time per-bar:

   ```python
   df_local = convert_df_timezone(df, tz)          # metadata.py:292
   session_times = derive_session_times(df_local)  # metadata.py:293
   sessions = derive_sessions(df_local)            # metadata.py:294
   ```
8. The results are placed in the JSON object at `metadata.py:299-312`.

## Where the gaps come from

`derive_sessions()` does **not** receive an explicit exchange session schedule. It infers "contiguous trading session blocks" by looking at the **set of all minute-of-day values** that appear anywhere in the DataFrame, and cutting the circle of 1,440 minutes wherever a gap exceeds `gap_threshold_minutes` (default 15).

For CME/CBOT equity-index futures such as `@ES`, the real Globex schedule is effectively a single long session with a regular daily maintenance pause (e.g., roughly 16:00-17:00 CT). That pause is a real non-trading period, so `derive_sessions()` should see a >15-minute gap and split the day into one session block that wraps midnight. There is no explicit "regular session" range in the code -- the split is inferred purely by the gap threshold in `derive_sessions()` at `metadata.py:191-198`.

## Root cause

The root cause is that `derive_sessions()` collapses every timestamp into a **single global set of `hour:minute` values**, ignoring the date. A stable local-time gap is detected correctly, but the *same* gap in UTC lands at different minute-of-day values across DST changes, so the union of all UTC minute-of-day values hides it.

The exact code that causes this is in `derive_sessions()`:

```python
# metadata.py:184
minutes = set((series.dt.hour * 60 + series.dt.minute).astype(int).tolist())

# metadata.py:191-198
sorted_minutes = sorted(minutes)
gaps = []
for i in range(len(sorted_minutes)):
    current = sorted_minutes[i]
    next_minute = sorted_minutes[(i + 1) % len(sorted_minutes)]
    gap_size = (next_minute - current - 1) % 1440
    if gap_size > gap_threshold_minutes:
        gaps.append((current, next_minute))

# metadata.py:188-201
if len(minutes) == 1440:
    return [{"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}]
...
if not gaps:
    return [{"start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": False}]
```

In `run_metadata()` the two fields are built from different time-of-day sets:

```python
# metadata.py:282-283
session_times_utc = derive_session_times(df)
sessions_utc = derive_sessions(df)

# metadata.py:291-294
df_local = convert_df_timezone(df, tz)
session_times = derive_session_times(df_local)
sessions = derive_sessions(df_local)
```

So:

- `sessions` is derived from the local-time DataFrame (`df_local`). The maintenance pause always falls at the same local minute-of-day, so `derive_sessions()` finds the gap and reports it.
- `sessions_utc` is derived from the raw UTC DataFrame (`df`). Because the dataset spans both CST and CDT, the same local pause maps to different UTC minute-of-day values in winter vs. summer. The global UTC minute set covers the whole 24 hours, no gap is detected, and `derive_sessions()` returns a single around-the-clock block.

The two JSON fields are therefore not mirror images: the local representation has the gap, the UTC representation does not.

## Worked example -- `@ES` (CME, America/Chicago)

Take the CME E-mini S&P 500 futures schedule as an example: a single Globex session that runs roughly 17:00 CT to 16:00 CT the next day, with a regular 1-hour maintenance halt from 16:00 CT to 17:00 CT.

Ignore the 1-minute bar-end timestamp convention for clarity (it shifts endpoints by one minute but does not change the gap behavior).

### Local exchange time (`sessions`)

Minute-of-day set observed in `df_local`:

```text
{0..960}  union  {1020..1439}
# 00:00-16:00  and  17:00-23:59
```

The gap is:

```text
960 (16:00) -> 1020 (17:00), gap_size = 59 minutes  (> 15)
```

`derive_sessions(df_local)` therefore returns one midnight-spanning block with the maintenance pause removed:

```json
[
  {
    "start": "17:00",
    "end": "16:00",
    "duration_hours": 23.02,
    "spans_midnight": true
  }
]
```

So `sessions` correctly contains the gap.

### UTC time (`sessions_utc`)

Winter (CST, UTC-6): the same session maps to

```text
{0..1320} union {1380..1439}
# 00:00-22:00 and 23:00-23:59
```

i.e., the pause is 22:00-23:00 UTC.

Summer (CDT, UTC-5): the session maps to

```text
{0..1260} union {1320..1439}
```

i.e., the pause is 21:00-22:00 UTC.

Now `derive_sessions(df)` sees the **union** of winter and summer minute-of-day values:

```text
{0..1320} union {1320..1439} = {0..1439}
```

Because both seasons together cover every minute of the UTC day, `len(minutes) == 1440`, and `derive_sessions()` returns:

```json
[
  {
    "start": "00:00",
    "end": "23:59",
    "duration_hours": 24.0,
    "spans_midnight": false
  }
]
```

### Result

```json
{
  "sessions":     [ { "start": "17:00", "end": "16:00", "duration_hours": 23.02, "spans_midnight": true } ],
  "sessions_utc": [ { "start": "00:00", "end": "23:59", "duration_hours": 24.0, "spans_midnight": false } ]
}
```

The local representation contains the real maintenance pause; the UTC representation does not. They are not the same shape.

## Timezone / exchange lookup mechanism

The timezone is resolved entirely inside `tradestation/metadata.py`:

- `EXCHANGE_TIMEZONES` at `metadata.py:32-44` maps exchange names to IANA timezone strings.
- `get_exchange_timezone()` at `metadata.py:47-51` performs a case-insensitive lookup.
- The exchange string comes from the TradeStation API symbol detail field `Exchange`, read at `metadata.py:288-289`.
- Conversion uses the stdlib `zoneinfo.ZoneInfo` inside `convert_df_timezone()` at `metadata.py:59-69`, which converts each timestamp individually, so DST is applied per-bar.

## Touchpoints for fix

A fix would need to touch at least these locations:

- `tradestation/metadata.py:172-226` -- `derive_sessions()` core algorithm that currently works on a global minute-of-day union.
- `tradestation/metadata.py:282-294` -- the `run_metadata()` branch where `sessions_utc` and `sessions` are produced independently from different time-of-day sets.
- `tradestation/metadata.py:54-70` -- `convert_df_timezone()` may need to expose or preserve the per-bar local<->UTC mapping so UTC session blocks can be derived from the local ones.
- `tradestation/metadata.py:299-312` -- JSON assembly if the returned session block structure changes.
- `tests/test_metadata.py:395-453` and `tests/test_metadata.py:604-640` -- integration tests that assert the current `sessions`/`sessions_utc` values and would need to reflect corrected behavior.
- `tests/test_metadata.py:499-602` -- `TestDeriveSessions` unit tests; a new case covering DST-shifted maintenance pauses is missing.
- `tests/test_metadata.py:710-719` -- `test_sessions_per_bar_dst_correct()` currently only verifies the session close time; it does not exercise a daily pause split in UTC.

## Tests / checks observed

- `tests/test_metadata.py:499-602` -- `TestDeriveSessions` covers empty, around-the-clock, single block, midnight-spanning, two blocks, small gaps, and the CBOT wheat real-world case. None of these cross DST periods, so none expose the UTC-union collapse.
- `tests/test_metadata.py:710-719` -- `test_sessions_per_bar_dst_correct()` checks that winter and summer close times both map to the same local close time; it does not check that a daily gap survives in the UTC representation.
- `tests/test_metadata.py:395-453` and `tests/test_metadata.py:604-640` -- integration tests for `run_metadata` use a tiny 6-bar sample in January only, so both local and UTC derivations agree by coincidence and the bug is not exercised.

No existing test asserts that `sessions` and `sessions_utc` have the same number of blocks and the same gaps.

---

**One-line summary:** `sessions_utc` is built by deriving contiguous blocks from the union of all UTC minute-of-day values across every date, which fills DST-shifted local-market gaps; `sessions` is built from per-bar local-time conversion, where the same gaps are stable and detected, so the two JSON fields diverge in shape.
