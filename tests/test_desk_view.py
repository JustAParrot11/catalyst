"""The desk view, and the question it was built to answer.

OWNER-ASKED 2026-08-21, twice:

  "the over detail tab isnt crazy enough ... like wallstreet traders
   screen, loads of graphs fluctuating numbers etc. I purely want
   financial, api and costing data all live, predictions etc"

  "should claude be spending daily? it failed to spend anything today?
   Does that mean its failing to research and get valuable insights on
   waht it should trade"

THE SECOND QUESTION IS THE IMPORTANT ONE, and it was a fair reading of
a page that had no answer. A $0 day is produced by at least five
different situations - a shut market, a quiet screen, a refused budget,
a dead service, a hunt that did not run - and every one of them
rendered as the same zero. The brief names that failure twice ("a zero
is never left unexplained"; "routine attrition must not look like
damage"), and it has already cost this project real debugging time.

So most of this file is about the verdict line: that each cause is
classified by the RULE rather than by a list of known cases (house rule
7), that a quiet day is never painted as a fault, and - the one that
actually matters - that a dead service is never painted as a quiet day.

THE OTHER RULE HERE: no forecast of a PRICE. The desk projects spend,
which is arithmetic on a measured series. It does not project where a
stock is going, and a test holds that, because a number like that on
this page would be one nothing in the system stands behind.
"""

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from tests.test_detailed_overview import bars_for
from tests.test_trades_page import CID, _seed


#: ONE CLOCK, READ ONCE, PASSED EXPLICITLY EVERYWHERE. House rule 6:
#: the code under test measures against datetime.now(), so a fixture
#: that stamps rows from one reading of the clock while the classifier
#: takes another goes red the moment a run straddles UTC midnight - a
#: failure with nothing to do with what these tests check. Every helper
#: below takes `now`, and every assertion passes the same one in.
NOW = datetime.now(timezone.utc)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
    bars_for(tmp_path)
    return _seed(tmp_path)


def _conn(path):
    return sqlite3.connect(path)


def _beat(path, hours_ago=0.1, now=NOW):
    """A completed cycle: the equity snapshot every pass writes."""
    when = now - timedelta(hours=hours_ago)
    c = _conn(path)
    c.execute("INSERT OR REPLACE INTO equity_snapshots VALUES (?,?,?,?,?,?)",
              (when.date().isoformat(), when.isoformat(), "2000.00",
               "1600.00", "400.00", "broker_read"))
    c.commit()
    c.close()


def _call(path, cents="19", latency=4200, usage=None, when=None):
    when = when or NOW
    c = _conn(path)
    c.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
              ("rc-" + when.isoformat(), CID, "claude-sonnet-5", "p", "[]",
               cents, latency, None, when.isoformat()))
    c.execute("INSERT INTO research_call_turns VALUES (?,?,?,?,?)",
              ("rc-" + when.isoformat(), 0, "{}",
               json.dumps(usage if usage is not None else {
                   "input_tokens": 1000, "output_tokens": 500,
                   "cache_read_input_tokens": 9000,
                   "cache_creation_input_tokens": 100,
                   "server_tool_use": {"web_search_requests": 3}}),
               "end_turn"))
    c.commit()
    c.close()


def verdict(path, now=NOW):
    db = Db(path)
    try:
        return queries.spend_today(db, now=now)
    finally:
        db.close()


def page(path):
    db = Db(path)
    try:
        return panels.detailed_overview(db, p="pro")
    finally:
        db.close()


# ---------------------------------------------------------------------
# Why today cost what it cost. The whole point of the exercise.
# ---------------------------------------------------------------------


class TestAZeroIsNeverLeftUnexplained:

    def test_a_dead_service_is_a_fault_not_a_quiet_day(self, seeded):
        """The failure mode that matters. No cycle for hours must never
        render as 'nothing to trade today' - that is a broken bot
        reading as a working one, which is the expensive direction."""
        _beat(seeded, hours_ago=9)
        v = verdict(seeded)
        assert v.kind == "fault"
        assert "no cycle" in v.headline
        assert v.hours_since_cycle > 8

    def test_never_having_run_is_also_a_fault(self, seeded):
        v = verdict(seeded)
        assert v.kind == "fault"
        assert "ever finished" in v.headline

    def test_a_quiet_screen_is_routine_and_says_why(self, seeded):
        """The ordinary $0 day. It must be legible as normal, because
        research is paid for per candidate and a day with no candidate
        costs nothing BY DESIGN."""
        _beat(seeded)
        c = _conn(seeded)
        c.execute("DELETE FROM candidates WHERE id = ?", (CID,))
        c.commit()
        c.close()
        v = verdict(seeded)
        assert v.kind == "routine"
        assert v.cents == 0
        assert "per candidate" in v.detail or "shut" in v.detail

    def test_a_budget_refusal_is_a_limit_not_a_fault(self, seeded):
        """The governor doing its job is neither damage nor silence. It
        gets its own class so it can be told apart from both."""
        _beat(seeded)
        c = _conn(seeded)
        c.execute("INSERT INTO cost_governor_events VALUES (?,?,?,?,?,?,?)",
                  (None, "scheduled", "19", "1000", "deny",
                   "daily_cap_exceeded", NOW.isoformat()))
        c.commit()
        c.close()
        v = verdict(seeded)
        assert v.kind == "limit"
        assert "daily_cap_exceeded" in v.detail

    def test_a_hunt_that_did_not_run_is_named(self, seeded):
        """At the owner's cap the daily hunt is the floor under a day's
        spend, so when it does not happen that IS the explanation."""
        _beat(seeded)
        c = _conn(seeded)
        c.execute("INSERT INTO logs (ts, level, component, message) "
                  "VALUES (?,?,?,?)",
                  (NOW.isoformat(), "INFO", "orchestrator",
                   "Hunt did not run: no_raw_events_to_read"))
        c.commit()
        c.close()
        v = verdict(seeded)
        assert "hunt" in v.headline
        assert "no_raw_events_to_read" in v.detail

    def test_spending_reads_as_spending(self, seeded):
        _beat(seeded)
        _call(seeded)
        v = verdict(seeded)
        assert v.kind == "spent"
        assert v.calls == 1

    def test_the_running_check_comes_before_the_quiet_check(self, seeded):
        """ORDER IS THE WHOLE CLASSIFIER. A dead bot also has no
        candidates, so if 'quiet' were tested first every outage would
        be reported as a quiet day - silently, and forever."""
        _beat(seeded, hours_ago=48)
        c = _conn(seeded)
        c.execute("DELETE FROM candidates WHERE id = ?", (CID,))
        c.commit()
        c.close()
        assert verdict(seeded).kind == "fault"

    def test_a_quiet_day_is_not_painted_as_a_warning(self, seeded):
        """Routine attrition must not look like damage - the brief says
        so, and this project has been burned by it twice."""
        _beat(seeded)
        c = _conn(seeded)
        c.execute("DELETE FROM candidates WHERE id = ?", (CID,))
        c.commit()
        c.close()
        html = panels._today_verdict(Db(seeded), "pro", now=NOW)
        assert "pill-crit" not in html
        assert "pill-warn" not in html

    def test_the_verdict_reaches_the_page(self, seeded):
        _beat(seeded, hours_ago=30)
        html = page(seeded)
        assert 'class="verdict"' in html
        assert "no cycle for" in html

    def test_the_reason_is_never_blank(self, seeded):
        """Whatever branch fires, a reader gets words. A verdict line
        that renders empty is the zero it was built to replace."""
        for setup in (lambda: None, lambda: _beat(seeded),
                      lambda: _call(seeded)):
            setup()
            v = verdict(seeded)
            assert v.headline.strip()
            assert v.detail.strip()


# ---------------------------------------------------------------------
# The API desk: what was asked of the model, and what it charged.
# ---------------------------------------------------------------------


class TestTheApiDesk:

    def test_cache_tokens_are_counted_as_billed_input(self, seeded):
        """TRAPS.md, the expensive one: cache tokens are billed and are
        NOT inside input_tokens. Counting input alone understates the
        bill by about half."""
        _call(seeded)
        db = Db(seeded)
        d = queries.api_desk(db)
        db.close()
        assert d.input_tokens == 1000
        assert d.cache_read_tokens == 9000
        assert d.cache_write_tokens == 100
        assert d.billed_input_tokens == 10100
        assert d.billed_input_tokens > d.input_tokens

    def test_web_searches_are_counted(self, seeded):
        _call(seeded)
        db = Db(seeded)
        d = queries.api_desk(db)
        db.close()
        assert d.web_searches == 3

    def test_an_unreadable_usage_object_is_counted_not_ignored(self, seeded):
        """House rule 3. A turn whose usage cannot be parsed must be
        NAMED, because silently treating it as zero tokens is exactly
        how a bill looks cheaper than it is."""
        _call(seeded, usage="not-an-object")
        db = Db(seeded)
        d = queries.api_desk(db)
        db.close()
        assert d.unparseable_turns == 1
        assert d.billed_input_tokens == 0
        assert "could not read" in panels._api_desk(Db(seeded), "pro")

    def test_latency_reports_median_and_worst(self, seeded):
        for i, ms in enumerate((1000, 2000, 30000)):
            _call(seeded, latency=ms, when=NOW - timedelta(minutes=i))
        db = Db(seeded)
        d = queries.api_desk(db)
        db.close()
        assert d.latency_median_ms == 2000
        assert d.latency_worst_ms == 30000

    def test_the_desk_survives_a_call_with_no_turns(self, seeded):
        c = _conn(seeded)
        c.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                  ("bare", CID, "m", "p", "[]", "5", 100, None,
                   NOW.isoformat()))
        c.commit()
        c.close()
        assert "The API, at work" in page(seeded)

    def test_bad_numbers_do_not_take_the_page_down(self, seeded):
        """A cost column that arrived as an empty string hides one bar.
        It never takes the desk down."""
        c = _conn(seeded)
        c.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                  ("junk", CID, "m", "p", "[]", "", "n/a", None,
                   NOW.isoformat()))
        c.commit()
        c.close()
        assert "The API, at work" in page(seeded)


# ---------------------------------------------------------------------
# The rest of the desk.
# ---------------------------------------------------------------------


class TestTheDesk:

    def test_every_held_name_is_on_the_top_of_book(self, seeded):
        """Showing only the quoted ones would let a symbol vanish at the
        moment its quote stopped arriving - the moment it most needs to
        be visible."""
        html = page(seeded)
        assert "Top of book" in html
        assert "EMBC" in html
        assert "cached close" in html

    def test_the_book_charts_both_dollars_and_R(self, seeded):
        html = page(seeded)
        assert "Open P&amp;L by position" in html
        assert "R multiple by position" in html

    def test_cost_and_api_survive_an_empty_book(self, tmp_path, monkeypatch):
        """An empty book must not take the live cost and API panels off
        the page - they are half of what was asked for and are live
        whether or not anything is held."""
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        path = _seed(tmp_path)
        c = _conn(path)
        c.execute("UPDATE positions SET status = 'closed'")
        c.commit()
        c.close()
        html = page(path)
        assert "What it costs to run" in html
        assert "The API, at work" in html

    def test_the_daily_ceiling_is_the_governors_not_a_second_copy(self):
        """One source of truth. The dashboard once printed a $5 ceiling
        while the bot spent against $10, because the arithmetic existed
        in two places."""
        import inspect

        from catalyst.cost.governor import daily_cap_cents

        src = inspect.getsource(panels._cost_desk)
        assert "daily_cap_cents" in src
        assert daily_cap_cents(Decimal("10000")) > 0

    def test_billed_bars_run_oldest_to_newest(self, seeded):
        """billed_q comes back newest first. Charted in that order the
        time axis runs backwards, and a rising burn rate reads as a
        falling one."""
        c = _conn(seeded)
        for i, cents in enumerate(("100", "900")):
            day = (NOW.date() - timedelta(days=i)).isoformat()
            c.execute(
                "INSERT INTO cost_reconciliation_events "
                "(id, target_date, kind, component, local_total_cents, "
                "cost_api_total_cents, discrepancy_cents, threshold_cents, "
                "api_raw_response, api_record_count, action_taken, "
                "reconciled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"rec-{i}", day, "scheduled", "all", cents, cents, "0",
                 "50", "{}", 1, "ok", NOW.isoformat()))
        c.commit()
        c.close()
        html = page(seeded)
        m = re.search(r'id="pro-billed-bars".*?</svg>', html, re.S)
        assert m, "the billed-spend chart did not render"
        titles = re.findall(r"<title>([^<]+)</title>", m.group(0))
        days = [t.split(":")[0] for t in titles]
        assert days == sorted(days), f"time axis runs backwards: {days}"

    def test_the_desk_never_forecasts_a_price(self):
        """THE LINE THAT DOES NOT MOVE. Spend is projected because it is
        arithmetic on a measured series. A price target is not, and the
        bot does not produce one - so the desk must never invent one."""
        import inspect

        for fn in (panels._cost_desk, panels._api_desk, panels._market_strip,
                   panels._position_bars, panels.detailed_overview):
            src = inspect.getsource(fn).lower()
            for banned in ("price_target", "target_price", "expected_price",
                           "projected_price", "forecast_price"):
                assert banned not in src, f"{fn.__name__} forecasts a price"

    def test_gauges_never_divide_by_a_missing_limit(self):
        assert panels._gauge_row([("x", 5, None, "n")], "s") == ""
        assert panels._gauge_row([("x", 5, 0, "n")], "s") == ""
        assert panels._gauge_row([("x", None, 10, "n")], "s") == ""

    def test_a_gauge_past_its_limit_is_critical_and_still_readable(self):
        html = panels._gauge_row([("over", 200, 100, "$2 / $1")], "s")
        assert "dg-crit" in html
        assert "width:100.0%" in html      # clipped, never overflowing
        assert "$2 / $1" in html           # the truth is in the number

    def test_bars_put_the_zero_line_where_zero_is(self):
        """A chart with losses on it must not draw them as small wins."""
        html = panels._bar_row([("a", 10), ("b", -10)], "s")
        assert "minibar-pos" in html and "minibar-neg" in html


# ---------------------------------------------------------------------
# A hostile ledger. Found by feeding these readers the things a real
# database eventually contains rather than the things a fixture does.
# ---------------------------------------------------------------------


class TestNothingHereCanBeKilledByOneBadRow:

    def _poison(self, path):
        c = _conn(path)
        c.execute("PRAGMA foreign_keys=OFF")
        for i, (cents, usage) in enumerate((
                ("NaN", '{"input_tokens": 1e400}'),
                ("Infinity", '{"output_tokens": NaN}'),
                ("", "null"),
                ("abc", "[]"),
                ("1e400", "not json at all"),
                ("0.000000001", '{"input_tokens": "lots"}'))):
            c.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                      (f"bad{i}", CID, "m", "p", "[]", cents, i * 7, None,
                       (NOW - timedelta(hours=i)).isoformat()))
            c.execute("INSERT INTO research_call_turns VALUES (?,?,?,?,?)",
                      (f"bad{i}", 0, "{}", usage, "end_turn"))
        for i, cents in enumerate(("NaN", "", "-1", "Infinity", "0.005")):
            c.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                      (f"bad-ce{i}", "{}", "m", "scheduled", "research",
                       cents, NOW.isoformat(), f"bad-ce{i}"))
        c.commit()
        c.close()

    def test_a_usage_blob_holding_infinity_does_not_take_the_page_down(
            self, seeded):
        """json.loads("1e400") is inf and int(inf) raises OverflowError,
        which is neither TypeError nor ValueError - so it went straight
        through the parser's guard and killed the whole detailed
        Overview. Exactly the shape of the UnboundLocalError that took
        out two pages on the owner's machine."""
        self._poison(seeded)
        assert "The API, at work" in page(seeded)

    def test_the_rows_it_could_not_read_are_counted_not_zeroed(self, seeded):
        """House rule 3 and the TRAPS.md trap in one: a row that
        silently prices at zero makes the bill look cheaper than it is.
        Unreadable is a number on the page, not an absence."""
        self._poison(seeded)
        db = Db(seeded)
        d = queries.api_desk(db)
        db.close()
        assert d.unparseable_turns >= 3

    def test_money_is_summed_as_Decimal_not_as_a_float(self, seeded):
        """priced_cents is TEXT holding an exact Decimal. Summing it via
        CAST(... AS REAL) turns money into a float on the way past,
        which is how a ledger and a dashboard begin disagreeing in the
        third decimal place for reasons nobody can reconstruct."""
        c = _conn(seeded)
        for i, cents in enumerate(("0.005", "0.005", "0.005")):
            c.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                      (f"tiny{i}", "{}", "m", "scheduled", "r", cents,
                       NOW.isoformat(), f"tiny{i}"))
        c.commit()
        c.close()
        db = Db(seeded)
        total = queries.spend_today_cents(db)
        db.close()
        assert isinstance(total, Decimal)
        assert total == Decimal("0.015"), f"lost precision: {total!r}"

    def test_a_non_finite_amount_never_poisons_the_total(self, seeded):
        self._poison(seeded)
        db = Db(seeded)
        total = queries.spend_today_cents(db)
        db.close()
        assert total.is_finite()


class TestTheCapShownIsTheCapBEINGSPENTAGAINST:
    """OWNER-REPORTED 2026-08-21, with a screenshot: "where has the
    month against cap come from, im unsure what it means, my API montly
    is 100" - against a panel reading "$19.77 / $8.00" with a full red
    bar, while the status strip on the SAME PAGE read "$19.77 of
    $100.00".

    _cost_desk read c.max_cap_cents, which is GOVERNOR_MAX_CAP_CENTS: a
    hard bound on the PROFIT-SHARE mechanism, capping how far realised
    profit may walk the budget up on its own. It has nothing to do with
    the figure the owner sets.

    One wrong field made five numbers wrong at once, and cost_panel
    already carries a comment about this exact defect - an owner who
    raised their budget and still saw the old cap concluded the setting
    had done nothing.
    """

    def _owner_cap(self, path, usd=100, cents="1977"):
        import catalyst.setup.credentials as creds_mod

        c = _conn(path)
        c.execute("INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
                  ("cap-e1", "{}", "m", "scheduled", "research", cents,
                   NOW.replace(day=1).isoformat(), "cap-e1"))
        c.commit()
        c.close()

        class Creds:
            anthropic_admin_key = ""
            settings = {"monthly_budget_usd": usd}

        real = creds_mod.load_credentials
        creds_mod.load_credentials = lambda *a, **k: Creds()
        db = Db(path)
        try:
            return panels._cost_desk(db, "p"), queries.cost_panel(db)
        finally:
            db.close()
            creds_mod.load_credentials = real

    def test_the_cap_tile_is_the_owners_figure(self, seeded):
        html, panel = self._owner_cap(seeded)
        assert panel.base_cap_cents == Decimal("10000")
        assert "$100.00" in html
        assert "$8.00" not in html, (
            "the profit-share bound is being shown as the owner's cap")

    def test_the_gauge_measures_against_the_same_cap(self, seeded):
        html, _ = self._owner_cap(seeded)
        assert "$19.77 / $100.00" in html

    def test_the_daily_ceiling_is_derived_from_that_cap(self, seeded):
        """It is derived, so reading the wrong monthly cap silently
        halved the daily one too - $5 where the bot spends against $10."""
        html, _ = self._owner_cap(seeded)
        assert "/ $10.00" in html

    def test_the_forecast_is_not_strangled_by_a_false_exhaustion(self, seeded):
        """With the cap read as $8 against $19.77 spent, forecast()
        returned already_exhausted and took the early return - so burn
        rate and projected month both rendered as a dash. Two more
        figures lost to the same wrong field."""
        html, _ = self._owner_cap(seeded)
        assert "$0.94" in html, "burn rate did not compute"
        assert "$29.18" in html, "projected month did not compute"

    def test_a_cap_already_spent_never_prints_the_word_None(self, seeded):
        """will_stop_early is true when the cap is ALREADY gone as well
        as when a date is projected, and in the first case exhausted_on
        is None - so the tile read "runs out None" at the owner."""
        html, _ = self._owner_cap(seeded, usd=5, cents="1977")
        assert "None" not in html
        assert "already spent" in html
