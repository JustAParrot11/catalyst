"""The conviction floor passed one long call in twenty-one.

OWNER-SET 2026-09-05: "The bot isnt aggressive ... optimize heavily to
ensure ... claude can make profitable trades and multiple times a
month". From their own bundles, every long view the model ever gave:

    n=21   median 0.54   max 0.60   cleared a 0.60 floor: 1

and the loop meant to test the floor could not run: its evidence is
scored refusals that were refused FOR the floor - 15 in a month against
a 30-sample minimum. A gate that passes one call in twenty-one and
cannot be measured is not a threshold; the brief names that shape as
the defect ("the system refuses good trades forever and never signals
that it is doing so").

0.50 is where the conviction definition already draws the line -
"below 0.50 on a direction is a contradiction" - so a call the model
stands behind at all is now sized by code inside the hard bounds, and
the refusal tracker finally gets a sample it can score. It can raise
the floor again on evidence, three times faster than it can lower it.

Fully offline.
"""

from decimal import Decimal

from catalyst.risk.adaptive_params import (
    DEFAULT_PARAMS, MIN_SAMPLE_SIZE, PARAM_RANGE, TIGHTEN_LOOSEN_RATIO,
)
from catalyst.risk.evaluate import PRICED_IN_CONVICTION_PREMIUM


class TestTheFloor:
    def test_it_is_the_definitions_own_floor(self):
        assert DEFAULT_PARAMS["conviction_floor"] == Decimal("0.50")

    def test_it_is_inside_the_adaptive_range(self):
        lo, hi = PARAM_RANGE["conviction_floor"]
        assert lo <= DEFAULT_PARAMS["conviction_floor"] <= hi

    def test_a_priced_in_long_still_needs_more(self):
        """The premium stands: a priced-in call at 0.50 is not a trade."""
        assert DEFAULT_PARAMS["conviction_floor"] + PRICED_IN_CONVICTION_PREMIUM \
            > Decimal("0.60")

    def test_it_can_still_be_raised_on_evidence_faster_than_lowered(self):
        assert MIN_SAMPLE_SIZE["conviction_floor"] >= 30
        assert TIGHTEN_LOOSEN_RATIO == Decimal("3")


class TestTheOwnersLongCallsWouldNowBeSized:
    """The literal convictions from the bundles, against the live gate."""

    def test_the_two_longs_from_the_week_clear_it(self):
        floor = DEFAULT_PARAMS["conviction_floor"]
        for conviction in (Decimal("0.56"), Decimal("0.57")):
            assert conviction >= floor

    def test_a_contradiction_still_does_not(self):
        floor = DEFAULT_PARAMS["conviction_floor"]
        for conviction in (Decimal("0.30"), Decimal("0.45"), Decimal("0.49")):
            assert conviction < floor

    def test_evaluate_trades_a_0_56_long(self):
        """End to end through the real gate, not arithmetic on the
        constant."""
        from datetime import date, datetime, timezone

        from catalyst.discovery import Candidate
        from catalyst.research.schema import ResearchView
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.evaluate import evaluate

        now = datetime.now(timezone.utc)
        c = Candidate(id="x", ticker="AAA", catalyst_type="insider_cluster",
                      catalyst_date=now.date(), catalyst_date_confidence="confirmed",
                      source_event_ids=("s",), discovered_at=now, sector="u",
                      correlation_tags=("type:insider_cluster",))
        v = ResearchView(candidate_id="x", direction="long", conviction=0.56,
                         thesis="t", invalidation="i", expected_holding_days=12,
                         priced_in=False, priced_in_reasoning="n")
        p = PortfolioState(equity_usd=Decimal("2000"),
                           settled_cash_usd=Decimal("2000"), open_positions=(),
                           day_pnl_usd=Decimal("0"), peak_equity_usd=Decimal("2000"),
                           consecutive_losses=0, as_of=now, reliable=True)
        m = MarketSnapshot(ticker="AAA", last_close=Decimal("10"),
                           half_spread_bp=Decimal("5"),
                           median_daily_dollar_volume=Decimal("5000000"))
        d = evaluate(c, v, p, DEFAULT_PARAMS, m)
        assert d.action == "trade", d.skip_reasons
