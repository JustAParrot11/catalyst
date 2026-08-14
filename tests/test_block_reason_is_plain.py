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

from catalyst.dashboard import charts
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
        text = " ".join(f"{r} {d}" for r, _, d in faults)
        assert "cost cross-check" in text, "no plain sentence"
        assert "reconciliation_discrepancy_unacknowledged" in text, (
            "the identifier was dropped - a log search can no longer "
            "match what the page says")


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
