"""The detailed book, and the one thing it must never pretend.

OWNER-ASKED 2026-08-21: "on the overview tab can we have a detailed
toggle. This shows realtime data as much as we can, current price of
each trade, price tracking, live graphs ... this needs no data missed,
understandable to a proper pro trader with loads of metrics".

"AS MUCH AS WE CAN" IS A REAL LIMIT AND IT IS ON THE PAGE. This
dashboard reads a database and a bar cache. It holds no broker session
and takes no quote. The freshest price it can show is the last DAILY
CLOSE the bot cached, so every mark carries the day it came from and a
stale one is called out.

A mark-to-market that looks like a tick and is a day old is how someone
believes they are flat when they are not. That is the failure this file
mostly guards against.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

import pytest

from catalyst.backtest.data import Bar, BarCache
from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from tests.test_trades_page import _seed


def bars_for(tmp_path, ticker="EMBC", close="5.20", sessions=90,
             last_day=None):
    last_day = last_day or date.today()
    days, d = [], last_day
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    v = Decimal(close)
    BarCache(str(tmp_path / "bars")).write_bars(ticker, [
        Bar(day=x, open=v, high=v, low=v, close=v, volume=Decimal("1"))
        for x in days])


@pytest.fixture
def marked(tmp_path, monkeypatch):
    monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
    bars_for(tmp_path)
    return _seed(tmp_path)


def page(path, p="pro"):
    db = Db(path)
    try:
        return panels.detailed_overview(db, p=p)
    finally:
        db.close()


def book(path):
    db = Db(path)
    try:
        return queries.live_book(db)
    finally:
        db.close()


class TestItNeverCallsACACHEDCLOSEALIVEPRICE:
    """The line that matters most on this page."""

    def test_the_word_live_is_never_claimed_of_a_mark(self, marked):
        html = page(marked)
        assert "not a live quote" in html
        assert "cached daily close" in html

    def test_every_mark_carries_the_day_it_came_from(self, marked):
        html = page(marked)
        assert "marked" in html.lower()
        assert str(date.today()) in html

    def test_a_stale_mark_is_flagged_not_shown_plainly(self, tmp_path,
                                                       monkeypatch):
        """A week-old close presented like today's is the whole danger."""
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        bars_for(tmp_path, last_day=date.today() - timedelta(days=9))
        html = page(_seed(tmp_path))
        assert "day(s) old" in html
        assert 'pill-crit' in html or 'pill-warn' in html

    def test_a_FRESH_mark_is_not_flagged_as_stale(self, marked):
        """`worst or 99` read a zero-day-old mark - the freshest
        possible - as 99 days stale, because 0 is falsy. It painted
        today's close critical."""
        html = page(marked)
        i = html.index("Marks as of")
        tile = html[i:i + 400]
        assert "pill-good" in tile, "today's close is flagged as stale"

    def test_no_bars_at_all_says_so_rather_than_marking_at_entry(
            self, tmp_path, monkeypatch):
        """Marking an unpriced position at its entry would show a
        perfectly flat P&L that is really an absence of data."""
        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        path = _seed(tmp_path)            # once: _seed is not idempotent
        b = book(path)
        assert b.positions
        assert b.positions[0].last is None
        assert b.positions[0].unrealised_usd is None
        assert "no cached bars" in page(path)


class TestTheMetricsAProTraderReads:
    def test_open_pnl_is_marked_from_the_close(self, marked):
        b = book(marked)
        pos = b.positions[0]
        # 79.1295 shares, paid 5.06, marked 5.20
        assert round(float(pos.unrealised_usd), 2) == round(
            79.1295 * (5.20 - 5.06), 2)
        assert round(float(pos.unrealised_pct), 2) == round(
            (5.20 - 5.06) / 5.06 * 100, 2)

    def test_R_now_is_the_open_result_over_the_money_at_risk(self, marked):
        b = book(marked)
        pos = b.positions[0]
        assert pos.risk_usd and pos.r_now
        assert round(float(pos.r_now), 3) == round(
            float(pos.unrealised_usd) / float(pos.risk_usd), 3)

    def test_the_book_totals_are_the_sum_of_its_positions(self, marked):
        b = book(marked)
        assert round(float(b.unrealised_usd), 4) == round(
            sum(float(x.unrealised_usd) for x in b.positions), 4)
        assert round(float(b.deployed_usd), 4) == round(
            sum(float(x.market_value) for x in b.positions), 4)

    def test_every_column_a_desk_expects_is_present(self, marked):
        html = page(marked)
        for col in ("entry", "last", "move", "open P&amp;L", "R now",
                    "stop", "to stop", "risk $", "held", "left", "conv",
                    "marked"):
            assert f">{col}</th>" in html, f"missing column: {col}"

    def test_days_held_and_left_are_both_shown(self, marked):
        b = book(marked)
        assert b.positions[0].days_held is not None
        assert b.positions[0].days_left is not None

    def test_R_and_move_carry_their_sign(self, marked):
        """+0.3R and -0.3R are what a reader scans for."""
        html = page(marked)
        assert re.search(r"[-+]\d+\.\d\dR", html)
        assert re.search(r'class="(pos|neg)"', html)


class TestThePriceTrack:
    def test_each_position_gets_a_sparkline(self, marked):
        html = page(marked)
        assert 'class="spark"' in html
        assert "spark-line" in html

    def test_the_entry_is_the_only_reference_drawn(self, marked):
        """A sparkline earns its place by being read in one glance. The
        one question the shape must answer is above or below what I
        paid."""
        html = page(marked)
        assert "spark-entry" in html

    def test_too_few_points_draws_nothing(self):
        assert panels._sparkline([1.0, 2.0]) == ""
        assert panels._sparkline([]) == ""
        assert panels._sparkline(None) == ""

    def test_a_flat_series_does_not_divide_by_zero(self):
        html = panels._sparkline([5.0] * 10, entry=5.0)
        assert "polyline" in html
        assert "nan" not in html.lower() and "inf" not in html.lower()

    def test_the_line_never_leaves_its_box(self):
        html = panels._sparkline([1.0, 500.0, 0.5, 90.0, 3.0], entry=2.0)
        ys = [float(y) for _x, y in
              (pt.split(",") for pt in
               re.search(r'points="([^"]+)"', html).group(1).split())]
        assert all(0 <= y <= 22 for y in ys), ys


class TestTheToggle:
    def test_the_switch_offers_both_views(self):
        html = panels.overview_switch(False)
        assert "Summary" in html and "Detailed" in html
        assert "?view=detailed" in html

    def test_it_marks_which_one_is_showing(self):
        assert "on" in panels.overview_switch(True)
        summary_on = panels.overview_switch(False)
        detailed_on = panels.overview_switch(True)
        assert summary_on != detailed_on

    def test_it_is_a_LINK_not_a_script(self):
        """This page has deliberately never needed JavaScript: a link
        survives a refresh, can be bookmarked, and works when a script
        does not load."""
        html = panels.overview_switch(False)
        assert "<a " in html
        assert "onclick" not in html and "<script" not in html

    def test_the_overview_renders_both_ways(self, marked):
        from catalyst.dashboard import server

        db = Db(marked)
        try:
            plain = server.HTML_ROUTES["/"](db, {})
            pro = server.HTML_ROUTES["/"](db, {"view": ["detailed"]})
        finally:
            db.close()
        assert "pro-section" not in plain
        assert "pro-section" in pro
        assert "ov-switch" in plain and "ov-switch" in pro

    def test_the_detailed_view_adds_no_duplicate_ids(self, marked):
        from catalyst.dashboard.render import duplicate_ids
        from catalyst.dashboard import server

        db = Db(marked)
        try:
            html = server.HTML_ROUTES["/"](db, {"view": ["detailed"]})
        finally:
            db.close()
        assert not duplicate_ids(html)


class TestItSurvivesNothing:
    def test_no_positions_is_a_zero_with_its_query(self, tmp_path,
                                                  monkeypatch):
        from catalyst.storage import init_db

        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        path = str(tmp_path / "e.db")
        init_db(path).close()
        html = page(path)
        assert "SELECT" in html and "nothing is open" in html

    def test_a_missing_database_does_not_raise(self, tmp_path):
        assert page(str(tmp_path / "missing.db"))

    @pytest.mark.parametrize("field,bad", [
        ("qty", "abc"), ("entry_price", ""), ("stop_price", "nan"),
    ])
    def test_unusable_values_give_None_not_a_wrong_mark(self, field, bad):
        st = queries.TradeStory(ticker="X", status="open", qty="10",
                                entry_price="10", stop_price="9")
        setattr(st, field, bad)
        m = queries.trade_metrics(st)
        assert m.risk_usd is None or m.r_multiple is None
