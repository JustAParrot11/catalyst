"""Live quotes for the dashboard, and the honesty that has to come with.

OWNER-ASKED 2026-08-21: "yes add live quotes".

Until now this dashboard read a database and a bar cache and nothing
else, which is why it could only mark a position to its last cached
daily close. This module is the one place that talks to the broker for
DISPLAY, and it is built to three rules.

1. IT CAN NEVER TAKE THE PAGE DOWN. A quote is a nicety; the page is
   the instrument. Every failure - no credentials, a timeout, a 500, a
   malformed body - returns a Quote carrying the reason, and the caller
   falls back to the cached close and says so. Nothing here raises.

2. IT APPLIES THE SAME SANITY RULES AS THE TRADING PATH. A quote is
   refused if it is stale, non-positive or crossed, exactly as
   build_market_snapshot refuses one. A dashboard willing to display a
   number the risk engine would reject is a dashboard that quietly
   disagrees with the bot about what a price is.

3. IT NEVER DECIDES ANYTHING. This module is imported by the dashboard
   alone. No sizing, no order, no threshold has ever seen it, and a
   test asserts no trading module imports it - because the moment a
   display path can influence a decision, "it is only the UI" stops
   being true.

A SHORT CACHE, because a dashboard gets refreshed. Without it, holding
the page open re-quotes every ticker on every keypress of F5, which is
rude to the broker and slow for the reader.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

#: The freshness gate the trading path uses (orchestrator MAX_QUOTE_AGE).
#: A quote older than this is refused rather than shown: off-hours the
#: "latest" endpoint happily returns the last quote of the session, and
#: presenting that as current is the whole failure this guards against.
MAX_QUOTE_AGE = timedelta(minutes=10)

#: How long a fetched quote is reused before going back to the broker.
#: A dashboard is refreshed; a fresh call per refresh is rude to the
#: broker and slow to read.
CACHE_TTL_S = 10.0

#: Per-symbol wall-clock budget. Five positions must not turn a page
#: load into half a minute because one symbol is slow.
TIMEOUT_S = 4.0

_lock = threading.Lock()
_cache: dict = {}          # ticker -> (monotonic_at, Quote)


@dataclass
class Quote:
    ticker: str = ""
    mid: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    at: datetime | None = None
    age_s: float | None = None
    spread_bp: Decimal | None = None
    #: Why there is no usable price. Always set when mid is None, never
    #: set when it is - "no data" and "the call is broken" must not look
    #: the same (house rule 3).
    error: str = ""

    @property
    def live(self) -> bool:
        return self.mid is not None


def _dec(value):
    try:
        d = Decimal(str(value))
    except Exception:          # noqa: BLE001 - any bad value is just absent
        return None
    return d if d.is_finite() else None


def quote_from_payload(ticker: str, payload, now=None) -> Quote:
    """Alpaca's latest-quote body, validated exactly as the trading path
    validates it. Pure - no network - so the rules are testable."""
    now = now or datetime.now(timezone.utc)
    q = Quote(ticker=ticker)
    if not isinstance(payload, dict):
        q.error = "the broker's answer was not an object"
        return q
    quote = payload.get("quote")
    if not isinstance(quote, dict):
        q.error = "the answer carried no quote"
        return q
    ts = quote.get("t")
    if not ts:
        q.error = "the quote carried no timestamp, so it cannot be aged"
        return q
    try:
        at = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        q.error = f"unreadable quote timestamp {ts!r}"
        return q
    q.at = at
    q.age_s = (now - at).total_seconds()
    if now - at > MAX_QUOTE_AGE:
        # Off hours the endpoint returns the session's last quote. It is
        # not wrong, it is just not NOW, and marking a book to it would
        # be marking to yesterday while calling it live.
        q.error = (f"the newest quote is {q.age_s / 60:.0f} minutes old, "
                   "so it is the last one of the session rather than a "
                   "live price")
        return q
    bid, ask = _dec(quote.get("bp")), _dec(quote.get("ask") or quote.get("ap"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        q.error = "the quote had no positive bid and ask"
        return q
    if bid > ask:
        q.error = f"the quote is crossed (bid {bid} above ask {ask})"
        return q
    q.bid, q.ask = bid, ask
    q.mid = (bid + ask) / 2
    q.spread_bp = ((ask - bid) / 2 / q.mid * Decimal("10000")).quantize(
        Decimal("0.1"))
    return q


def _broker():
    """A read-only broker handle, or (None, reason). Never raises."""
    try:
        from catalyst.execution.broker import Broker
        from catalyst.setup.credentials import load_credentials

        creds = load_credentials()
    except Exception as exc:      # noqa: BLE001 - reported, never raised
        return None, f"credentials could not be read: {type(exc).__name__}"
    key = getattr(creds, "alpaca_key", "")
    secret = getattr(creds, "alpaca_secret", "")
    if not key or not secret:
        return None, "no Alpaca credentials are saved"
    try:
        paper = getattr(creds, "account_mode", "paper") != "live"
        return Broker(key, secret, paper=paper, timeout=TIMEOUT_S), ""
    except Exception as exc:      # noqa: BLE001
        return None, f"broker could not be opened: {type(exc).__name__}"


def quotes_for(tickers, broker=None, now=None, use_cache=True) -> dict:
    """{ticker: Quote} for every ticker asked for. Never raises, never
    omits a ticker: one that could not be quoted comes back with its
    reason, so the caller can say WHY rather than showing a blank."""
    now = now or datetime.now(timezone.utc)
    wanted = [str(t).upper() for t in tickers if t]
    out: dict = {}
    if not wanted:
        return out

    fresh = []
    if use_cache:
        with _lock:
            for t in wanted:
                hit = _cache.get(t)
                if hit and (time.monotonic() - hit[0]) < CACHE_TTL_S:
                    out[t] = hit[1]
                else:
                    fresh.append(t)
    else:
        fresh = list(wanted)
    if not fresh:
        return out

    reason = ""
    if broker is None:
        broker, reason = _broker()
    if broker is None:
        for t in fresh:
            out[t] = Quote(ticker=t, error=reason or "no broker available")
        return out

    for t in fresh:
        try:
            q = quote_from_payload(t, broker.get_latest_quote(t), now=now)
        except Exception as exc:  # noqa: BLE001 - one symbol, not the page
            q = Quote(ticker=t,
                      error=f"{type(exc).__name__}: {str(exc)[:160]}")
        out[t] = q
        if use_cache and q.live:
            with _lock:
                _cache[t] = (time.monotonic(), q)
    return out


def clear_cache() -> None:
    """For tests, and for a caller that has just changed credentials."""
    with _lock:
        _cache.clear()
