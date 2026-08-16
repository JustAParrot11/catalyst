"""A number the model said can never become a number the bot trades on.

OWNER-ASKED: "if it finds a new article that says trading at $35 will it
treat it as gospel or ask alpaca and find its actually $37. I want to
ensure all data is correct and validated so we arent trading under false
pretenses."

THE ANSWER IS ARCHITECTURAL, NOT A CHECK SOMEWHERE. The model is not
trusted with prices because IT IS NEVER ASKED FOR ONE. Its submission
tool has exactly eight fields:

    direction, conviction, thesis, invalidation,
    expected_holding_days, priced_in, priced_in_reasoning, findings

There is no price, no target, no quantity, no notional. A "$35" read off
an article can only ever land in `thesis`, `priced_in_reasoning` or a
`findings` entry - free text, kept verbatim for the audit trail, read by
no arithmetic anywhere. The model cannot pass a price along even if it
is certain of one, so it cannot pass a WRONG one.

Every number that touches money comes from Alpaca instead:

    price   build_market_snapshot() reads the live NBBO and uses the
            MID of bid and ask, refusing a quote older than ten minutes,
            or one with a non-positive or crossed book.
    qty     sizing computes it from that price and the risk budget.
    stop    sizing computes it from that price and the stop width.
    entry   a MARKET order - no limit price exists to be wrong.
    fill    whatever the broker reports, recorded verbatim beside any
            modelled figure, never instead of it (TRAPS.md).

This file pins that shape. It is the kind of property that would be
discovered broken by losing money, because a model-supplied price would
work perfectly right up until the one time it was hallucinated.
"""

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.execution.broker import Broker
from catalyst.orchestrator.cycle import MAX_QUOTE_AGE, build_market_snapshot

NOW = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)


def quote_broker(bid="36.98", ask="37.02", age_minutes=0, **over):
    """An Alpaca that quotes $37 - whatever an article might claim."""
    at = (NOW - timedelta(minutes=age_minutes)).isoformat().replace(
        "+00:00", "Z")
    body = {"quote": {"bp": bid, "ap": ask, "t": at, **over}}

    def handler(request):
        return httpx.Response(200, json=body)

    return Broker("k", "s", transport=httpx.MockTransport(handler))


class TestTheModelIsNeverAskedForAPrice:
    """The reason a hallucinated price cannot reach an order."""

    def test_its_submission_tool_has_no_price_field(self):
        from catalyst.research.boundary import SUBMIT_RESEARCH_VIEW_TOOL

        props = SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"]
        banned = ("price", "target", "entry", "stop", "limit", "qty",
                  "quantity", "size", "notional", "usd", "dollar", "shares")
        offenders = [
            name for name in props
            if any(word in name.lower() for word in banned)
            # priced_in / priced_in_reasoning are a BOOLEAN judgement and
            # its prose, not a figure.
            and name not in ("priced_in", "priced_in_reasoning")]
        assert not offenders, (
            f"the model can now submit {offenders} - a number it invented "
            "could reach the risk engine")

    def test_the_view_it_returns_carries_no_price(self):
        import dataclasses

        from catalyst.research.schema import ResearchView

        names = {f.name for f in dataclasses.fields(ResearchView)}
        assert not (names & {"price", "target_price", "entry_price",
                             "stop_price", "qty", "notional_usd"})

    def test_priced_in_is_a_BOOLEAN_not_a_level(self):
        """"Already priced in" is a judgement the code turns into a
        higher conviction bar. If it ever became a number, that number
        would be the model's own and would size a position."""
        from catalyst.research.boundary import SUBMIT_RESEARCH_VIEW_TOOL

        props = SUBMIT_RESEARCH_VIEW_TOOL["input_schema"]["properties"]
        assert props["priced_in"]["type"] == "boolean"

    def test_the_thesis_is_documented_as_audit_trail_only(self):
        from catalyst.research import schema

        src = inspect.getsource(schema)
        assert "audit trail only" in src, (
            "the thesis is free text a model wrote; if anything ever parses "
            "a figure out of it, that figure is the model's not the market's")


class TestThePriceComesFromAlpaca:
    def test_the_snapshot_price_is_the_brokers_mid_not_anything_else(self):
        """An article says $35. Alpaca says 36.98/37.02. The bot uses
        the mid of the real book."""
        broker = quote_broker(bid="36.98", ask="37.02")
        try:
            snap = build_market_snapshot(broker, "AAA", NOW)
        finally:
            broker.close()
        assert snap is not None
        assert snap.last_close == Decimal("37.00"), (
            f"used {snap.last_close}, not the mid of the live book")
        assert snap.last_close != Decimal("35"), "took the article's word"

    def test_a_stale_quote_is_refused_outright(self):
        """Sizing off a stale book is a guess wearing a measurement's
        clothes - and off-hours "latest" quotes are Friday's."""
        broker = quote_broker(age_minutes=int(
            MAX_QUOTE_AGE.total_seconds() / 60) + 5)
        try:
            assert build_market_snapshot(broker, "AAA", NOW) is None
        finally:
            broker.close()

    def test_a_fresh_quote_is_accepted(self):
        broker = quote_broker(age_minutes=1)
        try:
            assert build_market_snapshot(broker, "AAA", NOW) is not None
        finally:
            broker.close()

    @pytest.mark.parametrize("bid,ask", [
        ("0", "37.02"), ("36.98", "0"), ("-1", "37.02"),
        ("37.10", "37.00"),          # crossed book
        ("abc", "37.02"), ("NaN", "37.02"), ("Infinity", "1"),
    ])
    def test_an_impossible_book_produces_no_price_at_all(self, bid, ask):
        """None, not a guess. A candidate with no trustworthy price is
        not sized rather than sized on a bad one."""
        broker = quote_broker(bid=bid, ask=ask)
        try:
            assert build_market_snapshot(broker, "AAA", NOW) is None
        finally:
            broker.close()

    def test_an_undatable_quote_is_refused(self):
        """A quote that cannot prove its age cannot pass a freshness
        gate, so it does not get the benefit of the doubt."""
        def handler(request):
            return httpx.Response(200, json={"quote": {"bp": "1", "ap": "2"}})

        broker = Broker("k", "s", transport=httpx.MockTransport(handler))
        try:
            assert build_market_snapshot(broker, "AAA", NOW) is None
        finally:
            broker.close()


class TestEveryTradedNumberDescendsFromThatPrice:
    def _sized(self, last_close):
        from catalyst.risk import MarketSnapshot, PortfolioState
        from catalyst.risk.hard_bounds import HARD_BOUNDS
        from catalyst.risk.sizing import size

        params = {"stop_width": {"earnings": Decimal("0.16")},
                  "adverse_gap_assumption": {"earnings": Decimal("0.14")}}
        portfolio = PortfolioState(
            equity_usd=Decimal("2000"), settled_cash_usd=Decimal("2000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("2000"), consecutive_losses=0,
            as_of=NOW, reliable=True)
        market = MarketSnapshot(ticker="AAA", last_close=Decimal(last_close),
                                half_spread_bp=Decimal("5"),
                                median_daily_dollar_volume=Decimal("0"))
        return size(True, "earnings", portfolio, params, HARD_BOUNDS, market)

    def test_qty_follows_the_real_price_not_the_article(self):
        """At $37 you buy fewer shares than at $35 for the same money.
        If qty ever came out the same, the price used was not the one
        passed in."""
        at_37 = self._sized("37.00")
        at_35 = self._sized("35.00")
        assert at_37.qty < at_35.qty

    def test_the_stop_is_derived_from_the_real_price(self):
        res = self._sized("37.00")
        # 37.00 x (1 - 0.16) = 31.08
        assert res.stop_price == Decimal("31.08"), (
            f"stop at {res.stop_price} does not descend from the live price")

    def test_sizing_takes_no_price_argument_from_anywhere_else(self):
        """The signature itself is the guarantee: there is no parameter
        a model-supplied price could arrive through."""
        from catalyst.risk.sizing import size

        params = set(inspect.signature(size).parameters)
        assert not (params & {"price", "target_price", "entry_price",
                              "model_price", "view"})

    def test_the_entry_is_a_market_order_with_no_limit_price(self):
        """A limit price is the one field a wrong number could occupy.
        There is not one."""
        from catalyst.execution import orders

        src = inspect.getsource(orders.place)
        assert 'order_type="market"' in src
        assert "limit" not in src.lower(), (
            "the entry carries a limit price - where did its value come "
            "from?")


class TestTheFillIsWhateverTheBrokerSays:
    def test_the_brokers_price_is_recorded_verbatim(self, tmp_path):
        """TRAPS.md: model the spread, but record it BESIDE the broker's
        price, never instead of it - reconciliation compares against the
        real fill."""
        from catalyst.execution.reconcile import reconcile
        from catalyst.storage import init_db

        conn = init_db(str(tmp_path / "c.db"))
        try:
            # orders.decision_id references candidates(id), and
            # production runs with foreign keys ON.
            conn.execute(
                "INSERT INTO candidates VALUES ('c1','AAA','earnings',"
                "'2026-08-20','estimated','[]',?,'tech','[]')",
                (NOW.isoformat(),))
            conn.execute(
                "INSERT INTO orders VALUES ('o1','c1','b1','buy','4',"
                "'market','day',?,'accepted','{}')", (NOW.isoformat(),))
            conn.commit()

            class B:
                def get_order_by_client_id(self, cid):
                    return {"id": "b1", "status": "filled",
                            "filled_qty": "4", "filled_avg_price": "37.0100",
                            "filled_at": NOW.isoformat()}

                def get_order(self, oid):
                    return self.get_order_by_client_id(oid)

            reconcile(B(), conn)
            row = conn.execute(
                "SELECT price, broker_reported_price FROM fills").fetchone()
            assert row[1] == "37.0100", "the broker's own price was altered"
            assert row[0] == row[1], (
                "the recorded price differs from what the broker reported")
        finally:
            conn.close()

    def test_modelled_slippage_sits_beside_it_never_replaces_it(self):
        from catalyst.execution import reconcile

        src = inspect.getsource(reconcile)
        assert "broker_reported_price" in src and "modeled_slippage" in src
        assert "beside" in src.lower(), (
            "nothing states the modelled figure must not replace the real "
            "one - that is the trap this column exists for")


class TestAHallucinatedPriceGoesNowhere:
    def test_a_thesis_full_of_wrong_numbers_changes_no_arithmetic(self):
        """The end-to-end statement of the owner's question: the model
        insists the stock is $35, the book says $37, and every traded
        number follows $37."""
        from catalyst.research.schema import ResearchView

        view = ResearchView(
            candidate_id="c1", direction="long", conviction=0.9,
            thesis="Trading at $35, target $60, buy 500 shares at $35.00.",
            invalidation="It is not $35.",
            expected_holding_days=10, priced_in=False,
            priced_in_reasoning="At $35 this is clearly cheap.")
        # Every figure in that text is free-form prose. Nothing reads it.
        assert isinstance(view.thesis, str)
        assert not hasattr(view, "price")
        assert not hasattr(view, "qty")

        broker = quote_broker(bid="36.98", ask="37.02")
        try:
            snap = build_market_snapshot(broker, "AAA", NOW)
        finally:
            broker.close()
        assert snap.last_close == Decimal("37.00")
