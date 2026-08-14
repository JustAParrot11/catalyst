"""The chain must tell the story IN ORDER, with a reason at every step.

OWNER-ASKED, in these words: "I want every decision with justification
in order from what is researched to find to placing a trade" and "need
a section for the bot to re-evaluate every now and again for current
trades".

The brain map answers "what is connected to what". It cannot answer
"what happened, then what, and why", because a picture of a graph has no
order in it. This is the other thing, and the two are not substitutes.

The rule these tests exist to hold: NO STEP IS SILENT. Every step says
why it moved on or why it stopped, and a step whose evidence is missing
says THAT rather than being quietly dropped - a chain that hides its
gaps reads as a decision that was never made.
"""

import sqlite3

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from tests.test_dashboard import bare, seeded  # noqa: F401 - shared fixtures


class TestTheChainIsOrderedAndComplete:
    def test_steps_run_found_to_traded_in_that_order(self, seeded):
        chains = queries.decision_chains(Db(seeded)).chains
        assert chains, "the seed should produce at least one chain"
        stages = [s.stage for c in chains for s in c.steps]
        assert "Found" in stages and "Judged" in stages
        for chain in chains:
            numbers = [s.n for s in chain.steps]
            assert numbers == sorted(numbers), (
                f"{chain.ticker}: steps are numbered out of order: {numbers}")
            order = [s.stage for s in chain.steps]
            if "Found" in order and "Judged" in order:
                assert order.index("Found") < order.index("Judged")
            if "Judged" in order and "Sized" in order:
                assert order.index("Judged") < order.index("Sized")

    def test_every_step_carries_a_reason(self, seeded):
        """THE POINT. A step with no `why` is exactly the thing the owner
        could not get from the map: it tells you something happened and
        not why it did."""
        for chain in queries.decision_chains(Db(seeded)).chains:
            for step in chain.steps:
                assert step.why and step.why.strip(), (
                    f"{chain.ticker} step {step.n} ({step.stage}) has no "
                    "justification")
                assert step.headline and step.headline.strip()

    def test_the_models_own_words_are_carried_through(self, seeded):
        """Not a paraphrase and not a score - the thesis it wrote,
        VERBATIM. Asserted against the fixture's sentinel rather than a
        length: "longer than 20 characters" was an arbitrary proxy that
        happened to fail on a short thesis while proving nothing about
        whether the words were the model's."""
        chains = queries.decision_chains(Db(seeded)).chains
        judged = [s for c in chains for s in c.steps if s.stage == "Judged"]
        assert judged
        assert any("THESIS-TEXT" in s.why for s in judged), (
            f"the thesis did not reach the Judged step: "
            f"{[s.why for s in judged]}")
        # ...and the invalidation, which is what a review is scored on
        assert any("INVALIDATION-TEXT" in str(s.detail) for s in judged)

    def test_a_missing_source_row_is_STATED_not_skipped(self, bare):
        """A chain that hides its gaps reads as a decision that was never
        made. The candidate cites source_event_ids that do not exist."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(bare)
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     ("ghost", "GHST", "x", now.date().isoformat(),
                      "estimated", '["no-such-event"]', now.isoformat(),
                      "s", "[]"))
        conn.commit()
        conn.close()
        chain = queries.decision_chains(Db(bare)).chains[0]
        found = [s for s in chain.steps if s.stage == "Found"][0]
        assert found.stopped, "an unresolvable source must mark the step"
        assert "no raw_events row matched" in str(found.detail)


class TestTheStepsExpandWithoutJavaScript:
    def test_each_step_is_a_details_element(self, seeded):
        html_out = panels.chain_panel(Db(seeded))
        assert html_out.count("<details") >= 2
        assert "<script" not in html_out.lower()
        assert "onclick" not in html_out.lower()

    def test_a_judged_step_links_to_the_full_record(self, seeded):
        html_out = panels.chain_panel(Db(seeded))
        assert "view=full" in html_out, (
            "the Judged step should offer the whole audit trail")

    def test_an_empty_database_explains_itself(self, bare):
        html_out = panels.chain_panel(Db(bare))
        assert "No candidates yet" in html_out
        assert "<script" not in html_out.lower()


class TestOpenPositionsAreReChecked:
    def test_it_names_the_review_interval(self, bare):
        from catalyst.research.position_review import REVIEW_INTERVAL_HOURS

        html_out = panels.open_positions_panel(Db(bare))
        assert str(REVIEW_INTERVAL_HOURS) in html_out

    def test_it_states_the_only_ever_shorten_rule(self, bare):
        """The rule that makes the whole feature safe belongs on the
        page, not only in the code."""
        html_out = panels.open_positions_panel(Db(bare))
        assert "never push it out" in html_out or "forward" in html_out

    def test_nothing_open_is_explained_rather_than_blank(self, bare):
        html_out = panels.open_positions_panel(Db(bare))
        assert "Nothing is open right now" in html_out

    def test_a_HOLD_review_is_shown_beside_one_that_acted(self, bare):
        """Listing only the reviews that changed something would make the
        model look decisive in hindsight and hide the common answer."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(bare)
        conn.execute(
            "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
            ("pos-r", "RVW", '["o1"]', None,
             (now - timedelta(days=4)).isoformat(),
             (now + timedelta(days=6)).date().isoformat(), "open"))
        for i, action in enumerate(("hold", "exit_now")):
            conn.execute(
                "INSERT INTO position_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"rv{i}", "pos-r", "RVW", action, i,
                 f"reasoning for {action}", "[]", "prompt", None,
                 "claude-sonnet-5", "8", None,
                 (now - timedelta(days=2 - i)).isoformat()))
        conn.commit()
        conn.close()
        html_out = panels.open_positions_panel(Db(bare))
        assert "hold" in html_out and "exit_now" in html_out
        assert "reasoning for hold" in html_out, (
            "a review that changed nothing must still be shown")
