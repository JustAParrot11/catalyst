"""The P&L chart says when, says what was banked, and updates itself.

OWNER-REPORTED 2026-09-15, with a screenshot of a closed trade:
*"this graph kind of makes sense, it is still confusing make clearer but
level of detail is good, there isnt a live continuously updating version
for an active trade however, only a past"*.

MEASURED BEFORE CHANGING ANYTHING, by rendering the owner's own case
rather than judging the picture by eye. Six defects, and the first is a
WRONG MONEY FIGURE:

1. `+$20.10 unrealised` against `+$11.95` actually banked - **$8.15
   out** - because the line ended at the last BAR and was labelled with
   that bar's mark-to-market. `exit_price` and `realized_pnl_cents` are
   both on `TradeStory` and neither reached the chart. And the word
   "unrealised" on a settled trade is simply false.
2. THE EXIT MARK WAS SILENTLY DROPPED whenever the sale settled after
   the final bar - the ordinary case, since a bar is stamped at the
   start of its interval. Section 10b calls the exit the single most
   important mark on a closed trade.
3. 67% OF THE WIDTH was one straight line drawn across hours when
   nothing traded. Alpaca returns session bars only, so an overnight gap
   is 17.5 hours of horizontal distance with no prices in it - which is
   what the owner was reading as price movement.
4. NO TIME AXIS AT ALL. Not one text element carried anything date- or
   time-like, so the picture could not say whether it spanned an
   afternoon or a fortnight.
5. FIVE MARKS CAPTIONED "Review", with hovers that all said "hold" -
   while Claude's actual reasoning sat at index 3 of the same tuple.
6. THE SHADED RISK BAND was named nowhere in any prose.

Plus the owner's own point: `refresh_seconds` existed with exactly one
caller (the detailed Overview), so this page fetched a live quote on
load and then sat still.
"""

import math
import re
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import panels, pnl
from catalyst.dashboard.queries import TradeStory

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FILL, QTY, STOP = 4.4949, 88.6812, 4.05

#: The owner's own trade, to the cent: what it sold at, and what the
#: broker actually banked. They differ from the last bar, which is the
#: whole point.
EXIT_PRICE = 4.6376
BANKED = 11.95


def session_bars(days=3, start=None):
    """REALISTIC bars: Alpaca returns the regular session only, so a
    multi-day hold has overnight holes in it. A continuous fixture
    cannot exercise the defect the owner reported (sections 14, 23, 24:
    a fixture that cannot produce the owner's state agrees with the
    bug)."""
    start = start or (NOW - timedelta(days=days)).replace(
        hour=13, minute=30, second=0, microsecond=0)
    out, i = [], 0
    for d in range(days):
        for k in range(13):                    # 6.5h of 30-minute bars
            at = start + timedelta(days=d, minutes=30 * k)
            # THE +0.03 IS LOAD-BEARING, not decoration. Without it the
            # first bar priced at exactly FILL, so its P&L was 0.00 -
            # identical to the purchase point's - and a sabotage drawing
            # the purchase at the first bar's value was a NO-OP against
            # this fixture (section 28). A real first bar after a fill
            # almost never matches it to the cent.
            out.append({"t": at.isoformat().replace("+00:00", "Z"),
                        "c": round(FILL + 0.03 + 0.12 * math.sin(i / 3.0)
                                   + 0.002 * i, 4)})
            i += 1
    return start, out


def closed(realised=BANKED, days=3):
    """A settled trade, built the way production builds one.

    THE FILL IS SEVEN MINUTES INTO THE SESSION, not exactly on a bar
    boundary. A fill lands whenever the order fills; a bar is stamped at
    the START of its interval. With the two coinciding there was no bar
    after the fill but before the first surviving one, so the purchase
    point was never inserted and the test written for it asserted
    against the first bar instead - green for the wrong reason.
    """
    session_start, bars = session_bars(days)
    opened = session_start + timedelta(minutes=7)
    sold_at = pnl._as_dt(bars[-1]["t"]) + timedelta(minutes=25)
    st = TradeStory(
        ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
        stop_price=str(STOP), status="closed", direction="long",
        opened_at=opened.isoformat(), closed_at=sold_at.isoformat(),
        exit_price=str(EXIT_PRICE), exit_reason="hard_exit",
        realized_pnl_cents=(int(round(realised * 100))
                            if realised is not None else None),
        reviews=[((opened + timedelta(hours=h)).isoformat(), "hold", False,
                  "The thesis is intact: no new filing, and the 50-day "
                  "average still sits above the entry.", [], None)
                 for h in (20, 27, 44, 51)])
    s = pnl.build(fill=FILL, qty=QTY, stop=STOP, bars=bars, opened=opened,
                  timeframe="30Min", marks=panels._pnl_marks(st),
                  exit_price=st.exit_price, exit_at=st.closed_at,
                  realised_pnl=(st.realized_pnl_cents / 100
                                if st.realized_pnl_cents is not None
                                else None))
    s.source = "broker_intraday"
    return st, s


def prose(block):
    got = re.search(r'<p class="trade-sum">(.*?)</p>', block, re.S)
    return re.sub(r"<[^>]+>", "", got.group(1)) if got else ""


def texts(svg, cls=None):
    pat = (rf'<text[^>]*class="{cls}"[^>]*>(.*?)</text>' if cls
           else r'<text[^>]*>(.*?)</text>')
    return [re.sub(r"<[^>]+>", "", t) for t in re.findall(pat, svg, re.S)]


class TestAClosedTradeShowsWhatWasActuallyBanked:
    """The defect that put a wrong number on a money chart."""

    def test_the_final_point_is_the_sale_not_the_last_bar(self):
        st, s = closed()
        assert s.last.final is True
        assert s.last.price == pytest.approx(EXIT_PRICE)
        # And it really is different from the last bar, or this test
        # would pass against the old behaviour too.
        bars_only = [p for p in s.points if not p.final]
        assert bars_only[-1].price != pytest.approx(EXIT_PRICE), (
            "the fixture's last bar equals the exit price, so this "
            "cannot distinguish the sale from the bar")

    def test_the_figure_shown_is_the_BROKERS_realised_one(self):
        st, s = closed()
        assert s.realised_is_broker is True
        assert s.last.pnl == pytest.approx(BANKED, abs=0.005)
        said = prose(panels._pnl_block(st, "t", 0, series=s))
        assert f"+${BANKED:,.2f}" in said, said
        assert "broker's own realised figure" in said, said

    def test_the_dots_HEIGHT_and_its_LABEL_come_from_one_number(self):
        """Sections 18 and 30 both paid for two numbers meaning the same
        thing. A dot drawn at the computed P&L and labelled with the
        broker's realised figure would be exactly that, on money."""
        st, s = closed()
        computed = (EXIT_PRICE - FILL) * QTY
        assert computed != pytest.approx(BANKED, abs=0.01), (
            "the fixture's fees are zero, so this cannot tell the two "
            "numbers apart")
        svg = panels._pnl_chart(st, "t", 0, series=s)
        assert f"+${BANKED:,.2f}" in " ".join(texts(svg, "pnl-val"))
        # The dot's y must be the realised value's y, not the computed
        # one's - checked through the series the chart was handed.
        assert s.last.pnl == pytest.approx(BANKED, abs=0.005)

    def test_with_no_realised_figure_it_says_so_and_computes(self):
        st, s = closed(realised=None)
        assert s.realised_is_broker is False
        assert s.last.pnl == pytest.approx((EXIT_PRICE - FILL) * QTY,
                                           abs=0.01)
        said = prose(panels._pnl_block(st, "t", 0, series=s))
        assert "No realised figure is on record" in said
        assert "carries no fees" in said

    def test_the_purchase_is_a_point_worth_exactly_nothing(self):
        """At the instant it was bought the position had made and lost
        nothing, so the line departs from break-even rather than
        starting partway up at an unexplained height.

        Asserted HERE as well as in the sibling file because the
        purchase point is what stops the "Bought" mark being dropped,
        and a sabotage drawing it at the first bar's value came back
        green for want of this assertion in this file's targets.
        """
        st, s = closed()
        assert s.points[0].at == pnl._as_dt(st.opened_at)
        assert s.points[0].pnl == 0.0, s.points[0]
        assert s.points[0].price == pytest.approx(FILL)
        # THE PREMISE, and my first version got it wrong: it checked the
        # SECOND bar, which was non-zero, while the first priced at
        # exactly the fill - so the assertion above could not tell the
        # purchase point from the bar beside it.
        bars_only = [p for p in s.points if not p.final and p.at != s.points[0].at]
        assert bars_only, s.points
        assert bars_only[0].pnl != 0.0, (
            "the first bar is worth exactly nothing, so this test cannot "
            "distinguish the purchase point from it")

    def test_the_word_unrealised_never_appears_on_a_settled_trade(self):
        st, s = closed()
        said = prose(panels._pnl_block(st, "t", 0, series=s))
        assert "unrealised" not in said.lower(), said
        assert "realised" in said.lower()

    def test_an_open_position_still_says_unrealised(self):
        """The distinction must cut both ways, or the fix has simply
        deleted the word rather than placing it correctly."""
        opened, bars = session_bars(3)
        st = TradeStory(ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
                        stop_price=str(STOP), status="open",
                        direction="long", opened_at=opened.isoformat())
        s = pnl.build(fill=FILL, qty=QTY, stop=STOP, bars=bars,
                      opened=opened, timeframe="30Min")
        s.source = "broker_intraday"
        said = prose(panels._pnl_block(st, "t", 0, series=s))
        assert "unrealised" in said.lower(), said
        # AND THE EXPLANATION, not just the word. Asserting the word
        # alone stayed GREEN when the trailing sentence was cut, because
        # the label at the front of the line supplies it too - so the
        # sentence that says what "unrealised" MEANS was untested.
        assert "nothing is banked until it closes" in said, said


class TestTheExitMarkCanNoLongerBeDropped:
    """Defect 2, and it was silent."""

    def test_a_sale_after_the_last_bar_is_still_marked(self):
        st, s = closed()
        labels = [m.label for m in s.marks]
        assert "Sold" in labels, labels
        # The premise: the sale really is after the final bar.
        last_bar = max(p.at for p in s.points if not p.final)
        assert pnl._as_dt(st.closed_at) > last_bar

    def test_the_purchase_before_the_first_bar_is_still_marked(self):
        """The same omission at the other end. A bar is stamped at the
        start of its interval, so the first one usually sits after the
        fill."""
        opened = (NOW - timedelta(days=2)).replace(hour=13, minute=0,
                                                   second=0, microsecond=0)
        _, bars = session_bars(2, start=opened + timedelta(minutes=45))
        st = TradeStory(ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
                        stop_price=str(STOP), status="open",
                        direction="long", opened_at=opened.isoformat())
        s = pnl.build(fill=FILL, qty=QTY, stop=STOP, bars=bars,
                      opened=opened, timeframe="30Min",
                      marks=panels._pnl_marks(st))
        assert pnl._as_dt(bars[0]["t"]) > opened, (
            "the fixture's first bar is not after the fill, so this "
            "cannot exercise the drop")
        assert "Bought" in [m.label for m in s.marks]


class TestTheLineBreaksWhereNothingTraded:
    """Defect 3: two thirds of the width was a straight line across a
    shut market."""

    def test_an_overnight_gap_splits_the_line(self):
        st, s = closed(days=3)
        assert len(s.segments) == 3, [len(r) for r in s.segments]
        assert len(s.gaps) == 2

    def test_no_segment_spans_longer_than_a_session(self):
        st, s = closed(days=5)
        limit = timedelta(minutes=pnl.SESSION_MINUTES)
        for run in s.segments:
            for a, b in zip(run, run[1:]):
                assert b.at - a.at <= limit, (a.at, b.at)

    def test_the_break_rule_is_the_session_length_already_in_the_module(self):
        """One number, not a second one to drift (sections 18, 30)."""
        assert pnl.SESSION_MINUTES == 390

    def test_a_daily_series_is_never_broken(self):
        """One point per trading day is what a daily series IS, so a
        weekend is not a gap in it."""
        day = (NOW - timedelta(days=20)).replace(hour=0, minute=0,
                                                 second=0, microsecond=0)
        bars = [{"t": (day + timedelta(days=d)).isoformat(),
                 "c": 4.5 + 0.01 * d} for d in (0, 1, 4, 5, 6, 11)]
        s = pnl.build(fill=FILL, qty=QTY, bars=bars, opened=day,
                      timeframe="1Day")
        assert len(s.segments) == 1
        assert s.gaps == []

    def test_the_closures_are_shaded_and_the_prose_says_what_they_are(self):
        st, s = closed(days=3)
        block = panels._pnl_block(st, "t", 0, series=s)
        assert block.count('class="pnl-shut"') == len(s.gaps)
        said = prose(block)
        assert "greyed column" in said
        assert "market was shut" in said

    def test_a_single_session_position_is_told_nothing_about_shading(self):
        """Section 18 paid three times for a sentence shown to a reader
        it did not apply to."""
        opened, bars = session_bars(1)
        st = TradeStory(ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
                        stop_price=str(STOP), status="open",
                        direction="long", opened_at=opened.isoformat())
        s = pnl.build(fill=FILL, qty=QTY, stop=STOP, bars=bars,
                      opened=opened, timeframe="30Min")
        assert s.gaps == []
        said = prose(panels._pnl_block(st, "t", 0, series=s))
        assert "greyed column" not in said, said


class TestThereIsATimeAxis:
    """Defect 4. Section 10b fixed exactly this on the PRICE chart and
    the P&L chart repeated it."""

    def test_the_chart_carries_dated_labels(self):
        st, s = closed()
        svg = panels._pnl_chart(st, "t", 0, series=s)
        axis = texts(svg, "pnl-axis-label")
        assert axis, "the chart has no time axis at all"
        assert all(re.search(r"\d", a) for a in axis), axis

    def test_no_label_is_printed_twice(self):
        """My own first render put "14 Sep" on the axis twice, because
        the final moment falls on a day a session tick already named."""
        for days in (1, 2, 3, 5, 10, 21):
            st, s = closed(days=days)
            axis = texts(panels._pnl_chart(st, "t", 0, series=s),
                         "pnl-axis-label")
            assert len(axis) == len(set(axis)), (days, axis)

    def test_a_same_day_position_is_labelled_by_the_CLOCK_not_the_date(self):
        """Three ticks all reading "15 Sep" would say nothing."""
        opened, bars = session_bars(1)
        st = TradeStory(ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
                        stop_price=str(STOP), status="open",
                        direction="long", opened_at=opened.isoformat())
        s = pnl.build(fill=FILL, qty=QTY, stop=STOP, bars=bars,
                      opened=opened, timeframe="30Min")
        axis = texts(panels._pnl_chart(st, "t", 0, series=s),
                     "pnl-axis-label")
        assert axis and all(":" in a for a in axis), axis

    def test_a_long_hold_is_not_labelled_once_per_session(self):
        """Twenty-one session starts measured as eleven dates across
        510px, which is clutter rather than an axis. The count is
        derived from the label width, so this asserts the OUTCOME."""
        st, s = closed(days=21)
        axis = texts(panels._pnl_chart(st, "t", 0, series=s),
                     "pnl-axis-label")
        assert len(s.segments) >= 20, len(s.segments)
        assert 2 <= len(axis) <= 6, axis

    def test_the_first_and_last_moments_always_keep_their_labels(self):
        """Thinning the axis must never drop the two a reader looks
        for."""
        st, s = closed(days=21)
        svg = panels._pnl_chart(st, "t", 0, series=s)
        axis = texts(svg, "pnl-axis-label")
        assert s.points[0].at.strftime("%-d %b") == axis[0], (axis)
        assert s.points[-1].at.strftime("%-d %b") == axis[-1], (axis)


class TestTheLabelsDoNotCollide:
    """The geometry, measured with this project's own tool rather than
    eyeballed - and my own first render collided in all 72 cases."""

    def _cases(self):
        for days in (1, 2, 3, 5, 10, 21):
            for closed_ in (False, True):
                st, s = closed(days=days)
                if not closed_:
                    st.status, st.closed_at = "open", ""
                    s.points = [p for p in s.points if not p.final]
                    s.marks = [m for m in s.marks if m.kind != "exit"]
                yield days, closed_, st, s

    def test_nothing_overlaps_and_nothing_leaves_the_viewbox(self):
        import svg_measure

        for days, closed_, st, s in self._cases():
            svg = panels._pnl_chart(st, "t", 0, series=s)
            assert svg, (days, closed_)
            over = svg_measure.overlaps(svg)
            out = svg_measure.outside_viewbox(svg)
            assert not over, (days, closed_, over)
            assert not out, (days, closed_, out)
            # AND NO AXIS LABEL MAY LEAVE THE PLOT. Measured: the edge
            # anchoring fires on 1168 of 595 probed shapes' labels, and
            # removing it broke nothing the viewBox check could see -
            # the label spills into the margin, not off the page. The
            # boundary this guard defends is the plot, so that is the
            # boundary to assert (sections 22, 24: assert the outcome
            # the code actually protects).
            # The axis labels are identified by their CLASS and matched
            # to their measured boxes by text - which is exact, because
            # the dedupe above guarantees those texts are unique. No y
            # heuristic, and no geometry re-implemented in the test
            # where it could agree with a bug.
            want = set(texts(svg, "pnl-axis-label"))
            assert want, (days, closed_, "no axis labels at all")
            seen = 0
            for bx in svg_measure.text_boxes(svg):
                if bx[4] not in want:
                    continue
                seen += 1
                assert bx[0] >= panels.PNL_PLOT_LEFT - 0.5, (
                    days, closed_, bx)
                assert bx[2] <= panels.PNL_PLOT_RIGHT + 0.5, (
                    days, closed_, bx)
            assert seen == len(want), (days, closed_, seen, want)

    def test_the_event_lane_clears_the_axis_band_by_construction(self):
        """Sections 18 and 30 both cost a typed gap disagreeing with a
        measured box height. This asserts the relationship, not a
        number."""
        assert panels.EVENT_LANE_TOP > panels.AXIS_BAND_PX + \
            panels.EVENT_FONT_PX, (
            panels.EVENT_LANE_TOP, panels.AXIS_BAND_PX)


class TestAReviewsHoverSaysWhatClaudeSaid:
    """Defect 5: five captions reading "Review" whose hovers all said
    "hold", while the reasoning sat in the same tuple."""

    def test_the_hover_carries_the_date_and_the_reasoning(self):
        st, s = closed()
        hovers = [m.detail for m in s.marks if m.label == "Review"]
        assert hovers
        for h in hovers:
            assert re.search(r"\d", h), h
            assert "50-day average" in h, h

    def test_the_hovers_are_not_all_identical(self):
        """What made five captions unreadable was that nothing could
        tell them apart."""
        st, s = closed()
        hovers = [m.detail for m in s.marks if m.label == "Review"]
        assert len(set(hovers)) > 1, hovers

    def test_a_review_with_no_reasoning_still_says_when(self):
        opened, bars = session_bars(3)
        st = TradeStory(
            ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
            stop_price=str(STOP), status="open", direction="long",
            opened_at=opened.isoformat(),
            reviews=[((opened + timedelta(hours=20)).isoformat(), "hold",
                      False, "", [], None)])
        marks = panels._pnl_marks(st)
        got = [m for m in marks if m["label"] == "Review"]
        assert got and re.search(r"\d", got[0]["detail"]), got

    def test_a_long_reasoning_is_cut_and_says_so(self):
        opened, _ = session_bars(3)
        long = "x" * (panels.REVIEW_HOVER_CHARS + 50)
        st = TradeStory(
            ticker="RLMD", entry_price=str(FILL), qty=str(QTY),
            status="open", direction="long", opened_at=opened.isoformat(),
            reviews=[((opened + timedelta(hours=20)).isoformat(), "hold",
                      False, long, [], None)])
        got = [m for m in panels._pnl_marks(st) if m["label"] == "Review"]
        assert got[0]["detail"].endswith("...")
        assert len(got[0]["detail"]) < len(long)


class TestTheRiskBandIsNamed:
    """Defect 6, and it must be named by POSITION, never by colour -
    there are two shaded regions now and a reader may not be able to
    use the difference."""

    def test_the_prose_says_what_the_band_between_the_rules_is(self):
        st, s = closed()
        said = prose(panels._pnl_block(st, "t", 0, series=s))
        assert "band between break-even" in said, said
        assert "-$39.45" in said or "$39.45" in said, said

    def test_the_band_is_not_identified_by_its_colour(self):
        st, s = closed()
        said = prose(panels._pnl_block(st, "t", 0, series=s)).lower()
        for hue in ("red", "pink", "the red band"):
            assert hue not in said, (hue, said)


class TestThePageRefreshesItselfOnlyWhileSomethingIsLive:
    """The owner's own point: `refresh_seconds` existed with exactly one
    caller, so this page never went back for a new quote."""

    def _db(self, tmp_path, name, open_positions):
        import sqlite3

        from catalyst.dashboard.queries import Db
        from catalyst.storage import init_db

        conn = init_db(str(tmp_path / name))
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        row = ("[]", NOW.isoformat(),
               (NOW + timedelta(days=14)).date().isoformat())
        for i in range(open_positions):
            conn.execute(
                "INSERT INTO positions (id, ticker, entry_order_ids, "
                "opened_at, planned_exit_date, status) VALUES (?,?,?,?,?,?)",
                (f"p{i}", "RLMD") + row + ("open",))
        # A CLOSED ONE IS ALWAYS PRESENT, so "some position exists" can
        # never be mistaken for "a position is open".
        conn.execute(
            "INSERT INTO positions (id, ticker, entry_order_ids, "
            "opened_at, planned_exit_date, status) VALUES (?,?,?,?,?,?)",
            ("pc", "EMBC") + row + ("closed",))
        conn.commit()
        conn.close()
        return Db(str(tmp_path / name))

    def test_an_open_position_makes_the_page_refresh(self, tmp_path):
        from catalyst.dashboard import server

        db = self._db(tmp_path, "open.db", 1)
        assert server._any_position_open(db) is True

    def test_a_page_of_closed_trades_does_not_refresh(self, tmp_path):
        """A page that reloads under the reader fights them for the
        scroll position, and on a settled trade nothing can change."""
        from catalyst.dashboard import server

        db = self._db(tmp_path, "shut.db", 0)
        assert server._any_position_open(db) is False

    def test_a_broken_count_costs_the_refresh_not_the_page(self, tmp_path):
        from catalyst.dashboard import server
        from catalyst.dashboard.queries import Db

        assert server._any_position_open(
            Db(str(tmp_path / "nope.db"))) is False

    def test_the_route_actually_passes_it(self):
        """Assert the CALL SITE, not just the helper - sections 12, 22
        and 31 all had a helper nobody called pass its own tests."""
        import inspect

        from catalyst.dashboard import server

        src = inspect.getsource(server.route_trades)
        assert "_any_position_open" in src
        assert "refresh_seconds" in src

    def test_the_refresh_reaches_the_rendered_page(self, tmp_path):
        """And the OUTCOME, because a substring in a function is not a
        behaviour (sections 22, 24)."""
        from catalyst.dashboard import server

        live_db = self._db(tmp_path, "live2.db", 1)
        shut_db = self._db(tmp_path, "shut2.db", 0)
        hot = server.route_trades(live_db, {})
        cold = server.route_trades(shut_db, {})
        assert 'http-equiv="refresh"' in hot
        assert 'http-equiv="refresh"' not in cold


class TestNothingHereCanSizeSpendOrTrade:
    def test_the_new_symbols_are_absent_from_the_money_path(self):
        from source_guard import source_matches

        for name in ("segments", "pnl-shut", "realised_is_broker",
                     "_any_position_open", "AXIS_BAND_PX"):
            for where in ("catalyst/risk", "catalyst/execution",
                          "catalyst/cost"):
                assert not source_matches(name, where), (name, where)


class TestTheExitReasonIsWordsEverywhere:
    """It was a dict literal inside `_trade_summary`, so the chart's own
    hover read `Sold: hard_exit`. An existing test caught it."""

    def test_the_two_reasons_production_actually_writes_read_as_words(self):
        """`reconcile.py` writes exactly these two and nothing else."""
        assert panels._exit_words("hard_exit") == "the clock ran out"
        assert panels._exit_words("stop") == "the stop was hit"

    def test_an_unlisted_reason_is_de_underscored_not_printed_raw(self):
        """House rule 7: the fallback is a rule, so the first reason
        nobody listed still reads as words."""
        assert panels._exit_words("some_new_reason") == "some new reason"
        assert "_" not in panels._exit_words("a_b_c")

    def test_a_missing_reason_says_so_rather_than_rendering_blank(self):
        for empty in ("", None, "   "):
            assert panels._exit_words(empty) == "no reason was recorded"

    def test_the_charts_exit_hover_uses_it(self):
        st, s = closed()
        sold = [m for m in s.marks if m.kind == "exit"]
        assert sold, [m.kind for m in s.marks]
        assert "the clock ran out" in sold[0].detail
        assert "hard_exit" not in sold[0].detail

    def test_the_summary_uses_THE_SAME_helper(self):
        """One source for the words, or the two callers drift - which is
        exactly how the hover came to show an enum."""
        import inspect

        src = inspect.getsource(panels._trade_summary)
        assert "_exit_words" in src
        assert '"hard_exit":' not in src, (
            "the words are inline again, so a second caller cannot "
            "reach them")
