"""Both lines on the performance chart must leave the same point.

OWNER-REPORTED 2026-08-24: "spy graph still feels wrong."

Reproduced. A baseline struck on a day the market is shut puts the bot's
100 on that day and SPY's 100 on the next SESSION, because SPY's base is
its first BAR inside the window and a weekend has no bar. The caption
said "indexed to 100 on the same day as the bot line" and there was no
way to check it - so two lines started at different heights under a
sentence promising they did not.

Every first run at a weekend does this, and this account's baseline has
been struck six times.

THE FIX IS NOT COSMETIC. Money committed to SPY on a closed day buys at
the next open, so the position really is flat at its opening value until
then - and the bot is flat over the same days for the same reason. The
prepended point is what happened, not a nudge to make a chart look tidy.

Fully offline: bars are written to a temporary cache, never fetched.
"""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from catalyst.storage import init_db

#: HOUSE RULE 6 does not apply: every date here is written into the
#: fixture AND is what the code indexes from, so the two move together.
#: `performance` does read today's date to extend the bot line to now,
#: which only ever ADDS a point on the right - it cannot move either
#: line's origin, which is all these tests assert on.
MONDAY = date(2026, 8, 17)
SATURDAY = date(2026, 8, 15)
SUNDAY = date(2026, 8, 16)


@pytest.fixture
def build(tmp_path, monkeypatch):
    """A database and a SPY cache holding weekday bars only."""

    def _build(baseline_day, first_bar=date(2026, 8, 10),
               last_bar=date(2026, 8, 24)):
        db = str(tmp_path / f"t{baseline_day}.db")
        conn = init_db(db)
        conn.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, start_date, "
            " source, account_fingerprint, reason, set_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("b1", "200000", baseline_day.isoformat(), "first_run", "acct",
             "first run",
             datetime.combine(baseline_day, datetime.min.time(),
                              timezone.utc).isoformat()))
        # One priced row, so there is an equity series to draw at all.
        conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            ("c1", "{}", "claude-sonnet-5", "scheduled", "research", "100",
             "2026-08-19T00:00:00+00:00", None))
        conn.commit()
        conn.close()

        bars = tmp_path / f"bars{baseline_day}"
        bars.mkdir()
        rows = ["date,open,high,low,close,volume"]
        d, px = first_bar, 600.0
        while d <= last_bar:
            if d.weekday() < 5:            # the market is shut at weekends
                rows.append(f"{d},{px},{px},{px},{px},1000000")
                px += 3.0
            d += timedelta(days=1)
        (bars / "SPY.csv").write_text("\n".join(rows) + "\n")
        (bars / "SPY.meta.json").write_text(
            '{"feed":"sip","adjustment":"all"}')

        monkeypatch.setenv("CATALYST_DB", db)
        monkeypatch.setenv("CATALYST_BARS", str(bars))
        import catalyst.backtest.data as bd
        bd.BarCache._mem = {}

        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db
        return queries.performance(Db(db))

    return _build


class TestTheTwoLinesShareAnOrigin:
    @pytest.mark.parametrize("baseline,name", [
        (MONDAY, "a trading day"),
        (SATURDAY, "a Saturday"),
        (SUNDAY, "a Sunday"),
    ])
    def test_both_start_on_the_same_day_at_100(self, build, baseline, name):
        perf = build(baseline)
        bot, spy = perf.bot_points[0], perf.spy_points[0]
        assert bot[0] == spy[0], (
            f"baseline struck on {name}: the bot is indexed from {bot[0]} "
            f"and SPY from {spy[0]}, so the chart draws two lines starting "
            "at different points under a caption saying they do not")
        assert bot[1] == 100.0
        assert spy[1] == 100.0

    def test_a_weekend_baseline_holds_spy_flat_until_the_open(self, build):
        """Not a cosmetic point: money committed on a closed day buys at
        the next open, so the position is genuinely flat until then."""
        perf = build(SATURDAY)
        days = [p[0] for p in perf.spy_points]
        assert days[0] == SATURDAY
        assert MONDAY in days
        flat = [p[1] for p in perf.spy_points if p[0] <= MONDAY]
        assert flat == [100.0, 100.0], (
            "SPY moved across a weekend it could not have traded in")

    def test_the_money_column_is_flat_over_the_weekend_too(self, build):
        perf = build(SATURDAY)
        pre = [p[2] for p in perf.spy_points if p[0] <= MONDAY]
        assert len(set(pre)) == 1, (
            "the '$ on a $2,000 account' column moved on a closed day")
        assert pre[0] == 200000

    def test_a_trading_day_baseline_gains_no_extra_point(self, build):
        """The prepend must fire only when it is needed, or a normal
        Monday start grows a duplicate origin."""
        perf = build(MONDAY)
        days = [p[0] for p in perf.spy_points]
        assert len(days) == len(set(days)), "a duplicate first day"
        assert days[0] == MONDAY


class TestThePageStatesTheOriginRatherThanClaimingIt:
    def test_the_provenance_names_the_day_the_index_is_struck(self, build):
        from catalyst.dashboard import panels

        perf = build(SATURDAY)
        html = panels._spy_lag_note(perf, "perf")  # noqa: SLF001 - smoke
        assert isinstance(html, str)

        # The sentence that used to make an uncheckable claim.
        from catalyst.dashboard.db import Db
        db = Db(os.environ["CATALYST_DB"])
        try:
            page = panels.performance_panel(db)
        finally:
            db.close()
        assert "indexed to 100 on" in page
        assert str(SATURDAY) in page
        assert "indexed to 100 on the same day as the bot line" not in page, (
            "the old wording asserted the thing that was wrong, and gave "
            "the reader no way to see it")


class TestTheCheckCanFail:
    """House rule 4: run the assertion against the shape it exists to
    catch, and confirm it catches it."""

    def test_an_unaligned_pair_would_be_caught(self, build):
        perf = build(SATURDAY)
        bot, spy = perf.bot_points[0], perf.spy_points[0]
        assert bot[0] == spy[0]
        # The pre-fix shape, constructed by hand: SPY's origin on the
        # Monday instead. The assertion above must reject it.
        broken = [p for p in perf.spy_points if p[0] != SATURDAY]
        assert broken[0][0] != bot[0]
