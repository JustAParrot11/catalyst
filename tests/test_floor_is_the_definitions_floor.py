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


def _decide(conviction, priced_in=False, direction="long"):
    """One liquid, unconstrained candidate through the real gate, so the
    only thing that can refuse it is a conviction bar."""
    from datetime import datetime, timezone

    from catalyst.discovery import Candidate
    from catalyst.research.schema import ResearchView
    from catalyst.risk import MarketSnapshot, PortfolioState
    from catalyst.risk.evaluate import evaluate

    now = datetime.now(timezone.utc)
    c = Candidate(id="x", ticker="AAA", catalyst_type="insider_cluster",
                  catalyst_date=now.date(), catalyst_date_confidence="confirmed",
                  source_event_ids=("s",), discovered_at=now, sector="u",
                  correlation_tags=("type:insider_cluster",))
    v = ResearchView(candidate_id="x", direction=direction,
                     conviction=conviction, thesis="t", invalidation="i",
                     expected_holding_days=12, priced_in=priced_in,
                     priced_in_reasoning="n")
    p = PortfolioState(equity_usd=Decimal("2000"),
                       settled_cash_usd=Decimal("2000"), open_positions=(),
                       day_pnl_usd=Decimal("0"), peak_equity_usd=Decimal("2000"),
                       consecutive_losses=0, as_of=now, reliable=True)
    m = MarketSnapshot(ticker="AAA", last_close=Decimal("10"),
                       half_spread_bp=Decimal("5"),
                       median_daily_dollar_volume=Decimal("5000000"))
    return evaluate(c, v, p, DEFAULT_PARAMS, m)


class TestTheFloor:
    def test_it_is_the_definitions_own_floor(self):
        assert DEFAULT_PARAMS["conviction_floor"] == Decimal("0.50")

    def test_it_is_inside_the_adaptive_range(self):
        lo, hi = PARAM_RANGE["conviction_floor"]
        assert lo <= DEFAULT_PARAMS["conviction_floor"] <= hi

    def test_a_priced_in_long_still_needs_more(self):
        """The premium stands: a priced-in call AT the floor is not a
        trade.

        Run through evaluate() rather than asserted as arithmetic. The
        line this replaces said `floor + premium > 0.60`, which was a
        statement about the premium BEING 0.15 dressed up as a statement
        about the floor - and it went red when the premium was measured
        and lowered on 2026-09-11 without anything about the floor
        changing at all."""
        assert PRICED_IN_CONVICTION_PREMIUM > 0
        floor = float(DEFAULT_PARAMS["conviction_floor"])
        priced_in = _decide(conviction=floor, priced_in=True)
        assert priced_in.action == "skip"
        assert "priced_in_below_raised_floor" in priced_in.skip_reasons
        assert _decide(conviction=floor, priced_in=False).action == "trade", (
            "the floor itself refused it, so this is not measuring the "
            "premium")

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
        d = _decide(conviction=0.56)
        assert d.action == "trade", d.skip_reasons
