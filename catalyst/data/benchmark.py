"""Keep the SPY benchmark series current, from the running bot.

The brief requires performance to be shown against the S&P net of
costs. That comparison is only as good as the benchmark behind it, and
two things were quietly wrong (both found 2026-08-10):

- `data/` is gitignored and no bar file is tracked, so the installer's
  fresh clone reaches the VPS with NO SPY history at all;
- nothing in the running bot ever wrote to the bar cache, so even a
  populated cache froze on its fetch date while the bot's own equity
  line kept advancing.

Either way the headline comparison degrades silently, which is the
worst way for it to fail. This module fixes both: it bootstraps a
missing cache from the documented SIP floor, and thereafter appends
only the days it is missing.

Design constraints:

- SAME BASIS, ALWAYS. feed=sip and adjustment=all come from
  backtest.data, the same constants the cached history was built with.
  An adjustment=raw fetch merged into an adjustment=all series would
  understate SPY by ~67pp over the cached window and nothing would look
  broken. The basis is also written into the cache metadata so the
  dashboard can state it rather than assume it.
- MERGE, NEVER REPLACE. New bars are merged over existing days by date;
  a bad or empty response can cost at most the update, never history.
- NEVER FATAL. This runs inside the trading loop. Every failure returns
  a stated reason carrying the raw upstream body; nothing raises.
- The account may lack SIP entitlement (a paper account often does).
  That arrives here as an HTTP error and is reported as one - it is not
  silently retried on a different feed, because the dashboard labels the
  series with the basis it was fetched on and that label must be true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from catalyst.backtest.data import (
    ADJUSTMENT, FEED, SIP_START, BarCache, fetch_daily_bars,
)

BENCHMARK_SYMBOL = "SPY"

#: Alpaca's daily bar for the current session is not final until the
#: close, so the refresher asks only up to yesterday.
_LAG_DAYS = 1


@dataclass(frozen=True)
class RefreshResult:
    written: int = 0
    first_day: date | None = None
    last_day: date | None = None
    skipped_reason: str | None = None
    raw_response: str | None = None
    #: Which feed actually produced these bars. Never assumed: a series
    #: labelled sip that is really iex is a lie about its own basis.
    feed: str | None = None
    #: NOTHING IS WRONG, as opposed to something is. CLAUDE.md: routine
    #: attrition must not look like damage. Decided HERE, beside the
    #: reasons themselves, so a reason added later cannot be silently
    #: misclassified by a list of strings kept in another module
    #: (house rule 7).
    routine: bool = False


#: A window this short cannot distinguish a market holiday from one
#: flaky fetch, and both are routine: every US market holiday closes a
#: SINGLE weekday, and a one-off failure is retried fifteen minutes
#: later. Two consecutive weekdays with no bar is not a holiday.
_ROUTINE_MISSING_WEEKDAYS = 1


def _weekdays_between(start: date, end: date) -> int:
    """Weekdays in [start, end]. Weekends are the only non-trading days
    that can be known without a calendar; holidays are handled by the
    tolerance above rather than by enumerating them."""
    days, cur = 0, start
    while cur <= end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


#: Preference order. SIP is the consolidated tape and the right answer
#: when the account is entitled to it. IEX is one exchange's prints -
#: for a DAILY close on an instrument as liquid as SPY that is a good
#: enough benchmark, and infinitely better than no comparison at all,
#: which is what an unentitled account got before (owner-reported
#: 2026-08-11: "reachable, but returned no SPY bar - usually means this
#: account has no SIP data entitlement").
FEED_PREFERENCE = ("sip", "iex")


def _cached_feed(cache) -> str | None:
    try:
        return (cache.read_meta() or {}).get("feed")
    except Exception:  # noqa: BLE001
        return None


class AllFeedsRefused(RuntimeError):
    """Every feed turned the credentials away. Distinct from an outage:
    it carries each feed's own status so the reason the caller reports
    still names it, rather than flattening to the exception type."""

    def __init__(self, message, statuses=(), bodies=()):
        super().__init__(message)
        self.statuses = list(statuses)
        #: Each feed's verbatim refusal. House rule 3: the raw upstream
        #: response goes beside the failure, and an exception that eats
        #: it makes the one diagnostic sentence unavailable.
        self.bodies = list(bodies)


def _fetch_with_fallback(client, start, end, have_feed):
    """Bars, and the feed that produced them.

    NEVER MIXES FEEDS. A series is one basis or it is not a series: half
    consolidated tape and half one exchange's prints would make every
    comparison against it quietly wrong, and the dashboard would go on
    labelling the whole thing with whichever feed was written last.

    So an existing cache pins the feed. Only a bootstrap - an empty
    cache - is free to choose, and it takes the best one that answers.

    THE TRAP THAT PIN CREATES, owner-reported 2026-08-20: "the SPY
    comparison line has disappeared so i cant visually see if we're
    beating SPY" - days after replacing the Alpaca keys. A cache built
    on `sip` keeps asking for `sip`; a new key without that
    subscription is refused every time, forever, and the comparison
    dies with no way back. Correct not to mix bases, but there has to
    be a door: rebuild_benchmark() opens it deliberately, discarding
    the series rather than splicing a second feed onto it.
    """
    if have_feed:
        by_symbol, notes = fetch_daily_bars(
            client, [BENCHMARK_SYMBOL], start, end, feed=have_feed)
        return by_symbol, notes, have_feed

    # A BOOTSTRAP TRIES EACH FEED UNTIL ONE ANSWERS - including the ones
    # that REFUSE rather than merely return nothing. It did not: a feed
    # the key has no subscription for raises, and an uncaught raise here
    # abandoned the whole loop, so the second preference was never
    # reached. That made "take the best one that answers" untrue for the
    # exact case it exists to handle, and it is why rebuilding onto a
    # reachable feed did not work either.
    #
    # Found by the rebuild test, not by reading: every existing test had
    # a client that answered on every feed.
    last_notes, refusals, bodies = [], [], []
    for feed in FEED_PREFERENCE:
        try:
            by_symbol, notes = fetch_daily_bars(
                client, [BENCHMARK_SYMBOL], start, end, feed=feed)
        except Exception as exc:   # noqa: BLE001 - try the next feed
            status = getattr(getattr(exc, "response", None), "status_code",
                             None)
            refusals.append(f"{feed}: {status or type(exc).__name__}")
            body = getattr(getattr(exc, "response", None), "text", "") or ""
            bodies.append(f"{feed}: {body or repr(exc)}")
            continue
        if by_symbol.get(BENCHMARK_SYMBOL):
            return by_symbol, notes, feed
        last_notes = notes
    if refusals and not last_notes:
        # Every feed refused. Raise rather than return empty, so the
        # caller reports a credentials problem instead of "no new bars",
        # which reads as a quiet weekend.
        raise AllFeedsRefused(
            "no market data feed accepted these credentials: "
            + "; ".join(refusals), statuses=refusals, bodies=bodies)
    return {}, last_notes, FEED_PREFERENCE[-1]


def _existing(cache: BarCache):
    try:
        return list(cache.load_bars(BENCHMARK_SYMBOL))
    except Exception:
        return []          # missing file / unreadable = bootstrap case


def refresh_benchmark(
    bars_root: str,
    alpaca_key: str,
    alpaca_secret: str,
    *,
    today: date | None = None,
    client_factory=None,
) -> RefreshResult:
    """Bring the local SPY cache up to yesterday. Never raises."""
    today = today or datetime.now(timezone.utc).date()
    if not alpaca_key or not alpaca_secret:
        return RefreshResult(skipped_reason="no_alpaca_credentials")

    cache = BarCache(bars_root)
    have = _existing(cache)
    have_feed = _cached_feed(cache)
    end = today - timedelta(days=_LAG_DAYS)
    start = (have[-1].day + timedelta(days=1)) if have else SIP_START
    if start > end:
        return RefreshResult(skipped_reason="already_current", routine=True,
                             last_day=have[-1].day if have else None)

    headers = {"APCA-API-KEY-ID": alpaca_key,
               "APCA-API-SECRET-KEY": alpaca_secret}
    try:
        if client_factory is not None:
            client = client_factory(headers)
        else:
            import httpx
            client = httpx.Client(headers=headers, timeout=30.0)
        try:
            by_symbol, notes, used_feed = _fetch_with_fallback(
                client, start, end, have_feed)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except Exception as exc:   # noqa: BLE001 - the trading loop must survive
        body = getattr(getattr(exc, "response", None), "text", "") or ""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reason = (f"fetch_failed_http_{status}" if status
                  else f"fetch_failed_{type(exc).__name__}")
        if isinstance(exc, AllFeedsRefused):
            # Keep the status visible: "fetch_failed_RuntimeError" tells
            # the owner nothing, and 403 tells them it is the key.
            codes = [p.rsplit(": ", 1)[-1] for p in exc.statuses]
            reason = "feeds_refused_http_" + "_".join(dict.fromkeys(codes))
            body = "\n".join(exc.bodies) or body
        # A PINNED FEED THAT IS NO LONGER PERMITTED is a different fault
        # from a flaky upstream, and it needs a different answer: it
        # will never fix itself, because every retry asks for the same
        # refused feed. Name it so the dashboard can offer the rebuild
        # instead of telling the owner to wait.
        if have_feed and status in (401, 403):
            reason = f"feed_no_longer_available_{have_feed}"
        return RefreshResult(skipped_reason=reason,
                             raw_response=(body or repr(exc))[:2000])

    fresh = by_symbol.get(BENCHMARK_SYMBOL) or []
    if not fresh:
        # A weekend, a holiday, or a broken query all look like this.
        # None of them is a reason to lose the history we already have -
        # but they are not the same event, and the owner has now twice
        # read the first as the third. The window itself separates them:
        # if it holds no trading weekday there was nothing to fetch, and
        # a single weekday is a market holiday or one flaky fetch that
        # the next cycle retries. Two is a feed that has stopped
        # answering.
        missing = _weekdays_between(start, end)
        return RefreshResult(skipped_reason="no_new_bars_upstream",
                             routine=missing <= _ROUTINE_MISSING_WEEKDAYS,
                             raw_response=str(notes)[:2000])

    # MERGE by day: existing history wins nothing and loses nothing, and
    # a repeated day from upstream simply replaces itself.
    merged = {b.day: b for b in have}
    merged.update({b.day: b for b in fresh})
    ordered = [merged[d] for d in sorted(merged)]
    cache.write_bars(BENCHMARK_SYMBOL, ordered)
    cache.write_meta({
        "symbol": BENCHMARK_SYMBOL,
        "feed": used_feed,
        "adjustment": ADJUSTMENT,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "first_day": ordered[0].day.isoformat(),
        "last_day": ordered[-1].day.isoformat(),
        "rows": len(ordered),
    })
    return RefreshResult(written=len(fresh), first_day=fresh[0].day,
                         last_day=fresh[-1].day, feed=used_feed,
                         routine=True)


def rebuild_benchmark(bars_root: str, alpaca_key: str, alpaca_secret: str,
                      *, today=None, client_factory=None) -> RefreshResult:
    """Throw the SPY series away and fetch it again on whatever feed the
    current credentials can actually reach.

    DELIBERATE AND DESTRUCTIVE, which is why it is a separate function
    nothing calls on a schedule. refresh_benchmark() pins the feed on
    purpose - a series that is half consolidated tape and half one
    exchange's prints makes every comparison against it quietly wrong -
    so the ONLY safe way onto a different feed is to stop having the old
    series at all.

    The owner reaches this when their key loses the subscription the
    cache was built on. The alternative on offer is a comparison that
    never comes back, so the trade is worth making, and the page says
    which feed the rebuilt series is on rather than leaving it implied.
    """
    cache = BarCache(bars_root)
    try:
        cache.write_bars(BENCHMARK_SYMBOL, [])
        cache.write_meta({})
    except Exception as exc:   # noqa: BLE001 - reported, never raised
        return RefreshResult(skipped_reason=f"could_not_clear_{type(exc).__name__}",
                             raw_response=repr(exc)[:2000])
    return refresh_benchmark(bars_root, alpaca_key, alpaca_secret,
                             today=today, client_factory=client_factory)
