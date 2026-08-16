"""The model was asked "are we too late" with the evidence withheld.

OWNER-ASKED: "determine if we're too late or still have time and then
re-evaluate the price and new news to see if its opinion has changed."

TWO DEFECTS, both live on every research call.

FIRST, IT WAS ASKED ABOUT HISTORY AND GIVEN THE PRESENT. Question 6 of
the brief asks "has the market already consumed these filings? ... what
price and volume have done since each filing became public". The MARKET
DATA block carried the last close, the spread, and nothing else - no
move, no trend, no range. The one question that decides whether an
opportunity is still open was asked with the answer withheld, leaving a
web search as the only route to it, and a search rarely returns "up 12%
since 5 August".

SECOND, AND WORSE, IT WAS TOLD SOMETHING FALSE. median_daily_dollar_
volume is populated as Decimal("0") - the snapshot's own comment says
"not consumed by any current sizing rule" - and rendered as:

    "median daily dollar volume: $0. Thin names move on little, and are
     also where a cluster is least likely to have been consumed
     already."

under a heading reading "measured at decision time (not from the
model)". A $60bn company arrived described as having no volume, with a
nudge attached saying that means the signal is probably still fresh.
Not a missing number - a wrong one wearing a measurement's clothes,
pointing the judgement in one direction on every call.

Both were free to fix: bar_history already caches three years of daily
bars for every candidate immediately before the risk engine runs.

WHAT THIS MUST NOT BECOME. It reports facts and draws no conclusion. "Is
that priced in" stays the model's judgement and what to do about it
stays deterministic code's. Handing over a number is not handing over an
opinion, and the tests below check the numbers are right rather than
that they lead anywhere in particular.
"""

import csv
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.data.price_action import price_action
from catalyst.research.prompts import render_market_section
from catalyst.risk import MarketSnapshot

START = date(2024, 1, 1)


def write(tmp_path, ticker, moves, *, start=100.0, volume=2_000_000,
          late_volume=None):
    """Daily bars where `moves` are the per-session returns."""
    rows, price = [], start
    for i, move in enumerate(moves):
        price *= (1 + move)
        vol = late_volume if (late_volume and i >= len(moves) - 5) else volume
        rows.append({"date": (START + timedelta(days=i)).isoformat(),
                     "open": f"{price:.4f}", "high": f"{price:.4f}",
                     "low": f"{price:.4f}", "close": f"{price:.4f}",
                     "volume": str(vol)})
    path = Path(tmp_path) / f"{ticker}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return tmp_path


class TestTheMoveSinceTheCatalystIsMeasured:
    """The number that answers "are we too late"."""

    def test_a_stock_that_already_ran_reports_the_run(self, tmp_path):
        moves = [0.0] * 380 + [0.02] * 20          # +~48% in the last 20
        write(tmp_path, "AAA", moves)
        pa = price_action(tmp_path, "AAA", since=START + timedelta(days=380))
        assert pa.measured
        assert pa.move_since_catalyst_pct > Decimal("30"), (
            f"a large completed move was reported as {pa.move_since_catalyst_pct}%")

    def test_a_flat_tape_after_the_catalyst_reports_flat(self, tmp_path):
        """The opposite case, and the one that means the opportunity may
        still be open."""
        write(tmp_path, "BBB", [0.0] * 400)
        pa = price_action(tmp_path, "BBB", since=START + timedelta(days=380))
        assert pa.move_since_catalyst_pct == Decimal("0.0")

    def test_it_counts_the_sessions_not_the_calendar_days(self, tmp_path):
        write(tmp_path, "CCC", [0.001] * 400)
        pa = price_action(tmp_path, "CCC", since=START + timedelta(days=390))
        assert pa.sessions_since_catalyst == 9

    def test_a_fall_since_the_catalyst_is_signed_correctly(self, tmp_path):
        moves = [0.0] * 380 + [-0.01] * 20
        write(tmp_path, "DDD", moves)
        pa = price_action(tmp_path, "DDD", since=START + timedelta(days=380))
        assert pa.move_since_catalyst_pct < 0


class TestTheOtherContextIsRight:
    def test_range_position_at_the_high_and_at_the_low(self, tmp_path):
        write(tmp_path, "HIGH", [0.002] * 300)
        assert price_action(tmp_path, "HIGH").range_position_pct == \
            Decimal("100.0")
        write(tmp_path, "LOW", [-0.002] * 300)
        assert price_action(tmp_path, "LOW").range_position_pct == \
            Decimal("0.0")

    def test_volume_is_the_real_median_not_zero(self, tmp_path):
        write(tmp_path, "VOL", [0.0] * 300, start=50.0, volume=1_000_000)
        pa = price_action(tmp_path, "VOL")
        # 50 x 1,000,000 = $50m a day
        assert pa.median_daily_dollar_volume == Decimal("50000000")

    def test_a_volume_spike_is_visible(self, tmp_path):
        """"The market noticed" has a measurable shape."""
        write(tmp_path, "SPIKE", [0.0] * 300, volume=1_000_000,
              late_volume=8_000_000)
        assert price_action(tmp_path, "SPIKE").recent_volume_ratio > Decimal("5")

    def test_a_quiet_name_reads_as_quiet(self, tmp_path):
        write(tmp_path, "QUIET", [0.0] * 300, volume=1_000_000)
        assert price_action(tmp_path, "QUIET").recent_volume_ratio == \
            Decimal("1.00")


class TestNoneMeansNotMeasuredNeverZero:
    """A zero is a claim. Every one of these must decline to make it."""

    def test_a_missing_file(self, tmp_path):
        pa = price_action(tmp_path, "NOSUCH")
        assert not pa.measured
        assert pa.median_daily_dollar_volume is None

    @pytest.mark.parametrize("body", [
        "", "date,close\n", "not a csv\x00",
        "date,open,high,low,close,volume\n2024-01-01,abc,1,1,1,1\n" * 3,
    ])
    def test_unusable_files_measure_nothing(self, tmp_path, body):
        (tmp_path / "JUNK.csv").write_text(body)
        pa = price_action(tmp_path, "JUNK")
        assert not pa.measured

    def test_no_catalyst_date_still_gives_the_rest(self, tmp_path):
        write(tmp_path, "EEE", [0.001] * 300)
        pa = price_action(tmp_path, "EEE", since=None)
        assert pa.move_since_catalyst_pct is None
        assert pa.range_position_pct is not None

    def test_a_catalyst_date_in_the_future_measures_no_move(self, tmp_path):
        write(tmp_path, "FFF", [0.001] * 300)
        pa = price_action(tmp_path, "FFF", since=date(2030, 1, 1))
        assert pa.move_since_catalyst_pct is None


class TestItReachesThePrompt:
    def _snapshot(self, action=None, vol=Decimal("0")):
        return MarketSnapshot(ticker="REGN", last_close=Decimal("612.40"),
                              half_spread_bp=Decimal("4.2"),
                              median_daily_dollar_volume=vol,
                              price_action=action)

    def test_the_move_is_stated_in_the_market_block(self, tmp_path):
        write(tmp_path, "REGN", [0.0] * 380 + [0.01] * 20,
              volume=1_000_000, late_volume=6_000_000)
        pa = price_action(tmp_path, "REGN", since=START + timedelta(days=380))
        text = render_market_section(self._snapshot(pa))
        assert "move since the catalyst date" in text
        assert "TOO LATE" in text.upper()
        assert "52-week range" in text
        assert "volume against its own median" in text

    def test_the_FALSE_zero_volume_claim_is_gone(self):
        """The defect: a $60bn company described as having no volume,
        with a nudge saying that means the signal is still fresh."""
        text = render_market_section(self._snapshot(vol=Decimal("0")))
        assert "$0" not in text, (
            "still telling the model the stock has no volume at all")
        assert "NOT MEASURED" in text

    def test_a_real_volume_figure_is_used_when_there_is_one(self, tmp_path):
        write(tmp_path, "REGN", [0.0] * 300, start=50.0, volume=1_000_000)
        pa = price_action(tmp_path, "REGN")
        text = render_market_section(self._snapshot(pa))
        assert "$50,000,000" in text

    def test_no_price_action_still_renders_and_says_nothing_false(self):
        text = render_market_section(self._snapshot(None))
        assert "last close" in text
        assert "move since the catalyst date" not in text, (
            "claiming a move that was never measured")

    def test_a_missing_snapshot_entirely_is_still_honest(self):
        text = render_market_section(None)
        assert "Unavailable" in text
        assert "unverified" in text


class TestItReportsFactsAndDrawsNoConclusion:
    def test_the_block_never_tells_the_model_what_to_conclude(self, tmp_path):
        """The model proposes; code disposes. A market block that said
        "so this is priced in" would be the research step deciding."""
        write(tmp_path, "GGG", [0.0] * 380 + [0.02] * 20)
        pa = price_action(tmp_path, "GGG", since=START + timedelta(days=380))
        text = render_market_section(self._snapshot_for(pa)).lower()
        for verdict in ("you should", "therefore trade", "do not trade",
                        "this is priced in", "conclude that"):
            assert verdict not in text, f"the block draws a conclusion: {verdict}"

    def _snapshot_for(self, action):
        return MarketSnapshot(ticker="GGG", last_close=Decimal("100"),
                              half_spread_bp=Decimal("5"),
                              median_daily_dollar_volume=Decimal("0"),
                              price_action=action)
