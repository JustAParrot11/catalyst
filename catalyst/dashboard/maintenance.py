"""Is everything actually talking to everything? The maintenance page.

Owner request: one place that says whether each moving part is online -
the bot itself, and each outside service it depends on.

Two kinds of check, deliberately kept apart on the page, because they
fail for different reasons and one of them is free:

PASSIVE checks read the database only. They answer "has this part done
its job recently", which is the question that catches a component that
is quietly not running. They cost nothing and cannot fail.

ACTIVE checks make one request to an outside service. They answer "can
we reach it right now", which is what you want when something looks
stuck. Every one of them is FREE:

  - Alpaca trading + market data: included in the subscription.
  - EDGAR: public, keyless. One request, well inside the 10 req/s limit.
  - Anthropic ADMIN cost report: an admin read, no tokens, no charge.

There is deliberately NO active check of the ordinary Anthropic key,
because the only way to prove that key works is to send a message, and
that costs money against a £20/month ceiling. Its health is inferred
from the ledger instead - the last real research call the bot made.

Every probe is injected, so the offline test suite drives all of this
without a socket. Every failure carries the raw upstream text beside it
(house rule 3), and no probe may raise: a maintenance page that dies
when a service is down is the opposite of useful.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

#: Short, because this runs while somebody waits for a page.
PROBE_TIMEOUT_SECONDS = 6.0

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"


@dataclass
class Check:
    name: str
    group: str                       # "The bot itself" | "Outside services"
    state: str                       # ok | warn | fail | unknown
    summary: str                     # one line, plain English
    detail: str = ""                 # what it means / what to do
    raw: str = ""                    # verbatim upstream text on failure
    latency_ms: int | None = None
    free: bool = True


def _age(iso: str | None, now: datetime | None = None):
    """How old, measured against `now` when one is given.

    It used to always read the real wall clock, so passive_checks(now=...)
    steered only _edgar_is_publishing while every age ignored the
    injected time. A check that cannot be pinned to a clock cannot be
    tested against one.
    """
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(str(iso))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) - when
    except Exception:
        return None


def _fmt_age(delta: timedelta | None) -> str:
    if delta is None:
        return "never"
    secs = int(delta.total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} hours ago"
    return f"{secs // 86400} days ago"


# --------------------------------------------------------------------------
# Passive: what the database already knows
# --------------------------------------------------------------------------


def stored_credentials_checks() -> list[Check]:
    """What is ACTUALLY SAVED on disk, with no network call at all.

    "Did my key save?" and "does my key work?" are different questions,
    and the page used to answer only the second - and only when the
    owner clicked to run a live probe. Worse, a failure to READ the
    credentials file was rendered as "no admin key entered (optional)",
    so a permissions problem and never having typed one looked
    identical. The owner saved a key, was told it worked, then read that
    no key was present, with no way to tell which screen was wrong.

    Each credential now reports its FINGERPRINT - a SHA-256 prefix, safe
    to display and to paste into a bug report. The save form echoes the
    same fingerprint, so the two can be compared directly.
    """
    from catalyst.setup.credentials import credentials_path, load_credentials

    from pathlib import Path

    path = credentials_path()
    if not Path(path).exists():
        # A fresh machine before anyone has filled the form in. Normal,
        # and emphatically not a fault - the install script hands the
        # owner this exact state on purpose.
        return [Check(
            "Saved credentials", "The bot itself", UNKNOWN,
            "setup has not been completed yet",
            "No credentials file exists. Open the setup page and enter "
            "your Alpaca and Anthropic details; nothing trades until then.",
            raw="")]
    try:
        creds = load_credentials()
    except Exception as exc:  # noqa: BLE001 - the state IS the answer
        return [Check(
            "Saved credentials", "The bot itself", FAIL,
            f"the file exists but could not be read: {type(exc).__name__}",
            "This is NOT the same as having entered nothing - a file that "
            "cannot be read would make every 'not entered' below a guess. "
            "Usual cause is ownership or permissions on the file.",
            raw=f"{path}: {exc}")]

    prints = creds.fingerprints()
    out = [Check(
        "Saved credentials", "The bot itself", OK,
        f"readable, last saved {creds.saved_at or 'at an unrecorded time'}",
        "Read straight from the file the bot itself uses. No network "
        "call, so this answers 'did it save', never 'does it work'.",
        raw="")]
    for label, name, needed in (
            ("Alpaca key", "alpaca_key", True),
            ("Alpaca secret", "alpaca_secret", True),
            ("Anthropic research key", "anthropic_key", True),
            ("Anthropic billing key (admin)", "anthropic_admin_key", False)):
        fp = prints.get(name) or ""
        out.append(Check(
            f"{label} - stored?", "The bot itself",
            OK if fp else (FAIL if needed else UNKNOWN),
            (f"stored, fingerprint {fp}" if fp
             else "not stored" if needed else "not stored (optional)"),
            "The fingerprint is a one-way hash of the saved value, so it "
            "is safe to read out and to send in a bug report. The setup "
            "page prints the same fingerprint when it saves - if the two "
            "match, the key you typed is the key the bot has."
            if fp else
            "Nothing is saved under this name. If you believe you entered "
            "one, the save did not reach the file.",
            raw=""))
    return out


def build_checks() -> list[Check]:
    """Which files the running dashboard is actually made of.

    Owner-reported 2026-08-11: the page showed a build hash matching no
    commit in any branch. That proves the running files differ from
    every released version and says nothing about how - a fingerprint
    with no provenance, which is the one thing this dashboard is not
    supposed to print. The manifest makes the next mismatch answerable
    from the browser instead of over SSH.
    """
    from catalyst.dashboard.build import build_manifest

    m = build_manifest()
    listing = "\n".join(
        f"{x['name']:<20} {x['sha256']}  {x['bytes']:>7} bytes"
        for x in m["files"])
    return [Check(
        "Dashboard build", "The bot itself", OK,
        f"{m['build_hash']} from {m['file_count']} file(s)",
        "This is the hash printed in the sidebar and by the upgrade. If "
        "they differ, the browser is showing a cached page. If it matches "
        "no released version, the file list below says which file is "
        "unexpected - an extra one, a missing one, or one whose contents "
        "differ.",
        raw=f"hashed from {m['directory']}\n\n{listing}")]


def _edgar_is_publishing(now=None) -> bool:
    """Is EDGAR in its own publishing window right now?

    Business days, roughly 06:00-22:00 New York. Computed as a UTC
    offset rather than with a timezone database, because being an hour
    out either side of a sixteen-hour window changes nothing here and a
    missing tzdata file must never take the page down.
    """
    now = now or datetime.now(timezone.utc)
    ny_hour = (now.hour - 4) % 24        # EDT; EST is an hour out, harmless
    weekday = now.weekday() < 5
    return weekday and 6 <= ny_hour < 22


def passive_checks(db, now=None) -> list[Check]:
    """`now` is injectable so the EDGAR window can be tested on BOTH
    sides of it. A check whose result depends on when the suite happens
    to run is not a check - that has bitten this project three times."""
    out: list[Check] = list(stored_credentials_checks())
    out.extend(build_checks())

    def one(sql, params=()):
        try:
            res = db.q(sql, params)
            return (res.rows[0] if res.rows else None), res.error
        except Exception as exc:  # noqa: BLE001
            return None, repr(exc)

    def many(sql, params=()):
        try:
            res = db.q(sql, params)
            return list(res.rows), res.error
        except Exception as exc:  # noqa: BLE001
            return [], repr(exc)

    # 1. The database itself.
    out.append(Check(
        "Database", "The bot itself",
        FAIL if db.open_error else OK,
        db.open_error or f"open, {len(db.tables())} tables",
        "Everything the bot records lives here. If this fails, nothing "
        "else on any page can be trusted.",
        raw=db.open_error or ""))

    # 2. Has a cycle run? cost/governor rows are written every cycle that
    #    considered spending; raw_events every cycle that fetched.
    row, err = one("SELECT MAX(fetched_at) t, COUNT(*) n FROM raw_events")
    age = _age(row["t"] if row else None, now)
    # EDGAR'S OWN CLOCK, not ours. The bot fetches every cycle, day and
    # night - discovery is not gated on market hours - but a stored row
    # only appears when EDGAR PUBLISHES something, and it publishes on
    # business days, roughly 06:00-22:00 New York. Overnight and at
    # weekends there is nothing new to store, so the newest row ages
    # exactly as it should. This check used to warn after six hours
    # whatever the time, which meant it cried wolf every single night
    # (owner-reported 2026-08-11: "does this only run during market
    # hours, if no it says last seen 6 hours ago").
    # THE FEED READS A ONCE-DAILY FILE, so hours are the wrong unit.
    # Owner-reported 2026-08-11: "EDGAR also says this 9 hours ago, 405
    # events stored - EDGAR is publishing right now and nothing new has
    # arrived - the feed or the scheduler may be stuck." It was not
    # stuck. This feed fetches the DAILY INDEX
    # (edgar/daily-index/.../form.YYYYMMDD.idx) - one file per day,
    # published in the evening. Between publishes the bot re-reads
    # indexes it already holds and INSERT OR IGNORE stores nothing new,
    # so a nine-hour-old newest row at midday is exactly correct.
    #
    # Warning on hours measured EDGAR's filing-acceptance window, which
    # is not what this feed consumes. The unit is days.
    if age is None:
        state, why = UNKNOWN, "no filing has been stored yet"
    elif age < timedelta(days=4) and not _edgar_is_publishing(now):
        # Overnight, or a weekend, which spans up to ~3 days with no
        # index published at all.
        state, why = OK, ("EDGAR is not publishing at this hour, so there "
                          "is nothing new to store - this is expected, not "
                          "a fault")
    elif age < timedelta(hours=30):
        state, why = OK, ("normal - the daily index is published once a "
                          "day, so this is the most recent batch")
    else:
        state, why = WARN, (
            "no new filing since - more than a day, which is longer than "
            "the gap between daily indexes, so the feed or the scheduler "
            "may be stuck")
    out.append(Check(
        "Filing feed (EDGAR) last delivered", "The bot itself", state,
        f"{_fmt_age(age)}" + (f", {row['n']} events stored" if row else "")
        + f" - {why}",
        "The bot fetches every cycle, roughly every 15 minutes, day and "
        "night - discovery is never gated on market hours. But this feed "
        "reads EDGAR's DAILY INDEX, which is one file per day published "
        "in the evening. Between publishes there is genuinely nothing new "
        "to store, so a gap of several hours is the normal state, not a "
        "fault. Only a gap longer than about a day means something is "
        "wrong.", raw=err or ""))

    # 2b. The SPY benchmark cache. Owner-reported 2026-08-11: "The graph
    # that has catalyst and SPY in blue and red has no red SPY line".
    # The chart draws SPY only when there are points, and there are none
    # when the cache is empty - but nothing said WHY it was empty or
    # when the daily refresh last tried. A missing line and a broken
    # refresh looked identical, which is the failure this whole
    # dashboard exists to prevent.
    try:
        from catalyst.dashboard.db import bars_path
        from pathlib import Path as _Path

        root = _Path(bars_path())
        csv = root / "SPY.csv"
        if not csv.exists():
            # UNKNOWN, not FAIL. A machine where nothing has run yet
            # legitimately has no cache, and a fresh install that reports
            # a failure teaches the owner to ignore this page.
            state, summary = UNKNOWN, f"not built yet - no SPY.csv under {root}"
            detail = ("The red SPY line cannot be drawn without this. The "
                      "scheduler refreshes it once a day from Alpaca; if it "
                      "is still missing after a full day the refresh is "
                      "failing - the Logs page will carry the reason, "
                      "filtered to component 'catalyst.scheduler'.")
        else:
            lines = [ln for ln in
                     csv.read_text().splitlines() if ln.strip()]
            n = max(0, len(lines) - 1)
            last = lines[-1].split(",")[0] if n else ""
            feed = ""
            meta = root / "cache_meta.json"
            if meta.exists():
                import json as _json
                try:
                    feed = str(_json.loads(meta.read_text()).get("feed") or "")
                except ValueError:
                    feed = "(unreadable cache_meta.json)"
            state = OK if n else FAIL
            summary = (f"{n} daily bar(s), newest {last}"
                       + (f", from the {feed.upper()} feed" if feed else ""))
            detail = ("This is what the red SPY line is drawn from. An IEX "
                      "feed means the account is not entitled to the full "
                      "consolidated tape - fine for a daily SPY benchmark, "
                      "but the Performance page says so rather than letting "
                      "you assume the tape."
                      if feed == "iex" else
                      "This is what the red SPY line is drawn from.")
        out.append(Check("SPY benchmark cache", "The bot itself",
                         state, summary, detail, raw=""))
    except Exception as exc:  # noqa: BLE001 - a check must never take the page down
        out.append(Check("SPY benchmark cache", "The bot itself", UNKNOWN,
                         f"could not be read: {type(exc).__name__}",
                         "", raw=repr(exc)))

    # 3. Research calls - the only path that spends money.
    row, err = one("SELECT MAX(called_at) t, COUNT(*) n FROM research_calls")
    age = _age(row["t"] if row else None, now)
    out.append(Check(
        "Claude research calls", "The bot itself",
        OK if row and row["n"] else UNKNOWN,
        (f"{row['n']} call(s), last {_fmt_age(age)}" if row and row["n"]
         else "none yet"),
        "Research only runs while the US market is open and a candidate "
        "has survived screening, so 'none yet' is normal on a new "
        "install. This is also the only live proof that the ordinary "
        "Anthropic key works - testing it directly would cost money.",
        raw=err or ""))

    # 4. Unpriced cost rows block ALL spending.
    row, err = one("SELECT COUNT(*) n FROM cost_events WHERE priced_cents IS NULL")
    n = (row["n"] if row else 0) or 0
    out.append(Check(
        "Cost ledger complete", "The bot itself",
        OK if not n else FAIL,
        "every recorded call is priced" if not n
        else f"{n} row(s) recorded but not priced",
        "An unpriced row means a billing field the bot did not "
        "recognise. It refuses to spend anything more until a human "
        "looks - deliberately. See the Cost page.", raw=err or ""))

    # 5. Reconciliation against the real bill.
    row, err = one("SELECT MAX(target_date) d FROM cost_reconciliation_events "
                   "WHERE action_taken != 'check_failed'")
    last = row["d"] if row else None
    out.append(Check(
        "Nightly bill check", "The bot itself",
        OK if last else UNKNOWN,
        f"last reconciled day: {last}" if last else "has not run yet",
        "Compares the bot's own spending record against the real "
        "Anthropic bill. Needs the optional ADMIN key; without it this "
        "stays 'not run'.", raw=err or ""))

    # 6. Unacknowledged discrepancy pauses spending.
    # NAME THE DAYS AND THE FIGURES, not just a count. Owner-reported
    # 2026-08-11: "it says there are 7 discrepancies, it doesnt actually
    # say what". A bare count cannot be acted on - it does not say which
    # days, how big, or whether they are all the same harmless thing, so
    # the only response available is to go and look somewhere else.
    rows, err = many(
        "SELECT target_date, local_total_cents, cost_api_total_cents, "
        "       discrepancy_cents FROM cost_reconciliation_events "
        "WHERE action_taken = 'scheduled_paused' AND acknowledged_at IS NULL "
        "ORDER BY target_date DESC LIMIT 8")
    rows = rows or []
    n = len(rows)
    if not n:
        detail = "no unacknowledged discrepancy"
    else:
        def _money(cents):
            try:
                return f"${Decimal(str(cents or 0)) / 100:.2f}"
            except (ArithmeticError, TypeError, ValueError):
                return f"{cents!r}"
        lines = [f"{r['target_date']}: this bot recorded "
                 f"{_money(r['local_total_cents'])}, Anthropic billed the "
                 f"organisation {_money(r['cost_api_total_cents'])} "
                 f"(gap {_money(r['discrepancy_cents'])})"
                 for r in rows]
        detail = (f"{n} awaiting your acknowledgement - "
                  + "; ".join(lines))
    out.append(Check(
        "Spending not paused", "The bot itself",
        OK if not n else FAIL, detail,
        "While one is outstanding the bot will not spend at all. A gap on "
        "a day the bot did not run is your OWN Anthropic usage, not an "
        "error - the Cost page explains which is which. Acknowledging "
        "changes no figure; it records that a human looked.",
        raw=err or ""))

    # 7. Kill switch.
    #
    # LIVE, NOT EVER-TRIPPED. Nothing writes `cleared_at`, so the old
    # "cleared_at IS NULL" count was "has ever tripped" and this check
    # read FAIL forever after the first trip. Owner's bundle 2026-08-24:
    # "Kill switches: 2 active" on a day the bot researched 115
    # candidates, placed an order, and logged no trip at all.
    #
    # The rule lives in queries.py so this page and the alerts strip
    # cannot disagree about whether the bot is stopped.
    from catalyst.dashboard.queries import (
        KILL_SWITCH_LIVE_SQL, kill_switch_is_live,
    )

    kill_rows, err = many(KILL_SWITCH_LIVE_SQL)
    live = [r for r in kill_rows if kill_switch_is_live(r)]
    stale = len(kill_rows) - len(live)
    out.append(Check(
        "Kill switches", "The bot itself",
        OK if not live else FAIL,
        ("none tripped" if not live
         else f"{len(live)} active: "
              + ", ".join(str(r["switch_name"]) for r in live))
        + (f" ({stale} earlier trip(s), since cleared by a later cycle)"
           if stale else ""),
        "A tripped kill switch blocks new positions while still "
        "protecting the ones already open. A trip counts as live only "
        "until a later cycle gets past the same check - the broker "
        "equity mark taken straight after it is the proof.",
        raw=err or ""))

    # 8. Benchmark freshness - the comparison rots silently otherwise.
    try:
        from catalyst.backtest.data import BarCache
        from catalyst.dashboard.db import bars_path
        cache = BarCache(bars_path())
        bars = cache.load_bars("SPY")
        last_day = bars[-1].day if bars else None
        gap = ((datetime.now(timezone.utc).date() - last_day).days
               if last_day else None)
        out.append(Check(
            "S&P benchmark data", "The bot itself",
            OK if gap is not None and gap <= 4 else WARN,
            (f"{len(bars)} daily bars, latest {last_day}" if last_day
             else "no bars cached"),
            "Refreshed once a day. Weekends and holidays make a gap of "
            "up to four days normal; longer means the refresh is "
            "failing and the comparison against the S&P will stall."))
    except KeyError:
        # NOT an error: `data/` is gitignored, so every fresh install
        # starts with no bar file and the refresher fills it on the
        # first cycle. Reporting this as a fault - with a raw exception
        # beside it - made a brand-new, perfectly healthy install look
        # broken, and tripped the upgrade's own test gate on the owner's
        # server (2026-08-10).
        out.append(Check(
            "S&P benchmark data", "The bot itself", UNKNOWN,
            "not fetched yet",
            "Downloaded automatically on the first cycle after start-up, "
            "free with your Alpaca subscription. If it is still empty an "
            "hour after the bot started, the download is failing and the "
            "comparison against the S&P has nothing to draw against."))
    except Exception as exc:  # noqa: BLE001 - a genuinely odd failure
        out.append(Check(
            "S&P benchmark data", "The bot itself", WARN,
            "benchmark data could not be read",
            "The file exists but could not be parsed. The raw error is "
            "beside this row.", raw=repr(exc)))
    return out


# --------------------------------------------------------------------------
# Active: one request each, all free
# --------------------------------------------------------------------------


def _timed(fn):
    start = time.monotonic()
    try:
        ok, message = fn()
        raw = ""
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        ok, message, raw = False, f"{type(exc).__name__}: {exc}", repr(exc)
    return ok, message, raw, int((time.monotonic() - start) * 1000)


def active_checks(
    creds,
    *,
    alpaca_probe=None,
    market_data_probe=None,
    edgar_probe=None,
    admin_probe=None,
) -> list[Check]:
    """Live reachability. `creds` may be None (nothing configured yet).

    Each probe is a zero-argument callable returning (ok, message); they
    are injected so the test suite never opens a socket.
    """
    out: list[Check] = []
    key = getattr(creds, "alpaca_key", "") if creds else ""
    secret = getattr(creds, "alpaca_secret", "") if creds else ""
    admin = getattr(creds, "anthropic_admin_key", "") if creds else ""
    anth = getattr(creds, "anthropic_key", "") if creds else ""

    # --- Alpaca trading API
    if not (key and secret):
        out.append(Check("Alpaca (your broker)", "Outside services", UNKNOWN,
                         "no keys entered yet",
                         "Enter them on the Setup page; nothing can trade "
                         "until then."))
    else:
        probe = alpaca_probe or _default_alpaca_probe(creds)
        ok, message, raw, ms = _timed(probe)
        out.append(Check(
            "Alpaca (your broker)", "Outside services",
            OK if ok else FAIL, message,
            "Placing orders, reading positions and the market clock all "
            "go through here. Free - it is part of your Alpaca account.",
            raw=raw, latency_ms=ms))

        probe = market_data_probe or _default_market_data_probe(creds)
        ok, message, raw, ms = _timed(probe)
        out.append(Check(
            "Alpaca market data", "Outside services",
            OK if ok else FAIL, message,
            "Live prices for sizing and stops, and the daily S&P bars "
            "behind the comparison chart. Also included in your "
            "subscription.", raw=raw, latency_ms=ms))

    # --- EDGAR: public, keyless
    probe = edgar_probe or _default_edgar_probe()
    ok, message, raw, ms = _timed(probe)
    out.append(Check(
        "SEC EDGAR (insider filings)", "Outside services",
        OK if ok else FAIL, message,
        "Where every candidate comes from. Public and free; the bot "
        "stays well inside the SEC's 10-requests-per-second limit.",
        raw=raw, latency_ms=ms))

    # --- Anthropic admin (free); ordinary key is NOT probed (costs money)
    if admin:
        probe = admin_probe or _default_admin_probe(admin)
        ok, message, raw, ms = _timed(probe)
        out.append(Check(
            "Anthropic billing (admin key)", "Outside services",
            OK if ok else FAIL, message,
            "Read-only access to your bill, used for the nightly check. "
            "Free: it reads spending totals, it does not use the model.",
            raw=raw, latency_ms=ms))
    else:
        out.append(Check(
            "Anthropic billing (admin key)", "Outside services", UNKNOWN,
            "no admin key entered (optional)",
            "Without it the bot cannot cross-check its own spending "
            "record against the real Anthropic bill."))

    out.append(Check(
        "Anthropic research key", "Outside services",
        OK if anth else UNKNOWN,
        "saved" if anth else "not entered yet",
        "Deliberately NOT tested live: the only way to prove this key "
        "works is to send Claude a message, and that costs real money "
        "against your monthly ceiling. Its true health shows up under "
        "'Claude research calls' above, from work the bot actually did.",
        free=False))
    return out


# --- default probes (only these touch the network) -------------------------


def _default_alpaca_probe(creds):
    def probe():
        from catalyst.setup.credentials import test_alpaca
        from catalyst.execution.broker import base_url_for_mode
        mode = str((getattr(creds, "settings", None) or {}).get(
            "account_mode", "paper"))
        return test_alpaca(creds.alpaca_key, creds.alpaca_secret,
                           base_url=base_url_for_mode(mode))
    return probe


def _default_market_data_probe(creds):
    """Ask the same question the benchmark asks, the same way.

    This probe pinned feed=sip while the benchmark had already learned
    to fall back to IEX, so an account without the SIP entitlement was
    told its market data was BROKEN when the bot was reading it happily
    (owner-reported 2026-08-11, twice). A check that disagrees with the
    code it is checking is worse than no check.
    """
    def probe():
        import httpx

        from catalyst.data.benchmark import FEED_PREFERENCE

        headers = {"APCA-API-KEY-ID": creds.alpaca_key,
                   "APCA-API-SECRET-KEY": creds.alpaca_secret}
        # A WINDOW, not "the latest bar". Asking for limit=1 with no
        # dates returns today's bar, which does not exist until the
        # market has closed - so the probe reported a broken feed every
        # morning and all weekend. Ten days always contains a session.
        today = datetime.now(timezone.utc).date()
        window = {"start": (today - timedelta(days=10)).isoformat(),
                  "end": today.isoformat()}
        last = ""
        for feed in FEED_PREFERENCE:
            resp = httpx.get(
                "https://data.alpaca.markets/v2/stocks/bars",
                params={"symbols": "SPY", "timeframe": "1Day", "limit": 10,
                        "feed": feed, "adjustment": "all", **window},
                headers=headers, timeout=PROBE_TIMEOUT_SECONDS)
            if resp.status_code != 200:
                last = f"HTTP {resp.status_code} on {feed}: {resp.text[:160]}"
                continue
            if ((resp.json() or {}).get("bars") or {}).get("SPY"):
                if feed == FEED_PREFERENCE[0]:
                    return True, f"reachable, SPY daily bar returned ({feed})"
                return True, (
                    f"reachable, SPY daily bar returned from the {feed.upper()} "
                    "feed. This account is not entitled to the full "
                    "consolidated tape, so the bot uses the best feed it can "
                    "read - the benchmark says so on the Performance page.")
            last = f"{feed}: reachable, no SPY bar returned"
        return False, (
            "reachable, but no feed returned a SPY bar. Last answer: "
            + (last or "none"))
    return probe


def _default_edgar_probe():
    def probe():
        # THROUGH THE SHARED PACER. The SEC's 10 req/s ceiling is per IP
        # and shared across every one of its APIs, so a probe that paces
        # itself independently of the feed can only ever add to the feed's
        # rate. One request cannot breach it on its own; the point is that
        # nothing in this process is allowed to talk to sec.gov without
        # going through the same limiter, so it stays true as call sites
        # are added.
        from catalyst.data.sources.edgar_form4 import (
            ARCHIVES_BASE, _default_http_get, sec_pacer, user_agent,
        )
        sec_pacer().acquire()
        resp = _default_http_get(f"{ARCHIVES_BASE}edgar/daily-index/",
                                 {"User-Agent": user_agent()})
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        return True, "reachable"
    return probe


def _default_admin_probe(admin_key: str):
    def probe():
        from catalyst.setup.credentials import test_admin_key
        return test_admin_key(admin_key)
    return probe


# --------------------------------------------------------------------------


@dataclass
class MaintenanceReport:
    checks: list = field(default_factory=list)
    ran_active: bool = False
    generated_at: str = ""

    @property
    def worst(self) -> str:
        for state in (FAIL, WARN, UNKNOWN):
            if any(c.state == state for c in self.checks):
                return state
        return OK

    def by_group(self, group: str) -> list:
        return [c for c in self.checks if c.group == group]


def build_report(db, creds=None, *, run_active: bool = False, now=None,
                 **probes) -> MaintenanceReport:
    checks = passive_checks(db, now=now)
    if run_active:
        checks += active_checks(creds, **probes)
    return MaintenanceReport(
        checks=checks, ran_active=run_active,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
