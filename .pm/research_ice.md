# ICE Exchange-String & Logging Research

> Researcher deliverable for the ICE timezone-attribution issue in
> `tradestation/metadata.py` (`EXCHANGE_TIMEZONES`). All findings below are
> from a live API probe using the project's own `config.yaml` credentials
> (token refresh succeeded; API responded 200 to all calls).

---

## Part 1 — API exchange strings for ICE-traded symbols

### How `run_metadata()` fetches the `Exchange` field
- **`run_metadata()`** lives in `tradestation/metadata.py:261`.
- It calls **`fetch_symbol_details(auth, symbols)`** (`tradestation/metadata.py:105`),
  which performs `GET {BASE_URL}/marketdata/symbols/{comma_separated_symbols}`
  (`BASE_URL = "https://api.tradestation.com/v3"`, `metadata.py:20`, request at
  `metadata.py:115`) and returns `data.get("Symbols", [])` (`metadata.py:134`).
- The `Exchange` field is read per symbol at `metadata.py:315`:
  `exchange = api_data.get("Exchange") if api_data else None`, then resolved to a
  timezone via `get_exchange_timezone(exchange)` (`metadata.py:50`), which does
  `EXCHANGE_TIMEZONES.get(exchange.upper())` (`metadata.py:54`).
- **Auth**: `TradeStationAuth` (`tradestation/auth.py:17`) — `get_access_token()`
  (`auth.py:47`) auto-refreshes via `_refresh_access_token()` (`auth.py:68`), a
  POST to `https://signin.tradestation.com/oauth/token` with
  `grant_type=refresh_token`.

### Live results — exact `Exchange` strings

The one-off scripts reused `load_config()` + `TradeStationAuth` + the exact
`/marketdata/symbols/{symbol}` endpoint that `fetch_symbol_details` uses.

| Symbol | Resolves? | Raw `Exchange` | `repr(Exchange)` | Description |
|--------|-----------|----------------|------------------|-------------|
| `@BRN` (Brent, ICE Europe) | **NO — NotFound** | — | — | `"invalid symbol"` |
| `@NBP` (UK NBP gas, ICE Europe) | NO — NotFound | — | — | `"invalid symbol"` |
| `@MWS` / `@MP5` (ICE Europe) | NO — NotFound | — | — | `"invalid symbol"` |
| `@G` (gasoil, ICE Europe) | NO — NotFound | — | — | `"invalid symbol"` |
| `@SB` (Sugar #11, ICE U.S.) | YES | `ICEUS` | `'ICEUS'` | Sugar No. 11 Continuous [Oct26] |
| `@KC` (Coffee C, ICE U.S.) | YES | `ICEUS` | `'ICEUS'` | Coffee C Continuous [Sep26] |
| `@CC` (Cocoa, ICE U.S.) | YES | `ICEUS` | `'ICEUS'` | Cocoa Continuous [Sep26] |
| `@CT` (Cotton, ICE U.S.) | YES | `ICEUS` | `'ICEUS'` | Cotton No. 2 Continuous [Dec26] |
| `@NG` (control, NYMEX) | YES | `NYMEX` | `'NYMEX'` | Natural Gas Continuous [Sep26] |
| `@ES` (control, CME) | YES | `CME` | `'CME'` | E-mini S&P 500 Continuous [Sep26] |

A broader sweep of **25** plausible ICE Futures Europe continuous roots
(`@BRN`, `@B`, `@BRT`, `@G`, `@GOI`, `@GSO`, `@NBP`, `@NGG`, `@TTF`, `@TFG`,
`@MWS`, `@MP5`, `@CRA`, `@CARB`, `@EUA`, `@CER`, `@EB`, `@EBP`, `@JIB`, `@FFI`,
`@GBL`, `@WBS`, `@WB`, `@CO`, …) returned **NotFound for all 25**. Only `@SB`,
`@KC` (ICE U.S.) and `@C`/`@ES` (CBOT/CME controls) resolved.

### Key API-behavior findings
1. **ICE U.S. symbols return a DISTINCT, specific string `"ICEUS"`** — never the
   generic `"ICE"`. No trailing/leading whitespace; exact case `ICEUS` (`repr`
   confirms `'ICEUS'`).
2. **`"ICEUS"` is NOT a key in `EXCHANGE_TIMEZONES`** (keys are `ICE`, `IFEU`,
   `ICE FUTURES EUROPE`). So `get_exchange_timezone("ICEUS")` returns **`None`**.
   → `@SB`/`@KC`/`@CC`/`@CT` currently get **`tz = None`** (NO timezone
   attribution at all), not New York.
3. **Every ICE Futures Europe symbol returns NotFound** with this account
   (`@BRN` and 24 alternatives). The API returns HTTP 200 with body
   `{"Symbols": [], "Errors": [{"Error": "NotFound", "Message": "invalid
   symbol", "Symbol": "@BRN"}]}`. This is an **account entitlement limitation**
   (no ICE Europe market-data subscription), not a naming issue — the same
   endpoint returns full records for ICE U.S. / CME / CBOT / NYMEX.
4. **`fetch_symbol_details` silently swallows per-symbol errors**: it only reads
   `data.get("Symbols", [])` (`metadata.py:134`) and ignores the `Errors`
   array. So when `@BRN` is in a batch, it vanishes with no log/warning. This
   is a secondary bug worth noting (a `NotFound` symbol produces zero output
   and zero log line).

---

## Part 2 — Project logging convention

- **Mechanism**: the standard library **`logging`** module. No `rich`,
  `structlog`, or custom framework. (Some CLI help/list output uses `print()`,
  e.g. `metadata.py:361` prints the JSON to stdout, `metadata.py:289` prints an
  error to stderr; `cli.py:115-137` uses `print()` for `--list-*`.)
- **Package-level loggers** (one per module, propagating to root):
  - `tradestation/metadata.py:18`   → `logger = logging.getLogger(__name__)`
  - `tradestation/downloader.py:21` → `logger = logging.getLogger(__name__)`
  - `tradestation/auth.py:10`       → `logger = logging.getLogger(__name__)`
  - `tradestation/cli.py:19`        → `logger = logging.getLogger(__name__)`
- **Handler configuration** — the root logger is configured once at CLI entry:
  - `tradestation/cli.py:14-18`:
    ```python
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ```
    This attaches a `StreamHandler` (stderr) to the **root** logger at
    **INFO** level. `-v/--verbose` lowers the root to DEBUG
    (`cli.py:212-213`).
- **Is an ERROR-level message loud/visible under `--metadata`?**
  YES. `run_metadata()` is invoked from `cli.py:151-153` AFTER `cli.py` import
  has run `basicConfig`, so the root logger already has a console handler at
  INFO. `metadata.py` emits errors via `logger.error(...)` (e.g. `metadata.py:266`,
  `:284`, `:301`, `:366`, and inside `fetch_symbol_details` at `:119/:122/:132`).
  Since `ERROR >= INFO`, these are formatted and printed to the console as
  `YYYY-MM-DD HH:MM:SS | ERROR    | ...`. No extra setup is required.

### Recommended loud-error approach for the implementer
Use the **existing** module logger in `metadata.py` (already `logger =
logging.getLogger(__name__)` at `metadata.py:18`) and emit at **`logger.error(...)`
or `logger.warning(...)`** from inside `run_metadata()` / `get_exchange_timezone()`
context. Because `cli.py:14-18` already configures a console handler on the root
logger at INFO, these messages **will** appear on stderr when running
`tradestation-download --metadata`. Do **not** introduce a new handler or
`print()` — reuse the logging path so it respects `-v` (DEBUG) and the
`%(levelname)-8s` formatting. `logger.error(...)` is the most prominent
(visible) level still below CRITICAL; for a non-fatal data-quality caveat
`logger.warning(...)` is appropriate and equally visible.

---

## Part 3 — Verdict

### Is the timezone-attribution problem true for ALL ICE-traded futures? → **NO**

The premise was: "if the API returns a GENERIC `ICE` string for all ICE symbols,
we can never distinguish U.S. from Europe → any ICE symbol could be
mis-attributed."

Live testing shows the API does **NOT** return a generic `"ICE"` string for the
ICE symbols this account can access. The distinguishing evidence:

- **ICE Futures U.S.** symbols (`@SB`, `@KC`, `@CC`, `@CT`) return the **distinct,
  specific** string **`"ICEUS"`** — not `"ICE"`.
- **ICE Futures Europe** symbols (`@BRN` and 24 alternatives) return
  **`NotFound / "invalid symbol"`** — they cannot be fetched at all with this
  account (entitlement limitation).

So the strings that distinguish the cases are:
- ICE U.S. ⇒ `"ICEUS"` (observed, specific)
- ICE Europe ⇒ `NotFound` (observed, with this account) — would presumably be
  `"ICE"`, `"IFEU"`, or `"ICE FUTURES EUROPE"` on an account *with* ICE Europe
  entitlements, but that is **untested/hypothetical** for this account.
- Generic `"ICE"` ⇒ **observed for ZERO symbols**. The generic `ICE` key in
  `EXCHANGE_TIMEZONES` is currently **dead/unused** — nothing resolves to it.

**Conclusion**: The "generic ICE → mis-attribution" failure mode does **not**
manifest for any resolvable ICE symbol today. The actual current defect is the
opposite direction: `"ICEUS"` is missing from the map, so ICE U.S. symbols get
`tz=None` (no timezone) rather than a wrong timezone.

### BRN status — does `@BRN` currently resolve correctly or mis-attribute?

**Neither — `@BRN` does not resolve at all.** It returns `NotFound / "invalid
symbol"` (`{"Symbols": [], "Errors": [...]}`), so this downloader cannot fetch
it and cannot build a metadata record for it. There is therefore **no current
opportunity for mis-attribution**: `@BRN` produces no data and no
`exchange_timezone` value.

- It does **not** hit the generic `ICE` → `America/New_York` key (no record is
  produced at all).
- It does **not** hit the `IFEU` / `ICE FUTURES EUROPE` → `Europe/London` keys.

**Hypothetical note (for the PM)**: If this account later gains ICE Europe
entitlements and `@BRN` resolves, its `Exchange` string would determine the
outcome:
- If the API returns `"ICE"` → hits the generic `ICE` key → **`America/New_York`
  (WRONG for London-traded Brent)** — the described mis-attribution WOULD occur.
- If the API returns `"IFEU"` or `"ICE FUTURES EUROPE"` → **`Europe/London`
  (correct)**.
- If the API returns `"ICEE"`/`"IFEU"`/some new ICE-Europe string not in the map
  → `tz=None` (no attribution).
Which of these happens is **untestable** with the current account.

---

## Recommended warning scope

The PM's options were:
(a) all symbols whose exchange string == `"ICE"` (generic),
(b) all ICE-family strings (`ICE`/`IFEU`/`ICE FUTURES EUROPE`/`ICE FUTURES U.S.`),
(c) only `@BRN`,
(d) something else.

**Recommendation: (a) for the *mis-attribution-to-New-York* warning, plus a
separate (d) data-quality warning for the *missing-key* case observed today.**

Rationale, grounded in observed API behavior:

1. **Primary warning — option (a)**: fire a loud `logger.warning`/`logger.error`
   when a symbol's `Exchange` string resolves **via the generic `ICE` key**
   (i.e. `get_exchange_timezone` returns `America/New_York` because the raw
   string upper-cased to exactly `"ICE"`). This is the one ambiguous path that
   could mask an ICE Europe (London) symbol as New York. It is the *precise*
   expression of the described risk. (Note: with the current account this
   currently fires for **zero** symbols, since nothing returns `"ICE"` — but it
   is the correct guard for when ICE Europe entitlement is added and a symbol
   returns the generic `"ICE"`.)

2. **Secondary warning — option (d)**: also warn (at least
   `logger.warning`) whenever an ICE-prefixed exchange string yields `tz=None`
   — i.e. `get_exchange_timezone` returns `None` but `exchange.upper()`
   starts with `"ICE"`. This catches the **actually-observed** defect today:
   `@SB`/`@KC`/`@CC`/`@CT` return `"ICEUS"`, which is **not** in
   `EXCHANGE_TIMEZONES`, so they silently get `tz=None` and their
   session-time derivation runs without any timezone conversion. This is the
   bug the user is most likely to hit right now.

3. **Do NOT use (b)** in isolation: `"IFEU"` and `"ICE FUTURES EUROPE"` already
   resolve **correctly** to `Europe/London`, so warning on them would be noise.
   A blanket "all ICE-family" warning would cry wolf on correctly-attributed
   London symbols.

4. **Do NOT use (c)**: `@BRN` is currently `NotFound`; a `@BRN`-only warning
   would never fire and wouldn't generalize to other ICE Europe symbols if
   entitlements change.

**Concrete implementation hint**: the natural place is around
`metadata.py:315-316` (`exchange = api_data.get("Exchange")`; `tz =
get_exchange_timezone(exchange)`), emitting via the existing
`logging.getLogger(__name__)` (`metadata.py:18`). Two cases:
- `exchange` upper == `"ICE"` → `logger.error("Symbol %s exchange '%s' is the
  generic ICE key -> America/New_York; may be an ICE Futures Europe (London)
  symbol mis-attributed. Verify entitlement/exchange.", symbol, exchange)`
- `tz is None and exchange and exchange.upper().startswith("ICE")` →
  `logger.warning("Symbol %s exchange '%s' not in EXCHANGE_TIMEZONES; timezone
  unknown, session times derived in UTC only.", symbol, exchange)`

(The PM makes the final scoping call; the above is the evidence-based
recommendation.)

---

## Files produced / cleaned up
- Temp one-off scripts (created under `.pm/`, **deleted** after use):
  `.pm/query_ice.py`, `.pm/query_ice2.py`, `.pm/query_ice3.py`
- This deliverable: `.pm/research_ice.md` (kept)
- No production code or tests were modified.
