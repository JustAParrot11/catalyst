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
    end = today - timedelta(days=_LAG_DAYS)
    start = (have[-1].day + timedelta(days=1)) if have else SIP_START
    if start > end:
        return RefreshResult(skipped_reason="already_current",
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
            by_symbol, notes = fetch_daily_bars(
                client, [BENCHMARK_SYMBOL], start, end)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except Exception as exc:   # noqa: BLE001 - the trading loop must survive
        body = getattr(getattr(exc, "response", None), "text", "") or ""
        status = getattr(getattr(exc, "response", None), "status_code", None)
        reason = (f"fetch_failed_http_{status}" if status
                  else f"fetch_failed_{type(exc).__name__}")
        return RefreshResult(skipped_reason=reason,
                             raw_response=(body or repr(exc))[:2000])

    fresh = by_symbol.get(BENCHMARK_SYMBOL) or []
    if not fresh:
        # A weekend, a holiday, or a broken query all look like this.
        # None of them is a reason to lose the history we already have.
        return RefreshResult(skipped_reason="no_new_bars_upstream",
                             raw_response=str(notes)[:2000])

    # MERGE by day: existing history wins nothing and loses nothing, and
    # a repeated day from upstream simply replaces itself.
    merged = {b.day: b for b in have}
    merged.update({b.day: b for b in fresh})
    ordered = [merged[d] for d in sorted(merged)]
    cache.write_bars(BENCHMARK_SYMBOL, ordered)
    cache.write_meta({
        "symbol": BENCHMARK_SYMBOL,
        "feed": FEED,
        "adjustment": ADJUSTMENT,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "first_day": ordered[0].day.isoformat(),
        "last_day": ordered[-1].day.isoformat(),
        "rows": len(ordered),
    })
    return RefreshResult(written=len(fresh), first_day=fresh[0].day,
                         last_day=fresh[-1].day)
