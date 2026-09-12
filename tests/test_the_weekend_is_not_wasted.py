"""Research while the market is shut, then gate Monday's entry in code.

OWNER-ASKED 2026-09-12: *"news is released on the weekend aswell right?
is there any harm in doing a deep dive into the news to find potential
for monday. e.g. it finds a good connection and market opportunity, it
says if price is less than this on monday buy, if not resume as normal?
I want proper connections being made here. Ensure we keep API cost in
mind still."*

WHAT WAS ACTUALLY HAPPENING, measured before building anything. The cycle
runs every fifteen minutes seven days a week - there is no weekday gate
anywhere in the scheduler. Feeds collect, the screens build, the hunt
nominates. And then ONE gate, `market_closed`, stopped research as well
as entries (cycle.py, the `block_entries` check ahead of `investigate`).
So the whole weekend was spent finding things and never forming a view on
any of them, and Monday's queue had to do the thinking at the worst
possible moment.

THE THREE THINGS THAT HAD TO CHANGE, and only one of them was the feature:

  1. Research may run against the newest cached daily close while the
     market is shut. Nothing may be sized from that price - `evaluate`
     refuses any snapshot whose `priced_off` is not "live_nbbo", which
     keeps risk review F5 exactly as it was.
  2. `already_researched` DROPPED the candidate. So a view formed on
     Saturday would have been thrown away on Monday and the paid call
     would have bought nothing. What finishes a candidate is a risk
     DECISION, not a view. This was a prerequisite, not a nicety, and it
     was found by reading the loop rather than by a test.
  3. The owner's Monday condition, with the threshold measured rather
     than stated - see risk/stale_view.py for why the model does not get
     to supply it.

COST, since the owner asked. This spends money the weekend structurally
cannot spend today: research is impossible on ~9 calendar days a month,
so a measured $3.50 on active days is ~$75/month against a $100 cap, not
the $105 recorded earlier in docs/WHAT-WE-TRIED.md (which multiplied a
trading-day rate by 30 calendar days). And it spends NO extra money on
Monday: a candidate with a view in hand does not pay for a second call.

Fully offline.
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.data import RawEvent
from catalyst.discovery import Candidate
from catalyst.execution.broker import Broker
from catalyst.orchestrator.cycle import run_cycle
from catalyst.research import prompts

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
ACCOUNT = {"equity": "1000", "cash": "1000", "last_equity": "1000",
           "non_marginable_buying_power": "1000"}
QUOTE = {"quote": {"bp": 49.95, "ap": 50.05, "t": "2026-08-10T13:59:30Z"}}


@pytest.fixture
def db(tmp_path):
    """PRODUCTION SETTINGS, DELIBERATELY. `init_db` turns on
    PRAGMA foreign_keys, which a raw sqlite3.connect +
    executescript does NOT - and 23 test files in this suite take the
    raw route. `_record_view_context` swallows sqlite3.Error, so an FK
    violation there would drop the row silently, the Monday gate would
    never fire, and a weekend view would trade unchecked - passing
    every test and failing on the owner's machine. Found while
    checking the upgrade rather than by a test, which is exactly why
    this fixture now matches production.
    """
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
        "this fixture exists to match production; foreign keys are off")
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def frozen_kill_switch_clock(monkeypatch):
    import catalyst.risk.kill_switches as kill_switches

    class _FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(kill_switches, "datetime", _FrozenClock)


@pytest.fixture(autouse=True)
def stub_prompts(monkeypatch):
    monkeypatch.setattr(prompts, "render_research_prompt",
                        lambda c, **kw: "research")
    monkeypatch.setattr(prompts, "exploration_tools", lambda *a, **kw: [])


def broker_for(state=None, market_open=True, mid=None):
    state = state if state is not None else {}
    state.setdefault("posts", [])
    state.setdefault("qty_by_id", {})
    state["market_open"] = market_open
    quote = dict(QUOTE)
    if mid is not None:
        half = Decimal("0.05")
        quote = {"quote": {"bp": float(Decimal(str(mid)) - half),
                           "ap": float(Decimal(str(mid)) + half),
                           "t": "2026-08-10T13:59:30Z"}}

    def handler(request):
        url = str(request.url)
        if "/v2/account" in url:
            return httpx.Response(200, json=dict(ACCOUNT))
        if "/v2/clock" in url:
            return httpx.Response(200, json={"is_open": state["market_open"]})
        if "/quotes/latest" in url:
            return httpx.Response(200, json=dict(quote))
        if "/v2/bars" in url or "/bars" in url:
            return httpx.Response(200, json={"bars": {}, "next_page_token": None})
        if request.method == "POST" and url.endswith("/v2/orders"):
            body = json.loads(request.content)
            state["posts"].append(body)
            bid = f"brok-{len(state['posts'])}"
            state["qty_by_id"][bid] = body["qty"]
            return httpx.Response(200, json={"id": bid, "status": "accepted"})
        if "by_client_order_id" in url:
            return httpx.Response(200, json={
                "id": "brok-x", "status": "filled", "filled_qty": "1",
                "filled_avg_price": "50.00",
                "filled_at": "2026-08-10T13:31:00Z"})
        if request.method == "GET" and "/v2/orders/" in url:
            bid = url.rsplit("/", 1)[1]
            return httpx.Response(200, json={
                "id": bid, "status": "filled",
                "filled_qty": state["qty_by_id"].get(bid, "1"),
                "filled_avg_price": "50.00",
                "filled_at": "2026-08-10T13:31:00Z"})
        if "/v2/positions" in url:
            return httpx.Response(200, json=[])
        if "/v2/orders" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "unexpected"})

    return Broker("k", "s", transport=httpx.MockTransport(handler),
                  backoff_s=0), state


def candidate(cid="cand-1", ticker="TEST"):
    return Candidate(
        id=cid, ticker=ticker, catalyst_type="insider_cluster",
        catalyst_date=date(2026, 8, 20), catalyst_date_confidence="estimated",
        source_event_ids=("e1",), discovered_at=NOW, sector="tech",
        correlation_tags=("tech",))


GOOD_VIEW = {"direction": "long", "conviction": 0.8, "thesis": "t",
             "invalidation": "i", "expected_holding_days": 12,
             "priced_in": False, "priced_in_reasoning": "r"}
USAGE = {"input_tokens": 100, "output_tokens": 50}


def model_transport(view=None, calls=None):
    v = view or dict(GOOD_VIEW)

    def transport(payload):
        if calls is not None:
            calls.append(payload)
        if (payload.get("tool_choice") or {}).get("type") == "tool":
            return {"content": [{"type": "tool_use",
                                 "name": "submit_research_view", "input": v}],
                    "stop_reason": "tool_use", "usage": dict(USAGE)}
        return {"content": [], "stop_reason": "end_turn", "usage": dict(USAGE)}

    return transport


def _day(i: int) -> str:
    """Distinct, ordered dates. 400 sessions do not fit in one month, and
    `price_action._rows` sorts by the parsed date - duplicates would make
    "the newest close" arbitrary."""
    from datetime import date as _d, timedelta as _td

    return (_d(2024, 1, 1) + _td(days=i)).isoformat()


def bars_for(tmp_path, ticker="TEST", closes=None):
    """A cached history file. Default: a calm stock whose 95th-percentile
    daily move is about 1%, so a 5% weekend gap is plainly outside it."""
    import csv

    d = tmp_path / "bars"
    d.mkdir(exist_ok=True)
    if closes is None:
        closes = []
        price = 50.0
        for i in range(400):
            price = 50.0 + (0.5 if i % 2 else -0.5)
            closes.append(price)
    with (d / f"{ticker.upper()}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low",
                                         "close", "volume"])
        w.writeheader()
        for i, close in enumerate(closes):
            w.writerow({"date": _day(i), "open": close,
                        "high": close, "low": close, "close": close,
                        "volume": 1_000_000})
    return str(d)


def run(db, broker, transport, cands, bars_dir=None, **kw):
    return run_cycle(
        db, broker, transport,
        feed_fetch=lambda s, u: [RawEvent(source="edgar_form4",
                                          source_id="acc-1", fetched_at=NOW,
                                          payload_raw={"accession": "acc-1"})],
        build_candidates_fn=lambda evs, as_of: cands,
        cluster_fn=lambda cs, ops: {c.id: f"{c.sector}-w34" for c in cs},
        now=NOW, bars_dir=bars_dir, **kw)


class TestTheWeekendFormsAView:
    def test_a_closed_market_researches_instead_of_doing_nothing(
            self, db, tmp_path):
        """The whole point. Before this the candidate was recorded as
        `market_closed` and that was the end of the weekend."""
        broker, state = broker_for(market_open=False)
        bars = bars_for(tmp_path)
        report = run(db, broker, model_transport(), [candidate()],
                     bars_dir=bars)
        assert report.funnel["researched"] == 1
        view = db.execute(
            "SELECT direction, conviction FROM research_views").fetchone()
        assert view == ("long", 0.8)
        assert any("researched_while_closed_awaiting_open" in r
                   for r in report.drop_reasons["researched"])

    def test_it_places_absolutely_nothing_while_shut(self, db, tmp_path):
        """A view is not permission. The market being shut is the reason
        entries were blocked in the first place, and that has not moved."""
        broker, state = broker_for(market_open=False)
        report = run(db, broker, model_transport(), [candidate()],
                     bars_dir=bars_for(tmp_path))
        assert state["posts"] == []
        assert report.funnel["orders_placed"] == 0
        assert report.funnel["proposed"] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM risk_decisions").fetchone()[0] == 0

    def test_the_price_it_reasoned_about_is_recorded_as_not_live(
            self, db, tmp_path):
        broker, _ = broker_for(market_open=False)
        run(db, broker, model_transport(), [candidate()],
            bars_dir=bars_for(tmp_path))
        row = db.execute("SELECT price_at_view, priced_off "
                         "FROM research_view_context").fetchone()
        assert row is not None, "nothing recorded what price the view saw"
        assert row[1] == "daily_close"
        assert Decimal(row[0]) > 0

    def test_no_cached_close_is_named_not_silent(self, db, tmp_path):
        """House rule 3. No bars means no price to reason about at all,
        and that is a different fact from the market being shut."""
        broker, _ = broker_for(market_open=False)
        empty = tmp_path / "nobars"
        empty.mkdir()
        report = run(db, broker, model_transport(), [candidate()],
                     bars_dir=str(empty))
        assert report.funnel["researched"] == 0
        assert any("market_closed_and_no_cached_close" in r
                   for r in report.drop_reasons["researched"])

    def test_an_unprotected_position_still_blocks_research(self, db, tmp_path):
        """Deliberately narrow: only `market_closed` relaxes. The other
        reasons entries are blocked are states where buying an opinion is
        not the right move."""
        import catalyst.orchestrator.cycle as cyc

        broker, _ = broker_for(market_open=False)
        report = run(db, broker, model_transport(), [candidate()],
                     bars_dir=bars_for(tmp_path))
        assert report.funnel["researched"] == 1      # baseline: it ran
        assert "unprotected_position_blocks_entries" not in str(
            report.drop_reasons)
        # And the relaxation is keyed on the exact string, not on
        # "block_entries is truthy".
        src = __import__("inspect").getsource(cyc.run_cycle)
        assert 'block_entries == "market_closed"' in src, (
            "the weekend relaxation is not keyed to market_closed, so an "
            "unprotected position or an unreadable clock would also let "
            "research run")


class TestTheViewSurvivesToMonday:
    def test_monday_sizes_the_weekend_view_without_paying_again(
            self, db, tmp_path):
        """The prerequisite that was a real bug: `already_researched`
        dropped the candidate, so a weekend view was thrown away and the
        paid call bought nothing."""
        bars = bars_for(tmp_path)
        shut, _ = broker_for(market_open=False)
        weekend_calls = []
        run(db, shut, model_transport(calls=weekend_calls), [candidate()],
            bars_dir=bars)
        assert weekend_calls, "the weekend never called the model at all"

        open_broker, state = broker_for(market_open=True, mid="50")
        monday_calls = []
        report = run(db, open_broker, model_transport(calls=monday_calls),
                     [candidate()], bars_dir=bars)

        assert monday_calls == [], (
            "Monday paid for a second research call on a view it already "
            "had - the weekend saved nothing")
        assert report.funnel["proposed"] == 1
        assert state["posts"], "the weekend view never became an order"

    def test_a_decided_candidate_is_still_screened_out(self, db, tmp_path):
        """The other half. Loosening the screen must not let a finished
        candidate round the loop forever."""
        bars = bars_for(tmp_path)
        broker, _ = broker_for(market_open=True, mid="50")
        run(db, broker, model_transport(), [candidate()], bars_dir=bars)
        report = run(db, broker, model_transport(), [candidate()],
                     bars_dir=bars)
        assert any("already_decided" in r
                   for r in report.drop_reasons["screened"])


class TestTheMondayConditionIsCodesToMake:
    def test_a_gap_up_past_the_stocks_own_noise_refuses(self, db, tmp_path):
        """THE OWNER'S CONDITION: "if price is less than this on monday
        buy". The stock is calm - a ~1% ordinary daily move - so opening
        5% higher is plainly outside it and the thesis was written about a
        price that no longer exists."""
        bars = bars_for(tmp_path)
        shut, _ = broker_for(market_open=False)
        run(db, shut, model_transport(), [candidate()], bars_dir=bars)

        gapped, state = broker_for(market_open=True, mid="52.60")   # +5.2%
        report = run(db, gapped, model_transport(), [candidate()],
                     bars_dir=bars)
        assert state["posts"] == [], "it bought into the gap"
        assert any("moved_up_past_view_price" in r
                   for r in report.drop_reasons["proposed"])

    def test_a_small_move_still_trades(self, db, tmp_path):
        """The other branch, and the one that matters for the owner's
        "multiple times a month": an ordinary Monday must not be refused.
        """
        bars = bars_for(tmp_path)
        shut, _ = broker_for(market_open=False)
        run(db, shut, model_transport(), [candidate()], bars_dir=bars)

        normal, state = broker_for(market_open=True, mid="50.10")
        report = run(db, normal, model_transport(), [candidate()],
                     bars_dir=bars)
        assert report.funnel["proposed"] == 1
        assert state["posts"], "an ordinary Monday was refused"

    def test_a_gap_down_refuses_too_and_says_so_differently(self, db, tmp_path):
        """Cheaper is not the same as better: something happened that the
        thesis never saw. Named separately so the refusal tracker can
        eventually say whether this half is right."""
        bars = bars_for(tmp_path)
        shut, _ = broker_for(market_open=False)
        run(db, shut, model_transport(), [candidate()], bars_dir=bars)

        crashed, state = broker_for(market_open=True, mid="45")     # -10%
        report = run(db, crashed, model_transport(), [candidate()],
                     bars_dir=bars)
        assert state["posts"] == []
        assert any("moved_down_past_view_price" in r
                   for r in report.drop_reasons["proposed"])

    def test_the_refusal_is_recorded_with_its_price_so_it_gets_scored(
            self, db, tmp_path):
        """The refusal tracker is the brief's "single most important
        feedback loop". A refusal that lives only in a log line is one it
        cannot read, so this gate cannot be judged later."""
        bars = bars_for(tmp_path)
        shut, _ = broker_for(market_open=False)
        run(db, shut, model_transport(), [candidate()], bars_dir=bars)
        gapped, _ = broker_for(market_open=True, mid="52.60")
        run(db, gapped, model_transport(), [candidate()], bars_dir=bars)

        row = db.execute(
            "SELECT price_at_refusal FROM refusals").fetchone()
        assert row is not None, "the refusal was never recorded"
        assert Decimal(row[0]) == Decimal("52.60")
        snap = db.execute("SELECT skip_reasons, adaptive_params_snapshot "
                          "FROM risk_decisions").fetchone()
        assert "moved_up_past_view_price" in snap[0]
        # The evidence travels with the decision, so the dashboard can
        # say WHY without recomputing anything.
        detail = json.loads(snap[1])
        assert Decimal(detail["price_at_view"]) > 0
        assert detail["ordinary_daily_move"] is not None

    def test_a_refused_view_is_superseded_so_it_can_be_researched_again(
            self, db, tmp_path):
        """The owner's own other branch: "if not, resume as normal". The
        candidate keeps its place and loses only the right to trade on a
        price that has gone."""
        bars = bars_for(tmp_path)
        shut, _ = broker_for(market_open=False)
        run(db, shut, model_transport(), [candidate()], bars_dir=bars)
        gapped, _ = broker_for(market_open=True, mid="52.60")
        run(db, gapped, model_transport(), [candidate()], bars_dir=bars)

        assert db.execute(
            "SELECT COUNT(*) FROM research_views").fetchone()[0] == 0, (
            "the stale view survived, so the candidate can never be "
            "re-examined at the price actually on offer")
        assert db.execute(
            "SELECT COUNT(*) FROM research_view_context").fetchone()[0] == 0

    def test_a_view_formed_at_a_live_mid_is_not_re_gated(self, db, tmp_path):
        """Only a view that crossed a session boundary is checked. Gating
        an intraday view would refuse candidates for ordinary drift that
        sizing and the spread gate already handle."""
        bars = bars_for(tmp_path)
        broker, state = broker_for(market_open=True, mid="50")
        run(db, broker, model_transport(), [candidate()], bars_dir=bars)
        row = db.execute(
            "SELECT priced_off FROM research_view_context").fetchone()
        assert row[0] == "live_nbbo"


class TestNothingSizesOffAPriceThatIsNotLive:
    """risk review F5, unchanged: "sizing and the spread gate off
    Friday's book is not a decision, it's a guess." The closed-market
    snapshot has to be able to EXIST now, so the refusal moves into the
    single gate every candidate passes through."""

    def _decide(self, market):
        from catalyst.discovery import Candidate as C
        from catalyst.research.schema import ResearchView
        from catalyst.risk import PortfolioState
        from catalyst.risk.adaptive_params import DEFAULT_PARAMS
        from catalyst.risk.evaluate import evaluate

        cand = C(id="x", ticker="TEST", catalyst_type="insider_cluster",
                 catalyst_date=date(2026, 8, 20),
                 catalyst_date_confidence="estimated",
                 source_event_ids=("e1",), discovered_at=NOW,
                 sector="tech", correlation_tags=("tech",))
        view = ResearchView(candidate_id="x", direction="long",
                            conviction=0.9, thesis="t", invalidation="i",
                            expected_holding_days=10, priced_in=False,
                            priced_in_reasoning="r")
        portfolio = PortfolioState(
            equity_usd=Decimal("1000"), settled_cash_usd=Decimal("1000"),
            open_positions=(), day_pnl_usd=Decimal("0"),
            peak_equity_usd=Decimal("1000"), consecutive_losses=0,
            as_of=NOW, reliable=True)
        return evaluate(cand, view, portfolio, dict(DEFAULT_PARAMS), market)

    def test_a_daily_close_snapshot_cannot_size(self):
        from catalyst.orchestrator.cycle import build_closed_market_snapshot
        from catalyst.risk import MarketSnapshot

        closed = MarketSnapshot(
            ticker="TEST", last_close=Decimal("50"),
            half_spread_bp=Decimal("1"),      # even a PERFECT spread
            median_daily_dollar_volume=Decimal("0"),
            priced_off="daily_close")
        decision = self._decide(closed)
        assert decision.action == "skip"
        assert "price_not_live_cannot_size" in decision.skip_reasons
        assert decision.qty is None and decision.notional_usd is None
        assert build_closed_market_snapshot is not None

    def test_a_live_snapshot_still_sizes(self):
        from catalyst.risk import MarketSnapshot

        live = MarketSnapshot(ticker="TEST", last_close=Decimal("50"),
                              half_spread_bp=Decimal("8"),
                              median_daily_dollar_volume=Decimal("0"))
        decision = self._decide(live)
        assert decision.action == "trade", decision.skip_reasons
        assert live.priced_off == "live_nbbo"    # the default, unchanged

    def test_the_closed_snapshot_carries_an_impossible_spread(self, tmp_path):
        """Belt and braces behind the priced_off refusal: if this ever
        did reach the spread gate, zero would sail through the owner's
        20bp hard bound as the tightest book ever measured."""
        from catalyst.orchestrator.cycle import build_closed_market_snapshot

        snap = build_closed_market_snapshot(bars_for(tmp_path), "TEST")
        assert snap is not None
        assert snap.priced_off == "daily_close"
        assert snap.half_spread_bp > Decimal("20")

    def test_no_bars_means_no_snapshot_rather_than_a_zero(self, tmp_path):
        from catalyst.orchestrator.cycle import build_closed_market_snapshot

        empty = tmp_path / "none"
        empty.mkdir()
        assert build_closed_market_snapshot(str(empty), "TEST") is None
        assert build_closed_market_snapshot(None, "TEST") is None


class TestAnUnmeasurableMoveRefuses:
    """The case a sabotage round found untested: if the stock's own daily
    move cannot be measured there is no way to tell an ordinary move from
    a violent one, and the only safe reading of an unmeasurable move is
    that we do not know. Waving it through would trade a Friday thesis on
    a Monday price with nothing checked at all."""

    def test_the_pure_function_refuses_a_missing_bound(self):
        from catalyst.risk.stale_view import UNMEASURABLE, move_against_view

        reason, move = move_against_view("50", "50.10", None)
        assert reason == UNMEASURABLE
        assert move > 0

    def test_a_zero_bound_is_a_bad_history_file_not_a_calm_stock(self):
        """`daily_move_percentile` returns None for a constant-price file
        and says why in its own comment. A zero arriving here anyway would
        refuse every candidate on any move at all, so it is read as the
        measurement failing rather than as a threshold of nothing."""
        from catalyst.risk.stale_view import UNMEASURABLE, move_against_view

        assert move_against_view("50", "50.01", "0")[0] == UNMEASURABLE
        assert move_against_view("50", "50.01", "-0.04")[0] == UNMEASURABLE

    @pytest.mark.parametrize("was, now_", [
        ("0", "50"), ("50", "0"), ("-5", "50"), ("abc", "50"),
        ("50", None), (None, None),
    ])
    def test_an_unreadable_price_refuses_rather_than_raising(self, was, now_):
        from catalyst.risk.stale_view import UNMEASURABLE, move_against_view

        assert move_against_view(was, now_, "0.04")[0] == UNMEASURABLE

    def test_too_little_history_refuses_the_weekend_view_end_to_end(
            self, db, tmp_path):
        """The integration case. A ticker with a readable newest close but
        fewer sessions than `daily_move_percentile` needs: the weekend can
        form a view, and Monday cannot measure whether the move was
        ordinary, so it does not trade on it."""
        thin = bars_for(tmp_path, closes=[50.0 + (i % 3) for i in range(30)])
        shut, _ = broker_for(market_open=False)
        report = run(db, shut, model_transport(), [candidate()],
                     bars_dir=thin)
        assert report.funnel["researched"] == 1, (
            "thirty sessions is enough for a newest close, so the weekend "
            "should still have formed a view")

        monday, state = broker_for(market_open=True, mid="50.10")
        report2 = run(db, monday, model_transport(), [candidate()],
                      bars_dir=thin)
        assert state["posts"] == []
        assert any("view_price_move_unmeasurable" in r
                   for r in report2.drop_reasons["proposed"])
