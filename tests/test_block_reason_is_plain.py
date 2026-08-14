"""A block must say what it IS and what to DO, not just its identifier.

OWNER-REPORTED, looking at the funnel: "Unsure what the reason for block
actually is it needs to be clearer."

The page said:

    125  spending was blocked: reconciliation_discrepancy_unacknowledged

That is exact and useless. It is a code identifier, and the owner cannot
tell from it whether the bot is out of money, whether something is
broken, or what would make it start again. The three governor gates need
three completely different responses and the page named none of them.

THE IDENTIFIER IS KEPT BESIDE THE SENTENCE, never instead of it. It is
what makes a report greppable and it is what a log search matches on, so
removing it would trade one audience's clarity for another's. Both fit.

Also pinned here: the auto-zoom on the brain map. "maybe zoom is as the
network gets bigger" - a map comfortable at 20 nodes is a wall at 120,
and asking someone to keep re-picking a zoom as the graph grows is
asking them to do the layout's job.
"""

import re

import pytest

from catalyst.dashboard import charts, queries
from catalyst.dashboard.queries import (
    GOVERNOR_REASONS,
    explain_governor_reason,
)
from tests.test_dashboard import bare, seeded  # noqa: F401 - shared fixtures


class TestEveryGateExplainsItself:
    @pytest.mark.parametrize("reason", sorted(GOVERNOR_REASONS))
    def test_it_has_a_plain_sentence_and_an_action(self, reason):
        plain, todo = explain_governor_reason(reason)
        assert plain != reason, f"{reason} was not translated at all"
        assert "_" not in plain, (
            f"{plain!r} still reads like an identifier")
        assert len(todo) > 30, f"{reason} has no actionable advice"

    def test_the_three_gates_do_NOT_read_the_same(self):
        """They need three different responses - raise the budget,
        acknowledge a check, report a bug. A page that describes them
        alike sends the owner to the wrong place."""
        plains = {explain_governor_reason(r)[0] for r in GOVERNOR_REASONS}
        assert len(plains) == len(GOVERNOR_REASONS)

    def test_the_reconciliation_block_says_it_is_NOT_out_of_money(self):
        """The one that actually caught the owner. It is the check
        holding spending, not an exhausted budget, and the fix is a click
        rather than a bigger cap."""
        plain, todo = explain_governor_reason(
            "reconciliation_discrepancy_unacknowledged")
        assert "NOT out of money" in todo
        assert "acknowledge" in todo.lower()

    def test_an_unknown_reason_is_passed_through_not_guessed(self):
        """A confident wrong explanation is worse than the identifier."""
        plain, todo = explain_governor_reason("some_new_gate")
        assert plain == "some_new_gate"
        assert "no plain-English explanation" in todo


class TestTheIdentifierSurvives:
    def test_the_funnel_carries_both_the_sentence_and_the_code(self, seeded):
        """Removing the identifier would trade one audience's clarity for
        another's: it is what a log search matches on."""
        import sqlite3
        from datetime import datetime, timezone

        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db

        conn = sqlite3.connect(seeded)
        conn.execute(
            "INSERT INTO cost_governor_events (cycle_id, requested_kind, "
            "estimate_cents, cap_cents, decision, reason, at) "
            "VALUES ('c','scheduled','15','500','deny',"
            "'reconciliation_discrepancy_unacknowledged',?)",
            (datetime.now(timezone.utc).isoformat(),))
        conn.commit()
        conn.close()

        faults = [f for s in queries.funnel(Db(seeded)).stages
                  for f in s.faults]
        assert faults, "the seed should produce a governor fault"
        text = " ".join(f"{f[0]} {f[2]}" for f in faults)
        assert "cost cross-check" in text, "no plain sentence"
        assert "reconciliation_discrepancy_unacknowledged" in text, (
            "the identifier was dropped - a log search can no longer "
            "match what the page says")


class TestTheAdviceLeadsSOMEWHERE:
    """OWNER-REPORTED, a second time: "still says 125 spending was
    blocked: reconciliation_discrepancy_unacknowledged".

    The advice read "Open Maintenance and acknowledge it". Verified by
    fetching both pages:

        /maintenance   acknowledge form present: NO
        /costs         acknowledge form present: YES

    So the owner did as told, found nothing to click, and reported the
    block as unfixed. It WAS unfixed - by a sentence, not by the cost
    code. Advice that names the wrong page is worse than no advice,
    because it costs a trip and reads as a broken feature.
    """

    @pytest.fixture
    def blocked(self, tmp_path):
        """A database in exactly the state the owner's is: one paused
        reconciliation, unacknowledged, and 125 denials citing it.

        THE DENIALS BELONG IN THE FIXTURE. Without them /funnel renders
        no fault block at all, and a test that reads the fault block then
        passes by finding nothing. That is not hypothetical - the first
        version of test_the_link_REACHES_THE_PAGE_as_a_link went green
        against a deliberately broken build for exactly this reason.
        """
        import pathlib
        import sqlite3
        from datetime import datetime, timezone

        p = str(tmp_path / "block.db")
        conn = sqlite3.connect(p)
        root = pathlib.Path(__file__).resolve().parents[1]
        for f in ("catalyst/storage/schema.sql",
                  "catalyst/storage/schema_graph.sql",
                  "catalyst/dashboard/schema_logs.sql"):
            conn.executescript((root / f).read_text())
        conn.execute(
            "INSERT INTO cost_reconciliation_events (id,target_date,kind,"
            "component,local_total_cents,cost_api_total_cents,"
            "discrepancy_cents,threshold_cents,action_taken,api_record_count,"
            "api_raw_response,reconciled_at) VALUES ('re1','2026-08-12',"
            "'scheduled','catalyst.research','1200','1207','7','5',"
            "'scheduled_paused',3,'{}','2026-08-13T00:00:00+00:00')")
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO cost_governor_events (cycle_id, requested_kind, "
            "estimate_cents, cap_cents, decision, reason, at) VALUES "
            "(?,'scheduled','15','500','deny',"
            "'reconciliation_discrepancy_unacknowledged',?)",
            [(f"cy{i}", now) for i in range(125)])
        conn.commit()
        conn.close()
        return p

    def test_the_fixture_really_does_render_a_fault_block(self, blocked):
        """Guards every test below it. If this stops holding they all
        start passing by finding nothing."""
        assert 'class="funnel-fault"' in self._render(blocked, "/funnel")

    def _render(self, db_path, path):
        """Render a route IN PROCESS. The suite is offline by contract,
        so this goes through the same route table the server dispatches
        on rather than over a socket."""
        from catalyst.dashboard.db import Db
        from catalyst.dashboard.server import HTML_ROUTES

        assert path in HTML_ROUTES, f"{path} is not a route at all"
        return HTML_ROUTES[path](Db(db_path), {})

    def test_the_page_the_advice_names_HAS_the_button(self, blocked):
        """Follow the instruction the way the owner would, and check the
        thing it promises is on the other end."""
        href, label = queries.governor_reason_link(
            "reconciliation_discrepancy_unacknowledged")
        assert label, "the link has no words on it"
        path = href.split("#")[0]
        assert "/acknowledge-reconciliation" in self._render(blocked, path), (
            f"the advice sends the owner to {path}, which has no "
            "acknowledge form on it")

    def test_the_page_it_used_to_name_still_does_NOT_have_it(self, blocked):
        """The sabotage this pair is built around: if /maintenance ever
        grows the form, the advice may point there again - but until it
        does, this records WHY the old wording was wrong rather than
        leaving it as a stale opinion."""
        assert "/acknowledge-reconciliation" not in self._render(
            blocked, "/maintenance")

    def test_the_anchor_it_jumps_to_actually_exists(self, blocked):
        """A link to #cost-unacked that lands nowhere drops the reader at
        the top of a long page to hunt."""
        href, _label = queries.governor_reason_link(
            "reconciliation_discrepancy_unacknowledged")
        path, _, anchor = href.partition("#")
        assert anchor, "no anchor, so the link lands at the top"
        assert f'id="{anchor}"' in self._render(blocked, path), (
            f"#{anchor} is not an id on {path}")

    def test_every_page_named_by_any_gate_is_a_real_route(self, blocked):
        """The same mistake in any of the three would read identically."""
        for reason in list(GOVERNOR_REASONS) + ["some_new_gate"]:
            href, label = queries.governor_reason_link(reason)
            assert label, f"{reason} offers a link with no words on it"
            path, _, anchor = href.partition("#")
            html_out = self._render(blocked, path)
            assert html_out, f"{reason} points at {path}, which rendered nothing"
            assert f'id="{anchor}"' in html_out, (
                f"{reason} points at #{anchor} on {path}, which has no such id")

    def test_the_link_REACHES_THE_PAGE_as_a_link(self, blocked):
        """The one a text-only check cannot make. The advice goes through
        raw(), which escapes and redacts - correctly, since stored text
        must never become markup. An anchor written INTO the sentence
        therefore renders as literal &lt;a href=...&gt; on screen, which
        is how this shipped the first time and how rendering caught it."""
        html_out = self._render(blocked, "/funnel")
        assert "&lt;a href" not in html_out, (
            "an anchor was escaped into visible gibberish")

    def test_the_funnel_block_offers_the_way_out(self, blocked):
        """End to end, in the owner's exact state: 125 denials on one
        unacknowledged reconciliation."""
        import re

        html_out = self._render(blocked, "/funnel")
        fault = re.search(r'<div class="funnel-fault".*?</div>', html_out, re.S)
        assert fault, "no fault block rendered at all"
        assert 'href="/costs#cost-unacked"' in fault.group(0), (
            "the block says spending stopped and offers no way to resume it")


class TestTheMapZoomsWithTheNetwork:
    def _width(self, n):
        layers = [("A", [(f"a{i}", f"n{i}", 1) for i in range(n)]),
                  ("B", [(f"b{i}", f"n{i}", 1) for i in range(n)])]
        svg = charts.neural_map(layers, [], chart_id="m", max_per_layer=999)
        return int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))

    def test_a_small_map_is_not_zoomed(self):
        assert self._width(6) == 1180

    def test_a_big_map_zooms_itself(self):
        assert self._width(40) > self._width(6)
        assert self._width(90) > self._width(40)

    def test_it_never_exceeds_the_manual_ceiling(self):
        """Auto-zoom must obey the same bound a pasted URL does, or a
        busy graph could ask for an unusable canvas."""
        assert self._width(500) <= 1180 * 3

    def test_an_explicit_zoom_can_still_go_HIGHER(self):
        """Automatic is a floor, not a cap - the owner can still ask for
        more than the map chose for itself."""
        layers = [("A", [(f"a{i}", f"n{i}", 1) for i in range(40)])]
        auto = int(re.search(r'viewBox="0 0 (\d+)', charts.neural_map(
            layers, [], chart_id="m", max_per_layer=999)).group(1))
        manual = int(re.search(r'viewBox="0 0 (\d+)', charts.neural_map(
            layers, [], chart_id="m", max_per_layer=999, zoom=3.0)).group(1))
        assert manual >= auto
