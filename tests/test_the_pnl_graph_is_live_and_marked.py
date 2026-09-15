"""A profit-and-loss line for an open trade, with the events on it.

OWNER-ASKED 2026-09-14: *"i want to be able to view some live or semi
live profit/loss graph info when viewing each individual trade, almost
live wall street like for current trades, and a profit loss graph with
lines detailing events so we can see maybe when something happened"*.

WHAT WAS MISSING AND WHAT WAS NOT. Measured before building: every event
the owner asked for was ALREADY on `TradeStory` - `reviews` with what
Claude said, `orders`, `stop_events`, `sources` with news timestamps -
and the live NBBO was already fetched by `dashboard/live.quotes_for`
with its own reasons. Ninth instance of this project's recurring defect.

What was genuinely absent is INTRADAY PRICES. `grep` for `1Min` across
`catalyst/` returned only the drawdown watermark, so the bot had never
fetched a bar finer than a day - and a position opened this morning has
no daily closes at all, which is why RLMD's card drew a price ladder.

THE ARITHMETIC IS NOT MODELLED. `pnl = (price - fill) x qty`, and the
stop becomes a floor at `(stop - fill) x qty`. On the real RLMD trade
that floor is -$39.45, which is the same number `limit_applications`
recorded against the $40.00 hard bound.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.dashboard import panels, pnl

# Anchored to a REAL hold length measured from now, never to a calendar
# date (house rule 6): the code under test compares against a clock.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
OPEN = NOW - timedelta(days=3)

FILL, QTY, STOP = "4.4949", "88.6812", "4.05"
#: The floor the real trade actually carries, to the cent.
RLMD_FLOOR = -39.45


def bars(n=60, step=0.004, start=4.4949, first=None, minutes=60):
    first = first or OPEN
    return [{"t": (first + timedelta(minutes=minutes * i)).isoformat()
             .replace("+00:00", "Z"),
             "c": round(start + step * i, 4)} for i in range(n)]


def series(**over):
    kw = dict(fill=FILL, qty=QTY, stop=STOP, bars=bars(), opened=OPEN,
              timeframe="1Hour")
    kw.update(over)
    return pnl.build(**kw)


class Story:
    """The fields `_pnl_chart` reads, and nothing else."""

    ticker = "RLMD"
    entry_price = FILL
    qty = QTY
    stop_price = STOP
    status = "open"
    direction = "long"
    opened_at = OPEN.isoformat()
    closed_at = ""
    exit_reason = ""
    reviews = ()
    sources = ()

    def __init__(self, **over):
        for k, v in over.items():
            setattr(self, k, v)


class TestTheMoneyArithmetic:
    """Nothing here is modelled, projected or estimated."""

    def test_the_stop_becomes_a_floor_in_dollars(self):
        s = series()
        assert s.stop_pnl == pytest.approx(RLMD_FLOOR, abs=0.01), (
            "the floor is not the real trade's own risk figure")

    def test_pnl_is_price_minus_fill_times_quantity(self):
        s = series(bars=[{"t": (OPEN + timedelta(hours=1)).isoformat(),
                          "c": 5.0}])
        assert s.points[0].pnl == pytest.approx(
            (5.0 - float(FILL)) * float(QTY))

    def test_break_even_is_always_inside_the_drawn_range(self):
        """A chart that crops zero cannot show whether it is winning."""
        for step in (+0.02, -0.02, 0.0):
            s = series(bars=bars(step=step))
            assert s.lo <= 0 <= s.hi, (step, s.lo, s.hi)

    def test_the_floor_is_always_inside_the_drawn_range(self):
        """Cropping the stop hides the worst case, which is the one
        figure the reader most needs."""
        s = series(bars=bars(step=+0.05))
        assert s.lo <= s.stop_pnl, (s.lo, s.stop_pnl)


class TestItIsSemiLive:
    def test_the_live_quote_becomes_the_newest_point(self):
        s = series(live_price=4.61, live_at=NOW)
        assert s.last.live is True
        assert s.last.price == pytest.approx(4.61)

    def test_the_live_point_replaces_a_bar_rather_than_doubling_back(self):
        """Two points at the same moment make the line fold over
        itself, which reads as a price spike that never happened.

        THE FIXTURE MUST CONTAIN A BAR AT OR AFTER THE QUOTE, or there
        is nothing for the replacement to remove and the assertion is
        vacuous - which is exactly how the first version of this test
        let its sabotage come back GREEN (section 28's no-op lesson).
        """
        later = bars(n=8, first=OPEN, minutes=60)
        quote_at = pnl._as_dt(later[4]["t"])
        assert any(pnl._as_dt(b["t"]) >= quote_at for b in later), (
            "the fixture has no bar at or after the quote, so this test "
            "cannot exercise the replacement at all")
        s = series(bars=later, live_price=9.99, live_at=quote_at)
        assert s.last.live is True
        assert s.last.at == quote_at
        # Every earlier point is STRICTLY before it, and the bars that
        # sat at or after it are gone rather than drawn beside it.
        assert all(p.at < quote_at for p in s.points[:-1]), (
            [p.at.isoformat() for p in s.points])
        assert [p.at for p in s.points] == sorted(p.at for p in s.points)
        assert sum(1 for p in s.points if p.at == quote_at) == 1

    def test_with_no_quote_the_newest_bar_is_the_newest_point(self):
        s = series()
        assert s.last.live is False

    def test_a_closed_position_needs_no_live_point(self, monkeypatch):
        """Asking for a quote on a settled trade spends a request to
        learn nothing."""
        from catalyst.dashboard import live

        asked = []
        monkeypatch.setattr(live, "quotes_for",
                            lambda t, **kw: asked.append(t) or {})
        monkeypatch.setattr(live, "bars_for",
                            lambda *a, **kw: (bars(), ""))
        panels._pnl_series(Story(status="closed",
                                 closed_at=(NOW - timedelta(days=1))
                                 .isoformat()), now=NOW)
        assert asked == [], f"a closed position asked for a quote: {asked}"


class TestTheSignIsNeverCarriedByColour:
    """The palette rule this dashboard already measured: green against
    red is deltaE 4.1 under deuteranopia on the light surface, so it is
    not a distinction a reader can rely on."""

    def _svg(self, step):
        s = series(bars=bars(step=step),
                   marks=[{"at": OPEN.isoformat(), "kind": "entry",
                           "label": "Bought"}])
        return panels._pnl_chart(Story(), "tr", 0, series=s), s

    def test_the_line_does_not_change_colour_with_the_outcome(self):
        import re

        up, su = self._svg(+0.02)
        down, sd = self._svg(-0.02)
        assert su.last.pnl > 0 > sd.last.pnl, "the fixture does not win/lose"
        pick = lambda t: sorted(set(re.findall(r'class="(pnl-li[\w-]*)"', t)))
        assert pick(up) == pick(down), (pick(up), pick(down))

    def test_the_sign_is_carried_by_the_value_label(self):
        import re

        up, _ = self._svg(+0.02)
        down, _ = self._svg(-0.02)
        assert re.search(r'class="pnl-val">\+\$', up), "no + on a gain"
        assert re.search(r'class="pnl-val">-\$', down), "no - on a loss"

    def test_break_even_is_drawn_as_the_reference(self):
        up, _ = self._svg(+0.02)
        assert 'class="pnl-zero"' in up
        assert "break even" in up


class TestTheEventsAreOnTheChart:
    def test_every_mark_gets_a_rule_and_a_hover(self):
        marks = [{"at": (OPEN + timedelta(hours=i * 9)).isoformat(),
                  "kind": k, "label": k.title(), "detail": f"what {i}"}
                 for i, k in enumerate(("entry", "review", "news"))]
        s = series(marks=marks)
        svg = panels._pnl_chart(Story(), "tr", 0, series=s)
        assert svg.count('class="pnl-event') >= 3
        for m in marks:
            assert m["detail"] in svg, f"{m['detail']} has no hover text"

    def test_the_exit_rule_is_distinguishable_from_a_review(self):
        s = series(marks=[
            {"at": (OPEN + timedelta(hours=2)).isoformat(),
             "kind": "review", "label": "Review"},
            {"at": (OPEN + timedelta(hours=40)).isoformat(),
             "kind": "exit", "label": "Sold", "detail": "hard_exit_date"}])
        svg = panels._pnl_chart(Story(), "tr", 0, series=s)
        assert 'class="pnl-event-exit"' in svg
        assert 'class="pnl-event"' in svg

    def test_a_mark_outside_the_plotted_window_is_dropped_not_pinned(self):
        """Drawn at an edge it would read as having happened there."""
        s = series(marks=[{"at": (OPEN - timedelta(days=30)).isoformat(),
                           "kind": "news", "label": "News"}])
        assert s.marks == []

    def test_an_unreadable_timestamp_is_dropped_rather_than_guessed(self):
        s = series(marks=[{"at": "not-a-date", "kind": "news",
                           "label": "News"}])
        assert s.marks == []

    def test_an_unreadable_timestamp_does_not_become_NOW(self):
        """DEFENCE IN DEPTH, and the half only this guard holds. The
        window check also drops a mark stamped "now" whenever now falls
        outside the plotted range, so the behavioural test above passed
        with the parse guard broken. What must be true of the parser
        itself is that it refuses rather than substitutes a clock -
        a mark at the wrong moment is worse than one that is missing,
        because the reader cannot tell it is wrong.
        """
        for bad in ("not-a-date", "2026-13-45", "yesterday", "??"):
            assert pnl._as_dt(bad) is None, bad

    def test_an_unreadable_mark_INSIDE_the_window_is_still_dropped(self):
        """The same defect where the window cannot mask it: bars that
        straddle the present, so a mark stamped "now" would land inside
        the plotted range and be drawn."""
        straddle = bars(n=6, first=NOW - timedelta(hours=3), minutes=60)
        s = series(bars=straddle, opened=NOW - timedelta(hours=4),
                   marks=[{"at": "not-a-date", "kind": "news",
                           "label": "News"}])
        assert s.points, "the fixture drew nothing, so nothing was tested"
        assert s.points[0].at <= NOW <= s.points[-1].at, (
            "the window does not contain now, so it would mask the defect")
        assert s.marks == []

    def test_a_skipped_review_is_not_marked(self):
        """It cost nothing and decided nothing - the same rule the
        review-count sentence already uses."""
        when = (OPEN + timedelta(hours=5)).isoformat()
        st = Story(reviews=[(when, "hold", "", "", "", 0),
                            (when, "hold", "", "", "", 1)])
        kinds = [m["kind"] for m in panels._pnl_marks(st)]
        assert kinds.count("review") == 1, kinds

    def test_news_reaches_the_chart_at_all(self):
        """News is what brings a review forward and it appeared on the
        chart nowhere before this."""
        when = (OPEN + timedelta(hours=5)).isoformat()
        st = Story(sources=[("alpaca_news", "x", when, "Big headline",
                             "pub", "https://e.test")])
        marks = panels._pnl_marks(st)
        assert any(m["kind"] == "news" and m["detail"] == "Big headline"
                   for m in marks), marks


class TestTheGeometryIsMEASUREDNotEyeballed:
    """`svg_measure` resolves each label's font size from the CSS, so a
    chart that styles text by CLASS can be measured at all. The stock
    `charts.text_boxes` requires an inline font-size attribute and
    therefore reports ZERO text elements for this chart - a clean
    result and a broken one look identical."""

    def _svg(self, n_marks, spread_h):
        marks = [{"at": (OPEN + timedelta(hours=1)).isoformat(),
                  "kind": "entry", "label": "Bought"}]
        marks += [{"at": (OPEN + timedelta(hours=2 + i * spread_h))
                   .isoformat(), "kind": "review", "label": "Review",
                   "detail": f"r{i}"} for i in range(n_marks)]
        s = series(bars=bars(n=70), marks=marks)
        return panels._pnl_chart(Story(), "tr", 0, series=s), s

    @pytest.mark.parametrize("n,spread", [(0, 9), (1, 30), (3, 18),
                                          (6, 9), (12, 5), (40, 1)])
    def test_no_label_ever_overlaps_another(self, n, spread):
        from svg_measure import overlaps

        svg, _ = self._svg(n, spread)
        assert overlaps(svg) == [], (
            "labels collide - the 'picket fence' this lane exists to "
            f"prevent: {overlaps(svg)}")

    @pytest.mark.parametrize("n,spread", [(0, 9), (3, 18), (12, 5), (40, 1)])
    def test_no_label_ever_leaves_the_viewbox(self, n, spread):
        from svg_measure import outside_viewbox

        svg, _ = self._svg(n, spread)
        assert outside_viewbox(svg) == [], outside_viewbox(svg)

    def test_the_row_pitch_clears_the_measured_box_height(self):
        """Section 18's defect, which this change repeated once: a 12px
        stagger against a 12.35px measured box, so every adjacent pair
        collided. Both numbers now come from one place."""
        from catalyst.dashboard import charts

        box_h = panels.EVENT_FONT_PX * charts.LINE_H
        assert panels.EVENT_ROW_PX > box_h, (panels.EVENT_ROW_PX, box_h)

    def test_a_crowded_lane_says_how_many_it_could_not_label(self):
        """Showing fewer captions than there are events, silently, is
        how a busy position reads as a quiet one."""
        svg, s = self._svg(40, 1)
        assert "events marked" in svg
        assert str(len(s.marks)) in svg


class TestTheResolutionIsDerived:
    def test_a_short_hold_gets_minutes_and_a_long_one_does_not(self):
        assert pnl.timeframe_for(timedelta(minutes=40)) == "1Min"
        assert pnl.timeframe_for(timedelta(days=90)) in ("1Hour", "1Day")

    def test_the_point_count_never_exceeds_what_can_be_drawn(self):
        """More points than pixels is detail nobody can see, paid for
        in bandwidth."""
        per = dict(pnl.TIMEFRAMES)
        for days in (0.02, 1, 3, 10, 21, 60, 400):
            tf = pnl.timeframe_for(timedelta(days=days))
            sessions = days * pnl.SESSION_MINUTES * (5.0 / 7.0)
            assert sessions / per[tf] <= pnl.PLOT_PX + 1, (days, tf)

    def test_it_is_monotonic_in_the_hold_length(self):
        """A longer hold must never get a FINER resolution."""
        order = [n for n, _ in pnl.TIMEFRAMES]
        seen = [order.index(pnl.timeframe_for(timedelta(hours=h)))
                for h in (1, 6, 24, 72, 240, 2400, 24000)]
        assert seen == sorted(seen), seen


class TestWhatItRefusesToDraw:
    def test_a_short_is_refused_rather_than_sign_flipped(self):
        """The account is long-only, so the short branch has never been
        exercised - and a P&L chart with the sign backwards is worse
        than no chart."""
        s = series(side="short")
        assert s.points == []
        assert "long-only" in s.empty_reason

    def test_no_fill_draws_nothing_and_says_why(self):
        s = series(fill=None)
        assert s.points == []
        assert "fill price" in s.empty_reason

    def test_no_stop_draws_no_floor_rather_than_a_floor_at_zero(self):
        s = series(stop=None)
        assert s.stop_pnl is None
        svg = panels._pnl_chart(Story(), "tr", 0, series=s)
        assert 'class="pnl-floor"' not in svg
        assert 'class="pnl-risk"' not in svg
        assert 'class="pnl-zero"' in svg, "break even must still be drawn"

    def test_no_bars_renders_no_chart_at_all(self):
        """A chart with no series is not a chart (section 10b)."""
        s = series(bars=[])
        assert panels._pnl_chart(Story(), "tr", 0, series=s) == ""

    def test_a_bar_before_the_fill_is_not_plotted(self):
        """It would draw P&L on a position that did not exist, which on
        an intraday chart is most of the entry day."""
        early = bars(n=4, first=OPEN - timedelta(hours=4))
        s = series(bars=early + bars(n=4))
        assert all(p.at >= OPEN for p in s.points)


class TestTheReaderIsToldWhatItIsLookingAt:
    def _block(self, **over):
        s = series(**over)
        return panels._pnl_block(Story(), "tr", 0, series=s)

    def test_it_says_unrealised_and_that_fees_are_not_in_it(self):
        text = self._block(live_price=4.61, live_at=NOW)
        assert "unrealised" in text.lower()
        assert "no spread or fees" in text

    def test_it_names_the_floor_in_money(self):
        assert "-$39.45" in self._block()

    def test_it_says_which_source_drew_the_line(self):
        """Section 24: "Alpaca said" and "a file on disk said" are not
        the same claim."""
        s = series()
        s.source = "cached_daily_close"
        assert "cached daily closes" in panels._pnl_block(
            Story(), "tr", 0, series=s)
        s.source = "broker_intraday"
        assert "Alpaca" in panels._pnl_block(Story(), "tr", 0, series=s)

    def test_an_open_position_with_no_quote_says_NOT_LIVE_with_the_reason(
            self):
        s = series()
        s.quote_error = "HTTP 503 from the data API"
        block = panels._pnl_block(Story(status="open"), "tr", 0, series=s)
        assert "Not live" in block
        assert "HTTP 503" in block, "the upstream reason was swallowed"

    def test_an_empty_series_explains_itself_instead_of_drawing(self):
        block = panels._pnl_block(Story(), "tr", 0, series=series(bars=[]))
        assert "No profit-and-loss line" in block
        assert "no prices are on record" in block

    def test_the_caption_has_no_double_escaped_entity(self):
        """prov() escapes, so an entity written into it reaches the
        page as a literal. Fourth instance of that trap here."""
        block = self._block()
        assert "&amp;mdash;" not in block
        assert "&amp;" not in block.replace("&amp;mdash;", "")


class TestTheFetchIsSafe:
    def test_a_broker_that_raises_costs_no_chart_and_no_page(self):
        from catalyst.dashboard import live

        class Boom:
            def get_bars(self, *a, **kw):
                raise RuntimeError("data api down")

        got, why = live.bars_for("RLMD", "2026-09-01", "2026-09-14",
                                 "1Hour", broker=Boom(), use_cache=False)
        assert got is None
        assert "data api down" in why, why

    def test_a_non_list_response_is_refused_with_its_type(self):
        class Odd:
            def get_bars(self, *a, **kw):
                return {"bars": []}

        from catalyst.dashboard import live

        got, why = live.bars_for("RLMD", "a", "b", "1Hour", broker=Odd(),
                                 use_cache=False)
        assert got is None and "dict" in why, why

    def test_an_empty_answer_is_named_rather_than_silent(self):
        class Empty:
            def get_bars(self, *a, **kw):
                return []

        from catalyst.dashboard import live

        got, why = live.bars_for("RLMD", "a", "b", "1Hour", broker=Empty(),
                                 use_cache=False)
        assert got == [] and why, "an empty answer carried no reason"

    def test_the_series_falls_back_to_cached_closes_and_names_it(
            self, monkeypatch):
        from catalyst.dashboard import live

        class Bar:
            def __init__(self, day, close):
                self.day, self.close = day, Decimal(str(close))

        monkeypatch.setattr(live, "bars_for", lambda *a, **kw: (None, "down"))
        monkeypatch.setattr(live, "quotes_for", lambda *a, **kw: {})
        monkeypatch.setattr(
            panels, "_load_position_bars",
            lambda t: [Bar((OPEN + timedelta(days=i)).date(), 4.5 + i * 0.05)
                       for i in range(3)])
        s = panels._pnl_series(Story(), now=NOW)
        assert s.points, "the cached fallback produced nothing"
        assert s.source == "cached_daily_close"

    def test_nothing_here_can_size_spend_or_trade(self):
        from source_guard import source_matches

        for name in ("_pnl_series", "_pnl_chart", "bars_for",
                     "timeframe_for", "dashboard.pnl"):
            hits = source_matches(name, "catalyst/risk", "catalyst/execution",
                                  "catalyst/cost")
            assert hits == [], f"{name} reached the money path:\n{hits}"


class TestTheBrokerChangeIsAdditiveOnly:
    """`get_bars` was extracted from `get_daily_bars`, which now
    delegates. MONEY-CRITICAL file, so the property that matters is
    that the existing call is byte-for-byte what it was."""

    def test_get_daily_bars_still_asks_for_1Day(self):
        import httpx

        from catalyst.execution.broker import Broker

        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"bars": [{"t": "2026-09-01T00:00:00Z",
                                                       "c": 1.0}]})

        b = Broker("k", "s", transport=httpx.MockTransport(handler))
        b.get_daily_bars("RLMD", "2026-09-01", "2026-09-02")
        assert seen.get("timeframe") == "1Day", seen
        assert seen.get("adjustment") == "split", seen

    def test_get_bars_passes_the_timeframe_through(self):
        import httpx

        from catalyst.execution.broker import Broker

        seen = {}

        def handler(request):
            seen.update(dict(request.url.params))
            return httpx.Response(200, json={"bars": []})

        b = Broker("k", "s", transport=httpx.MockTransport(handler))
        b.get_bars("RLMD", "2026-09-01", "2026-09-02", timeframe="5Min")
        assert seen.get("timeframe") == "5Min", seen

    def test_both_follow_paging(self):
        """A silently shortened history means a smaller measured gap,
        which means a LARGER position - the one direction an error here
        must never go."""
        import httpx

        from catalyst.execution.broker import Broker

        pages = [
            {"bars": [{"t": "2026-09-01T00:00:00Z", "c": 1.0}],
             "next_page_token": "p2"},
            {"bars": [{"t": "2026-09-02T00:00:00Z", "c": 2.0}]},
        ]
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json=pages[min(len(calls) - 1,
                                                      len(pages) - 1)])

        b = Broker("k", "s", transport=httpx.MockTransport(handler))
        assert len(b.get_bars("X", "a", "b", timeframe="1Min")) == 2
        assert len(calls) == 2, "paging was not followed"
