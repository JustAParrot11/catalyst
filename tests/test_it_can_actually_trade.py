"""The bot must be able to say yes, and be shown the numbers to say it on.

OWNER, 2026-08-14, across three messages:

    "the real value here is the bot reading the market and news and
     using the numbers and data as backing for it to make the ultimate
     call"
    "I want an agentic trading bot that can make confident trades doing
     its own research aswell and linking internet searches to
     opportunity"
    "balance it out so it can also make money confidently"

On the owner's live day it made zero trades from 30 views, and the two
causes were structural rather than a matter of the model being cautious:

  1. IT WAS ASKED TO JUDGE PRICE WITHOUT PRICE. The prompt asks "what
     price and volume have done since each filing became public" and
     carried neither. The snapshot existed - cycle.py built it three
     lines before the call and handed it only to the risk engine. 26 of
     30 views answered "already priced in", which is what a question
     with no evidence attached gets answered.

  2. PRICED-IN WAS AN ABSOLUTE VETO PLACED AHEAD OF CONVICTION, so a
     candidate the model was sure about was discarded without conviction
     ever being read. It has never been measured: it is not an adaptive
     parameter, and the refusal tracker aggregates only
     below_conviction_floor.

The strategy analyst's measurement is why the second one matters:
accepting every signal beat SPY by 16.6pp, refusing three quarters lost
by 59.5pp, refusing all of them - the live rate - by 68.7pp. A filter
needs roughly 60/40 discrimination just to break even against not
filtering at all.

WHAT MUST STAY TRUE. The model still only ever proposes. Code still
decides whether to trade, how large and where the stop sits, and the
hard bounds are untouched. Balance is not the same as removing the
brakes, so every test below that opens the gate has a partner that
keeps it shut.
"""

from decimal import Decimal

import pytest

from catalyst.research import prompts
from catalyst.risk.evaluate import PRICED_IN_CONVICTION_PREMIUM


class _Market:
    ticker = "ABCD"
    last_close = Decimal("12.34")
    half_spread_bp = Decimal("18")
    median_daily_dollar_volume = Decimal("2400000")


class TestTheModelIsShownTheNumbers:
    def test_the_prompt_carries_price_spread_and_volume(self):
        text = prompts.render_market_section(_Market())
        assert "12.34" in text, "no price"
        assert "18" in text, "no spread"
        assert "2,400,000" in text, "no volume"

    def test_a_missing_snapshot_is_STATED_not_left_blank(self):
        """A blank is filled by the model with an assumption; a sentence
        is not."""
        text = prompts.render_market_section(None)
        assert "Unavailable" in text
        assert "unverified" in text

    def test_the_question_points_at_the_data_it_is_given(self):
        from catalyst.discovery import Candidate
        from datetime import date

        c = Candidate(
            id="c1", ticker="ABCD", catalyst_type="insider_cluster",
            catalyst_date=date(2026, 8, 25),
            catalyst_date_confidence="confirmed",
            source_event_ids=("e1",), discovered_at=None,
            sector="2870", correlation_tags=("tech",))
        text = prompts.render_research_prompt(c, market=_Market())
        assert "MARKET DATA" in text
        assert "12.34" in text
        assert "Use the MARKET DATA above" in text
        assert "SAY WHICH EVIDENCE YOU USED" in text

    def test_searching_is_framed_as_the_job_not_an_overhead(self):
        """Owner: "linking internet searches to opportunity". The brief
        used to say "search only when the result could change your
        answer", which is a budget instruction wearing a reasoning
        instruction's clothes."""
        from catalyst.discovery import Candidate
        from datetime import date

        c = Candidate(
            id="c1", ticker="ABCD", catalyst_type="insider_cluster",
            catalyst_date=date(2026, 8, 25),
            catalyst_date_confidence="confirmed",
            source_event_ids=("e1",), discovered_at=None,
            sector="2870", correlation_tags=("tech",))
        text = prompts.render_research_prompt(c, market=_Market())
        assert "searching is the job" in text
        assert "search only when the result could change your answer" \
            not in text
        assert "Unused searches are not a saving" in text


class TestTheGateCanOpenAndStillShuts:
    """Both directions, because a premium that admits everything is as
    wrong as a veto that admits nothing."""

    def test_the_premium_is_real(self):
        assert PRICED_IN_CONVICTION_PREMIUM > Decimal("0")

    def test_it_is_not_so_large_it_is_a_veto_by_another_name(self):
        """Conviction is bounded at 1.0. A floor of 0.60 plus a premium
        that pushes past 1.0 would reinstate the veto while looking like
        a threshold."""
        assert Decimal("0.60") + PRICED_IN_CONVICTION_PREMIUM < Decimal("1.0")


class TestTheDashboardSaysWhichItWas:
    def test_the_two_priced_in_reasons_do_not_read_alike(self):
        """They need different responses: one says the floor is too
        high, the other says the premium is."""
        from catalyst.dashboard.queries import _plain_skip

        raised = _plain_skip("priced_in_below_raised_floor")
        ordinary = _plain_skip("conviction_below_floor")
        assert raised != ordinary
        assert "higher bar" in raised
        assert "_" not in raised, "still reads like an identifier"

    def test_an_unknown_skip_code_still_surfaces(self):
        """A new reason must appear rather than read as blank."""
        from catalyst.dashboard.queries import _plain_skip

        assert _plain_skip("some_new_gate") == "some new gate"

    def test_a_decision_records_whether_it_SAW_the_numbers(self, tmp_path):
        """A priced-in call made without price is a guess, and after the
        fact the only way to tell is the prompt that was actually sent."""
        import pathlib
        import sqlite3

        from catalyst.dashboard.db import Db
        from catalyst.dashboard.queries import _market_data_note

        p = str(tmp_path / "d.db")
        conn = sqlite3.connect(p)
        root = pathlib.Path(__file__).resolve().parents[1]
        for f in ("catalyst/storage/schema.sql",
                  "catalyst/dashboard/schema_logs.sql"):
            conn.executescript((root / f).read_text())
        conn.execute(
            "INSERT INTO candidates VALUES ('c1','ABCD','insider_cluster',"
            "'2026-08-25','confirmed','[]','2026-08-14T00:00:00+00:00',"
            "'2870','[]')")
        # A call from BEFORE the market section existed.
        conn.execute(
            "INSERT INTO research_calls VALUES ('old','c1','claude-sonnet-5',"
            "'ANSWER THESE ... 6. priced_in','[]','5',10,NULL,"
            "'2026-08-13T00:00:00+00:00')")
        conn.commit()
        note = _market_data_note(Db(p), "c1")
        assert "NONE" in note, (
            "an uninformed decision is being presented as an informed one")

        # ...and one from after.
        conn.execute(
            "INSERT INTO research_calls VALUES ('new','c1','claude-sonnet-5',"
            "?,'[]','5',10,NULL,'2026-08-14T12:00:00+00:00')",
            (prompts.render_market_section(_Market()),))
        conn.commit()
        conn.close()
        note2 = _market_data_note(Db(p), "c1")
        assert "12.34" in note2
        assert "NONE" not in note2

    def test_it_reads_the_PROMPT_not_the_current_code(self, tmp_path):
        """Re-deriving from today's code would quietly relabel every old
        decision as well-informed."""
        import pathlib
        import sqlite3

        from catalyst.dashboard.db import Db
        from catalyst.dashboard.queries import _market_data_note

        p = str(tmp_path / "e.db")
        conn = sqlite3.connect(p)
        root = pathlib.Path(__file__).resolve().parents[1]
        for f in ("catalyst/storage/schema.sql",
                  "catalyst/dashboard/schema_logs.sql"):
            conn.executescript((root / f).read_text())
        conn.execute(
            "INSERT INTO candidates VALUES ('c1','ABCD','insider_cluster',"
            "'2026-08-25','confirmed','[]','2026-08-14T00:00:00+00:00',"
            "'2870','[]')")
        conn.commit()
        conn.close()
        assert "no research prompt recorded" in _market_data_note(Db(p), "c1")
