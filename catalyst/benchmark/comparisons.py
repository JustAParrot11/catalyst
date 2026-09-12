"""The stocks drawn BESIDE the bot, each with its own money and date.

OWNER-ASKED 2026-09-12: *"on the tab where i can set where to track SPY
from, can you edit it a bit so i can track up to 10 stocks at once, I
type the stock name exactly and set the date and amount, set SPY as
default, but then show as different colours on the graph so I can track
how we are beating multiple stocks."*

WHAT THIS IS NOT. It is not the account baseline. `benchmark.current()`
still answers "what money, from when, is the bot itself judged against",
it is still append-only, and nothing here writes to it. These rows are
extra LINES on a chart - a reporting preference - so they are edited in
place, and losing one costs a colour on a graph rather than a month of
tracking.

SPY IS THE DEFAULT, AND IT IS SYNTHESISED RATHER THAN SEEDED. An empty
list means the page draws SPY from the account baseline, exactly as it
did before this module existed, so an owner who never touches the form
sees no change and an upgrade needs no migration. The moment the owner
saves anything, the list is what it says.

THE COLOUR SLOT IS STORED, NOT DERIVED FROM LIST ORDER. Deriving it
would re-colour every existing line each time a stock is added or
removed, and the whole point is to watch the same stock over weeks.

**Colour cannot carry identity here and it is not asked to.** Measured
against this dashboard's own two surfaces, with CIE76 dE under normal
vision plus simulated deuteranopia, protanopia and tritanopia:

    series drawn   worst-pair CVD dE, light / dark
        2              96.3 / 95.7
        3              42.8 / 42.6
        4              18.6 / 26.5
        5              14.9 / 18.4     <- the last row that is reliable
        6              14.0 / 11.1
        8              11.7 /  7.1
       11               5.8 /  4.0

So beyond five series (four comparisons) colour is a grouping cue only,
and identity is carried by a per-series dash pattern and by the ticker
printed at the right-hand end of its own line. That is this project's
standing rule - a status is never carried by colour alone - applied to
the case where the arithmetic says it cannot be.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

#: The owner asked for ten. It is a display bound, not a money bound, so
#: it lives here rather than in hard_bounds.
MAX_COMPARISONS = 10

#: What the list holds when the owner has never edited it.
DEFAULT_TICKER = "SPY"

#: Slot 0 belongs to the bot's own line, permanently, so a comparison
#: never takes the colour the reader has learned means "us".
BOT_SLOT = 0
FIRST_COMPARISON_SLOT = 1

#: Colour slots available to comparisons. One more than MAX_COMPARISONS
#: because slot 0 is the bot's.
N_SLOTS = MAX_COMPARISONS + 1

#: The account-baseline form's own bounds, IMPORTED rather than repeated,
#: so the two forms cannot disagree about what an amount is - and so this
#: module defines no new `*_CENTS` constant. `test_no_cost_is_hard_coded`
#: flags a cost-shaped name outside the modules that account for one, and
#: it was right to: a second pair of bounds here would have been two
#: numbers meaning the same thing, free to drift (the pattern §6 of
#: WHAT-WE-TRIED records twice). Imported lazily because `panels` imports
#: this module.
def _amount_bounds():
    from catalyst.dashboard.panels import (
        MAX_BASELINE_CENTS, MIN_BASELINE_CENTS,
    )

    return Decimal(str(MIN_BASELINE_CENTS)), Decimal(str(MAX_BASELINE_CENTS))

#: SPY's own inception. A comparison cannot start before the instrument
#: existed, and no bar cache could answer it.
EARLIEST_START = "1993-01-29"

#: A ticker is 1-5 letters, optionally with a class suffix (BRK.B) or a
#: warrant/unit marker (RDW.WS). Checked as a SHAPE rather than against a
#: list of known symbols: a list would reject the first new listing
#: nobody thought of, and the real test of whether a symbol exists is
#: whether bars can be fetched for it - which the page reports.
_MAX_TICKER_LEN = 10


@dataclass(frozen=True)
class Comparison:
    """One line on the chart, and where it came from."""

    ticker: str
    start_date: date
    capital_cents: Decimal
    slot: int
    set_at: str = ""
    reason: str = ""
    #: True when this row is not stored at all - it is SPY, synthesised
    #: from the account baseline because the owner has never edited the
    #: list. The page says so, because "the default" and "a choice I made"
    #: are different facts.
    is_default: bool = False


class Invalid(ValueError):
    """A refusal the owner reads as a sentence. Never a traceback."""


def clean_ticker(raw) -> str:
    """The ticker as it will be stored, or raise `Invalid`.

    The owner said "I type the stock name exactly", so this corrects case
    and whitespace and nothing else - it never guesses at a symbol.
    """
    text = " ".join(str(raw or "").split()).upper()
    if not text:
        raise Invalid(
            "Give the stock's ticker symbol - for example AAPL, or SPY for "
            "the S&P 500 fund. Nothing was changed.")
    if len(text) > _MAX_TICKER_LEN:
        raise Invalid(
            f"{text!r} is longer than any US ticker ({_MAX_TICKER_LEN} "
            "characters). Enter the symbol, not the company name - AAPL "
            "rather than Apple. Nothing was changed.")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ.-")
    if not set(text) <= allowed or not text[0].isalpha():
        raise Invalid(
            f"{text!r} is not a ticker symbol. Tickers are letters, "
            "sometimes with a dot or a dash (BRK.B). Enter the symbol, not "
            "the company name. Nothing was changed.")
    return text


def clean_amount(raw) -> Decimal:
    """Dollars as typed -> cents, or raise `Invalid`.

    The same refusals the account-baseline form gives, for the same
    reasons: `Decimal` accepts "NaN" and "Infinity" happily and both
    would sail through a `> 0` test and poison every percentage on the
    page.
    """
    text = str(raw or "").strip()
    if not text:
        raise Invalid(
            "Give the amount to put into this stock, in dollars - for "
            "example 2000. Nothing was changed.")
    cleaned = text.replace("$", "").replace(",", "").replace(" ", "")
    try:
        dollars = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        raise Invalid(
            f"{text!r} is not an amount of money. Enter digits, for example "
            "2000 or 2000.50. Nothing was changed.") from None
    if not dollars.is_finite():
        raise Invalid(f"{text!r} is not a finite amount of money. Nothing "
                      "was changed.")
    cents = (dollars * 100).quantize(Decimal("1"))
    low, high = _amount_bounds()
    if cents < low:
        raise Invalid(
            f"${dollars} is below the $1 minimum. A comparison bought with "
            "nothing has no answer, and it would make every percentage on "
            "the page a division by nothing. Nothing was changed.")
    if cents > high:
        raise Invalid(
            f"${dollars:,} is above the $10,000,000 maximum this form "
            "accepts - a guard against a mistyped figure, not a view about "
            "your account. Nothing was changed.")
    return cents


def clean_start(raw, *, today: date | None = None) -> date:
    """A start date, or raise `Invalid`."""
    text = str(raw or "").strip()
    if not text:
        raise Invalid("Give the date the money went in, as YYYY-MM-DD. "
                      "Nothing was changed.")
    try:
        start = date.fromisoformat(text)
    except ValueError:
        raise Invalid(
            f"{text!r} is not a date this form can read. Use YYYY-MM-DD, "
            "for example 2026-07-01. Nothing was changed.") from None
    today = today or datetime.now(timezone.utc).date()
    if start > today:
        raise Invalid(
            f"{start} is in the future. A comparison has to start on a day "
            f"the market has already traded, so the latest this accepts is "
            f"{today}. Nothing was changed.")
    if start < date.fromisoformat(EARLIEST_START):
        raise Invalid(
            f"{start} is before {EARLIEST_START}, when the first S&P 500 "
            "fund began trading. No bar cache can answer a date before "
            "that. Nothing was changed.")
    return start


def _row(r) -> Comparison:
    return Comparison(
        ticker=str(r[0]), start_date=date.fromisoformat(str(r[1])),
        capital_cents=Decimal(str(r[2])), slot=int(r[3]),
        set_at=str(r[4] or ""), reason=str(r[5] or ""))


def stored(conn: sqlite3.Connection) -> list[Comparison]:
    """The rows as stored, oldest slot first. Never raises.

    A row this code cannot parse is SKIPPED rather than allowed to take
    the page down - but it is skipped one row at a time, so one bad row
    never hides the other nine. `unreadable()` counts them so the page
    can say so instead of quietly drawing fewer lines.
    """
    try:
        rows = conn.execute(
            "SELECT ticker, start_date, capital_cents, slot, set_at, reason "
            "FROM benchmark_comparisons ORDER BY slot").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        try:
            out.append(_row(r))
        except (ValueError, ArithmeticError, TypeError, IndexError):
            continue
    return out


def unreadable(conn: sqlite3.Connection) -> int:
    """How many stored rows could not be parsed. House rule 3: a line
    that silently does not appear is the zero with no explanation."""
    try:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM benchmark_comparisons").fetchone()[0])
    except (sqlite3.Error, TypeError, IndexError):
        return 0
    return max(0, total - len(stored(conn)))


def tracked(conn: sqlite3.Connection, baseline) -> list[Comparison]:
    """What the chart should draw beside the bot.

    THE DEFAULT IS SYNTHESISED, NOT SEEDED. With no stored rows this
    returns SPY bought with the account baseline's own money on the
    account baseline's own date - byte for byte the comparison this
    dashboard drew before the list existed. So an owner who never opens
    the form sees no change, and no migration writes anything.

    It also means the list can never be empty: an empty list would leave
    the page with no comparison at all, which is the one thing the panel
    exists to provide.
    """
    rows = stored(conn)
    if rows:
        return rows
    return [Comparison(
        ticker=DEFAULT_TICKER,
        start_date=getattr(baseline, "start_date", None)
        or datetime.now(timezone.utc).date(),
        capital_cents=Decimal(str(getattr(baseline, "capital_cents", 0) or 0)),
        slot=FIRST_COMPARISON_SLOT, is_default=True,
        reason="the default comparison: SPY, bought with the account "
               "baseline's own money on its own start date. Nothing has been "
               "saved on this form, so this is what the page has always "
               "drawn.")]


def free_slot(conn: sqlite3.Connection) -> int:
    """The lowest colour slot nothing is using.

    Lowest free rather than "one past the highest": removing the third of
    three stocks and adding a fourth should reuse the vacated colour
    rather than walk off the end of the palette.
    """
    taken = {c.slot for c in stored(conn)}
    for slot in range(FIRST_COMPARISON_SLOT, N_SLOTS):
        if slot not in taken:
            return slot
    raise Invalid(
        f"All {MAX_COMPARISONS} comparison slots are in use. Remove one "
        "before adding another - the limit is the palette: past about five "
        "lines a reader cannot tell two colours apart reliably, which is "
        "measured rather than assumed. Nothing was changed.")


def add(conn: sqlite3.Connection, *, ticker, amount, start,
        reason: str = "", today: date | None = None) -> Comparison:
    """Validate and store one comparison. Raises `Invalid` with a
    sentence, or `sqlite3.Error` if the write itself fails.

    Replacing a ticker already tracked KEEPS ITS SLOT, so correcting a
    typo in the amount does not change the colour of a line the owner has
    been watching for a fortnight.
    """
    symbol = clean_ticker(ticker)
    cents = clean_amount(amount)
    day = clean_start(start, today=today)
    existing = {c.ticker: c for c in stored(conn)}
    if symbol in existing:
        slot = existing[symbol].slot
        what = "updated"
    else:
        if len(existing) >= MAX_COMPARISONS:
            raise Invalid(
                f"{MAX_COMPARISONS} stocks are already tracked, which is the "
                "most this chart can tell apart. Remove one before adding "
                f"{symbol}. Nothing was changed.")
        slot = free_slot(conn)
        what = "added"
    note = " ".join(str(reason or "").split())[:500]
    why = (f"{what} by hand on the Maintenance page: track {symbol} as if "
           f"${cents / 100:,.2f} had been bought on {day}.")
    if note:
        why += f" Owner's note: {note}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO benchmark_comparisons (ticker, start_date, "
        "capital_cents, slot, set_at, reason) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(ticker) DO UPDATE SET start_date=excluded.start_date, "
        "capital_cents=excluded.capital_cents, set_at=excluded.set_at, "
        "reason=excluded.reason",
        (symbol, day.isoformat(), str(cents), slot, now, why))
    conn.commit()
    return Comparison(ticker=symbol, start_date=day, capital_cents=cents,
                      slot=slot, set_at=now, reason=why)


def remove(conn: sqlite3.Connection, ticker) -> str:
    """Stop tracking one stock. Returns the ticker actually removed.

    REMOVING THE LAST ONE IS ALLOWED, and the list then falls back to the
    synthesised SPY default rather than to nothing - the page's job is to
    compare, and a chart with one line cannot.
    """
    symbol = clean_ticker(ticker)
    cur = conn.execute("DELETE FROM benchmark_comparisons WHERE ticker = ?",
                       (symbol,))
    conn.commit()
    if not cur.rowcount:
        raise Invalid(f"{symbol} is not in the tracked list, so there was "
                      "nothing to remove. Nothing was changed.")
    return symbol


def seed_from_baseline(conn: sqlite3.Connection, baseline) -> bool:
    """Write the synthesised SPY default as a real row.

    Called the first time the owner ADDS a second stock, so the list they
    then see contains the SPY line they were already looking at. Without
    this, adding AAPL to a defaulted list would make AAPL the only row
    and SPY would silently vanish from the chart the owner was reading.

    Returns True if it wrote. Idempotent: it does nothing once any row
    exists.
    """
    if stored(conn):
        return False
    default = tracked(conn, baseline)[0]
    if default.capital_cents <= 0:
        # No usable baseline money yet - a fresh install before the first
        # broker read. Writing $0 would put a division by nothing in the
        # table; the synthesised default keeps working meanwhile.
        return False
    conn.execute(
        "INSERT OR IGNORE INTO benchmark_comparisons (ticker, start_date, "
        "capital_cents, slot, set_at, reason) VALUES (?,?,?,?,?,?)",
        (default.ticker, default.start_date.isoformat(),
         str(default.capital_cents), FIRST_COMPARISON_SLOT,
         datetime.now(timezone.utc).isoformat(),
         "kept from the account baseline when the tracked list was first "
         "edited, so the SPY line already on the chart did not disappear "
         "when another stock was added."))
    conn.commit()
    return True
