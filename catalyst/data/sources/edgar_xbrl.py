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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: One large JSON per company, carrying a `filed` date on every fact -
#: which is what makes the point-in-time discipline possible at all.
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

#: THE DRIFT WINDOW IS THE WHOLE TRADE, so an old filing is not a
#: cheaper version of a fresh one - it is nothing at all.
#:
#: OWNER'S BUNDLE, 2026-08-30: the arm's first day live produced 6,293
#: candidates, dated from 2019 onwards - `A-2019-02-22-AMH-51`,
#: `A-2020-02-14-AAT-65`. build_events replays every earnings event a
#: company has ever filed, which is exactly right for grading ten years
#: of history and exactly wrong for deciding what to buy this morning.
#: 810 of them were queued every cycle, and on the next trading day six
#: a cycle would have gone into PAID research on earnings reports filed
#: up to seven years ago - roughly $30 of a $10 daily ceiling, spent
#: before lunch on trades that cannot exist.
#:
#: Candidate A enters on the filing and holds 12 trading days. Five
#: calendar days covers a Friday filing first seen the following
#: Wednesday and still leaves most of the window; past that the graded
#: trade has already happened without us.
MAX_EVENT_AGE_DAYS = 5

#: Companies report quarterly, so most re-fetches find nothing new -
#: but the cadence is NOT free to choose. A filing is invisible until
#: the cache is refreshed, so a refresh slower than MAX_EVENT_AGE_DAYS
#: means a filing can go stale before it is ever seen, and the arm
#: silently produces nothing at all. The two numbers are one rule:
#: refresh strictly faster than the window, and a test holds it.
FACTS_REFRESH_DAYS = 2

#: Per cycle. The bot runs 96 cycles a day, so this is a ceiling on the
#: burst, not on the throughput: a few hundred companies still reach
#: currency within a day or two without ever spending the SEC budget in
#: one go.
MAX_FETCHES_PER_PASS = 8


#: THE UNIVERSE IS EVERY COMPANY THAT FILED EARNINGS, not every company
#: an insider traded in.
#:
#: OWNER'S 7-DAY BUNDLE, 2026-09-05: zero drift candidates in a week.
#: The arm's universe was `_issuer_pairs` - the companies in the Form 4
#: feed, 141 tickers - and none of them filed a 10-Q that week. The
#: event this arm trades IS the filing, so the daily filing index is
#: where the universe has to come from (edgar_form4.daily_filers).
#:
#: The index carries a CIK and a company name, never a ticker, and the
#: bot trades tickers. SEC publishes the mapping as one keyless JSON
#: file, refreshed rarely: tickers change on a listing event, not on a
#: filing, so a week-old map is the map.
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
TICKER_MAP_FILE = "company_tickers.json"
TICKER_MAP_REFRESH_DAYS = 7

#: Which forms carry the earnings the drift signal is built from.
#: Amendments are excluded on purpose: the signal is the FIRST-filed
#: value for a period (earnings_drift.py, point-in-time discipline).
EARNINGS_FORMS = ("10-Q", "10-K")

#: How many days of the filing index to read for filers. A filing
#: accepted after the evening publish lands in the next day's index,
#: and a weekend sits between Friday and Monday - three calendar days
#: sees all of them and stays well inside MAX_EVENT_AGE_DAYS.
FILER_LOOKBACK_DAYS = 3


def cik_ticker_map(facts_dir, *, http_get=None, now=None,
                   refresh_days: int = TICKER_MAP_REFRESH_DAYS
                   ) -> tuple[dict, str]:
    """({cik int: TICKER}, note). Cached beside the facts; refreshed
    when older than `refresh_days`. Never raises: an unreadable map is
    an empty map with the reason beside it (house rule 3), and the
    arm simply cannot name those filers this pass."""
    now = now or datetime.now(timezone.utc)
    path = Path(facts_dir) / TICKER_MAP_FILE
    stale = True
    try:
        age = now.timestamp() - path.stat().st_mtime
        stale = age > refresh_days * 86400
    except OSError:
        stale = True
    note = ""
    if stale:
        try:
            from catalyst.data.sources.edgar_form4 import (
                RateLimitBlocked, _default_http_get, sec_pacer, user_agent,
            )
            sec_pacer().acquire()
            resp = (http_get or _default_http_get)(
                TICKER_MAP_URL, {"User-Agent": user_agent(None),
                                 "Accept-Encoding": "gzip, deflate"})
            status = int(getattr(resp, "status_code", 0) or 0)
            if status != 200:
                raise ValueError(
                    f"HTTP {status}: {str(getattr(resp, 'text', ''))[:200]}")
            body = resp.json()
            if not isinstance(body, dict) or not body:
                raise ValueError("company_tickers.json is not a non-empty object")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body))
        except Exception as exc:  # noqa: BLE001 - use what is cached, say so
            if type(exc).__name__ == "RateLimitBlocked":
                raise
            note = (f"ticker map not refreshed ({type(exc).__name__}: "
                    f"{str(exc)[:160]}); using the cached copy if any")
    try:
        body = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        return {}, (note or f"no ticker map on disk ({type(exc).__name__})")
    out: dict = {}
    for entry in (body.values() if isinstance(body, dict) else ()):
        if not isinstance(entry, dict):
            continue
        try:
            cik = int(entry.get("cik_str"))
        except (TypeError, ValueError):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        # The first listing wins: SEC orders the file by market cap, so a
        # company with several classes maps to its primary line.
        if ticker and cik > 0:
            out.setdefault(cik, ticker)
    return out, note


def earnings_filer_pairs(index_rows, ticker_map: dict) -> list:
    """(ticker, cik) for every 10-Q/10-K filer the map can name, in the
    order filed. Filers the map cannot name are dropped - a CIK with no
    ticker is not something the bot can buy."""
    seen: dict = {}
    for row in index_rows or ():
        try:
            cik = int(str(getattr(row, "cik", "")).strip().lstrip("0") or "0")
        except ValueError:
            continue
        ticker = ticker_map.get(cik)
        if ticker and ticker not in seen:
            seen[ticker] = str(cik)
    return list(seen.items())


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


@dataclass
class DriftLiveness:
    """Why the live arm emitted what it emitted, in the shape a log line
    can read. A drift arm producing nothing is the normal case on most
    days - the point of these counts is to say WHICH nothing it is
    (house rule 3: a zero never stands alone)."""

    built: int = 0
    live: int = 0
    too_old: int = 0
    in_the_future: int = 0
    newest_filed: object = None
    max_age_days: int = 0

    def why_empty(self) -> str:
        if self.live:
            return ""
        if self.built == 0:
            return ("the companyfacts cache produced no surprise events at "
                    "all - either nothing is cached yet or no company in it "
                    "cleared the surprise threshold")
        newest = (f"newest filing in the cache is {self.newest_filed}"
                  if self.newest_filed else "no filing dates were readable")
        return (f"{self.built} graded event(s), none filed within the last "
                f"{self.max_age_days} days, so the drift window has closed "
                f"on all of them; {newest}")


def drift_candidates(facts_dir, tickers, *, sue_min=None):
    """EVERY earnings-drift candidate the cache can support, back to the
    start of the company's XBRL history.

    A thin pass-through to the GRADED code: build_events and
    build_candidates are the versions the bake-off measured and are not
    reimplemented here. Never raises - a drift arm that cannot build is
    a quiet arm, not a dead cycle.

    NOT FOR THE LIVE PIPELINE. This is the ten-years-of-history shape
    the backtest needs. Wiring it into a cycle queues earnings reports
    from 2019 for paid research; use live_drift_candidates(), and
    test_drift_arm_is_live_not_historical.py holds that the orchestrator
    does.
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


def live_drift_candidates(facts_dir, tickers, as_of, *, sue_min=None,
                          max_age_days: int = MAX_EVENT_AGE_DAYS):
    """The drift candidates that are tradeable NOW -> (cands, table, stats).

    Same graded signal, filtered to the events whose drift window is
    still open, and stamped with the real discovery time.

    THE TIMESTAMP MATTERS TOO. build_candidates sets
    discovered_at=2016-01-01 - a backtest sentinel, since a replay has
    no "now". Live, that date is a lie the dashboard believes: every
    window on the candidates table is keyed on discovered_at, so 6,293
    candidates created on 2026-08-30 did not appear in a single one of
    them. The owner would have read "no candidates today" while the
    research queue filled up.
    """
    cands, table = drift_candidates(facts_dir, tickers, sue_min=sue_min)
    stats = DriftLiveness(built=len(cands), max_age_days=max_age_days)
    if not cands:
        return [], {}, stats
    try:
        today = as_of.date()
    except AttributeError:
        today = as_of
    oldest = today - timedelta(days=max_age_days)
    stats.newest_filed = max(c.catalyst_date for c in cands)

    live = []
    for c in cands:
        if c.catalyst_date > today:
            # A `filed` date after today means the cache or the clock is
            # wrong. Refusing is the safe direction and it is counted,
            # never dropped in silence.
            stats.in_the_future += 1
            continue
        if c.catalyst_date < oldest:
            stats.too_old += 1
            continue
        # THE SURPRISE GOES ON THE CANDIDATE, so the research prompt can
        # state it. The signal table this function returns is discarded
        # by the live wiring (the model researches, not make_signal_fn),
        # and without these the prompt could only say "an earnings
        # filing happened" - which is not a thesis. Same `fact:` shape
        # the insider arm uses for its buyers.
        ev = table.get(c.id)
        facts = ()
        if ev is not None and hasattr(ev, "sue"):
            facts = (f"fact:sue={getattr(ev, 'sue', 0):+.2f}",
                     f"fact:period_end={getattr(ev, 'period_end', '')}",
                     f"fact:filed={getattr(ev, 'filed', '')}",
                     f"fact:form={getattr(ev, 'form', '') or '10-Q/10-K'}")
        live.append(replace(c, discovered_at=as_of,
                            correlation_tags=tuple(c.correlation_tags) + facts))
    stats.live = len(live)
    # The id is unchanged by replace(), so the signal table still keys.
    return live, {c.id: table[c.id] for c in live if c.id in table}, stats
