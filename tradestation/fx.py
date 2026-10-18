"""ECB daily euro reference rates for the TD Pro converter (``--fx``).

Self-contained FX feature module: fetches the ECB's daily Euro-Referenzkurs
series (``EXR/D.{CUR}.EUR.SP00.A``), caches them as JSON under
``orders/fx_cache/<CCY>.json`` (ECB-published values stored as strings for
exactness, plus the fetched ``windows`` that prove coverage), and resolves
the ``cur -> base`` conversion rate for a given calendar date using the
last-published-rate fallback (covers weekends, holidays, and
today-before-publication).

Coverage is EVIDENCE-BASED: a recorded window ends at the newest
publication the ECB actually returned — never the requested end — so a
window can never claim a day whose publication did not yet exist (a
pre-publication or delayed-publication fetch simply records a shorter
window and the next run refetches). Honest limitation: an ex-post ECB
REVISION to an already-published rate inside a recorded window is not
re-verified (out of scope).
"""

import contextlib
import csv
import io
import json
import logging
import os
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

ECB_URL = "https://data-api.ecb.europa.eu/service/data/EXR"
# cwd-relative, like orders.ORDERS_ROOT; NOT under orders/cache/<account>
# so `tradestation-download orders --full` (which wipes the per-account
# cache dir) can never delete it.
FX_CACHE_DIR = Path("orders") / "fx_cache"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 60
# Lookback margin applied to the fetch range start so a refetch also
# back-fills the days preceding the oldest needed date (publications are
# irregular — weekends/TARGET holidays).
FETCH_LOOKBACK_DAYS = 14
# Max gap a recorded window may span between two consecutive cached
# rates: the longest TRUE publication gap is ~5 days (the Easter
# cluster), so a >14d hole marks MISSING data (API omission, hand-edit,
# legacy bug) that no window may claim. Shared by the writer
# (``ensure_rates`` splits the recorded window there, leaving the hole
# unclaimed so it self-heals) and the loader (``_load_cache`` drops
# windows spanning such a hole).
MAX_RATE_GAP_DAYS = 14
# The ECB publication calendar is CET — all "today" logic uses the
# Europe/Berlin date, not UTC.
BERLIN = ZoneInfo("Europe/Berlin")


def _easter_sunday(year: int) -> date:
    """Easter Sunday of ``year`` via the Anonymous Gregorian computus
    (Meeus's algorithm — valid for all Gregorian years)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - canonical computus letter
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def ecb_publication_possible(d: date) -> bool:
    """Whether the ECB could publish a reference rate on ``d``.

    Publications happen on TARGET working days only: weekdays minus the
    TARGET closing days — Jan 1, Good Friday, Easter Monday, May 1,
    Dec 25, Dec 26 (the Easter-based days derive from the Anonymous
    Gregorian computus in ``_easter_sunday``). A date for which this is
    False can never have a publication, so the last published rate before
    it is final, not provisional.
    """
    if d.weekday() >= 5:
        return False
    easter = _easter_sunday(d.year)
    return d not in {
        date(d.year, 1, 1),
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        date(d.year, 5, 1),
        date(d.year, 12, 25),
        date(d.year, 12, 26),
    }


class FxError(Exception):
    """Raised when an ECB rate cannot be fetched, cached, or resolved."""


def _cache_path(currency: str) -> Path:
    return FX_CACHE_DIR / f"{currency}.json"


def _split_on_rate_gaps(
    rates: dict[date, Decimal], lo: date, hi: date
) -> list[tuple[date, date]]:
    """Split ``[lo, hi]`` at every >MAX_RATE_GAP_DAYS hole between
    consecutive cached rate dates inside the range.

    Each segment is ``[seg_start, seg_last_rate]``: ``lo`` for the first
    segment (its start→first-rate stretch holds leading pub-free days,
    which is normal), the segment's own first rate for later ones — the
    hole between segments stays unclaimed. Empty if no rate lies in
    ``[lo, hi]``. Used by both the writer (splitting the recorded fetch
    window) and the loader (>1 segment = the window claims a missing-
    data hole → drop it).
    """
    days = sorted(d for d in rates if lo <= d <= hi)
    segments: list[tuple[date, date]] = []
    seg_start = lo
    prev: date | None = None
    for d in days:
        if prev is not None and (d - prev).days > MAX_RATE_GAP_DAYS:
            segments.append((seg_start, prev))
            seg_start = d
        prev = d
    if prev is not None:
        segments.append((seg_start, prev))
    return segments


def _load_cache(
    currency: str,
) -> tuple[dict[date, Decimal], list[tuple[date, date]]]:
    """Load the cached rate series and fetched windows.

    Returns ``(rates, windows)``; any malformed content (unparseable JSON,
    non-ISO dates, non-Decimal/non-finite/non-positive rate values, bad
    window pairs) discards the WHOLE cache as corrupt — ``({}, [])`` plus
    a warning — so garbage values can never silently serve a lookup.
    The current format is v3 (``"version": 3`` written by ``_save_cache``);
    a missing ``"version"`` marks a legacy file and needs no special
    handling — the same validation rules apply. Legacy caches without a
    ``"windows"`` key load with ``windows=[]`` (the rates are kept and a
    refetch fills in the coverage bookkeeping).
    Individually overclaimed windows — an ``end`` that is not itself a
    published rate date, or a span crossing a >MAX_RATE_GAP_DAYS hole
    between consecutive cached rates — are dropped (info-level log,
    rates kept) so a one-time refetch rebuilds honest evidence-bounded
    coverage.
    """
    path = _cache_path(currency)
    if not path.exists():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rates: dict[date, Decimal] = {}
        for day, value in data["rates"].items():
            rate = Decimal(str(value))
            if not rate.is_finite() or rate <= 0:
                raise ValueError(f"invalid rate value {value!r}")
            rates[date.fromisoformat(day)] = rate
        windows = []
        for pair in data.get("windows", []):
            if len(pair) != 2:
                raise ValueError(f"malformed window {pair!r}")
            start, end = date.fromisoformat(pair[0]), date.fromisoformat(pair[1])
            if start > end:
                raise ValueError(f"inverted window {pair!r}")
            if end not in rates:
                # Overclaimed window: the v3 writer only records windows
                # ending on a real publication date (end = max fetched),
                # so an end that is not itself a published rate date is
                # legacy-inflated (recorded by the pre-v3 writer, whose
                # window end was the REQUESTED date, not evidence) or
                # corrupt. Drop it (keeping the rates) so the next
                # ensure_rates refetches once and records an
                # evidence-bounded window.
                logger.info(
                    "Dropping overclaimed window %s..%s in FX cache %s: "
                    "end is not a published rate date",
                    start, end, path,
                )
                continue
            if len(_split_on_rate_gaps(rates, start, end)) > 1:
                # The window spans a >MAX_RATE_GAP_DAYS hole between
                # consecutive cached rates — longer than any true
                # publication gap, so it marks missing data the window
                # must not claim (the window-start→first-rate stretch
                # does NOT count: leading pub-free days are normal).
                logger.info(
                    "Dropping overclaimed window %s..%s in FX cache %s: "
                    "interior rate gap exceeds %d days",
                    start, end, path, MAX_RATE_GAP_DAYS,
                )
                continue
            windows.append((start, end))
        return rates, windows
    except Exception as e:
        logger.warning("Ignoring corrupt FX cache %s: %s", path, e)
        return {}, []


def _merge_windows(
    windows: list[tuple[date, date]],
) -> list[tuple[date, date]]:
    """Sort windows and merge overlapping or CONTIGUOUS pairs — ``start <=
    prev_end + 1 day``, i.e. zero missing days between them. A single
    missing day must NOT merge: an unfetched day between two windows can
    never be claimed as covered, and merging would fabricate coverage of a
    day that was never fetched."""
    merged: list[list[date]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _save_cache(
    currency: str,
    rates: dict[date, Decimal],
    windows: list[tuple[date, date]],
) -> None:
    """Write ``<CCY>.json`` atomically (unique tmp file + ``os.replace``).

    Current cache format is v3 (``"version": 3``): rates as strings plus
    evidence-bounded ``windows`` that never span a >MAX_RATE_GAP_DAYS
    interior rate hole. Merged windows are re-split through
    ``_split_on_rate_gaps`` before writing: day-adjacent merging
    (``_merge_windows``) can join two windows across an unfetched hole
    exactly one day wide at the seam, and only the rate data itself
    proves no claimable day was skipped.
    """
    path = _cache_path(currency)
    payload = {
        "version": 3,
        "rates": {
            day.isoformat(): str(rate) for day, rate in sorted(rates.items())
        },
        "windows": [
            [seg_start.isoformat(), seg_end.isoformat()]
            for start, end in _merge_windows(windows)
            for seg_start, seg_end in _split_on_rate_gaps(rates, start, end)
        ],
    }
    tmp_path: Path | None = None
    try:
        FX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=FX_CACHE_DIR, prefix=path.name + ".", suffix=".tmp"
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp_path, path)
    except OSError as e:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()
        raise FxError(f"Could not write FX cache {path}: {e}") from e


def fetch_ecb_daily(currency: str, start: date, end: date) -> dict[date, Decimal]:
    """Fetch the ECB daily ``{currency}/EUR`` reference series for [start, end].

    Returns the published values (units of ``currency`` per 1 EUR) keyed by
    date. An empty dict means the series has no observations in the range.

    Raises:
        FxError: Non-200 response, retry-exhausted request/5xx/429, or
            unparseable CSV payload.
    """
    url = f"{ECB_URL}/D.{currency}.EUR.SP00.A"
    params = {
        "startPeriod": start.isoformat(),
        "endPeriod": end.isoformat(),
        "format": "csvdata",
        "detail": "dataonly",
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("ECB request failed: %s. Retrying in %ds...", e, wait)
                time.sleep(wait)
                continue
            raise FxError(
                f"ECB request failed after {MAX_RETRIES} tries: {e} — "
                "check your internet connection and re-run — already-cached "
                "rates are kept and won't be re-fetched"
            ) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < MAX_RETRIES - 1:
                if resp.status_code == 429:
                    try:
                        retry_after = int(resp.headers.get("Retry-After", 60))
                        wait = min(max(retry_after, 0), 60)
                    except (TypeError, ValueError):
                        wait = 60
                else:
                    wait = 2 ** attempt
                logger.warning(
                    "ECB returned %d for %s. Retrying in %ds...",
                    resp.status_code, url, wait,
                )
                time.sleep(wait)
                continue
            raise FxError(
                f"ECB request failed ({resp.status_code}) for {url} "
                f"after {MAX_RETRIES} tries: {resp.text[:200]}"
            )
        if resp.status_code == 404:
            raise FxError(
                f"ECB returned 404 for {url} — currency {currency} is not an "
                "ECB-quoted daily reference currency (the ECB publishes ~30 "
                "currencies; check the code)"
            )
        if resp.status_code != 200:
            raise FxError(
                f"ECB request failed ({resp.status_code}) for {url}: {resp.text[:200]}"
            )
        break

    rates: dict[date, Decimal] = {}
    try:
        for rec in csv.DictReader(io.StringIO(resp.text)):
            rates[date.fromisoformat(rec["TIME_PERIOD"])] = Decimal(rec["OBS_VALUE"])
    except (KeyError, TypeError, ValueError, ArithmeticError) as e:
        raise FxError(f"Could not parse ECB CSV response for {currency}: {e}") from e
    return rates


def _is_covered(
    d: date,
    rates: dict[date, Decimal],
    windows: list[tuple[date, date]],
) -> bool:
    """Coverage invariant for one needed date ``d``.

    ``d`` is covered iff some recorded fetch window ``[s, e]`` reaches it
    (``e >= d`` — proving every publication through ``d`` is known) AND
    some cached rate ``r`` satisfies ``s <= r <= d``. The max such ``r`` is
    then provably the true last-published rate <= ``d``: the window
    contains every publication in ``[s, e]``, and ``e`` — being the newest
    publication the ECB actually returned — can never claim a day whose
    publication did not yet exist. If no rate lies in ``[s, d]``, the true
    last-published precedes ``s`` and cannot be proven cached.
    """
    return any(
        e >= d and any(s <= r <= d for r in rates)
        for s, e in windows
    )


def ensure_rates(
    currency: str,
    needed_dates: set[date],
    *,
    today: date | None = None,
) -> dict[date, Decimal]:
    """Return the cached rate series for ``currency``, fetching once if needed.

    Coverage is tracked via FETCHED WINDOWS recorded in the cache file —
    rates alone cannot distinguish "the ECB published nothing" from "we
    never fetched". Windows are EVIDENCE-BASED: a needed date ``d`` is
    covered iff some recorded window ``[s, e]`` satisfies ``e >= d`` and
    some cached rate ``r`` satisfies ``s <= r <= d`` (see
    ``_is_covered``), where ``e`` is the newest publication the ECB
    actually returned for that fetch — a window never claims a day whose
    publication did not exist at fetch time.

    Any uncovered needed date issues ONE fetch over
    ``[min(needed) - FETCH_LOOKBACK_DAYS days, today]`` (the end is
    clamped up to the start so future needed dates cannot invert the
    range; a start before the ECB series' 1999-01-01 beginning is left
    as-is — the ECB just returns from the series start). Fetched values
    are merged into the cache (fetched wins), the range
    ``[start, max(fetched_dates)]`` is recorded as a window — split at
    every >MAX_RATE_GAP_DAYS hole between consecutive merged rates
    (missing data must not be claimed) — and the cache is saved. A needed date ``d`` that remains uncovered is
    PROVISIONAL as long as any cached rate ``r <= d`` exists — uncovered-
    with-a-prior-rate means "beyond proven evidence": a weekend, holiday,
    or today-before-/delayed-publication, but also a legitimately ENDED
    series (e.g. HRK stopped being published in 2022 — its series-final
    rate is the mandated last-published rate for every later date, so it
    stays provisional and refetches once per run forever). Both are
    exactly the sanctioned last-published fallback served by ``rate_on``.
    Only a ``d`` with NO rate ``<= d`` at all is a REAL HOLE raising
    FxError. A provisional ``d`` past the cached evidence refetches on
    every run until the publication lands (self-healing, bounded to one
    GET).

    ``today`` is the Berlin-local fetch end (defaults to
    ``datetime.now(BERLIN).date()``) — tests may pass a fixed date to
    freeze it.

    Raises:
        FxError: The ECB returned no rates at all (unknown currency or a
            range before the series start), or a needed date remains
            uncovered after the fetch with no cached rate on or before it.
    """
    if today is None:
        today = datetime.now(BERLIN).date()
    rates, windows = _load_cache(currency)
    if not needed_dates:
        return rates

    def uncovered() -> list[date]:
        return sorted(
            d for d in needed_dates
            if not _is_covered(d, rates, windows)
        )

    if uncovered():
        start = min(needed_dates) - timedelta(days=FETCH_LOOKBACK_DAYS)
        end = max(today, start)  # future needed dates must not invert the range
        fetched = fetch_ecb_daily(currency, start, end)
        if not fetched and not rates:
            raise FxError(
                f"ECB returned no rates for {currency} in "
                f"{start.isoformat()}..{end.isoformat()} — either {currency} "
                "is not an ECB-quoted currency or the requested range "
                "precedes the series start (1999-01-01)"
            )
        rates.update(fetched)
        if fetched:
            # Record coverage only through the newest publication the ECB
            # ACTUALLY returned — never the requested end. A pre-/
            # delayed-publication fetch therefore records a window ending
            # before today, and the next run refetches. The recorded
            # window also SPLITS at every >MAX_RATE_GAP_DAYS hole between
            # consecutive merged (cached+fetched) rates in
            # [start, today]: such a gap is longer than any true
            # publication gap, so it marks missing data that must not be
            # claimed — the hole stays uncovered and refetches on future
            # runs until filled (self-healing).
            windows.extend(_split_on_rate_gaps(rates, start, end))
        _save_cache(currency, rates, windows)
        # Still-uncovered dates are provisional iff a rate <= d exists —
        # those are served by rate_on's last-published fallback, and the
        # user-facing warning for them lives in tdpro._make_rate_lookup
        # (fires for d >= today with served < d only when an ECB
        # publication on d is possible — see ecb_publication_possible).
        # Only uncovered-with-NO-rate<=d is a real hole -> FxError.
        holes = [
            d for d in uncovered()
            if not any(r <= d for r in rates)
        ]
        if holes:
            days = ", ".join(d.isoformat() for d in holes)
            raise FxError(
                f"{currency}: no ECB rate covering needed date(s): {days}"
            )
    return rates


def last_published_on(rates: dict[date, Decimal], on: date) -> date:
    """Date of the last published rate <= ``on``.

    Raises:
        FxError: No rate on or before ``on`` exists in the series.
    """
    eligible = [day for day in rates if day <= on]
    if not eligible:
        raise FxError(f"No ECB rate available on or before {on}")
    return max(eligible)


def rate_on(rates: dict[date, Decimal], on: date) -> Decimal:
    """Last published rate with date <= ``on`` (weekend/holiday fallback).

    Raises:
        FxError: No rate on or before ``on`` exists in the series.
    """
    return rates[last_published_on(rates, on)]


def rate_to_base(
    rates_by_currency: dict[str, dict[date, Decimal]],
    currency: str,
    base: str,
    on: date,
) -> Decimal:
    """Conversion multiplier ``currency -> base`` on date ``on``.

    ``rate(cur->base) = ecb(base) / ecb(cur)`` where ``ecb(EUR) = 1`` and
    ``ecb(X)`` is the last published X-per-EUR rate on or before ``on``.
    Callers never invoke this with ``currency == base``.
    """
    ecb_base = Decimal(1) if base == "EUR" else rate_on(rates_by_currency[base], on)
    ecb_cur = Decimal(1) if currency == "EUR" else rate_on(rates_by_currency[currency], on)
    return ecb_base / ecb_cur


def fx_date(dt: datetime, mode: str) -> date:
    """Calendar date for the rate lookup from a naive-UTC fill datetime.

    ``"berlin"`` converts to Europe/Berlin first (the German-tax-relevant
    local date of the fill); ``"utc"`` takes the recorded date as-is.
    """
    if mode == "berlin":
        return dt.replace(tzinfo=UTC).astimezone(ZoneInfo("Europe/Berlin")).date()
    if mode == "utc":
        return dt.date()
    raise FxError(f"Unknown fx date mode: {mode!r}")
