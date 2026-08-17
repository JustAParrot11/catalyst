"""Claude nominates its own candidates, and cannot invent one.

OWNER-ASKED: "surely to make this properly agentic we want claude go out
and finds its own trades and then ask the rest of the bot for data?
deterministic isnt an agentically trading bit surely?"

They were right, and the objection had been aimed at the wrong thing.
Two rules had been treated as one:

  WHO SIZES AND PLACES          stays deterministic. Ruin prevention.
  WHO CHOOSES WHAT TO LOOK AT   the brief says nothing about it, and
                                there is no safety reason it cannot be
                                the model.

The second one is now Claude's. The first one is untouched, and the
same discipline is applied to both: the model proposes, code disposes.

WHAT MAKES IT SAFE IS THAT NOMINATION IS NOT CREATION. Claude may only
point at raw_events that already exist, by their real source_id, and
every nomination is checked against the evidence it cites. Most of this
file is those checks, because they are the entire safety argument:

    cited ids must resolve to rows really in the feed
    the ticker must appear in those rows' own payloads
    the catalyst type must be one the risk engine prices
    the date must be near-term and not in the past
    the ticker must pass the same tradeability screen as everything else

The worst a confidently wrong model can do is waste its own nomination.
It cannot conjure a company, move a real filing onto a different ticker,
or invent a catalyst the sizing code has never heard of.
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.data import RawEvent
from catalyst.discovery import hunt as H

NOW = datetime.now(timezone.utc)
SOON = (NOW + timedelta(days=10)).date().isoformat()


def event(source_id, ticker, source="edgar_fts", extra=""):
    return RawEvent(
        source=source, source_id=source_id, fetched_at=NOW,
        payload_raw={"ticker": ticker, "company": f"{ticker} Inc",
                     "filed_date": str(NOW.date()), "note": extra})


FEED = [event("acc-1", "BIOX"), event("acc-2", "MRNS"),
        event("news-1", "APTV", source="alpaca_news")]


def nomination(**over):
    base = {"ticker": "BIOX", "catalyst_type": "clinical_readout",
            "catalyst_date": SOON, "date_confidence": "estimated",
            "source_event_ids": ["acc-1"],
            "why": "Phase 3 topline expected; single-product company."}
    base.update(over)
    return base


def transport_returning(noms, usage=None):
    def transport(payload):
        return {
            "content": [{"type": "tool_use", "name": "nominate_candidates",
                         "input": {"nominations": noms}}],
            "stop_reason": "tool_use",
            "usage": usage or {"input_tokens": 40000, "output_tokens": 900},
        }
    return transport


@pytest.fixture
def ctx(tmp_path):
    from catalyst.research.boundary import CostContext
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "h.db"))
    yield CostContext(conn=conn, governor_profit_share=Decimal("0"),
                      cycle_id="c1", kind="scheduled",
                      owner_monthly_cap_cents=Decimal("10000"))
    conn.close()


def run(ctx, noms):
    return H.hunt(FEED, NOW, transport_returning(noms), ctx)


class TestItCannotInventAnything:
    def test_a_ticker_the_cited_events_never_mention_is_refused(self, ctx):
        """The load-bearing check. A real filing must not be re-labelled
        onto a different company - that is how a hallucinated trade
        would look if one were possible."""
        res = run(ctx, [nomination(ticker="TSLA")])
        assert res.candidates == []
        assert any("do not mention this ticker" in why
                   for _t, why in res.rejected), res.rejected

    def test_a_source_id_that_does_not_exist_is_refused(self, ctx):
        res = run(ctx, [nomination(source_event_ids=["acc-999"])])
        assert res.candidates == []
        assert any("no source event that exists" in why
                   for _t, why in res.rejected), res.rejected

    def test_citing_nothing_at_all_is_refused(self, ctx):
        res = run(ctx, [nomination(source_event_ids=[])])
        assert res.candidates == []

    def test_a_catalyst_type_the_risk_engine_cannot_price_is_refused(self, ctx):
        """No sizing basis means no position. Refused rather than
        silently given a default gap, which would size a real trade off
        a number nobody chose."""
        res = run(ctx, [nomination(catalyst_type="vibes")])
        assert res.candidates == []
        assert any("no sizing basis" in why for _t, why in res.rejected)

    @pytest.mark.parametrize("bad", ["", "1234", "TOOLONGSYM", "B-X", None])
    def test_an_implausible_symbol_is_refused(self, ctx, bad):
        res = run(ctx, [nomination(ticker=bad)])
        assert res.candidates == []

    def test_a_date_in_the_past_is_refused(self, ctx):
        past = (NOW - timedelta(days=3)).date().isoformat()
        res = run(ctx, [nomination(catalyst_date=past)])
        assert res.candidates == []
        assert any("in the past" in why for _t, why in res.rejected)

    def test_a_date_beyond_the_hold_bound_is_refused(self, ctx):
        far = (NOW + timedelta(days=H.MAX_DAYS_AHEAD + 5)).date().isoformat()
        res = run(ctx, [nomination(catalyst_date=far)])
        assert res.candidates == []
        assert any("beyond" in why for _t, why in res.rejected)

    def test_a_non_date_is_refused(self, ctx):
        res = run(ctx, [nomination(catalyst_date="next quarter-ish")])
        assert res.candidates == []

    def test_an_excluded_symbol_gets_the_SAME_universe_rule(self, ctx):
        """A fund is not a company with a catalyst. The hunt must not be
        a way round the screen every other source passes through."""
        feed = FEED + [event("acc-spy", "SPY")]
        res = H.hunt(feed, NOW, transport_returning(
            [nomination(ticker="SPY", source_event_ids=["acc-spy"])]), ctx)
        assert res.candidates == []

    def test_the_same_ticker_twice_in_one_hunt_counts_once(self, ctx):
        res = run(ctx, [nomination(), nomination()])
        assert len(res.candidates) == 1
        assert any("twice" in why for _t, why in res.rejected)


class TestAValidNominationBecomesAnOrdinaryCandidate:
    def test_it_produces_a_real_candidate(self, ctx):
        res = run(ctx, [nomination()])
        assert len(res.candidates) == 1
        c = res.candidates[0]
        assert c.ticker == "BIOX"
        assert c.catalyst_type == "clinical_readout"
        assert c.catalyst_date == date.fromisoformat(SOON)
        assert "acc-1" in c.source_event_ids

    def test_the_rationale_is_kept_for_the_audit_trail(self, ctx):
        res = run(ctx, [nomination()])
        cid = res.candidates[0].id
        assert "Phase 3 topline" in res.rationales[cid]

    def test_an_unknown_sector_is_admitted_not_guessed(self, ctx):
        """The cluster bound stops four bets on the same thing looking
        like four bets. Guessing a sector from a headline would corrupt
        it silently; "unknown" is what the cluster key already expects."""
        res = run(ctx, [nomination()])
        assert res.candidates[0].sector == "unknown"

    def test_nothing_worth_nominating_is_a_valid_answer(self, ctx):
        res = run(ctx, [])
        assert res.candidates == []
        assert res.skipped_reason is None


class TestItSpendsThroughTheGovernorLikeEverythingElse:
    def test_a_refused_budget_means_no_hunt_and_no_call(self, tmp_path):
        import uuid

        from catalyst.research.boundary import CostContext
        from catalyst.storage import init_db

        conn = init_db(str(tmp_path / "b.db"))
        # Spend the month.
        conn.execute(
            "INSERT INTO cost_events (id,raw_usage_json,model,kind,component,"
            "priced_cents,priced_at,api_call_id) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "{}", "claude-sonnet-5", "scheduled",
             "research", "9990", NOW.isoformat(), "a"))
        conn.commit()
        called = []

        def transport(payload):
            called.append(payload)
            return {}

        ctx = CostContext(conn=conn, governor_profit_share=Decimal("0"),
                          cycle_id="c", kind="scheduled",
                          owner_monthly_cap_cents=Decimal("10000"))
        try:
            res = H.hunt(FEED, NOW, transport, ctx)
        finally:
            conn.close()
        assert not called, "the hunt called the API after being refused"
        assert res.skipped_reason and "budget_denied" in res.skipped_reason

    def test_the_call_is_priced_even_when_the_answer_is_useless(self, ctx):
        """A call that produced nothing usable still cost money. Priced
        before the answer is parsed, so a bad reply cannot lose the row."""
        def transport(payload):
            return {"content": [], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 50000, "output_tokens": 10}}

        res = H.hunt(FEED, NOW, transport, ctx)
        assert res.skipped_reason
        n = ctx.conn.execute(
            "SELECT COUNT(*) FROM cost_events WHERE component='hunt'"
        ).fetchone()[0]
        assert n == 1, "a billed hunt left no cost row"

    def test_hunts_scale_with_the_budget(self):
        assert H.hunts_per_day(None) == 0
        assert H.hunts_per_day(Decimal("2000")) == 0     # $20/mo: judge, do not hunt
        assert H.hunts_per_day(Decimal("10000")) >= 1    # $100/mo
        assert H.hunts_per_day(Decimal("10000")) < \
            H.hunts_per_day(Decimal("100000"))

    def test_a_budget_too_small_to_research_what_it_finds_does_not_hunt(self):
        """A nomination nobody can afford to research is worse than no
        nomination - it spends the hunt's money and then starves."""
        assert H.hunts_per_day(Decimal("500")) == 0


class TestItNeverBreaksDiscovery:
    @pytest.mark.parametrize("response", [
        {}, None, "not a dict", {"content": []},
        {"content": [{"name": "nominate_candidates", "input": {}}]},
        {"content": [{"name": "nominate_candidates",
                      "input": {"nominations": "not a list"}}]},
        {"content": [{"name": "nominate_candidates",
                      "input": {"nominations": [None, 42]}}]},
    ])
    def test_a_malformed_reply_returns_empty_rather_than_raising(
            self, ctx, response):
        res = H.hunt(FEED, NOW, lambda p: response, ctx)
        assert res.candidates == []

    def test_a_transport_that_raises_is_caught(self, ctx):
        def boom(payload):
            raise RuntimeError("connection reset")

        res = H.hunt(FEED, NOW, boom, ctx)
        assert res.candidates == []
        assert "transport_error" in (res.skipped_reason or "")

    def test_no_transport_means_no_hunt(self, ctx):
        res = H.hunt(FEED, NOW, None, ctx)
        assert res.skipped_reason == "no_model_transport_configured"

    def test_an_empty_feed_means_no_hunt(self, ctx):
        res = H.hunt([], NOW, transport_returning([nomination()]), ctx)
        assert res.candidates == []
        assert res.skipped_reason == "no_raw_events_to_read"


class TestThePromptShowsRealEvidenceAndBoundsItsCost:
    def test_the_digest_carries_the_real_source_ids(self):
        text = H.render_hunt_prompt(FEED, NOW)
        for e in FEED:
            assert e.source_id in text

    def test_it_says_which_tickers_the_screen_already_found(self):
        text = H.render_hunt_prompt(FEED, NOW, already_known={"REGN", "ACME"})
        assert "REGN" in text and "ACME" in text

    def test_a_huge_feed_does_not_make_an_unbounded_prompt(self):
        big = [event(f"acc-{i}", "AAAA", extra="x" * 5000) for i in range(2000)]
        text = H.render_hunt_prompt(big, NOW)
        cap = H.MAX_EVENTS_IN_DIGEST * (H.DIGEST_CHARS + 200) + 4000
        assert len(text) < cap, (
            f"{len(text)} characters of prompt from a busy feed day - the "
            "input cost scales with feed volume")

    def test_it_tells_the_model_an_empty_answer_is_fine(self):
        """Otherwise every quiet day produces a weak nomination that
        spends a research call for nothing."""
        text = H.render_hunt_prompt(FEED, NOW).lower()
        assert "nominate nothing" in text

    def test_it_does_not_ask_for_sizes_or_prices(self):
        text = H.render_hunt_prompt(FEED, NOW).lower()
        assert "never sizes or prices" in text
        props = H.NOMINATE_TOOL["input_schema"]["properties"]["nominations"]
        fields = props["items"]["properties"]
        banned = ("price", "qty", "quantity", "size", "notional", "stop")
        assert not [f for f in fields if any(b in f.lower() for b in banned)]
