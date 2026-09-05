"""The hunt could not look anything up. Now it can.

OWNER-ASKED 2026-09-05: "i dont want ... claude not trading well or
finding opportunities himself, he needs to get really creative with
finding links".

WHAT IT WAS. One forced call: a digest of 220 feed items cut at 320
characters, and nominate_candidates as the only tool. No search, no
reading, no way to chase a hunch - a model that cannot look anything up
cannot find a link the feed did not already print. The "creativity" was
picking from a list.

WHAT IT IS. A bounded tool loop: search_filings (EDGAR full text, a
phrase the model chooses), read_filing (the body, where the dates live),
search_news (the paid feed pointed at a name). Anything a tool returns
is written to raw_events and becomes citable, so the rule that made the
hunt safe - cite only what exists - is unchanged; what changed is that
the model decides what exists by going and finding it.

WHAT BOUNDS IT. MAX_TOOL_CALLS a hunt, the governor asked before every
tool turn, one forced nomination when the model stops, and a sec.gov
block disabling the SEC tools for the rest of the hunt.

Fully offline: the transport and every searcher are fakes.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.data import RawEvent
from catalyst.discovery import hunt as H
from catalyst.discovery import hunt_tools as T

NOW = datetime.now(timezone.utc)
SOON = (NOW + timedelta(days=10)).date().isoformat()


def event(source_id, ticker, source="edgar_fts", **extra):
    return RawEvent(source=source, source_id=source_id, fetched_at=NOW,
                    payload_raw={"ticker": ticker, "company": f"{ticker} Inc",
                                 "accession": "0001234567-26-000001",
                                 "cik": "1234567", **extra})


FEED = [event("acc-1", "BIOX"), event("news-1", "APTV", source="alpaca_news")]


@pytest.fixture
def ctx(tmp_path):
    from catalyst.research.boundary import CostContext
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "h.db"))
    yield CostContext(conn=conn, governor_profit_share=Decimal("0"),
                      cycle_id="c1", kind="scheduled",
                      owner_monthly_cap_cents=Decimal("10000"))
    conn.close()


def use(name, inputs, tid="tu-1"):
    return {"type": "tool_use", "id": tid, "name": name, "input": inputs}


def reply(blocks, stop="tool_use"):
    return {"content": blocks, "stop_reason": stop,
            "usage": {"input_tokens": 30000, "output_tokens": 500}}


def nominate(noms):
    return reply([{"type": "tool_use", "id": "nom", "name": "nominate_candidates",
                   "input": {"nominations": noms}}])


def nomination(ticker="VOTE", sid="found-1", **over):
    base = {"ticker": ticker, "catalyst_type": "merger_vote",
            "catalyst_date": SOON, "date_confidence": "confirmed",
            "source_event_ids": [sid],
            "why": "Special meeting to approve the merger; date in the proxy."}
    base.update(over)
    return base


class Script:
    """A transport that plays a fixed sequence and records what it saw."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.payloads = []

    def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        return self.replies.pop(0) if self.replies else nominate([])


def fake_searchers(found=None, news=None, body="SPECIAL MEETING on the date"):
    calls = []

    def search_filings(phrase, forms, catalyst_type, now):
        calls.append(("search_filings", phrase, forms))
        return found if found is not None else [
            event("found-1", "VOTE", form="DEFM14A", filed_date=str(now.date()))]

    def search_news(symbols, now):
        calls.append(("search_news", tuple(symbols)))
        return news or [event("news-found-1", symbols[0], source="alpaca_news")]

    def read_filing(ev):
        calls.append(("read_filing", ev.source_id))
        return body

    return {"search_filings": search_filings, "search_news": search_news,
            "read_filing": read_filing}, calls


class TestItCanSearchFindAndCite:
    def test_a_found_filing_becomes_a_candidate(self, ctx):
        """The whole point: search, read, nominate what the feed never
        carried - and it is a real candidate."""
        searchers, calls = fake_searchers()
        t = Script([
            reply([use("search_filings", {"phrase": '"special meeting"',
                                          "catalyst_type": "merger_vote"})]),
            reply([use("read_filing", {"source_id": "found-1"}, "tu-2")]),
            nominate([nomination()]),
        ])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert res.skipped_reason is None, res.skipped_reason
        assert [c.ticker for c in res.candidates] == ["VOTE"]
        assert res.candidates[0].source_event_ids == ("found-1",)
        assert [c[0] for c in calls] == ["search_filings", "read_filing"]
        assert res.turns == 3 and len(res.tool_calls) == 2
        assert res.found == 1

    def test_what_it_found_is_stored_beside_the_feed(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        row = ctx.conn.execute(
            "SELECT source, payload_raw FROM raw_events WHERE source_id='found-1'"
        ).fetchone()
        assert row and row[0] == "edgar_fts" and "VOTE" in row[1]

    def test_the_model_sees_the_results_with_citable_ids(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        second = t.payloads[1]["messages"]
        assert second[1]["role"] == "assistant"
        results = second[2]["content"]
        assert results[0]["type"] == "tool_result"
        assert results[0]["tool_use_id"] == "tu-1"
        assert "found-1" in results[0]["content"] and "citable" in results[0]["content"]
        assert results[0]["is_error"] is False

    def test_read_filing_hands_back_the_body(self, ctx):
        searchers, _ = fake_searchers(body="The special meeting will be held on "
                                           "the tenth. " * 5)
        t = Script([reply([use("read_filing", {"source_id": "acc-1"})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        text = t.payloads[1]["messages"][2]["content"][0]["content"]
        assert "special meeting will be held" in text
        assert text.startswith("[edgar_fts] acc-1")

    def test_news_for_a_name(self, ctx):
        searchers, calls = fake_searchers()
        t = Script([reply([use("search_news", {"symbols": ["aptv"]})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert calls == [("search_news", ("APTV",))]


class TestItStillCannotInventAnything:
    def test_a_ticker_never_found_is_still_refused(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    nominate([nomination(ticker="GHOST", sid="found-1")])])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert res.candidates == []
        assert any("GHOST" in r[0] for r in res.rejected)

    def test_an_id_no_tool_returned_is_still_refused(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([nominate([nomination(sid="never-found")])])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert res.candidates == []

    def test_reading_an_id_that_is_not_in_the_feed_is_an_error_result(self, ctx):
        searchers, calls = fake_searchers()
        t = Script([reply([use("read_filing", {"source_id": "made-up"})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        r = t.payloads[1]["messages"][2]["content"][0]
        assert r["is_error"] is True and "made-up" in r["content"]
        assert calls == [], "nothing was fetched for an id that does not exist"


class TestItIsBounded:
    def test_at_most_max_tool_calls_then_it_must_nominate(self, ctx):
        searchers, calls = fake_searchers()
        turns = [reply([use("search_filings", {"phrase": f"q{i}",
                                               "catalyst_type": "merger_vote"},
                            f"tu-{i}")]) for i in range(T.MAX_TOOL_CALLS + 3)]
        t = Script(turns + [nominate([])])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert len(calls) == T.MAX_TOOL_CALLS
        assert len(res.tool_calls) == T.MAX_TOOL_CALLS
        # The turn after the cap is answered with an error result and
        # the nomination is forced.
        over = t.payloads[T.MAX_TOOL_CALLS + 1]
        assert over["tool_choice"] == {"type": "tool", "name": "nominate_candidates"}
        last_user = over["messages"][-1]["content"]
        assert any(b.get("type") == "tool_result" and b.get("is_error")
                   for b in last_user)

    def test_a_governor_refusal_ends_the_tool_work(self, ctx, monkeypatch):
        import catalyst.cost.governor as governor

        decisions = iter([True, False])   # first turn ok, tool turn refused

        class D:
            def __init__(self, ok):
                self.authorized, self.reason = ok, "" if ok else "cap_exceeded"

        # hunt() imports authorize from the governor at call time.
        monkeypatch.setattr(governor, "authorize",
                            lambda *a, **k: D(next(decisions, False)))
        searchers, calls = fake_searchers()
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert calls == [], "a tool ran after the governor refused the turn"
        assert t.payloads[1]["tool_choice"]["name"] == "nominate_candidates"

    def test_every_turn_is_priced(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    reply([use("read_filing", {"source_id": "found-1"}, "tu-2")]),
                    nominate([])])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        n = ctx.conn.execute("SELECT COUNT(*) FROM cost_events "
                             "WHERE component='hunt'").fetchone()[0]
        assert n == 3 and res.turns == 3
        assert res.cost_cents > 0

    def test_a_model_that_stops_without_nominating_is_asked_once(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([reply([{"type": "text", "text": "Nothing here."}],
                          stop="end_turn"),
                    nominate([])])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert res.skipped_reason is None
        assert t.payloads[1]["tool_choice"]["name"] == "nominate_candidates"
        assert res.turns == 2

    def test_a_model_that_never_nominates_is_given_up_on(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([reply([{"type": "text", "text": "no"}], stop="end_turn"),
                    reply([{"type": "text", "text": "still no"}], stop="end_turn")])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert "no nominate_candidates" in (res.skipped_reason or "")
        assert res.turns == 2


class TestFailuresAreReadableNotFatal:
    def test_a_searcher_that_raises_is_an_error_result(self, ctx):
        def boom(*a, **k):
            raise RuntimeError("efts down")
        searchers = {"search_filings": boom}
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    nominate([])])
        res = H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        r = t.payloads[1]["messages"][2]["content"][0]
        assert r["is_error"] and "efts down" in r["content"]
        assert res.skipped_reason is None

    def test_an_sec_block_disables_the_sec_tools_for_the_rest_of_the_hunt(self, ctx):
        from catalyst.data.sources.edgar_form4 import RateLimitBlocked

        calls = []

        def blocked(*a, **k):
            raise RateLimitBlocked("blocked", raw_text="Threshold Exceeded")

        def read(ev):
            calls.append("read")
            return "body"
        searchers = {"search_filings": blocked, "read_filing": read}
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    reply([use("read_filing", {"source_id": "acc-1"}, "tu-2")]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        assert calls == [], "a read went to sec.gov after it blocked us"
        r = t.payloads[2]["messages"][-1]["content"][0]
        assert r["is_error"] and "not available" in r["content"]

    def test_a_search_that_finds_nothing_says_so(self, ctx):
        searchers, _ = fake_searchers(found=[])
        t = Script([reply([use("search_filings", {"phrase": "x",
                                                  "catalyst_type": "merger_vote"})]),
                    nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        r = t.payloads[1]["messages"][2]["content"][0]
        assert "found nothing" in r["content"] and r["is_error"] is False


class TestWithoutHandsNothingChanged:
    def test_no_searchers_means_the_old_single_forced_call(self, ctx):
        t = Script([nominate([])])
        H.hunt(FEED, NOW, t, ctx)
        p = t.payloads[0]
        assert p["tool_choice"] == {"type": "tool", "name": "nominate_candidates"}
        assert [x["name"] for x in p["tools"]] == ["nominate_candidates"]
        assert "YOU HAVE HANDS" not in p["messages"][0]["content"]

    def test_with_searchers_the_choice_is_the_models(self, ctx):
        searchers, _ = fake_searchers()
        t = Script([nominate([])])
        H.hunt(FEED, NOW, t, ctx, searchers=searchers)
        p = t.payloads[0]
        assert p["tool_choice"] == {"type": "auto"}
        assert {x["name"] for x in p["tools"]} == {
            "nominate_candidates", "search_filings", "search_news", "read_filing"}
        assert "YOU HAVE HANDS" in p["messages"][0]["content"]


class TestThePromptTellsItWhatTheFixedFeedAlreadySearches:
    def test_the_fixed_phrases_are_listed_so_it_looks_elsewhere(self, ctx):
        from catalyst.data.sources.edgar_fts import QUERIES

        text = H.render_hunt_prompt(FEED, NOW, searchers=fake_searchers()[0])
        assert QUERIES[0].phrase in text
        assert "yours to look for" in text

    def test_it_is_told_the_date_lives_in_the_body(self, ctx):
        text = H.render_hunt_prompt(FEED, NOW, searchers=fake_searchers()[0])
        assert "The DATE is in the body" in text


class TestTheLiveSearchersAreWired:
    def test_the_scheduler_hands_the_hunt_real_searchers(self):
        from pathlib import Path

        import catalyst.orchestrator.scheduler as sch

        src = Path(sch.__file__).read_text()
        assert "searchers=live_searchers(" in src

    def test_live_searchers_offer_news_only_with_credentials(self):
        assert "search_news" not in T.live_searchers(None, None)
        assert "search_news" in T.live_searchers("k", "s")
        assert {"search_filings", "read_filing"} <= set(T.live_searchers(None, None))

    def test_read_filing_resolves_the_sec_archive_url(self):
        ev = event("acc-1", "BIOX")
        assert T._archive_url(ev) == (
            "https://www.sec.gov/Archives/edgar/data/1234567/"
            "000123456726000001/0001234567-26-000001.txt")
        assert T._archive_url(RawEvent("alpaca_news", "n", NOW, {"url": "x"})) is None

    def test_markup_is_stripped_and_capped(self):
        html = "<html><body><p>Special&nbsp;meeting</p><br/>" + "x" * 50_000
        out = T._strip_markup(html)
        assert out.startswith("Special meeting")
        assert "<p>" not in out


class TestTheCheckCanFail:
    def test_the_old_shape_is_detectable(self, ctx):
        """Without searchers there is exactly one turn and one tool -
        which is what the hunt was. The tests above would all fail
        against it."""
        t = Script([nominate([])])
        res = H.hunt(FEED, NOW, t, ctx)
        assert res.turns == 1 and res.tool_calls == []
