"""The API bill on the value bridge is not the lifetime bill, and used
to say it was.

OWNER-REPORTED 2026-08-24: "it looks like it barely spent any API
usage", against a panel reading

    Alpaca account value    $1,991.68
    Net value after costs   $1,987.42
    Difference                  $4.26

on a month whose real spend was $23.15 local and $27.93 billed.

BOTH FIGURES WERE RIGHT. Costs on this panel are filtered to the CURRENT
baseline, and this account's baseline has been struck six times; every
penny before the newest one drops out of the comparison. Deducting spend
from before the comparison began would be wrong - it would charge the
new baseline for the old one's bill - so the arithmetic stays.

What was wrong was the label. The row said "less API spend TO DATE" and
the number never meant that, and `excluded_cost_cents` was already being
computed and then thrown away rather than shown.

Fully offline.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.dashboard.db import Db
from catalyst.dashboard.panels import value_reconciliation_panel

SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"

#: HOUSE RULE 6: the baseline start date is written into the fixture AND
#: is what the code filters on, so the two move together. Nothing here is
#: compared against datetime.now() except the broker snapshot's own day,
#: which is set from the same constant.
BASELINE_DAY = date(2026, 8, 20)
BEFORE = BASELINE_DAY - timedelta(days=5)
AFTER = BASELINE_DAY + timedelta(days=1)


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA.read_text())

    def baseline(self, day=BASELINE_DAY, capital_cents=200000):
        self.conn.execute(
            "INSERT INTO benchmark_baselines "
            "(id, capital_cents, start_date, source, account_fingerprint, "
            " reason, set_at) VALUES (?,?,?,?,?,?,?)",
            (f"b-{day.isoformat()}", str(capital_cents), day.isoformat(),
             "account_changed", "acct-new",
             "a different broker account was connected",
             datetime.combine(day, datetime.min.time(),
                              timezone.utc).isoformat()))
        self.conn.commit()

    def spend(self, day, cents, kind="scheduled"):
        self.conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            (f"c-{day.isoformat()}-{cents}", "{}", "claude-sonnet-5", kind,
             "research", str(cents),
             datetime.combine(day, datetime.min.time(),
                              timezone.utc).isoformat(), None))
        self.conn.commit()

    def broker_read(self, equity_usd, day=AFTER):
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_snapshots "
            "(day, taken_at, equity_usd, settled_cash_usd, "
            " positions_notional, source) VALUES (?,?,?,?,?,?)",
            (day.isoformat(),
             datetime.combine(day, datetime.min.time(),
                              timezone.utc).isoformat(),
             str(equity_usd), "1500.00", "490.00", "broker_read"))
        self.conn.commit()

    def view(self):
        return Db(self.path)

    def close(self):
        self.conn.close()


@pytest.fixture
def led(tmp_path):
    l = Ledger(tmp_path / "t.db")
    yield l
    l.close()


def render(led):
    view = led.view()
    try:
        return value_reconciliation_panel(view)
    finally:
        view.close()


def the_owners_shape(led):
    """$1.90 of spend since the baseline, $21.25 before it."""
    led.baseline()
    led.spend(BEFORE, "2125")
    led.spend(AFTER, "190")
    led.broker_read("1991.68")


class TestTheExcludedSpendIsShownRatherThanDiscarded:
    def test_the_money_from_before_the_baseline_appears(self, led):
        the_owners_shape(led)
        html = render(led)
        assert "$21.25" in html, (
            "spend from before the baseline was computed and thrown away; "
            "the owner sees a small figure and no reason for it")

    def test_it_says_why_it_is_not_deducted(self, led):
        the_owners_shape(led)
        html = render(led)
        assert "not deducted" in html
        assert "before this comparison" in html

    def test_it_points_at_the_page_that_does_count_it(self, led):
        the_owners_shape(led)
        assert "Cost page" in render(led), (
            "two figures for 'what has this cost me' need one of them to "
            "say where the other lives")

    def test_the_label_no_longer_claims_to_date(self, led):
        the_owners_shape(led)
        html = render(led)
        assert "API spend since the comparison started" in html
        assert "API spend to date" not in html, (
            "the row said 'to date' and the number never meant it")


class TestTheArithmeticIsUnchanged:
    def test_pre_baseline_spend_is_still_not_deducted(self, led):
        """The number was right. A fix that starts charging the new
        baseline for the old one's bill would be a worse defect than the
        label it replaces."""
        the_owners_shape(led)
        html = render(led)
        # 200000c capital, no closed trades, 190c spent since baseline.
        assert "$1,998.10" in html

    def test_a_first_baseline_shows_no_excluded_line(self, led):
        """Nothing was excluded, so nothing is claimed to be."""
        led.baseline()
        led.spend(AFTER, "190")
        led.broker_read("1991.68")
        html = render(led)
        assert "not deducted" not in html

    def test_the_deducted_figure_is_the_post_baseline_spend(self, led):
        the_owners_shape(led)
        assert "-$1.90" in render(led)


class TestTheCheckCanFail:
    """House rule 4: the assertions above must be able to catch the
    behaviour they describe, not merely pass."""

    def test_a_bigger_excluded_sum_is_reported_as_such(self, led):
        led.baseline()
        led.spend(BEFORE, "5000")
        led.spend(AFTER, "190")
        led.broker_read("1991.68")
        html = render(led)
        assert "$50.00" in html
        assert "$21.25" not in html
