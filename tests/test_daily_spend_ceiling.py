"""A ceiling on the RATE, which must not become a throttle.

OWNER-SET, 2026-08-14: "ensure the limit isnt actually limiting the bot
currently, the limit is so that it doesnt go far beyond. $5 a day usage
is ok."

Both halves are testable and both matter, in opposite directions.

WHY IT EXISTS. The monthly cap bounds the TOTAL and not the RATE.
MAX_RESEARCH_PER_CYCLE=3 against a 900-second cycle permits 288
investigations a day; at conjunction prices that is a month's budget in
an afternoon followed by thirty dark days. cycle.py already records this
class of failure happening once - "~51c a cycle, which spends the whole
$5 monthly cap in under an hour" - and the fix applied then bounded
repeat attempts on ONE candidate, which does not bound the rate at all.

WHY IT MUST NOT BIND. The owner's live day cost 193.30c across 20
scheduled calls. A ceiling that stopped that is a throttle wearing a
safety bound's clothes, and it would do the same damage the priced-in
veto did: quietly convert a working bot into one that refuses.

One thing worth stating because it surprised me while writing this: the
daily ceiling ($5/day) is the SAME figure as the base monthly cap
($5/month), so under defaults it can never bind - the month runs out
first. It only starts doing work once the owner raises the monthly
figure, which is the case they are actually in ($25/month).
"""

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.cost import CostEstimate
from catalyst.cost import governor as gov
from catalyst.cost.ledger import day_to_date_cents, month_to_date_cents

#: What the owner's bot actually spent in a day, from the live bundle
#: for 2026-08-14. The ceiling is judged against this, not against a
#: number I would like it to be.
MEASURED_LIVE_DAY_CENTS = Decimal("193.30")
MEASURED_LIVE_CALLS = 20


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "g.db")
    root = Path(__file__).resolve().parents[1]
    conn.executescript((root / "catalyst/storage/schema.sql").read_text())
    return conn


def _spend(conn, cents, when: date, kind="scheduled"):
    conn.execute(
        "INSERT INTO cost_events (id, raw_usage_json, model, kind, component, "
        "priced_cents, priced_at) VALUES (?,'{}','claude-sonnet-5',?,'research',"
        "?,?)",
        (f"e{cents}-{when}-{conn.total_changes}", kind, str(cents),
         f"{when.isoformat()}T12:00:00+00:00"))
    conn.commit()


def _auth(conn, cents, as_of, owner=Decimal("2500")):
    return gov.authorize(
        CostEstimate(estimated_cents=Decimal(str(cents)), basis="test",
                     kind="scheduled", component="research"),
        conn, Decimal("0.10"), as_of=as_of, owner_monthly_cap_cents=owner)


class TestItDoesNotLimitWhatTheBotActuallyDoes:
    """The owner's first requirement, and the easier one to get wrong."""

    def test_a_full_live_day_is_authorised_end_to_end(self, db):
        """Replay the measured day - 20 calls, 193.30c - and every one
        must be allowed. If this fails the ceiling is a throttle."""
        today = date(2026, 8, 20)
        per_call = MEASURED_LIVE_DAY_CENTS / MEASURED_LIVE_CALLS
        for i in range(MEASURED_LIVE_CALLS):
            d = _auth(db, per_call, today)
            assert d.authorized, (
                f"call {i + 1} of {MEASURED_LIVE_CALLS} was refused with "
                f"{d.reason!r} - the ceiling is limiting normal operation")
            _spend(db, per_call, today)
        assert day_to_date_cents("scheduled", db, today) <= gov.DAILY_CAP_CENTS

    def test_the_ceiling_has_real_headroom_over_the_measured_day(self):
        """Not a hair above it. A ceiling that sits at the observed rate
        fires on any ordinary busy day."""
        assert gov.DAILY_CAP_CENTS >= MEASURED_LIVE_DAY_CENTS * 2, (
            f"{gov.DAILY_CAP_CENTS}c leaves less than 2x headroom over the "
            f"measured {MEASURED_LIVE_DAY_CENTS}c day")

    def test_it_is_the_figure_the_owner_chose(self):
        assert gov.DAILY_CAP_CENTS == Decimal("500"), "$5/day, owner-set"


class TestItStopsARunaway:
    """The other direction. A guard that never fires is decoration."""

    def test_spending_the_day_stops_the_day(self, db):
        today = date(2026, 8, 20)
        _spend(db, gov.DAILY_CAP_CENTS, today)
        d = _auth(db, 20, today)
        assert not d.authorized
        assert d.reason == "daily_cap_exceeded"

    def test_a_single_call_bigger_than_a_whole_day_is_refused(self, db):
        """No one research call should be able to cost more than the
        day's entire allowance."""
        d = _auth(db, gov.DAILY_CAP_CENTS + 1, date(2026, 8, 20))
        assert not d.authorized
        assert d.reason == "daily_cap_exceeded"

    def test_the_runaway_cycle_from_cycle_py_is_now_bounded(self, db):
        """cycle.py:614 records the real event: "~51c a cycle, which
        spends the whole $5 monthly cap in under an hour". At 3 calls a
        cycle and 96 cycles a day this is what the ceiling exists for."""
        today = date(2026, 8, 20)
        authorised = 0
        for _ in range(288):                      # a day of 15-min cycles
            d = _auth(db, 51, today, owner=Decimal("100000"))
            if not d.authorized:
                break
            authorised += 1
            _spend(db, 51, today)
        assert authorised < 288, "the runaway was not bounded at all"
        # JUDGED AGAINST THE CEILING THIS CAP DERIVES, not the flat
        # constant. The ceiling stopped being a fixed $5 when it started
        # following the owner's budget - this test hands it $1,000/month,
        # which derives $100/day (three days of even spending, the same
        # rule at any size). The REQUIREMENT is unchanged and is what is
        # asserted: a runaway stops inside a day instead of eating the
        # month.
        ceiling = gov.daily_cap_cents(Decimal("100000"))
        assert ceiling > gov.DAILY_CAP_CENTS, (
            "the derivation is not loosening for a large budget, so this "
            "test is no longer exercising what it claims to")
        assert day_to_date_cents("scheduled", db, today) <= ceiling
        # And still a small fraction of the month, which is the point.
        assert day_to_date_cents("scheduled", db, today) <= Decimal("100000") / 5

    def test_tomorrow_starts_clean_without_anyone_doing_anything(self, db):
        """It is a rate limit, not a fault. Nothing should need
        acknowledging."""
        today = date(2026, 8, 20)
        _spend(db, gov.DAILY_CAP_CENTS, today)
        assert not _auth(db, 20, today).authorized
        assert _auth(db, 20, today + timedelta(days=1)).authorized


class TestTheTwoLimitsStayDistinguishable:
    def test_the_daily_and_monthly_denials_do_not_read_alike(self):
        """"today's allowance is gone, it resumes at midnight" and "the
        month's budget is gone, it resumes on the 1st" need completely
        different responses."""
        from catalyst.dashboard.queries import explain_governor_reason

        daily, daily_todo = explain_governor_reason("daily_cap_exceeded")
        monthly, _ = explain_governor_reason("cap_exceeded")
        assert daily != monthly
        assert "_" not in daily, "still reads like an identifier"
        assert "resets at midnight" in daily_todo
        assert "nothing needs doing" in daily_todo.lower()

    def test_the_daily_gate_is_checked_FIRST(self, db):
        """The more specific limit should name itself. With both
        exhausted the owner needs to know the rate stopped it, because
        that one clears by itself."""
        today = date(2026, 8, 20)
        for day in range(1, 21):
            _spend(db, 120, date(2026, 8, day))
        _spend(db, gov.DAILY_CAP_CENTS, today)
        assert month_to_date_cents("scheduled", db, today) > Decimal("2500")
        d = _auth(db, 20, today, owner=Decimal("2500"))
        assert d.reason == "daily_cap_exceeded"

    def test_manual_spend_is_not_rate_limited(self, db):
        """A human deliberately testing something is already bounded
        monthly and for the lifetime of the build. Rate-limiting a
        person at a keyboard helps nobody."""
        today = date(2026, 8, 20)
        _spend(db, gov.DAILY_CAP_CENTS * 2, today, kind="manual")
        d = gov.authorize(
            CostEstimate(estimated_cents=Decimal("20"), basis="test",
                         kind="manual", component="research"),
            db, Decimal("0.10"), as_of=today)
        assert d.reason != "daily_cap_exceeded"


class TestTheDayLedgerIsTheLOCALOne:
    def test_it_counts_only_the_day_asked_for(self, db):
        _spend(db, 100, date(2026, 8, 19))
        _spend(db, 250, date(2026, 8, 20))
        assert day_to_date_cents("scheduled", db, date(2026, 8, 20)) == \
            Decimal("250")

    def test_it_never_pools_kinds(self, db):
        today = date(2026, 8, 20)
        _spend(db, 100, today, kind="scheduled")
        _spend(db, 400, today, kind="manual")
        assert day_to_date_cents("scheduled", db, today) == Decimal("100")

    def test_unpriced_rows_are_excluded_here(self, db):
        """They block authorisation entirely via has_unpriced_rows, which
        is a stronger gate than counting them as zero."""
        today = date(2026, 8, 20)
        db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            "component, priced_cents, priced_at) VALUES "
            "('u','{}','m','scheduled','research',NULL,?)",
            (f"{today.isoformat()}T12:00:00+00:00",))
        db.commit()
        assert day_to_date_cents("scheduled", db, today) == Decimal("0")
