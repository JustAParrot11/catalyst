"""Size against the stock, not just its catalyst's category.

MEASURED, 2026-08-15, over 8,432,494 overnight gaps across 4,674 tickers
with ten years of daily bars (scripts/measure_adverse_gaps.py, offline):

    all tickers, worst day each:  median 18.6%  p75 31.4%  p90 50.7%
    volatile decile, worst day:   median 35.3%  p75 52.1%  p90 66.3%

The finding that mattered was not the one expected. Unconditionally a
60% gap is beyond the 99.9th percentile, which makes the shipped
assumption look absurd. But conditioned on each ticker's WORST day - the
closest available proxy for the binary event the parameter exists for -
0.60 sits around the 88th percentile for a volatile name. It was
invented, but it is not wrong.

THE DEFECT IS THAT IT IS ONE NUMBER. A blanket 0.60 sizes a $40bn pharma
with a PDUFA date exactly as it sizes a $50m microcap. The median
ticker's worst day in a decade is 18.6%; sizing it against 60% cuts its
position to under a third of what its own history justifies, silently.

And the gap was not even the binding constraint - `max(gap, stop)` meant
the 0.50 category stop width capped those positions regardless. Only
measuring both showed that, and only fixing both moves anything.

THE SAFETY DIRECTION IS THE POINT OF THIS FILE. Per-stock evidence may
only ever TIGHTEN. The category value is a ceiling and the fallback on
any doubt, so a missing or unreadable history can make a position
smaller but never larger. Hard bounds are untouched throughout.
"""

import csv
from decimal import Decimal

import pytest

from catalyst.risk import stock_gap


def write_bars(tmp_path, ticker, moves, start=100.0, sessions=300):
    """A CSV of `sessions` bars where `moves` are injected as overnight
    gaps (open against the previous close)."""
    rows, price = [], start
    for i in range(sessions):
        gap = moves[i] if i < len(moves) else 0.0
        open_ = price * (1 + gap)
        close = open_
        rows.append({"date": f"2020-01-{i % 28 + 1:02d}", "open": f"{open_:.4f}",
                     "high": f"{open_:.4f}", "low": f"{open_:.4f}",
                     "close": f"{close:.4f}", "volume": "1000000"})
        price = close
    path = tmp_path / f"{ticker}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return tmp_path


class TestPerStockEvidenceCanOnlyEverTighten:
    """The safety invariant. Everything else is an optimisation."""

    def test_a_calm_stock_never_exceeds_its_category(self, tmp_path):
        """Even a stock that has never gapped at all is capped by the
        catalyst type's own value."""
        write_bars(tmp_path, "CALM", [-0.01] * 5)
        gap, _ = stock_gap.effective_gap("earnings", Decimal("0.14"),
                                         tmp_path, "CALM")
        assert gap <= Decimal("0.14")

    def test_a_wild_stock_is_capped_at_its_category_too(self, tmp_path):
        """A history worse than the category does not widen the gap
        beyond it - the category remains the ceiling in both
        directions, so this can never enlarge a position."""
        write_bars(tmp_path, "WILD", [-0.85, 5.0, -0.70])
        gap, why = stock_gap.effective_gap("earnings", Decimal("0.14"),
                                           tmp_path, "WILD")
        assert gap == Decimal("0.14")
        assert "category value stands" in why

    def test_no_history_falls_back_to_the_category(self, tmp_path):
        gap, why = stock_gap.effective_gap("fda_decision", Decimal("0.60"),
                                           tmp_path, "NOSUCH")
        assert gap == Decimal("0.60")
        assert "no usable price history" in why

    def test_too_little_history_falls_back(self, tmp_path):
        """Under a year cannot describe a tail."""
        write_bars(tmp_path, "NEW", [-0.02] * 5, sessions=100)
        assert stock_gap.worst_overnight_gap(tmp_path, "NEW") is None

    def test_an_unreadable_file_never_raises_into_sizing(self, tmp_path):
        (tmp_path / "JUNK.csv").write_text("this is not a csv\x00\x01")
        gap, why = stock_gap.effective_gap("earnings", Decimal("0.14"),
                                           tmp_path, "JUNK")
        assert gap == Decimal("0.14")


class TestTheBinaryFloor:
    def test_a_quiet_stock_still_gets_the_event_floor(self, tmp_path):
        """A calm history means a binary has not happened YET, not that
        it cannot. Sizing on that calm is how a surprise CRL takes a
        position that was never stress-tested."""
        write_bars(tmp_path, "QUIET", [-0.03, -0.02])
        gap, why = stock_gap.effective_gap("fda_decision", Decimal("0.60"),
                                           tmp_path, "QUIET")
        assert gap == stock_gap.MIN_EVENT_GAP
        assert "has not happened yet" in why

    def test_the_floor_is_the_measured_median_worst_day(self):
        """0.20 is not a preference - it is the median worst-day gap
        across all 4,674 measured tickers (18.6%), rounded up."""
        assert stock_gap.MIN_EVENT_GAP == Decimal("0.20")

    def test_a_non_binary_type_gets_no_such_floor(self, tmp_path):
        """An earnings date is not a binary. A quiet stock may be sized
        against its own quiet history."""
        write_bars(tmp_path, "QUIET2", [-0.03, -0.02])
        gap, _ = stock_gap.effective_gap("earnings", Decimal("0.14"),
                                         tmp_path, "QUIET2")
        assert gap < stock_gap.MIN_EVENT_GAP


class TestTheStopIsWhatActuallyBinds:
    """Sizing uses max(gap, stop). For the binaries the category stop is
    0.50 - wider than almost any measured gap - so lowering the gap
    alone changes nothing at all."""

    def test_a_calm_stock_gets_a_tighter_stop(self, tmp_path):
        write_bars(tmp_path, "CALM3", [-0.005] * 20)
        stop, why = stock_gap.effective_stop("fda_decision", Decimal("0.50"),
                                             tmp_path, "CALM3")
        assert stop < Decimal("0.50")
        assert stop >= stock_gap.MIN_STOP_WIDTH

    def test_a_volatile_stock_keeps_the_category_stop(self, tmp_path):
        # Price-NEUTRAL pair (0.75 x 1.3333 = 1.0). A pattern that
        # compounds downward walks the price under the $1 filter within
        # about 36 sessions, leaving too little history to measure - the
        # fixture would then be testing the no-history path instead.
        write_bars(tmp_path, "WILD3", [-0.25, 1 / 3] * 150)
        stop, why = stock_gap.effective_stop("fda_decision", Decimal("0.50"),
                                             tmp_path, "WILD3")
        assert stop == Decimal("0.50")
        assert "category value stands" in why

    def test_the_stop_never_goes_below_the_noise_floor(self, tmp_path):
        """A stop inside the spread and the day's chop is churn, not
        protection."""
        write_bars(tmp_path, "FLAT", [0.0] * 5)
        stop, _ = stock_gap.effective_stop("earnings", Decimal("0.16"),
                                           tmp_path, "FLAT")
        assert stop >= stock_gap.MIN_STOP_WIDTH

    def test_no_history_keeps_the_category_stop(self, tmp_path):
        stop, why = stock_gap.effective_stop("fda_decision", Decimal("0.50"),
                                             tmp_path, "GONE")
        assert stop == Decimal("0.50")
        assert "no usable price history" in why


class TestItReachesTheActualPositionSize:
    def test_a_calm_large_cap_gets_a_bigger_position(self, tmp_path):
        """The whole point. Sized against its own history rather than
        its category's worst case."""
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        write_bars(tmp_path, "BIGPHARMA", [-0.01] * 30)
        params = {"stop_width": {"fda_decision": Decimal("0.50")},
                  "adverse_gap_assumption": {"fda_decision": Decimal("0.60")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=__import__("datetime").datetime(
                2026, 8, 15, tzinfo=__import__("datetime").timezone.utc),
            reliable=True)
        market = MarketSnapshot(
            ticker="BIGPHARMA", last_close=Decimal("50"),
            half_spread_bp=Decimal("5"),
            median_daily_dollar_volume=Decimal("0"))

        without = size(True, "fda_decision", portfolio, params, HARD_BOUNDS,
                       market)
        with_hist = size(True, "fda_decision", portfolio, params, HARD_BOUNDS,
                         market, bars_dir=str(tmp_path))
        assert with_hist.notional_usd > without.notional_usd, (
            f"per-stock history did not increase the position: "
            f"{without.notional_usd} -> {with_hist.notional_usd}")

    def test_it_records_WHY_beside_the_size(self, tmp_path):
        """"Why is this position that size" is the question the decision
        page exists to answer, and a rule name plus two numbers cannot
        answer it once the bound is derived from history."""
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        write_bars(tmp_path, "EXPLAIN", [-0.01] * 30)
        params = {"stop_width": {"earnings": Decimal("0.16")},
                  "adverse_gap_assumption": {"earnings": Decimal("0.14")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=__import__("datetime").datetime(
                2026, 8, 15, tzinfo=__import__("datetime").timezone.utc),
            reliable=True)
        market = MarketSnapshot(
            ticker="EXPLAIN", last_close=Decimal("50"),
            half_spread_bp=Decimal("5"),
            median_daily_dollar_volume=Decimal("0"))
        res = size(True, "earnings", portfolio, params, HARD_BOUNDS, market,
                   bars_dir=str(tmp_path))
        notes = [l.note for l in res.limits_applied
                 if l.rule_name.startswith("per_stock")]
        assert len(notes) == 2, "both per-stock bounds should be recorded"
        for note in notes:
            assert "EXPLAIN" in note and len(note.split()) > 8, (
                f"not an explanation: {note!r}")

    def test_omitting_bars_dir_changes_nothing(self, tmp_path):
        """Every existing caller must behave exactly as before."""
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        params = {"stop_width": {"earnings": Decimal("0.16")},
                  "adverse_gap_assumption": {"earnings": Decimal("0.14")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=__import__("datetime").datetime(
                2026, 8, 15, tzinfo=__import__("datetime").timezone.utc),
            reliable=True)
        market = MarketSnapshot(
            ticker="ANY", last_close=Decimal("50"),
            half_spread_bp=Decimal("5"),
            median_daily_dollar_volume=Decimal("0"))
        res = size(True, "earnings", portfolio, params, HARD_BOUNDS, market)
        assert not [l for l in res.limits_applied
                    if l.rule_name.startswith("per_stock")]
        # 2% of 2000 = 40, worst case max(0.14, 0.16) = 0.16 -> 250
        assert res.notional_usd == Decimal("250.00")


class TestHardBoundsAreUntouched:
    def test_the_position_still_obeys_the_slot_ceiling(self, tmp_path):
        """No amount of per-stock evidence may breach a hard bound."""
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        write_bars(tmp_path, "TINYVOL", [0.0] * 5)
        params = {"stop_width": {"earnings": Decimal("0.16")},
                  "adverse_gap_assumption": {"earnings": Decimal("0.14")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=__import__("datetime").datetime(
                2026, 8, 15, tzinfo=__import__("datetime").timezone.utc),
            reliable=True)
        market = MarketSnapshot(
            ticker="TINYVOL", last_close=Decimal("50"),
            half_spread_bp=Decimal("5"),
            median_daily_dollar_volume=Decimal("0"))
        res = size(True, "earnings", portfolio, params, HARD_BOUNDS, market,
                   bars_dir=str(tmp_path))
        slot = Decimal("2000") / HARD_BOUNDS.max_open_positions
        assert res.notional_usd <= slot, (
            "per-stock evidence breached the equal-weight slot ceiling")


class TestUnusableHistoryAlwaysFallsBackToTheCategory:
    """Found by stress-sweeping the real reader, not by reading it.

    Fifteen kinds of unusable bar file were pushed through
    `effective_gap` and `effective_stop`. None raised - but SIX of them
    produced a stop of 8% where the category says 50%, because they
    parse into CONSTANT prices: a column of 1s, a file whose only
    readable column is `close`, one mangled by a BOM. Every daily move
    is then zero, which reads as "this stock never moves" and puts the
    stop on the MIN_STOP_WIDTH floor.

    Not dangerous - the adverse gap still dominates sizing, so the
    position does not grow - but an 8% stop on a biotech binary is taken
    out by ordinary noise, and the cause would have been an unreadable
    file rather than measured risk. No real security has a
    95th-percentile daily move of zero over a year; a file claiming
    otherwise is telling us it is not real.
    """

    CASES = {
        "empty file": "",
        "header only": "date,open,high,low,close,volume\n",
        "no header": "2024-01-01,1,1,1,1,1\n" * 400,
        "NaN prices": "date,open,high,low,close,volume\n"
                      + "2024-01-01,NaN,NaN,NaN,NaN,1\n" * 400,
        "Infinity": "date,open,high,low,close,volume\n"
                    + "2024-01-01,Infinity,1,1,1,1\n" * 400,
        "negative prices": "date,open,high,low,close,volume\n"
                           + "2024-01-01,-5,-5,-5,-5,1\n" * 400,
        "zero prices": "date,open,high,low,close,volume\n"
                       + "2024-01-01,0,0,0,0,0\n" * 400,
        "text prices": "date,open,high,low,close,volume\n"
                       + "2024-01-01,abc,def,ghi,jkl,1\n" * 400,
        "missing columns": "date,close\n" + "2024-01-01,5\n" * 400,
        "null bytes": "date,open,high,low,close,volume\n"
                      + "2024-01-01,\x00,1,1,1,1\n" * 400,
        "BOM and CRLF": "﻿date,open,high,low,close,volume\r\n"
                        + "2024-01-01,1,1,1,1,1\r\n" * 400,
        "huge numbers": "date,open,high,low,close,volume\n"
                        + "2024-01-01,1e308,1e308,1e308,1e308,1\n" * 400,
        "unicode prices": "date,open,high,low,close,volume\n"
                          + "2024-01-01,\U0001f642,1,1,1,1\n" * 400,
        "ragged rows": "date,open,high,low,close,volume\n"
                       + "2024-01-01,1\n" * 400,
        "constant prices": "date,open,high,low,close,volume\n"
                           + "2024-01-01,7,7,7,7,1\n" * 400,
    }

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_both_bounds_fall_back(self, tmp_path, name):
        (tmp_path / "ZZZZ.csv").write_text(self.CASES[name], encoding="utf-8")
        gap, _ = stock_gap.effective_gap("fda_decision", Decimal("0.60"),
                                         tmp_path, "ZZZZ")
        stop, _ = stock_gap.effective_stop("fda_decision", Decimal("0.50"),
                                           tmp_path, "ZZZZ")
        assert gap == Decimal("0.60"), f"{name}: gap became {gap}"
        assert stop == Decimal("0.50"), (
            f"{name}: stop became {stop} - an unreadable file produced a "
            f"tighter stop than the category, which is churn caused by bad "
            f"data rather than by measured risk")

    def test_a_directory_where_a_file_belongs(self, tmp_path):
        (tmp_path / "DIRR.csv").mkdir()
        gap, _ = stock_gap.effective_gap("earnings", Decimal("0.14"),
                                         tmp_path, "DIRR")
        assert gap == Decimal("0.14")

    def test_an_unreadable_file(self, tmp_path):
        import os
        import stat

        path = tmp_path / "NOPE.csv"
        path.write_text("x")
        os.chmod(path, 0)
        try:
            gap, _ = stock_gap.effective_gap("earnings", Decimal("0.14"),
                                             tmp_path, "NOPE")
            assert gap == Decimal("0.14")
        finally:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def test_zero_volatility_is_read_as_no_data(self, tmp_path):
        """The specific mechanism, pinned on its own."""
        (tmp_path / "FLAT.csv").write_text(
            "date,open,high,low,close,volume\n"
            + "2024-01-01,7,7,7,7,1\n" * 400)
        assert stock_gap.daily_move_percentile(tmp_path, "FLAT") is None
