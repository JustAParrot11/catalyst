"""Live XBRL companyfacts, so the graded earnings-drift arm can run.

WHY THIS EXISTS. `catalyst/strategies/earnings_drift.py` (bake-off
Candidate A) is fully written, pre-registered and graded - and had no
live feed, so in production it produced nothing. The bot ran one arm.

On the bake-off it was the best-behaved arm out of sample:

    arm                       n     hit    mean/trade   maxDD   worst
    A  XBRL earnings drift    84   57.1%     +1.59%      8.8%  -18.5%
    C  insider clusters      203   49.3%     +0.87%     41.2%  -57.4%   <- live

A's hit rate is identical in and out of sample (57.1% both), which is
the one thing in that table that did not decay across the split. C's
fell from 53.1% to 49.3% - under a coin flip - and carries five times
the drawdown.

READ THE CAVEAT WITH IT. Neither arm beat SPY over the full range once
costs were applied, and A's out-of-sample n is 84. This module adds a
better-GRADED source, not a proven-profitable one, and the sample it
rests on is small. STRATEGY-BAKEOFF.md carries the full table.

WHAT THIS MODULE DOES, AND WHAT IT REFUSES TO DO. It keeps a local
cache of SEC companyfacts current for companies the bot is already
seeing, and nothing else. It derives no signal, scores nothing and
decides nothing - `earnings_drift.build_events` and `build_candidates`
own that, unchanged from the versions that were graded.

COST DISCIPLINE. companyfacts is one large JSON per company and a
company files quarterly, so daily re-fetching would spend the SEC rate
limit - shared across every SEC feed in this process, TRAPS.md - on
bytes that cannot have changed. Two bounds:

  - a ticker is re-asked only when its cache is older than
    FACTS_REFRESH_DAYS, tracked in `xbrl_facts_fetched`;
  - at most MAX_FETCHES_PER_PASS companies are fetched in one cycle, so
    a cold start spreads over days instead of hammering sec.gov once.

NEVER RAISES. This runs inside the trading loop. Every failure is
counted and returned with the raw upstream reason beside it (house
rule 3); a company that cannot be fetched simply has no drift events
until it can.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: One large JSON per company, carrying a `filed` date on every fact -
#: which is what makes the point-in-time discipline possible at all.
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

#: Companies report quarterly. A week is far inside that and still
#: catches a filing within days of it appearing.
FACTS_REFRESH_DAYS = 7

#: Per cycle. The bot runs 96 cycles a day, so this is a ceiling on the
#: burst, not on the throughput: a few hundred companies still reach
#: currency within a day or two without ever spending the SEC budget in
#: one go.
MAX_FETCHES_PER_PASS = 8


@dataclass
class FactsRefreshResult:
    """What the pass did, in the shape a log line and a panel can read."""

    considered: int = 0
    already_current: int = 0
    fetched: int = 0
    absent: int = 0
    failed: int = 0
    #: (ticker, reason) - the raw upstream text, house rule 3.
    reasons: list = field(default_factory=list)
    skipped_reason: str = ""

    def why_empty(self) -> str:
        if self.fetched:
            return ""
        if self.skipped_reason:
            return self.skipped_reason
        if self.considered == 0:
            return ("no company had both a ticker and a CIK to ask about - "
                    "the universe comes from Form 4 filings, so an empty "
                    "filing window makes this empty too")
        if self.already_current == self.considered:
            return (f"all {self.considered} companies were fetched within "
                    f"the last {FACTS_REFRESH_DAYS} days; companyfacts only "
                    "changes when a company files")
        if self.reasons:
            return f"every fetch failed, first: {self.reasons[0]}"
        return "nothing was fetched and no reason was recorded"


def _stale_tickers(conn, pairs, now, refresh_days):
    """(ticker, cik) pairs whose cache is missing or old enough to ask
    again. Never raises: a database without the table means everything
    looks stale, which is the safe direction - it fetches rather than
    silently skipping."""
    cutoff = (now - timedelta(days=refresh_days)).isoformat()
    try:
        rows = conn.execute(
            "SELECT ticker, fetched_at FROM xbrl_facts_fetched").fetchall()
        fresh = {str(t) for t, at in rows if str(at) > cutoff}
    except Exception:  # noqa: BLE001 - an older database has no table yet
        fresh = set()
    return [(t, c) for t, c in pairs if t not in fresh]


def _remember(conn, ticker, cik, status, note, now):
    try:
        conn.execute(
            "INSERT OR REPLACE INTO xbrl_facts_fetched "
            "(ticker, cik, fetched_at, status, note) VALUES (?,?,?,?,?)",
            (ticker, str(cik), now.isoformat(), status, note or None))
        conn.commit()
    except Exception:  # noqa: BLE001 - the note is not worth the fetch
        pass


def refresh_facts(pairs, facts_dir, conn, *, http_get=None, now=None,
                  max_fetches: int = MAX_FETCHES_PER_PASS,
                  refresh_days: int = FACTS_REFRESH_DAYS
                  ) -> FactsRefreshResult:
    """Bring the companyfacts cache up to date for `pairs`.

    `pairs`: (ticker, cik). Written as `{ticker}.json.gz` into
    `facts_dir`, which is exactly the layout
    `earnings_drift.build_events` already reads - the graded code is
    not touched.
    """
    now = now or datetime.now(timezone.utc)
    result = FactsRefreshResult()

    clean = []
    seen = set()
    for ticker, cik in pairs or ():
        # A ticker must be a ticker and a CIK must be a number. Guarding
        # on the VALUE rather than on str() of it - the news feed taught
        # that lesson with a company called NONE.
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        try:
            cik_n = int(str(cik).strip().lstrip("0") or "0")
        except (TypeError, ValueError):
            continue
        if cik_n <= 0:
            continue
        t = ticker.strip().upper()
        if t in seen:
            continue
        seen.add(t)
        clean.append((t, cik_n))

    result.considered = len(clean)
    if not clean:
        return result

    stale = _stale_tickers(conn, clean, now, refresh_days)
    result.already_current = len(clean) - len(stale)
    if not stale:
        return result

    try:
        from catalyst.data.sources.edgar_form4 import (
            RateLimitBlocked, _default_http_get, sec_pacer, user_agent,
        )
    except Exception as exc:  # noqa: BLE001 - reporting, never trading
        result.skipped_reason = f"could not load the SEC client: {exc!r}"
        return result

    get = http_get or _default_http_get
    headers = {"User-Agent": user_agent(None),
               "Accept-Encoding": "gzip, deflate"}
    out_dir = Path(facts_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        result.skipped_reason = f"cannot write to {facts_dir}: {exc!r}"
        return result

    for ticker, cik in stale[:max_fetches]:
        url = FACTS_URL.format(cik=cik)
        try:
            sec_pacer().acquire()
            resp = get(url, headers)
        except RateLimitBlocked as exc:
            # sec.gov blocked this IP. Every further request extends the
            # timeout, so the pass stops touching SEC entirely.
            result.skipped_reason = f"sec.gov rate-limited this IP: {exc}"
            return result
        except Exception as exc:  # noqa: BLE001 - one company, not the pass
            result.failed += 1
            result.reasons.append((ticker, f"{type(exc).__name__}: {exc}"))
            _remember(conn, ticker, cik, "failed", str(exc)[:500], now)
            continue

        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 404:
            # A real answer: this CIK files no XBRL. Recorded so it is
            # not asked again every week for nothing.
            result.absent += 1
            _remember(conn, ticker, cik, "absent", "404 from companyfacts", now)
            continue
        if status != 200:
            body = str(getattr(resp, "text", ""))[:500]
            result.failed += 1
            result.reasons.append((ticker, f"HTTP {status}: {body}"))
            _remember(conn, ticker, cik, "failed", f"HTTP {status}: {body}", now)
            continue

        try:
            facts = resp.json()
            if not isinstance(facts, dict) or "facts" not in facts:
                raise ValueError("no 'facts' block in the response")
            (out_dir / f"{ticker}.json.gz").write_bytes(
                gzip.compress(json.dumps(facts).encode()))
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.reasons.append((ticker, f"unreadable: {exc}"))
            _remember(conn, ticker, cik, "failed", f"unreadable: {exc}"[:500],
                      now)
            continue

        result.fetched += 1
        _remember(conn, ticker, cik, "ok", None, now)

    return result


def drift_candidates(facts_dir, tickers, *, sue_min=None):
    """Earnings-drift candidates from whatever the cache holds.

    A thin pass-through to the GRADED code: build_events and
    build_candidates are the versions the bake-off measured and are not
    reimplemented here. Never raises - a drift arm that cannot build is
    a quiet arm, not a dead cycle.
    """
    try:
        from catalyst.strategies.earnings_drift import (
            SUE_MIN, build_candidates, build_events,
        )

        names = sorted({t.strip().upper() for t in (tickers or ())
                        if isinstance(t, str) and t.strip()})
        if not names:
            return [], {}
        events = build_events(facts_dir, names)
        return build_candidates(events, sue_min=SUE_MIN if sue_min is None
                                else sue_min)
    except Exception:  # noqa: BLE001 - reporting, never trading
        return [], {}
