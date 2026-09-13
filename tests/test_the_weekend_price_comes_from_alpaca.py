"""The price the model reasons about while the market is shut comes from
Alpaca, is dated, and is refused when it is too old to mean anything.

OWNER-ASKED 2026-09-13: *"are we ensuring the pricing info it is pulling
is accurate aswell, i dont want it to read a price that may not be life,
hence we should allow it to tap into alpaca or somehow get alpaca to give
a live read"*.

DURING MARKET HOURS THIS WAS ALREADY TRUE and remains untouched:
`build_market_snapshot` asks Alpaca for the live NBBO every cycle and
refuses a quote that is absent, undatable, older than `MAX_QUOTE_AGE`,
non-positive or crossed; the mid is then cross-checked against the cached
close. Sizing accepts that provenance and no other.

**WHILE THE MARKET WAS SHUT IT WAS NOT TRUE, AND THE NUMBER IS 30.**
`bar_history.MAX_CACHE_AGE_DAYS` refetches a symbol's history only when
the file is over thirty days old - correct for what that cache exists
for, because a 95th-percentile daily move and a worst-case gap measured
across three years barely move in a month. But the weekend research
feature made the same file the source of THE PRICE, and a price may not
be a month old.

MEASURED, from the owner's own bundle `catalyst-logic-7d-20260913-125402`:
ACVA was rendered into the prompt at `last close: $7.22` while the stock
traded around $10.43 after an all-cash tender offer at $10.50.
$10.43 / 1.44 = $7.24, so the cached close predated the announcement by a
session or more. The model caught it only because it happened to search
and then disbelieved its own input - *"either the price feed is
stale/broken for this name or there is a data error; either way I cannot
rely on it to size an edge."* That is luck, not a guard.

WHAT THIS HOLDS:

    1. **Alpaca is asked first**, and its answer wins over the file.
    2. **The cache is a named fallback**, not a silent one - the page and
       the prompt say which produced the number.
    3. **The close carries its DATE**, which `price_action._rows` has
       always returned and nothing read.
    4. **A close older than the market has plausibly been shut is
       REFUSED**, with the numbers beside the refusal (house rule 3).
    5. **Neither closed-market provenance can size anything.** That is
       the property this whole design rests on and it is asserted for
       both, by the rule `risk.evaluate` actually applies.

Fully offline: every broker is an `httpx.MockTransport`. Every clock is
supplied (house rule 6).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from catalyst.execution.broker import Broker
from catalyst.orchestrator import cycle
from catalyst.orchestrator.cycle import (
    MAX_RESEARCH_CLOSE_AGE_DAYS,
    build_closed_market_snapshot,
)

NOW = datetime(2026, 9, 13, 0, 6, tzinfo=timezone.utc)


def bars_dir(tmp_path, ticker="ACVA", closes=None, newest_day=None,
             n=400):
    """A cached history whose NEWEST close lands on `newest_day`.

    Anchored to a date the caller chooses, because the whole subject here
    is staleness - a fixture that always writes recent bars cannot show
    the bound working, and one that always writes ancient bars cannot
    show the ordinary weekend working.
    """
    import csv

    newest_day = newest_day or (NOW.date() - timedelta(days=1))
    d = tmp_path / "bars"
    d.mkdir(exist_ok=True)
    closes = closes or [50.0 + (0.5 if i % 2 else -0.5) for i in range(n)]
    with (d / f"{ticker.upper()}.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low",
                                          "close", "volume"])
        w.writeheader()
        for i, close in enumerate(closes):
            day = newest_day - timedelta(days=(len(closes) - 1 - i))
            w.writerow({"date": day.isoformat(), "open": close, "high": close,
                        "low": close, "close": close, "volume": 1_000_000})
    return str(d)


def broker_serving(bars):
    """A broker whose /bars endpoint returns exactly `bars`.

    `bars` may be a list of Alpaca bar dicts, or an exception class to
    raise, or None for "the endpoint answers with nothing usable".
    """
    def handler(request):
        url = str(request.url)
        if "/bars" in url:
            if isinstance(bars, type) and issubclass(bars, Exception):
                raise bars("upstream exploded")
            return httpx.Response(200, json={"bars": bars,
                                             "next_page_token": None})
        return httpx.Response(404, json={"message": "unexpected"})

    return Broker("k", "s", transport=httpx.MockTransport(handler),
                  backoff_s=0)


def bar(day, close):
    return {"t": f"{day.isoformat()}T00:00:00Z", "c": close, "o": close,
            "h": close, "l": close, "v": 1_000_000}


def _closed_cycle(tmp_path, *, broker_bars, newest_day, stale_close=50.0,
                  want_conn=False):
    """Run ONE closed-market cycle offline and hand back its report.

    Everything is injected: the clock, the bars the broker serves, and the
    date the cache's newest close lands on. The point is to make "was the
    broker actually passed" and "was the stale reason actually written"
    answerable by OUTCOME, which a grep over the source cannot do.
    """
    import json
    import sqlite3

    import catalyst.risk.kill_switches as kill_switches
    from catalyst.data import RawEvent
    from catalyst.discovery import Candidate
    from catalyst.orchestrator.cycle import run_cycle
    from catalyst.storage import init_db

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    real = kill_switches.datetime
    kill_switches.datetime = _Frozen
    conn = init_db(str(tmp_path / "c.db"))
    try:
        cand = Candidate(
            id="c1", ticker="ACVA", catalyst_type="insider_cluster",
            catalyst_date=(NOW + timedelta(days=7)).date(),
            catalyst_date_confidence="estimated", source_event_ids=("e1",),
            discovered_at=NOW, sector="tech", correlation_tags=("tech",))
        report = run_cycle(
            conn, _cycle_broker(broker_bars), _view_transport(),
            feed_fetch=lambda s, u: [RawEvent(
                source="edgar_form4", source_id="e1", fetched_at=NOW,
                payload_raw={"accession": "e1"})],
            build_candidates_fn=lambda evs, as_of: [cand],
            cluster_fn=lambda cs, ops: {c.id: "tech-w1" for c in cs},
            now=NOW,
            bars_dir=bars_dir(tmp_path, "ACVA", closes=[stale_close] * 400,
                              newest_day=newest_day))
        if want_conn:
            return report, conn
        return report
    finally:
        kill_switches.datetime = real
        if not want_conn:
            with __import__("contextlib").suppress(sqlite3.Error):
                conn.close()


def _cycle_broker(bars):
    """A whole-cycle broker: shut market, an account, and `bars` on the
    bars endpoint."""
    import json

    def handler(request):
        url = str(request.url)
        if "/v2/account" in url:
            return httpx.Response(200, json={
                "equity": "2000", "cash": "2000", "buying_power": "2000",
                "id": "acct-1", "account_number": "A1"})
        if "/v2/clock" in url:
            return httpx.Response(200, json={"is_open": False})
        if "/bars" in url:
            if isinstance(bars, type) and issubclass(bars, Exception):
                raise bars("upstream exploded")
            return httpx.Response(200, json={"bars": bars,
                                             "next_page_token": None})
        if "/v2/positions" in url:
            return httpx.Response(200, json=[])
        if "/v2/orders" in url:
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "unexpected"})

    return Broker("k", "s", transport=httpx.MockTransport(handler),
                  backoff_s=0)


def _view_transport():
    """A model that always returns one long view, so "did it research"
    is decided by the price path and never by the model."""
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def transport(_payload, **_kw):
        return {"content": [{"type": "tool_use",
                             "name": "submit_research_view",
                             "input": {"direction": "long",
                                       "conviction": 0.6,
                                       "thesis": "t", "invalidation": "i",
                                       "expected_holding_days": 10,
                                       "priced_in": False,
                                       "priced_in_reasoning": "r"}}],
                "stop_reason": "tool_use", "usage": dict(usage)}

    return transport


class TestAlpacaIsAskedFirst:

    def test_the_brokers_close_wins_over_the_cached_one(self, tmp_path):
        """THE ACVA CASE, exactly. The cache holds a pre-announcement
        $7.22; Alpaca knows the post-announcement $10.43. The model must
        be shown the one that is true."""
        cached = bars_dir(tmp_path, "ACVA", closes=[7.22] * 400,
                          newest_day=NOW.date() - timedelta(days=4))
        live_ish = broker_serving([bar(NOW.date() - timedelta(days=2),
                                      10.43)])
        snap = build_closed_market_snapshot(cached, "ACVA",
                                           broker=live_ish, now=NOW)
        assert snap is not None
        assert snap.last_close == Decimal("10.43"), (
            "the stale cached close was used while the broker had a "
            "fresher one - this is the defect")
        assert snap.priced_off == "broker_daily_close"

    def test_it_takes_the_NEWEST_bar_not_the_last_in_the_list(self,
                                                             tmp_path):
        """Bar order is the feed's business, not ours. A page returned out
        of order must not decide the price."""
        out_of_order = broker_serving([
            bar(NOW.date() - timedelta(days=2), 10.43),
            bar(NOW.date() - timedelta(days=6), 7.22),
        ])
        snap = build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA"), "ACVA", broker=out_of_order, now=NOW)
        assert snap.last_close == Decimal("10.43")
        assert snap.close_date == NOW.date() - timedelta(days=2)

    def test_no_broker_still_works_off_the_cache(self, tmp_path):
        """The scheduler has always been able to call this without a
        broker, and a missing broker must not become a missing price."""
        snap = build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA"), "ACVA", broker=None, now=NOW)
        assert snap is not None
        assert snap.priced_off == "cached_daily_close"

    @pytest.mark.parametrize("bad", [
        RuntimeError,            # the broker raised
        {},                      # Alpaca's own empty shape, a dict not a list
        [],                      # a list with nothing in it
        [{"t": "not-a-date", "c": 10.0}],
        [{"t": "2026-09-11T00:00:00Z", "c": None}],
        [{"t": "2026-09-11T00:00:00Z", "c": 0}],      # non-positive
        [{"t": "2026-09-11T00:00:00Z", "c": -3}],
        # NOTE: a non-dict element cannot reach `_broker_daily_close` via
        # a real broker - `Broker.get_daily_bars` filters with
        # `isinstance(b, dict)` first - so the guard inside the loop is
        # DEFENCE IN DEPTH and sabotaging it alone stays green. Recorded
        # rather than papered over (§14, §17). The property only the inner
        # guard holds is tested directly below.
        ["not even a dict"],
    ])
    def test_an_unusable_broker_answer_falls_back_rather_than_failing(
            self, tmp_path, bad):
        """A feed that answers badly must cost the FRESHNESS, never the
        price. Research has to survive a bad upstream exactly as it did
        before the broker was asked at all."""
        snap = build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA"), "ACVA", broker=broker_serving(bad),
            now=NOW)
        assert snap is not None, f"a bad answer ({bad!r}) lost the price"
        assert snap.priced_off == "cached_daily_close"


    def test_a_non_dict_bar_cannot_take_the_cycle_down(self):
        """The half of the pair only the INNER guard holds.

        `Broker.get_daily_bars` filters non-dicts, so the isinstance check
        inside `_broker_daily_close` is unreachable through a real broker
        and sabotaging it alone came back green. It is still load-bearing
        for any caller that hands the helper a list directly - a stub, a
        replay harness, a future feed - where a bare string would raise
        AttributeError, which is NOT among the exceptions the loop catches
        and would therefore escape into the cycle.
        """
        class Raw:
            @staticmethod
            def get_daily_bars(*_a, **_k):
                return ["not even a dict", bar(NOW.date(), 10.43)]

        got = cycle._broker_daily_close(Raw(), "ACVA", NOW)
        assert got == (NOW.date(), Decimal("10.43"))


class TestTheCloseCarriesItsDate:

    def test_the_snapshot_records_which_day_the_close_is(self, tmp_path):
        yesterday = NOW.date() - timedelta(days=1)
        snap = build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA", newest_day=yesterday), "ACVA",
            now=NOW)
        assert snap.close_date == yesterday

    def test_a_live_snapshot_carries_no_date_because_now_is_the_answer(self):
        from catalyst.risk import MarketSnapshot

        live = MarketSnapshot(ticker="ACVA", last_close=Decimal("10.43"),
                              half_spread_bp=Decimal("8"),
                              median_daily_dollar_volume=Decimal("0"))
        assert live.close_date is None
        assert live.priced_off == "live_nbbo"

    def test_the_prompt_states_the_date_and_the_age(self):
        from catalyst.research import prompts
        from catalyst.risk import MarketSnapshot

        shut = MarketSnapshot(
            ticker="ACVA", last_close=Decimal("10.43"),
            half_spread_bp=Decimal("100000"),
            median_daily_dollar_volume=Decimal("0"),
            priced_off="broker_daily_close", close_date=date(2026, 9, 11))
        text = prompts.render_market_section(shut, NOW)
        assert "2026-09-11" in text
        assert "2 day(s) before today" in text
        assert "NOT A LIVE QUOTE" in text

    def test_the_prompt_says_WHICH_source_produced_the_number(self):
        """"Alpaca said" and "a file on disk said" are not the same claim,
        and the second one is the one that was wrong."""
        from catalyst.research import prompts
        from catalyst.risk import MarketSnapshot

        def rendered(off):
            return prompts.render_market_section(MarketSnapshot(
                ticker="ACVA", last_close=Decimal("10.43"),
                half_spread_bp=Decimal("100000"),
                median_daily_dollar_volume=Decimal("0"),
                priced_off=off, close_date=date(2026, 9, 11)), NOW)

        assert "Alpaca's own newest daily close" in rendered(
            "broker_daily_close")
        fell_back = rendered("cached_daily_close")
        assert "local cache" in fell_back
        assert "could not be reached" in fell_back

    def test_the_prompt_tells_the_model_to_trust_its_own_search_instead(self):
        """The belt behind the guard. ACVA was saved by the model
        disbelieving its input; that should be instruction, not luck."""
        from catalyst.research import prompts
        from catalyst.risk import MarketSnapshot

        text = prompts.render_market_section(MarketSnapshot(
            ticker="ACVA", last_close=Decimal("7.22"),
            half_spread_bp=Decimal("100000"),
            median_daily_dollar_volume=Decimal("0"),
            priced_off="cached_daily_close", close_date=date(2026, 9, 9)), NOW)
        assert "trust what you find" in text


class TestAStaleCloseIsRefusedAndSaysSo:

    def test_a_month_old_close_is_refused(self, tmp_path):
        """The ACVA failure mode, turned into a refusal. The cache is
        refetched only every 30 days, so this is a state production
        reaches on its own."""
        stale = bars_dir(tmp_path, "ACVA",
                         newest_day=NOW.date() - timedelta(days=30))
        refused: list = []
        assert build_closed_market_snapshot(stale, "ACVA", now=NOW,
                                            refused=refused) is None
        assert refused, "refused silently, which is the shape of the bug"

    def test_the_refusal_carries_the_numbers(self, tmp_path):
        """House rule 3: the raw facts beside the zero, so the funnel can
        say a month-old cache was declined rather than that nothing
        happened."""
        old_day = NOW.date() - timedelta(days=30)
        refused: list = []
        build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA", newest_day=old_day), "ACVA",
            now=NOW, refused=refused)
        said = refused[0]
        assert old_day.isoformat() in said
        assert "30 day(s) old" in said
        assert str(MAX_RESEARCH_CLOSE_AGE_DAYS) in said
        assert "cached_daily_close" in said, (
            "the refusal does not say which source produced the stale "
            "close, so the reader cannot tell a dead feed from a dead "
            "cache")

    def test_an_ordinary_weekend_is_NOT_refused(self, tmp_path):
        """The bound must not break the feature it guards. Friday's close
        read on a Sunday is the normal case."""
        for days_back in (1, 2, 3, 4):
            snap = build_closed_market_snapshot(
                bars_dir(tmp_path, "ACVA",
                         newest_day=NOW.date() - timedelta(days=days_back)),
                "ACVA", now=NOW)
            assert snap is not None, (
                f"a close {days_back} day(s) old was refused - that is an "
                "ordinary long weekend")

    def test_the_bound_spans_the_longest_scheduled_closure(self):
        """Four calendar days is a Friday close to a Tuesday open across a
        Monday holiday. The bound has to clear that or the feature breaks
        every public holiday."""
        assert MAX_RESEARCH_CLOSE_AGE_DAYS > 4

    def test_a_future_dated_close_is_refused_not_treated_as_fresh(self,
                                                                  tmp_path):
        """A clock skew or a mislabelled bar would otherwise read as the
        freshest possible price."""
        refused: list = []
        assert build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA",
                     newest_day=NOW.date() + timedelta(days=3)),
            "ACVA", now=NOW, refused=refused) is None
        assert any("future" in r for r in refused)

    def test_no_close_at_all_is_a_different_fact_from_a_stale_one(self,
                                                                 tmp_path):
        """Two states, two reasons. `refused` stays empty when there was
        simply nothing, so the caller can tell them apart on the funnel."""
        empty = tmp_path / "none"
        empty.mkdir()
        refused: list = []
        assert build_closed_market_snapshot(str(empty), "ACVA", now=NOW,
                                            refused=refused) is None
        assert refused == [], (
            "'no close' was reported as 'stale close', which sends the "
            "reader to look at the wrong thing")

    def test_the_cycle_records_the_stale_refusal_on_the_funnel(self,
                                                              tmp_path):
        """END TO END, because a grep cannot see the branch.

        The first version of this test grepped `run_cycle` for
        `refused=stale` and `closed_market_close_too_stale`. Two
        sabotages walked straight past it - removing `broker=broker` from
        the call, and replacing `if market is None and stale:` with
        `if False:` - because both strings survive either edit. §22's
        lesson again: **a substring still present in the source is not a
        behaviour.**
        """
        report = _closed_cycle(tmp_path, broker_bars=RuntimeError,
                              newest_day=NOW.date() - timedelta(days=40))
        assert report.funnel.get("researched") == 0
        said = " ".join(report.drop_reasons.get("researched", []))
        assert "closed_market_close_too_stale" in said, (
            f"a 40-day-old close was not named on the funnel: {said!r}")
        assert "40 day(s) old" in said, (
            "the reason reached the funnel without its numbers")

    def test_the_cycle_HANDS_THE_BROKER_to_the_snapshot(self, tmp_path):
        """The other sabotage the grep missed. With a stale cache AND a
        broker that has a fresh bar, research can only succeed if the
        broker was actually passed - so this distinguishes wired from
        unwired by outcome rather than by source text."""
        report = _closed_cycle(
            tmp_path,
            broker_bars=[bar(NOW.date() - timedelta(days=1), 50.0)],
            newest_day=NOW.date() - timedelta(days=40))
        assert report.funnel.get("researched") == 1, (
            "the cache was 40 days stale and Alpaca had yesterday's close, "
            "so a cycle that researched nothing did not pass the broker: "
            f"{report.drop_reasons}")

    def test_and_the_price_it_used_was_the_BROKERS(self, tmp_path):
        """Not merely that it researched - that the number it reasoned
        about is the fresh one. A stale cache at $7.22 and Alpaca at
        $10.43 is the ACVA case run through the whole cycle."""
        report, conn = _closed_cycle(
            tmp_path, broker_bars=[bar(NOW.date() - timedelta(days=1), 10.43)],
            newest_day=NOW.date() - timedelta(days=40), stale_close=7.22,
            want_conn=True)
        try:
            assert report.funnel.get("researched") == 1, report.drop_reasons
            row = conn.execute("SELECT price_at_view, priced_off FROM "
                               "research_view_context").fetchone()
            assert row is not None, "no view context was recorded"
            assert Decimal(row[0]) == Decimal("10.43"), (
                f"the cycle reasoned about {row[0]} - the stale cached "
                "close - while the broker had 10.43")
            assert row[1] == "broker_daily_close"
        finally:
            conn.close()

    def test_the_stale_reason_reads_as_routine_not_as_a_fault(self):
        """CLAUDE.md: routine attrition must not look like damage, and an
        unclassified reason on the `researched` stage defaults to FAULT."""
        from catalyst.dashboard.queries import skip_kind

        assert skip_kind("researched",
                         "closed_market_close_too_stale") == "ROUTINE"

    def test_the_stale_reason_has_a_plain_english_label(self):
        from catalyst.dashboard import queries

        assert "closed_market_close_too_stale" in queries.ROUTINE_SKIPS


class TestNeitherClosedProvenanceCanEverSize:
    """The property the whole design rests on (risk review F5). Asserted
    for BOTH new values, by the rule `evaluate` actually applies."""

    @pytest.mark.parametrize("off", ["broker_daily_close",
                                     "cached_daily_close",
                                     "some_provenance_invented_in_2027"])
    def test_evaluate_refuses_anything_that_is_not_the_live_mid(self, off):
        from catalyst.risk import evaluate as ev

        import inspect

        src = inspect.getsource(ev)
        assert '!= "live_nbbo"' in src, (
            "the refusal is no longer a rule - if it became a list of "
            "known closed-market values, the next one added would size")
        # And the value itself is not the live one, so the rule catches it.
        assert off != "live_nbbo"

    def test_the_closed_snapshot_still_carries_an_impossible_spread(
            self, tmp_path):
        """Belt and braces behind the priced_off refusal, unchanged: zero
        would sail through the owner's 20bp hard bound as the tightest
        book ever measured."""
        snap = build_closed_market_snapshot(
            bars_dir(tmp_path, "ACVA"), "ACVA",
            broker=broker_serving([bar(NOW.date() - timedelta(days=1),
                                       10.43)]), now=NOW)
        assert snap.half_spread_bp == Decimal("100000")

    def test_the_live_path_is_untouched(self):
        """What was already right must stay right: a live quote older than
        ten minutes is refused, and that is what sizing runs on."""
        assert cycle.MAX_QUOTE_AGE == timedelta(minutes=10)

    def test_the_dashboard_classifies_the_provenance_by_the_RULE(self,
                                                                tmp_path):
        """It compared `off == "daily_close"`, so the moment a second
        closed-market provenance existed a weekend view would have been
        labelled "waiting for the risk engine" - the opposite of true."""
        import json

        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        for off, expected in (("broker_daily_close", "Alpaca's own"),
                              ("cached_daily_close", "local cache")):
            path = str(tmp_path / f"{off}.db")
            conn = init_db(path)
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         ("c1", "ACVA", "merger", "2026-09-20", "estimated",
                          json.dumps([]), NOW.isoformat(), "6199",
                          json.dumps([])))
            conn.execute("INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                         ("c1", "long", 0.6, "t", "i", 10, 0, "r"))
            conn.execute("INSERT INTO research_view_context VALUES (?,?,?,?)",
                         ("c1", "10.43", off, NOW.isoformat()))
            conn.commit()
            conn.close()
            said = panels._why_not_researched(Db(path), "c1")
            assert "market was shut" in said, (off, said)
            assert expected in said, (off, said)
