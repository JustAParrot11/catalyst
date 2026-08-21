"""Live quotes on the dashboard, and the honesty that comes with them.

OWNER-ASKED 2026-08-21: "yes add live quotes".

Until now the dashboard read a database and a bar cache and could only
mark a position to its last cached daily close. It now takes a real
quote where one can be had.

THREE RULES, and every test here defends one of them:

  1. IT CANNOT TAKE THE PAGE DOWN. A quote is a nicety; the page is the
     instrument. Every failure returns a reason and the mark falls back
     to the cached close.
  2. IT APPLIES THE TRADING PATH'S OWN SANITY RULES. Stale, non-positive
     or crossed quotes are refused exactly as build_market_snapshot
     refuses them. A dashboard willing to show a price the risk engine
     would reject is a dashboard quietly disagreeing with the bot about
     what a price is.
  3. IT NEVER DECIDES ANYTHING. No sizing, no order, no threshold may
     import it.

Everything below is offline: quote_from_payload is pure, and the
fetching tests inject a fake broker.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.dashboard import live


def payload(bid="5.00", ask="5.04", at=None, key="ap"):
    at = at or datetime.now(timezone.utc)
    return {"quote": {"bp": bid, key: ask,
                      "t": at.isoformat().replace("+00:00", "Z")}}


class FakeBroker:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get_latest_quote(self, symbol):
        self.asked.append(symbol)
        a = self.answers[symbol]
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture(autouse=True)
def _clean():
    live.clear_cache()
    yield
    live.clear_cache()


class TestItValidatesLikeTheTradingPath:
    def test_a_good_quote_becomes_a_mid(self):
        q = live.quote_from_payload("EMBC", payload("5.00", "5.04"))
        assert q.live
        assert q.mid == Decimal("5.02")
        assert q.spread_bp == Decimal("39.8")

    def test_a_STALE_quote_is_refused(self):
        """Off hours the endpoint returns the session's last quote. It
        is not wrong, it is just not NOW - and marking a book to it
        while calling it live is the whole danger."""
        old = datetime.now(timezone.utc) - timedelta(minutes=45)
        q = live.quote_from_payload("EMBC", payload(at=old))
        assert not q.live
        assert "minutes old" in q.error

    def test_the_age_gate_is_the_one_the_bot_uses(self):
        from catalyst.orchestrator.cycle import MAX_QUOTE_AGE

        assert live.MAX_QUOTE_AGE == MAX_QUOTE_AGE, (
            "the dashboard and the trading path disagree about when a "
            "quote is too old to trust")

    def test_a_CROSSED_quote_is_refused(self):
        q = live.quote_from_payload("EMBC", payload("5.10", "5.00"))
        assert not q.live and "crossed" in q.error

    @pytest.mark.parametrize("bid,ask", [("0", "5"), ("5", "0"),
                                         ("-1", "5"), ("abc", "5")])
    def test_a_non_positive_or_unreadable_quote_is_refused(self, bid, ask):
        q = live.quote_from_payload("EMBC", payload(bid, ask))
        assert not q.live and q.error

    def test_an_undatable_quote_is_refused(self):
        q = live.quote_from_payload("EMBC", {"quote": {"bp": 5, "ap": 5.1}})
        assert not q.live and "timestamp" in q.error

    @pytest.mark.parametrize("body", [None, {}, [], "nope", {"quote": 3}])
    def test_a_malformed_body_never_raises(self, body):
        q = live.quote_from_payload("EMBC", body)
        assert not q.live and q.error

    def test_every_refusal_carries_a_REASON(self):
        """House rule 3. "No price" and "the call is broken" must not
        look the same."""
        for body in (None, {}, payload("5.10", "5.00"),
                     payload(at=datetime.now(timezone.utc)
                             - timedelta(hours=3))):
            q = live.quote_from_payload("X", body)
            assert q.error, body


class TestItCannotTakeThePageDown:
    def test_one_symbol_blowing_up_does_not_lose_the_others(self):
        b = FakeBroker({"AAA": payload(), "BBB": RuntimeError("boom")})
        got = live.quotes_for(["AAA", "BBB"], broker=b)
        assert got["AAA"].live
        assert not got["BBB"].live and "RuntimeError" in got["BBB"].error

    def test_no_ticker_is_ever_silently_dropped(self):
        """A missing row is worse than a row saying why: the reader
        cannot tell a quote failure from a position that vanished."""
        b = FakeBroker({"AAA": RuntimeError("x"), "BBB": RuntimeError("y")})
        got = live.quotes_for(["AAA", "BBB"], broker=b)
        assert set(got) == {"AAA", "BBB"}

    def test_no_credentials_is_a_reason_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(live, "_broker",
                            lambda: (None, "no Alpaca credentials are saved"))
        got = live.quotes_for(["AAA"])
        assert not got["AAA"].live
        assert "credentials" in got["AAA"].error

    def test_asking_for_nothing_calls_nothing(self):
        b = FakeBroker({})
        assert live.quotes_for([], broker=b) == {}
        assert b.asked == []


class TestItDoesNotHammerTheBroker:
    def test_a_repeat_within_the_ttl_is_served_from_cache(self):
        b = FakeBroker({"AAA": payload()})
        live.quotes_for(["AAA"], broker=b)
        live.quotes_for(["AAA"], broker=b)
        assert b.asked == ["AAA"], "a refresh re-quoted inside the TTL"

    def test_a_FAILED_quote_is_not_cached(self):
        """Caching a failure would keep the page broken for the whole
        TTL after the cause has cleared."""
        b = FakeBroker({"AAA": RuntimeError("transient")})
        live.quotes_for(["AAA"], broker=b)
        live.quotes_for(["AAA"], broker=b)
        assert b.asked == ["AAA", "AAA"]

    def test_the_cache_can_be_bypassed(self):
        b = FakeBroker({"AAA": payload()})
        live.quotes_for(["AAA"], broker=b)
        live.quotes_for(["AAA"], broker=b, use_cache=False)
        assert len(b.asked) == 2


class TestItNeverDecidesAnything:
    @pytest.mark.parametrize("module", [
        "catalyst.risk.sizing", "catalyst.risk.hard_bounds",
        "catalyst.risk.kill_switches", "catalyst.execution.orders",
        "catalyst.orchestrator.cycle", "catalyst.research.position_review",
        "catalyst.cost.governor",
    ])
    def test_no_trading_module_imports_the_live_quote_layer(self, module):
        """The moment a display path can influence a decision, "it is
        only the UI" stops being true."""
        import importlib
        import inspect

        src = inspect.getsource(importlib.import_module(module))
        assert "dashboard.live" not in src
        assert "dashboard import live" not in src

    def test_it_only_ever_READS(self):
        import inspect

        src = inspect.getsource(live)
        for forbidden in ("submit_order", "cancel", "post", "POST",
                          "delete", "DELETE"):
            assert forbidden not in src, (
                f"{forbidden!r} in a module that may only take quotes")


class TestTheBookSaysWhichKindOfPriceItIs:
    def test_a_live_quote_marks_the_position_and_is_labelled(self, tmp_path,
                                                             monkeypatch):
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db
        from tests.test_trades_page import _seed

        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        db = Db(_seed(tmp_path))
        try:
            q = live.quote_from_payload("EMBC", payload("6.00", "6.02"))
            book = queries.live_book(db, quotes={"EMBC": q})
        finally:
            db.close()
        pos = book.positions[0]
        assert pos.source == "live"
        assert pos.last == Decimal("6.01")
        assert book.n_live == 1

    def test_a_failed_quote_falls_back_and_says_so(self, tmp_path,
                                                  monkeypatch):
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db
        from tests.test_trades_page import _seed

        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "bars"))
        db = Db(_seed(tmp_path))
        try:
            bad = live.quote_from_payload("EMBC", {})
            book = queries.live_book(db, quotes={"EMBC": bad})
        finally:
            db.close()
        pos = book.positions[0]
        assert pos.source != "live"
        assert book.n_live == 0
        assert book.quote_note
