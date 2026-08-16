"""Make sure a candidate's own price history is on disk before it is sized.

THE DEFECT THIS FIXES WAS AN EMPTY CUPBOARD, not wrong code.

`risk/stock_gap.py` sizes a position against the stock's OWN measured
worst overnight gap and its own daily volatility, falling back to the
catalyst category's number when there is no history. That fallback is
the safe direction, and it was firing for essentially every candidate,
because `data/` is gitignored: on the owner's machine the only thing
that ever writes a bar cache is the daily SPY benchmark refresh.
Measured - 125 symbols in the live cache against 4,861 in development.

So the mechanism was correct, tested, and inert. Sizing read a cupboard
nobody had stocked.

WHAT DIFFERENCE IT MAKES, measured over 400 random tickers from the
development cache:

    fda_decision   per-stock decides  89%   category binds  10%
    earnings       per-stock decides  37%   category binds  63%
    merger         per-stock decides   0%   category binds 100%

The wide categories - where an invented number was doing the most
damage - are where the stock's own record takes over almost entirely.
The tight ones stay bound by their category, which is the ceiling doing
its job.

WHY THE NETWORK LIVES HERE AND NOT IN SIZING. `stock_gap` reads CSVs and
nothing else, so it stays pure, offline and instant. This module is the
only thing that can reach Alpaca, it is called once per candidate before
the risk engine runs, and it never raises: a fetch that fails leaves no
file, sizing finds no history, and the category value stands. The bot
sizes more conservatively than it might have. Nothing breaks.

COST. Bars are already inside the Alpaca subscription and cost no API
credit. At most MAX_RESEARCH_PER_CYCLE candidates a cycle need one
request each, against a documented ~200/minute ceiling, and a cached
symbol is re-fetched only when it goes stale.
"""

import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger("catalyst.bar_history")

#: How much history to ask for. `stock_gap` needs 250 sessions before it
#: will say anything at all, and a worst-case gap wants more than one
#: market regime in it - three years spans 2022's drawdown and the
#: recovery either side of it.
HISTORY_YEARS = 3

#: Refetch a cached symbol after this long. Daily bars go stale slowly
#: and the figures drawn from them (a worst gap over years, a 95th
#: percentile daily move) move even more slowly, so this is deliberately
#: not aggressive - it is a cache refresh, not a quote.
MAX_CACHE_AGE_DAYS = 30

#: Below this there is no point writing the file: stock_gap refuses to
#: draw a tail from less, so a short file is a slower way of reaching
#: the same fallback.
MIN_USEFUL_SESSIONS = 250

_FIELDS = ("date", "open", "high", "low", "close", "volume")


def cache_path(cache_dir, ticker: str) -> Path:
    return Path(cache_dir) / f"{str(ticker).upper()}.csv"


def is_fresh(cache_dir, ticker: str, *, now=None,
             max_age_days: int = MAX_CACHE_AGE_DAYS) -> bool:
    """True when a usable, recent-enough file already exists."""
    path = cache_path(cache_dir, ticker)
    try:
        stat = path.stat()
    except OSError:
        return False
    now = now or datetime.now(timezone.utc)
    age = now - datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    if age > timedelta(days=max_age_days):
        return False
    try:
        with path.open(newline="") as f:
            return sum(1 for _ in f) - 1 >= MIN_USEFUL_SESSIONS
    except OSError:
        return False


def _rows_from(bars: list) -> list[dict]:
    """Alpaca bar objects -> the CSV shape stock_gap reads.

    Anything unparseable is DROPPED rather than written as a zero. A
    zero open or close would read as a -100% gap, which is the largest
    possible measured worst case, which would shrink every position in
    that name to nothing - a silent refusal caused by bad data rather
    than by risk.
    """
    rows = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        try:
            day = str(bar["t"])[:10]
            o, h, low, c = (float(bar["o"]), float(bar["h"]),
                            float(bar["l"]), float(bar["c"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not all(v > 0 for v in (o, h, low, c)):
            continue
        if not all(v == v and abs(v) != float("inf") for v in (o, h, low, c)):
            continue
        rows.append({"date": day, "open": f"{o:.6f}", "high": f"{h:.6f}",
                     "low": f"{low:.6f}", "close": f"{c:.6f}",
                     "volume": str(bar.get("v") or 0)})
    rows.sort(key=lambda r: r["date"])
    return rows


def ensure_history(broker, cache_dir, ticker: str, *, now=None,
                   years: int = HISTORY_YEARS) -> bool:
    """Have this ticker's daily history on disk if it can be had.

    Returns True when a usable file is present afterwards. NEVER raises:
    every failure path leaves the cache as it was, sizing finds no
    history, and the catalyst category's value stands - which is the
    conservative direction and exactly what happened before this
    existed.
    """
    ticker = str(ticker or "").strip().upper()
    # Alphabetic AND a plausible symbol length. `isalpha()` alone
    # happily accepts a 200-character string, which would then be
    # interpolated into a URL path.
    if not ticker.isalpha() or not 1 <= len(ticker) <= 5:
        return False
    if is_fresh(cache_dir, ticker, now=now):
        return True

    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(days=int(365.25 * years))).date().isoformat()
    # Yesterday, not today: a partial session's bar is not a session.
    end = (now - timedelta(days=1)).date().isoformat()

    try:
        bars = broker.get_daily_bars(ticker, start, end)
    except Exception:  # noqa: BLE001 - sizing must never die on a feed
        _log.debug("No price history could be fetched for %s; its catalyst "
                   "category's assumption will be used instead.", ticker,
                   exc_info=True)
        return False

    rows = _rows_from(bars or [])
    if len(rows) < MIN_USEFUL_SESSIONS:
        _log.debug(
            "%s returned %d usable session(s), under the %d needed to "
            "measure a tail; the category assumption stands.",
            ticker, len(rows), MIN_USEFUL_SESSIONS)
        return False

    path = cache_path(cache_dir, ticker)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written to a temporary file and moved into place, so a crash
        # or a full disk mid-write cannot leave a half-file that reads
        # as a short history - which would understate the worst gap and
        # OVERSIZE the position.
        tmp = path.with_suffix(".csv.part")
        with tmp.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(path)
    except OSError:
        _log.debug("Could not write the price history for %s.", ticker,
                   exc_info=True)
        return False

    _log.info(
        "Fetched %d sessions of daily history for %s, so its position can be "
        "sized against what this stock has actually done rather than against "
        "its catalyst category's assumption.", len(rows), ticker)
    return True
