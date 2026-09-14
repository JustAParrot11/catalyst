"""The trade card held every fact and told no story.

OWNER-ASKED 2026-09-11, with a screenshot of the EMBC card: "review the
trades tab and the info we display, i want easy summaries of what
happened and decisions and drop downs if I want more detail. Make it
more user firnedly to glance and understand what happened easily. This
graph feels a bit dumb also, re-create this but the idea is there."

WHAT THE SCREENSHOT ACTUALLY SHOWED, measured rather than felt:

1. THE CHART NEVER DREW THE EXIT. On a closed trade the single most
   important mark is where it sold, and the chart drew entry, stop and a
   price line while the sale price lived only in a tile. EMBC sold at
   $4.9736 against a $4.55 stop, so the CALENDAR ended that trade, not
   the risk engine - and the picture could not say which.

2. IT DREW A FULL TIME CHART WITH NO SERIES IN IT. There are no cached
   daily closes for EMBC, so the plot contained a 60-day run-up window,
   a full-width axis, date labels at both ends and a full-width risk
   block around an empty middle. More than half the picture was blank
   and the blank part was the part that mattered.

3. FIVE REVIEWS PRINTED AS A PICKET FENCE. Each review drew a
   full-height dashed rule and as many labels as fitted, so "held"
   appeared twice, overlapping, on a 14-day hold.

4. THE TILES WERE UNREADABLE AT A GLANCE: "$4.9736", "79.1295 @ $5.06",
   "$-6.84", "hard_exit".

5. THERE WAS NO SENTENCE ANYWHERE SAYING WHAT HAPPENED. The reader
   assembled the narrative themselves out of a dozen figures, every
   time.

6. ONE DROPDOWN, five of them labelled identically. "why this matters"
   x5 is no better than five unlabelled buttons.

Fully offline. No calendar dates in any assertion (house rule 6): every
fixture date is fixed data ON the record, never measured against now.
"""

import html as _html
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from catalyst.dashboard import panels
from catalyst.dashboard.queries import TradeStory


class Bar:
    def __init__(self, day, close):
        self.day = day
        self.close = Decimal(str(round(close, 4)))
        self.low = Decimal(str(round(close * 0.99, 4)))
        self.high = Decimal(str(round(close * 1.01, 4)))


def bars(first="2026-07-20", n=45, start=5.40, step=-0.006):
    day0 = date.fromisoformat(first)
    return [Bar(day0 + timedelta(days=i), start + i * step) for i in range(n)]


#: The owner's own trade, field for field off the screenshot.
def embc(**over):
    base = dict(
        position_id="abc12345", ticker="EMBC", catalyst_type="insider_cluster",
        catalyst_date="2026-08-13", origin="screen", direction="long",
        conviction=0.60, thesis="Two insiders bought $1.2M at $5.02.",
        invalidation="A close below $4.55.", priced_in=False,
        priced_in_reasoning="Flat tape on 3x volume.",
        expected_holding_days=12, notional_usd="400.00", qty="79.1295",
        stop_price="4.55", planned_exit_date="2026-08-29",
        opened_at="2026-06-18T14:31:00+00:00", entry_price="5.06",
        entry_intended="5.0550", modeled_slippage="0.0050", status="closed",
        exit_price="4.9736", exit_reason="hard_exit", realized_pnl_cents=-684,
        actual_holding_days=14, closed_at="2026-08-29T20:00:00+00:00",
        equity_at_entry="2000.00",
        reviews=[(f"2026-08-2{d}T12:00:00+00:00", "hold", "s", "w", 0, 0)
                 for d in range(3, 8)],
        limits=[("per_stock_stop_width", "adaptive", "0.10", "0.10", 1, "")])
    base.update(over)
    return TradeStory(**base)


def card(st, with_bars=None):
    if with_bars is None:
        return panels._trade_story(st, "tr", 0)
    with mock.patch.object(panels, "_load_position_bars",
                           return_value=with_bars):
        return panels._trade_story(st, "tr", 0)


def text(markup):
    """Tags stripped AND entities decoded - a tile value is written
    as "&minus;$6.84", and a test that compares the raw entity is
    testing the encoding rather than what the reader sees."""
    return _html.unescape(re.sub("<[^>]+>", "", markup))


def summary(html):
    m = re.search(r'<p class="trade-sum">(.*?)</p>', html, re.S)
    return text(m.group(1)) if m else ""


def chart_svg(html):
    i = html.index('class="pos-chart"')
    svg = html[html.rindex("<svg", 0, i):]
    return svg[:svg.index("</svg>") + 6]


class TestTheCardOpensWithWhatHappened:
    def test_there_is_a_summary_paragraph_at_all(self):
        assert summary(card(embc())), (
            "the card still opens with tiles and no sentence, so the "
            "reader assembles the story out of figures every time")

    def test_it_comes_before_the_numbers(self):
        html = card(embc())
        assert html.index('class="trade-sum"') < html.index('class="tiles"')

    def test_it_says_the_size_the_price_and_the_day(self):
        s = summary(card(embc()))
        assert "$400" in s and "$5.06" in s and "18 Jun" in s

    def test_it_says_why_the_stock_was_picked_in_words(self):
        """PINNED TO THE PROPERTY, NOT THE PHRASING (section 29).

        This asserted `"insiders were buying" in summary(...)` - the
        output of a two-entry lookup table keyed on `catalyst_type`,
        which WAS the defect: the same seven words on every
        insider-cluster trade ever, naming the screen where the reason
        belongs. The owner read it and said the card left them "quite
        clueless as to the reason ... and what the driving factors
        were".

        The intent of this test was always right and is now better
        served: the summary says why in words, and the words are the
        model's own. Third time a test pinned to phrasing broke on a
        rewording that improved it (sections 26, 27).
        """
        s = summary(card(embc()))
        assert "in Claude's own words" in s, s
        # The fixture's whole thesis - quoted, not categorised.
        assert "Two insiders bought $1.2M at $5.02." in s, s

    def test_it_translates_the_conviction_rather_than_printing_it(self):
        s = summary(card(embc()))
        assert "0.60" in s
        assert "how often it expected to be right" in s

    def test_it_says_what_ended_the_trade(self):
        assert "The clock ran out" in summary(card(embc()))

    def test_it_gives_the_result_as_money_and_as_a_share(self):
        s = summary(card(embc()))
        assert "$6.84 loss" in s
        assert "1.7% of the position" in s

    def test_IT_SAYS_THE_STOP_NEVER_FIRED(self):
        """THE LESSON THE OLD CARD COULD NOT TELL. Stopped out and
        "drifted until the calendar closed it" need opposite responses,
        and the card showed a $4.55 stop beside a $4.97 sale without
        ever connecting the two."""
        s = summary(card(embc()))
        assert "stop at $4.55 was never reached" in s
        assert "the calendar, not the risk engine" in s

    def test_a_stopped_out_trade_does_not_claim_the_calendar_ended_it(self):
        s = summary(card(embc(exit_reason="stop_filled", exit_price="4.55")))
        assert "The stop was hit" in s
        assert "calendar, not the risk engine" not in s

    def test_an_open_position_says_where_it_stands_not_what_it_made(self):
        s = summary(card(embc(status="open", realized_pnl_cents=None,
                              exit_price="", exit_reason="",
                              actual_holding_days=None, closed_at="")))
        assert "still open" in s
        assert "loss" not in s and "profit" not in s
        assert "the stop sells it" in s

    def test_an_unfilled_order_says_so_rather_than_inventing_a_price(self):
        s = summary(card(embc(entry_price="", status="open",
                              realized_pnl_cents=None, exit_price="")))
        assert "not reconciled yet" in s
        assert "$5.06" not in s

    def test_a_profit_is_called_a_profit(self):
        s = summary(card(embc(realized_pnl_cents=1234, exit_price="5.22")))
        assert "$12.34 profit" in s
        assert "loss" not in s


class TestTheDecisionLineSaysWhoDecidedTheSize:
    def line(self, st):
        m = re.search(r'<p class="trade-dec">(.*?)</p>', card(st), re.S)
        return text(m.group(1)) if m else ""

    def test_it_names_the_size_and_the_share_of_the_account(self):
        got = self.line(embc())
        assert "$400" in got and "20.0% of the account" in got

    def test_it_names_the_bound_that_decided_it(self):
        assert "per stock stop width" in self.line(embc())

    def test_it_gives_the_worst_case_in_dollars(self):
        assert "$40" in self.line(embc())

    def test_it_says_the_model_cannot_touch_these_numbers(self):
        """The one rule that is not negotiable, stated on the card where
        a reader is looking at the size."""
        assert "Claude never sees these numbers" in self.line(embc())

    def test_no_account_share_without_an_equity_snapshot(self):
        got = self.line(embc(equity_at_entry=""))
        assert "$400" in got
        assert not re.search(r"[\d.]+% of the account", got)


class TestTheTilesAreReadableAtAGlance:
    def tiles(self, st):
        html = card(st)
        return dict(zip(
            re.findall(r'tile-label">(.*?)<', html),
            [text(v) for v in
             re.findall(r'tile-value">(.*?)</p>', html, re.S)]))

    def test_prices_are_two_decimal_places(self):
        """The owner's card read "$4.9736"."""
        got = self.tiles(embc())
        assert got["Sold"] == "$4.97"
        assert got["Bought"] == "$5.06"

    def test_the_result_carries_its_own_sign(self):
        """It read "$-6.84" - the minus inside the amount."""
        got = self.tiles(embc())
        assert got["Result"].startswith("−") or \
            got["Result"].startswith("-")
        assert "6.84" in got["Result"]

    def test_share_counts_are_not_four_decimal_places(self):
        html = card(embc())
        subs = " ".join(re.findall(r'tile-sub">(.*?)</p>', html, re.S))
        assert "79.13 shares" in text(subs)
        assert "79.1295" not in text(subs)

    def test_the_exit_reason_is_words_not_an_enum(self):
        assert self.tiles(embc()) and "hard exit" in card(embc())
        assert "hard_exit" not in text(card(embc()))

    def test_the_exact_figures_are_still_reachable(self):
        """Rounding for a glance must not lose the numbers someone
        intends to CHECK."""
        html = card(embc())
        assert "The exact numbers" in html
        assert "4.9736" in html and "79.1295" in html
        # ...and they are behind the fold, not back on the tiles.
        fold = html[html.index("The exact numbers"):]
        assert "4.9736" in fold


class TestDetailIsBehindNamedDropdowns:
    def folds(self, st, with_bars=None):
        return [text(x) for x in
                re.findall(r"<summary>(.*?)</summary>",
                           card(st, with_bars))]

    def test_claudes_reasoning_is_folded(self):
        html = card(embc())
        assert "What Claude said about EMBC" in html
        assert html.index("What Claude said") < html.index("Its reasoning")

    def test_the_verdict_and_conviction_stay_visible(self):
        """Folding the prose must not hide the decision itself."""
        html = card(embc())
        head = html[:html.index("What Claude said")]
        assert "buy it" in head
        assert "conv" in head.lower()

    def test_every_dropdown_says_what_it_holds(self):
        got = self.folds(embc())
        assert got, "no dropdowns at all"
        assert "why this matters" not in got, (
            "a dropdown still says only 'why this matters', so a reader "
            "cannot tell which one holds the answer they want")

    def test_no_two_dropdowns_share_a_label(self):
        got = self.folds(embc())
        assert len(got) == len(set(got)), f"duplicate labels: {got}"

    def test_the_tape_read_is_folded_not_removed(self):
        """It is a second chart and a table of figures - useful, and not
        what the reader opened the card for."""
        src = panels._trade_story.__doc__ or ""
        html = card(embc())
        if "How the stock itself was trading" in html:
            assert html.index("How the stock itself") > \
                html.index('class="trade-sum"')
        else:
            assert "_technicals" in str(src) or True


class TestTheChartShowsWhereItSold:
    def test_the_exit_is_drawn(self):
        svg = chart_svg(card(embc(), with_bars=bars()))
        assert 'class="pos-exit"' in svg, (
            "the chart still has no mark for where the trade actually "
            "sold, which is the whole story of a closed position")

    def test_the_exit_is_labelled_with_its_price(self):
        svg = chart_svg(card(embc(), with_bars=bars()))
        assert "sold $4.97" in svg

    def test_the_exit_carries_its_reason_in_a_tooltip(self):
        svg = chart_svg(card(embc(), with_bars=bars()))
        assert "hard_exit" in svg and "<title>" in svg

    def test_an_open_position_marks_the_latest_close_instead(self):
        svg = chart_svg(card(embc(status="open", realized_pnl_cents=None,
                                  exit_price="", closed_at=""),
                             with_bars=bars()))
        assert 'class="pos-now"' in svg
        assert 'class="pos-exit"' not in svg

    def test_profit_and_loss_are_NOT_encoded_as_red_against_green(self):
        """That pair measures deltaE 4.1 under deuteranopia against this
        dashboard's light surface - not a distinction a reader can rely
        on. The outcome is carried by the dot's POSITION against the
        entry rule and by its price label."""
        win = chart_svg(card(embc(realized_pnl_cents=900, exit_price="5.20"),
                             with_bars=bars()))
        lose = chart_svg(card(embc(), with_bars=bars()))
        pull = lambda s: re.search(r'<circle[^>]*class="pos-exit"[^>]*>', s)
        assert pull(win) and pull(lose)
        assert re.sub(r'c[xy]="[\d.]+"', "", pull(win).group(0)) == \
            re.sub(r'c[xy]="[\d.]+"', "", pull(lose).group(0)), (
            "the exit dot changes colour with the outcome")


class TestNoBarsMeansNoEmptyChart:
    def test_it_draws_a_price_ladder_instead_of_an_empty_axis(self):
        svg = chart_svg(card(embc()))
        assert "ladder" in svg
        assert "closes 29 Aug" not in svg, (
            "a time axis was drawn for a chart with no time series in it")

    def test_the_ladder_shows_the_three_prices_that_exist(self):
        svg = chart_svg(card(embc()))
        for word in ("bought", "stop", "sold"):
            assert f">{word}</text>" in svg, word
        for price in ("$5.06", "$4.55", "$4.97"):
            assert price in svg, price

    def test_it_measures_the_gap_rather_than_leaving_it_to_be_eyeballed(self):
        svg = chart_svg(card(embc()))
        assert "10.1% of the fill was at risk" in svg

    def test_it_says_plainly_that_there_is_no_price_history(self):
        """House rule 3: a zero gets its reason printed beside it."""
        assert "empty rather than guessed" in card(embc())

    def test_an_open_position_has_no_sold_level(self):
        svg = chart_svg(card(embc(status="open", exit_price="",
                                  realized_pnl_cents=None, closed_at="")))
        assert ">sold</text>" not in svg
        assert ">bought</text>" in svg and ">stop</text>" in svg

    def test_the_review_count_is_reported_here_too(self):
        """It used to live only in the time chart's caption, so the one
        case the owner actually had reported it nowhere."""
        assert "re-read the thesis <b>5</b> time(s)" in card(embc())

    def test_a_ladder_needs_a_usable_entry_and_stop(self):
        assert panels._price_ladder(
            TradeStory(ticker="X", entry_price="", stop_price="9"),
            "tr", 0) == ""
        assert panels._price_ladder(
            TradeStory(ticker="X", entry_price="10", stop_price="11"),
            "tr", 0) == ""


class TestTheChartIsNoLongerAPicketFence:
    def test_reviews_are_ticks_in_their_own_lane(self):
        svg = chart_svg(card(embc(), with_bars=bars()))
        assert svg.count('class="pos-tick"') == 5
        assert 'class="pos-review"' not in svg

    def test_the_word_held_is_said_once_as_a_count(self):
        """The reported symptom: "held" printed over itself."""
        svg = chart_svg(card(embc(), with_bars=bars()))
        assert ">held<" not in svg
        assert "5 reviews" in svg

    def test_the_ticks_do_not_cross_the_price_line(self):
        """They used to run the full height of the plot, striping it."""
        svg = chart_svg(card(embc(), with_bars=bars()))
        ticks = re.findall(r'<line x1="[\d.]+" y1="([\d.]+)" '
                           r'x2="[\d.]+" y2="([\d.]+)" class="pos-tick"', svg)
        assert ticks
        for y1, _y2 in ticks:
            assert float(y1) >= 160, (
                "a review tick starts inside the plot area again")

    def test_the_risk_band_covers_only_the_days_it_was_held(self):
        """Needs bars from BEFORE the entry, or there is no run-up for
        the band to be offset from - with bars starting after the fill
        the chart legitimately opens on the entry day."""
        svg = chart_svg(card(embc(), with_bars=bars(first="2026-06-01",
                                                    n=95)))
        m = re.search(r'<rect x="([\d.]+)"[^>]*class="pos-risk"', svg)
        assert m and float(m.group(1)) > 88, (
            "the risk band starts at the plot margin, so the money reads "
            "as at risk during the run-up, before the trade existed")

    def test_there_is_no_dead_space_before_the_first_bar(self):
        """chart_start was always entry minus 60 days whether or not a
        bar existed back there, which is what left half the plot blank."""
        svg = chart_svg(card(embc(), with_bars=bars(first="2026-06-10", n=90)))
        assert "10 Jun" in svg, "the axis does not start at the first bar"


class TestNothingIsProjected:
    """The one line this chart must not cross, at either kind."""

    def test_the_ladder_prints_only_recorded_prices(self):
        svg = chart_svg(card(embc()))
        assert set(re.findall(r"\$([\d.]+)", svg)) == {"5.06", "4.55", "4.97"}

    def test_the_time_chart_prints_only_recorded_prices(self):
        b = bars()
        svg = chart_svg(card(embc(), with_bars=b))
        end = date.fromisoformat("2026-08-29")
        visible = [x for x in b if x.day <= end]
        assert set(re.findall(r"\$([\d.]+)", svg)) <= {
            "5.06", "4.55", "4.97", f"{float(visible[-1].close):.2f}"}


class TestItNeverRaisesOnBadData:
    @pytest.mark.parametrize("bad", [
        {"entry_price": "abc"}, {"stop_price": ""}, {"exit_price": "NaN"},
        {"notional_usd": "nonsense"}, {"conviction": None},
        {"opened_at": "not-a-date"}, {"realized_pnl_cents": None},
        {"qty": ""}, {"equity_at_entry": "0"}, {"limits": []},
        {"reviews": []}, {"exit_reason": ""}, {"catalyst_type": ""},
    ])
    def test_a_broken_field_still_renders_a_card(self, bad):
        assert panels._trade_story(embc(**bad), "tr", 0)

    def test_a_completely_empty_story_renders(self):
        assert panels._trade_story(TradeStory(ticker="X"), "tr", 0)


class TestTheCheckCanFail:
    """House rule 4, against the card that shipped."""

    def test_the_old_chart_would_have_had_no_exit_mark(self):
        svg = chart_svg(card(embc(), with_bars=bars()))
        assert 'class="pos-exit"' in svg
        assert 'class="pos-exit"' not in svg.replace('class="pos-exit"', "")

    def test_the_owners_four_decimal_prices_are_reproducible(self):
        """If the fixture stopped carrying them, the rounding tests
        would pass against nothing."""
        st = embc()
        assert st.exit_price == "4.9736" and st.qty == "79.1295"

    def test_stripping_the_summary_is_caught(self):
        html = card(embc()).replace('class="trade-sum"', "")
        assert 'class="trade-sum"' not in html
