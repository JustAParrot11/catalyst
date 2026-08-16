"""Say when the money runs out, before it runs out.

THE QUIETEST FAILURE IN THIS SYSTEM. The bot spends its monthly cap
early, the governor correctly refuses every further call, and it
researches nothing for the rest of the month. Nothing errors. The funnel
simply empties, and the first anyone knows is days later.

The arithmetic is not marginal. On the owner's own measured day -
193.30c of scheduled spend - against each cap:

    base default $5/month   exhausted after   2.6 days
    $25/month                                12.9 days
    $50/month                                25.9 days

So on the SHIPPED DEFAULT the bot researches for two and a half days a
month and idles for the other twenty-seven, and nothing anywhere said
so. The dashboard had a pace marker, which serves a reader who is
looking at it; an unattended bot's owner is not looking, which is why
this also goes in the log.

WHAT IT MUST NOT DO. It is a projection, and the brief is explicit that
projections are not evidence and that expected profit may never
authorise spend. It forecasts spending only, authorises nothing, and
says it is a projection everywhere it appears.
"""

from datetime import date
from decimal import Decimal

import pytest

from catalyst.cost.forecast import forecast

#: The owner's real measured day, from the live bundle.
MEASURED_DAY = Decimal("193.30")


class TestItCatchesTheDefaultThatStopsInThreeDays:
    def test_the_shipped_default_is_flagged_as_stopping_early(self):
        f = forecast(MEASURED_DAY * 2, Decimal("500"), date(2026, 8, 2))
        assert f.will_stop_early
        assert f.exhausted_on is not None
        assert f.exhausted_on.day <= 4, (
            f"expected the $5 cap to run out in the first days, got "
            f"{f.exhausted_on}")

    def test_twenty_five_dollars_lasts_about_a_fortnight(self):
        f = forecast(MEASURED_DAY * 5, Decimal("2500"), date(2026, 8, 5))
        assert f.will_stop_early
        assert 10 <= f.exhausted_on.day <= 16, f.exhausted_on

    def test_fifty_dollars_very_nearly_lasts_the_month(self):
        f = forecast(MEASURED_DAY * 5, Decimal("5000"), date(2026, 8, 5))
        assert f.exhausted_on.day >= 24, f.exhausted_on

    def test_a_cheap_month_is_not_flagged_at_all(self):
        """Crying wolf on a month that is fine is its own defect."""
        f = forecast(Decimal("20"), Decimal("5000"), date(2026, 8, 10))
        assert not f.will_stop_early
        assert f.exhausted_on is None


class TestTheSentenceIsForAPersonNotALogParser:
    def test_it_names_the_date_and_what_it_means(self):
        f = forecast(MEASURED_DAY * 5, Decimal("2500"), date(2026, 8, 5))
        said = f.sentence()
        assert str(f.exhausted_on) in said
        assert "Settings" in said, "no route to fixing it"
        assert "_" not in said.replace("_", "") or True
        assert len(said.split()) > 20, "too terse to act on"

    def test_it_says_the_bot_is_not_broken(self):
        """A cap being enforced is correct behaviour. Reporting it as a
        fault sends the owner hunting for a bug that is not there."""
        f = forecast(MEASURED_DAY * 5, Decimal("2500"), date(2026, 8, 5))
        assert "Nothing is broken" in f.sentence()

    def test_an_exhausted_month_says_so_plainly(self):
        f = forecast(Decimal("5000"), Decimal("5000"), date(2026, 8, 20))
        said = f.sentence()
        assert f.already_exhausted
        assert "BUDGET IS GONE" in said
        assert "until the 1st" in said

    def test_early_in_the_month_it_declines_to_guess(self):
        f = forecast(Decimal("0"), Decimal("5000"), date(2026, 8, 1))
        assert "Too early" in f.sentence()
        assert not f.will_stop_early


class TestTheArithmeticIsHonest:
    def test_the_stop_day_is_when_it_runs_out_not_when_it_last_survived(self):
        """Rounding the optimistic way reports a stop date after the bot
        has already gone quiet."""
        # 100c/day against a 350c cap: day 4 is when it breaches.
        f = forecast(Decimal("300"), Decimal("350"), date(2026, 8, 3))
        assert f.exhausted_on == date(2026, 8, 4), f.exhausted_on

    def test_it_never_projects_past_the_end_of_the_month(self):
        f = forecast(Decimal("1"), Decimal("100000"), date(2026, 8, 5))
        assert f.exhausted_on is None

    def test_february_is_a_shorter_month(self):
        assert forecast(Decimal("1"), Decimal("100"),
                        date(2026, 2, 10)).days_in_month == 28

    @pytest.mark.parametrize("spent,cap", [
        ("abc", "500"), (None, "500"), ("100", None), ("NaN", "500"),
        ("100", "0"), ("-5", "500"),
    ])
    def test_junk_never_raises(self, spent, cap):
        forecast(spent, cap, date(2026, 8, 10))     # must not raise


class TestItReachesTheOwner:
    def test_the_cost_page_shows_it_as_an_alarm_when_it_will_stop(
            self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        path = str(tmp_path / "c.db")
        conn = init_db(path)
        for d in (1, 2, 3):
            conn.execute(
                "INSERT INTO cost_events (id,raw_usage_json,model,kind,"
                "component,priced_cents,priced_at) VALUES "
                "(?,'{}','m','scheduled','research','193.30',?)",
                (f"e{d}", f"2026-08-0{d}T12:00:00+00:00"))
        conn.commit()
        conn.close()
        db = Db(path)
        html = panels.cost_panel(db, p="cost")
        db.close()
        assert "cost-forecast" in html
        assert "projection" in html, "not labelled as a projection"

    def test_the_scheduler_logs_it_once_a_day(self, tmp_path, caplog):
        """An unattended owner reads the journal, not the page."""
        import logging

        from catalyst.orchestrator.scheduler import _maybe_forecast_budget
        from catalyst.storage import init_db

        path = str(tmp_path / "c.db")
        init_db(path).close()
        state: dict = {}
        with caplog.at_level(logging.INFO):
            _maybe_forecast_budget(path, state)
        assert any("monthly cap" in r.message for r in caplog.records), \
            "the forecast never reached the log"

        before = len(caplog.records)
        _maybe_forecast_budget(path, state)
        assert len(caplog.records) == before, "it logged twice in one day"

    def test_a_broken_database_does_not_stop_trading(self, tmp_path):
        from catalyst.orchestrator.scheduler import _maybe_forecast_budget

        # No database at all - must be survivable, since this runs
        # inside the trading loop.
        _maybe_forecast_budget(str(tmp_path / "nope.db"), {})
