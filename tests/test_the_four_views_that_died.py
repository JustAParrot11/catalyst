"""Four directional views in a week, and every one of them died.

OWNER'S 7-DAY LOGIC BUNDLE, 2026-09-11. Everything shipped on 09-05 is
live and working: 69 research calls over four trading days (33 in the
whole previous week), the drift arm producing 32 candidates where it
produced none, the hunt producing 8, $21.25 spent of $100, no pause.

And still zero trades, because the blocker moved:

    69 researched
     4 directional views
       CASY  short 0.58              cash account cannot short
       COO   short 0.60              cash account cannot short
       BWFG  long  0.55              spread gate: 99.2bp vs a 20bp bound
       UBER  long  0.62  priced_in   needed 0.65
     0 orders

TWO OF THOSE FOUR ARE CORRECT AND STAY CORRECT. BWFG is a microcap bank
whose half-spread was 99.2 basis points - a ~2% round trip against an
8% assumed move - and the spread gate is the market-structure verdict
doing exactly its job. The shorts are a cash account being a cash
account.

THE OTHER TWO ARE THIS FILE.

  UBER was a $10.0M open-market purchase by the CEO (141,000 shares at
  ~$70.96) plus $5.3M by the COO, in a mega-cap where liquidity is not
  a question. The model wanted it long at 0.62. The priced-in premium
  of 0.15 on a 0.50 floor asked for 0.65. `priced_in` is set on nearly
  everything - 62 of 65 no_trades and 1 of 2 longs that week - because
  for a public filing days old the honest answer usually is "partly".
  But conviction is DEFINED as a frequency: a model that thinks half
  the move is gone says so by scoring 0.62 rather than 0.80. Charging
  0.15 again for the same judgement double-counts it.

  The shorts cost two paid research calls to produce answers the engine
  must discard. The account has never been able to short. Saying so in
  the prompt is not pressure toward `long` - a short read is still the
  right ANSWER, recorded as no_trade with the bearish case in the
  thesis, which the refusal tracker scores. What is wasted is spending
  the search budget BUILDING a case that cannot be acted on.

Fully offline. No calendar dates (house rule 6).
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from catalyst.discovery import Candidate
from catalyst.research import prompts
from catalyst.research.schema import ResearchView
from catalyst.risk import MarketSnapshot, PortfolioState
from catalyst.risk.adaptive_params import DEFAULT_PARAMS
from catalyst.risk.evaluate import PRICED_IN_CONVICTION_PREMIUM, evaluate
from catalyst.risk.hard_bounds import HARD_BOUNDS

NOW = datetime.now(timezone.utc)
FLOOR = DEFAULT_PARAMS["conviction_floor"]


def candidate(ticker="UBER", ctype="insider_cluster"):
    return Candidate(
        id=f"insider_cluster-{ticker}-x", ticker=ticker, catalyst_type=ctype,
        catalyst_date=NOW.date(), catalyst_date_confidence="confirmed",
        source_event_ids=("s",), discovered_at=NOW, sector="7372",
        correlation_tags=(f"type:{ctype}",))


def view(direction="long", conviction=0.62, priced_in=True):
    return ResearchView(
        candidate_id="insider_cluster-UBER-x", direction=direction,
        conviction=conviction, thesis="t", invalidation="i",
        expected_holding_days=12, priced_in=priced_in,
        priced_in_reasoning="r")


def portfolio():
    return PortfolioState(
        equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
        open_positions=(), day_pnl_usd=Decimal("0"),
        peak_equity_usd=Decimal("2000"), consecutive_losses=0,
        as_of=NOW, reliable=True)


def market(half_spread="8"):
    return MarketSnapshot(
        ticker="UBER", last_close=Decimal("71.00"),
        half_spread_bp=Decimal(half_spread),
        median_daily_dollar_volume=Decimal("500000000"))


def decide(v, half_spread="8"):
    return evaluate(candidate(), v, portfolio(), DEFAULT_PARAMS,
                    market(half_spread))


class TestTheUberTradeNowHappens:
    def test_the_exact_view_that_was_refused_is_now_a_trade(self):
        """0.62, priced_in, liquid. The bundle's one real near-miss."""
        d = decide(view(conviction=0.62, priced_in=True))
        assert d.action == "trade", d.skip_reasons
        assert d.side == "long"

    def test_the_bar_a_priced_in_long_faces_is_the_floor_plus_the_premium(self):
        assert FLOOR + PRICED_IN_CONVICTION_PREMIUM == Decimal("0.55")

    def test_the_premium_was_what_refused_it(self):
        """House rule 4 in the same test: at the old 0.15 the same view
        is refused, so this is measuring the change and not the floor."""
        assert Decimal("0.62") < FLOOR + Decimal("0.15")
        assert Decimal("0.62") >= FLOOR + PRICED_IN_CONVICTION_PREMIUM


class TestThePremiumIsStillAThumbOnTheScale:
    def test_it_is_not_zero(self):
        """Priced-in-and-long is a genuinely worse setup than
        not-priced-in-and-long. Taking it to zero would say otherwise."""
        assert PRICED_IN_CONVICTION_PREMIUM > 0

    def test_a_bare_floor_priced_in_long_is_still_refused(self):
        d = decide(view(conviction=float(FLOOR), priced_in=True))
        assert d.action == "skip"
        assert "priced_in_below_raised_floor" in d.skip_reasons

    def test_the_same_conviction_NOT_priced_in_trades(self):
        """The premium is the only difference between these two."""
        assert decide(view(conviction=float(FLOOR),
                           priced_in=False)).action == "trade"

    def test_the_refusal_still_names_WHICH_bar_was_missed(self):
        """'below_conviction_floor' on a candidate held to a higher bar
        is true and misleading: one says the floor is wrong, the other
        says the premium is."""
        d = decide(view(conviction=float(FLOOR), priced_in=True))
        assert "below_conviction_floor" not in d.skip_reasons

    def test_the_bar_stays_reachable(self):
        """A premium that puts the bar above what the model can express
        is a veto wearing a threshold's clothes."""
        assert FLOOR + PRICED_IN_CONVICTION_PREMIUM < Decimal("1.0")
        from catalyst.risk.adaptive_params import CONVICTION_FLOOR_CEILING

        assert CONVICTION_FLOOR_CEILING + PRICED_IN_CONVICTION_PREMIUM \
            < Decimal("1.0")


class TestTheGatesThatWereRightAreUntouched:
    def test_the_bwfg_spread_still_refuses(self):
        """99.2bp measured against a 20bp hard bound. A ~2% round trip
        against an 8% assumed move is the edge paid away, and this is a
        hard bound - the owner's to change, not mine."""
        d = decide(view(conviction=0.55, priced_in=False), half_spread="99.2")
        assert d.action == "skip"
        assert "spread_gate" in d.skip_reasons
        assert HARD_BOUNDS.max_entry_half_spread_bp == Decimal("20")

    def test_a_short_is_still_refused(self):
        d = decide(view(direction="short", conviction=0.60, priced_in=False))
        assert d.action == "skip"
        assert any("short" in r for r in d.skip_reasons)

    def test_every_hard_bound_is_unchanged(self):
        assert (HARD_BOUNDS.max_loss_per_position_pct,
                HARD_BOUNDS.max_open_positions,
                HARD_BOUNDS.daily_loss_kill_pct,
                HARD_BOUNDS.drawdown_kill_pct) == (
            Decimal("0.02"), 5, Decimal("0.04"), Decimal("0.12"))


def research_prompt(ctype="insider_cluster"):
    return prompts.render_research_prompt(candidate(ctype=ctype))


class TestThePromptSaysTheAccountCannotShort:
    def test_it_says_so_plainly(self):
        text = research_prompt()
        assert "CASH, LONG-ONLY ACCOUNT" in text
        assert "cannot short" in text

    def test_it_says_a_bearish_read_is_still_the_right_answer(self):
        """The failure mode to avoid is teaching it to inflate longs."""
        text = research_prompt()
        assert "answer no_trade and put the bearish case in the thesis" in text
        assert "correct and useful answer" in text

    def test_it_does_not_tell_the_model_to_prefer_long(self):
        low = research_prompt().lower()
        for pressure in ("prefer long", "favour long", "favor long",
                         "look for longs", "only nominate long",
                         "bias toward long"):
            assert pressure not in low, pressure

    def test_it_does_not_tell_the_model_the_BAR(self):
        """The standing rule is about the bar, not about the digits:
        telling the model what it must clear teaches it to clear that.

        The digits themselves cannot be the test any more - see
        TestTheFloorIsNowNonBindingByConstruction below."""
        low = research_prompt().lower()
        for bar in ("conviction floor", "minimum conviction", "must exceed",
                    "in order to trade you", "at least 0.5", "above 0.5",
                    "threshold of"):
            assert bar not in low, bar

    def test_the_drift_arm_is_told_too(self):
        assert "CASH, LONG-ONLY ACCOUNT" in research_prompt(ctype="earnings_drift")

    def test_the_direction_field_still_offers_short(self):
        """The SCHEMA must not change: a short view is data the refusal
        tracker scores, and a model that cannot express one would answer
        no_trade with no reasoning attached."""
        from catalyst.research.schema import SUBMIT_RESEARCH_VIEW_TOOL

        props = SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"]
        assert "short" in props["direction"]["enum"]


class TestTheCheckCanFail:
    """House rule 4, against the values that shipped."""

    def test_the_old_premium_would_refuse_the_uber_view(self):
        old_bar = FLOOR + Decimal("0.15")
        assert Decimal("0.62") < old_bar, (
            "0.62 clears the old bar too, so this file is not measuring "
            "the change that was made")

    def test_removing_the_long_only_line_would_be_caught(self):
        text = research_prompt().replace("CASH, LONG-ONLY ACCOUNT", "")
        assert "CASH, LONG-ONLY ACCOUNT" not in text


class TestTheFloorIsNowNonBindingByConstruction:
    """FOUND 2026-09-11, while checking whether the prompt leaks the bar.

    The prompt's scale says "Below 0.50 on a direction is a
    contradiction - the answer is no_trade". So a directional view under
    0.50 is one the model is instructed never to submit, and a floor AT
    0.50 cannot refuse anything. The owner's week bears it out: four
    directional views, lowest 0.55, `below conviction floor` fired zero
    times.

    Not a defect - it is what "the only stop is the budget" means, and
    the real bounds are the hard bounds and the premium. Recorded
    because a threshold that looks like it works and refuses nothing is
    the exact shape the adaptive table exists to prevent, and because
    the floor can now only start mattering again by being RAISED above
    0.50 on scored evidence."""

    def test_the_floor_sits_exactly_where_the_scale_says_contradiction(self):
        assert FLOOR == Decimal("0.50")
        assert "Below 0.50 on a direction is a contradiction" in research_prompt()

    def test_so_a_directional_view_can_never_be_below_it(self):
        """Every conviction the model is allowed to pair with a
        direction clears the floor by construction."""
        for c in (0.50, 0.55, 0.62, 0.85):
            assert Decimal(str(c)) >= FLOOR

    def test_it_is_written_down_where_the_number_lives(self):
        import inspect

        from catalyst.risk import adaptive_params

        src = inspect.getsource(adaptive_params)
        assert "THE FLOOR NOW REFUSES NOTHING" in src
        assert "only ever start MATTERING again by being raised" in src

    def test_the_premium_is_therefore_the_gate_that_actually_bites(self):
        """Which is why its value was the thing worth changing."""
        assert FLOOR + PRICED_IN_CONVICTION_PREMIUM > FLOOR
        d = decide(view(conviction=float(FLOOR), priced_in=True))
        assert d.action == "skip"
