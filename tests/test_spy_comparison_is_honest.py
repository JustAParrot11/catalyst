"""The SPY comparison must not flatter the bot.

OWNER-REPORTED 2026-08-21: "the S&p graph, ensure it is 100% accurate, i
dont want the false idea we are beating SPY, also still dont understand
what beating it by 0.89pp means, can you just show a percentage symbol
equivilant".

TWO FAULTS, and the first was the page contradicting itself.

The headline tile said "exposure-matched". A provenance line said
"Exposure is NOT matched" - and section() sweeps every provenance line
into a disclosure at the FOOT of the panel. So the false half was the
prominent half, and the true half was one click away at the bottom.

Why it matters here rather than in general: this account holds one or
two positions and is otherwise cash. A mostly-cash account falls less
than a fully invested index in EVERY down market. On the owner's own
chart the bot was -0.28% against SPY's -1.39%, which reads as skill and
is very largely just not being in the market. That is the "false idea"
in one number.

It cannot be corrected, only stated: matching exposure properly needs a
daily position-value series nothing writes yet, and inventing one from
today's figure would look like a measurement.
"""

import pathlib
import re
from datetime import date
from decimal import Decimal

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import QueryResult
from catalyst.dashboard.render import signed_pct, signed_pp


def perf(bot=99.72, spy=98.61, start=Decimal("200000")):
    empty = QueryResult("", (), [], None)
    p = queries.Performance(closed_q=empty, costs_q=empty)
    p.bot_points = [(date(2026, 8, 17), 100.0), (date(2026, 8, 21), bot)]
    p.spy_points = [(date(2026, 8, 17), 100.0), (date(2026, 8, 21), spy)]
    p.start_day, p.end_day = date(2026, 8, 17), date(2026, 8, 21)
    p.n_closed = 1
    return p


class TestTheFalseClaimIsGone:
    def test_the_page_never_says_exposure_matched(self, tmp_path):
        """It was not true, and it was the loudest thing on the tile.

        Asserted on the RENDERED page rather than the source, so the
        explanation of the bug is allowed to name it."""
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        from test_trades_page import _seed

        from catalyst.dashboard.db import Db

        db = Db(_seed(tmp_path, closed=True))
        try:
            html = panels.performance_panel(db, p="perf")
        finally:
            db.close()
        assert "exposure-matched" not in html, (
            "the page claims exposure-matching again")

    def test_the_pointer_is_not_promised_without_the_warning(self, tmp_path):
        """The tile says "see the exposure warning below" - which must
        not appear on a page that has no SPY series and therefore no
        warning. Sending a reader after a paragraph that is not there is
        its own small dishonesty."""
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).parent))
        from test_trades_page import _seed

        from catalyst.dashboard.db import Db

        db = Db(_seed(tmp_path, closed=True))
        try:
            html = panels.performance_panel(db, p="perf")
        finally:
            db.close()
        if "exposure warning below" in html:
            assert "not like for like" in html

    def test_the_warning_is_NOT_a_provenance_line(self):
        """section() lifts every <p class="prov"> into a fold at the
        foot of the panel. A condition on how a number may be READ does
        not belong down there - that is where it was, under a tile
        saying the opposite."""
        html = panels._exposure_warning(perf(), "p")
        assert 'class="prov"' not in html
        assert 'class="caveat"' in html

    def test_it_says_plainly_the_lines_are_not_comparable(self):
        html = panels._exposure_warning(perf(), "p")
        assert "not like for like" in html
        assert "fully invested" in html

    def test_it_names_the_direction_of_the_distortion_BOTH_ways(self):
        """"falls less in a down market" alone would read as an excuse
        for a bad number. It cuts the other way too and must say so."""
        html = panels._exposure_warning(perf(), "p")
        assert "falls less" in html and "rises less" in html

    def test_it_does_not_invent_an_exposure_matched_figure(self):
        """The honest limit. A corrected number built from today's
        exposure would look like a measurement of the whole window."""
        html = panels._exposure_warning(perf(), "p")
        assert "nothing writes yet" in html
        assert "stated rather than silently corrected" in html

    def test_the_bold_is_not_escaped_into_visible_markup(self):
        """caveat() escapes; caveat_html() does not. Getting this wrong
        prints &lt;b&gt; on the page - the same trap as &mdash;."""
        html = panels._exposure_warning(perf(), "p")
        assert "&lt;b&gt;" not in html
        assert "<b>" in html


class TestThePercentageIsReadable:
    def test_pp_became_a_percent_sign(self):
        """OWNER-REPORTED: "still dont understand what beating it by
        0.89pp means, can you just show a percentage symbol equivalent"."""
        assert signed_pct(0.89) == "+0.89%"
        assert signed_pct(-1.5) == "-1.50%"

    def test_the_number_itself_is_unchanged(self):
        """Only the label moved. If the value changed, the fix would be
        a lie dressed as a clarification."""
        for v in (0.89, -1.5, 0.0, 12.345):
            assert signed_pct(v)[:-1] == signed_pp(v)[:-2]

    def test_a_missing_comparison_is_not_zero(self):
        assert signed_pct(None) == "n/a"

    def test_the_headline_shows_BOTH_sides_not_just_the_gap(self):
        """A gap is only readable if you can see what it is a gap
        between. -0.28% against -1.39% is a sentence; "+1.11pp" is not."""
        html = panels.performance_panel.__doc__ or ""
        src = open(panels.__file__).read()
        assert "You {you:+.2f}%" in src
        assert "SPY {spy:+.2f}%" in src

    def test_and_the_gap_in_money(self):
        """The line nobody has to translate at all."""
        src = open(panels.__file__).read()
        assert "than the same cash in SPY would have been" in src

    def test_the_money_figure_is_the_excess_applied_to_the_baseline(self):
        """+1.11% of a $2,000 baseline is $22.20. Wrong arithmetic here
        would be a wrong number in the most trusted sentence on the
        page."""
        p = perf()
        excess = p.excess_pp
        assert round(excess, 2) == 1.11
        assert round(float(Decimal("200000") * Decimal(str(excess)) / 100)) == 2220


class TestTheBenchmarkBasisIsStated:
    def test_it_says_total_return_not_price_return(self):
        """adjustment=all includes dividends. A price-return SPY would
        understate the benchmark by roughly 67pp over the cached window,
        which would flatter the bot enormously."""
        src = open(panels.__file__).read()
        assert "total return (adjustment=all)" in src

    def test_both_series_are_indexed_to_the_same_day(self):
        src = open(panels.__file__).read()
        assert "indexed to 100 on the same day as the bot" in src


class TestTheWarningIsActuallyWIRED:
    """FOUND BY SABOTAGE (house rule 4). Deleting the call to
    _exposure_warning from performance_panel broke nothing: every test
    above calls the function directly, so all of them passed while the
    warning vanished from the page.

    That is the same hole the chart captions had - testing the function
    instead of the page. A warning nobody renders is not a warning.
    """

    def test_the_panel_calls_it(self):
        import inspect

        src = inspect.getsource(panels.performance_panel)
        assert "_exposure_warning(perf, p)" in src, (
            "performance_panel no longer renders the exposure warning, so "
            "the excess figure is back to standing alone")

    def test_it_is_rendered_wherever_a_SPY_SERIES_EXISTS(self):
        """Guarded by spy_points, and it must be INSIDE that guard: a
        comparison that exists is exactly when the caveat is needed."""
        import inspect

        src = inspect.getsource(panels.performance_panel)
        call = src.index("_exposure_warning(perf, p)")
        # rindex: there are two spy_points guards - the headline pointer
        # and this one. The nearest PRECEDING guard is the one that
        # governs this call.
        guard = src.rindex("if perf.spy_points:", 0, call)
        assert call - guard < 200, (
            "the warning has drifted away from the spy_points guard")

    def test_and_the_headline_never_promises_it_without_it(self):
        import inspect

        src = inspect.getsource(panels.performance_panel)
        assert 'if perf.spy_points:\n            headline_sub +=' in src, (
            "the tile promises an exposure warning unconditionally again")


class TestTheHeadlineBranchACTUALLYRENDERS:
    """OWNER-REPORTED 2026-08-21, as a traceback from the live machine:

        File ".../panels.py", line 209, in performance_panel
          if start > 0 and excess_v is not None:
        UnboundLocalError: cannot access local variable 'excess_v'

    It took out BOTH /performance and the Overview.

    WHY IT SHIPPED. `excess_v` is bound further down, in the tiles
    block; the headline block has `excess`. The branch only runs when a
    bot series AND a SPY series are both present, and no test had ever
    rendered the panel in that state - every fixture had one or neither.
    A crash needs the shape of the data, not just the shape of the code.

    So this renders the real panel, through the real entry point, with
    both series present.
    """

    def real_perf(self, bot=99.72, spy=98.61):
        from datetime import date
        from decimal import Decimal

        from catalyst.dashboard.db import QueryResult

        class Base:
            capital_cents = Decimal("200000")
            is_placeholder = False
            source = "broker_read"
            start_date = date(2026, 8, 17)
            reason = "account opened"
            set_at = "2026-08-17T00:00:00+00:00"
            account_fingerprint = "abcd1234"

        empty = QueryResult("", (), [], None)
        p = queries.Performance(closed_q=empty, costs_q=empty)
        p.baseline = Base()
        p.bot_points = [(date(2026, 8, 17), 100.0), (date(2026, 8, 21), bot)]
        p.spy_points = [(date(2026, 8, 17), 100.0), (date(2026, 8, 21), spy)]
        p.start_day, p.end_day = date(2026, 8, 17), date(2026, 8, 21)
        p.n_closed = 1
        p.spy_source = "local bar cache"
        p.spy_rows = 2
        return p

    def render(self, monkeypatch, perf):
        monkeypatch.setattr(queries, "performance", lambda db: perf)
        return panels.performance_panel(None, p="perf")

    def test_it_does_not_raise_with_both_series_present(self, monkeypatch):
        """THE CRASH, exactly."""
        assert self.render(monkeypatch, self.real_perf())

    @pytest.mark.parametrize("bot,spy", [
        (99.72, 98.61),      # ahead
        (98.00, 99.50),      # behind
        (100.0, 100.0),      # level
    ])
    def test_it_survives_ahead_behind_and_level(self, monkeypatch, bot, spy):
        assert self.render(monkeypatch, self.real_perf(bot, spy))

    def test_the_headline_says_percent_not_pp(self, monkeypatch):
        """The "%" fix landed on the TILE and missed this sentence, so
        the headline still printed the jargon that was reported."""
        html = self.render(monkeypatch, self.real_perf())
        head = html[html.index("perf-headline"):][:400]
        assert "pp<" not in head and "pp " not in head
        assert re.search(r"[-+]\d+\.\d\d%", head), head[:200]

    def test_the_money_line_is_the_excess_on_the_baseline(self, monkeypatch):
        """+1.11% of $2,000 is $22.20 - and it is computed from `excess`,
        the variable that exists here."""
        html = self.render(monkeypatch, self.real_perf())
        assert "$22.20" in html
        assert "than the same cash in SPY would have been" in html

    def test_both_sides_are_printed(self, monkeypatch):
        html = self.render(monkeypatch, self.real_perf())
        assert "You -0.28%" in html and "SPY -1.39%" in html

    def test_and_the_exposure_warning_is_there_too(self, monkeypatch):
        """The branch that crashed is the same one that must carry the
        caveat. Both, or neither is worth much."""
        html = self.render(monkeypatch, self.real_perf())
        assert "not like for like" in html
