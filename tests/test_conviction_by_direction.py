"""Why it has not traded, said as a number instead of a shrug.

OWNER-ASKED 2026-08-26: "is it still trading and making decisions as its
meant to? The logic isnt broken? Its not traded in a while."

Measured over all 154 views on their machine, conviction is two
distributions and it is not close:

    no_trade   n=137   median 0.68   max 0.85   101 at or above 0.60
    long       n=17    median 0.54   max 0.60     1 at or above 0.60

The model scores its DECLINES high and its directional calls low. That
is what a calibrated model does - CLAUDE.md defines conviction as how
often this call would be right, and "not worth trading" is an easier
thing to be right about than the direction of a stock over days to
weeks. It also means a 0.60 floor is being asked of the one population
that rarely reaches it.

The conviction panel could not show this: it drops no_trade views, on
the reasonable ground that they carry no directional call. So "the model
is consistently below the bar" read as a timid model, when the same
model scores 0.85 the moment it declines something.

NOTHING HERE MOVES THE FLOOR, and a test holds that. The floor is an
adaptive parameter and CLAUDE.md is explicit that it moves on closed,
scored outcomes and never on the model's own confidence - which is
exactly what conviction is. This closes the silence, not the loop.

Fully offline.
"""

from decimal import Decimal

import pytest

from catalyst.dashboard.panels import _conviction_by_direction

FLOOR = Decimal("0.60")


class Q:
    """The shape conviction_panel's query returns."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.error = None


def views(committed, declined):
    rows = [{"conviction": c, "direction": "long", "priced_in": 0,
             "skip_reasons": None} for c in committed]
    rows += [{"conviction": c, "direction": "no_trade", "priced_in": 1,
              "skip_reasons": '["model_no_trade"]'} for c in declined]
    return Q(rows)


#: The owner's real shape, rounded to a readable sample.
COMMITTED = [0.30, 0.35, 0.45, 0.54, 0.55, 0.56, 0.58, 0.60]
DECLINED = [0.55, 0.62, 0.65, 0.68, 0.70, 0.74, 0.80, 0.85]


class TestItShowsBothPopulations:
    def test_both_rows_are_present(self):
        html = _conviction_by_direction(views(COMMITTED, DECLINED), FLOOR, "conv")
        assert "gave a direction" in html
        assert "said no_trade" in html

    def test_the_medians_are_the_measured_ones(self):
        html = _conviction_by_direction(views(COMMITTED, DECLINED), FLOOR, "conv")
        assert "0.55" in html          # committed median (0.54/0.55 midpoint)
        assert "0.69" in html          # declined median (0.68/0.70 midpoint)

    def test_it_counts_how_many_reached_the_floor_in_each(self):
        html = _conviction_by_direction(views(COMMITTED, DECLINED), FLOOR, "conv")
        assert "1 (12%)" in html, "one of eight committed views reached 0.60"
        assert "7 (88%)" in html, "seven of eight declines did"


class TestItNamesTheFindingRatherThanLeavingItToBeSpotted:
    def test_it_says_the_declines_score_higher(self):
        html = _conviction_by_direction(views(COMMITTED, DECLINED), FLOOR, "conv")
        assert "scores its DECLINES" in html

    def test_it_says_that_is_calibration_not_breakage(self):
        """The owner's question was 'is the logic broken'. A table that
        shows the gap without saying what it means invites the wrong
        answer."""
        html = _conviction_by_direction(views(COMMITTED, DECLINED), FLOOR, "conv")
        assert "calibrated model does, not a broken one" in html

    def test_it_points_at_the_refusal_tracker_for_the_verdict(self):
        html = _conviction_by_direction(views(COMMITTED, DECLINED), FLOOR, "conv")
        assert "refusal tracker" in html
        assert "never on the model's own confidence" in html


class TestItStaysQuietWhenThereIsNoContrast:
    def test_no_declines_renders_nothing(self):
        assert _conviction_by_direction(views(COMMITTED, []), FLOOR, "conv") == ""

    def test_no_directional_calls_renders_nothing(self):
        assert _conviction_by_direction(views([], DECLINED), FLOOR, "conv") == ""

    def test_the_verdict_is_withheld_when_commits_score_higher(self):
        """The healthy direction. If the model's directional calls are
        the confident ones, there is nothing to explain away."""
        html = _conviction_by_direction(views(DECLINED, COMMITTED), FLOOR, "conv")
        assert "gave a direction" in html, "the table still renders"
        assert "scores its DECLINES" not in html

    def test_unreadable_convictions_are_skipped_not_crashed(self):
        q = views(COMMITTED, DECLINED)
        q.rows.append({"conviction": None, "direction": "long",
                       "priced_in": 0, "skip_reasons": None})
        q.rows.append({"conviction": "n/a", "direction": "no_trade",
                       "priced_in": 1, "skip_reasons": None})
        assert "gave a direction" in _conviction_by_direction(q, FLOOR, "conv")


class TestItDoesNotTouchTheFloor:
    """CLAUDE.md: adaptive parameters move on closed, scored outcomes and
    never on the model's own confidence. Conviction IS the model's own
    confidence, so this panel may describe it and must never act on it.
    """

    def test_the_panel_only_reads(self):
        import inspect

        # The CODE, not the docstring - which necessarily discusses
        # adaptation in order to say this panel must not do it.
        src = inspect.getsource(_conviction_by_direction)
        body = src.split('"""', 2)[-1]
        for forbidden in ("UPDATE ", "INSERT ", "conviction_floor =",
                          "set_param(", "adapt("):
            assert forbidden not in body, (
                f"{forbidden!r} appears in a panel that must only report")

    def test_the_floor_it_prints_is_the_one_it_was_given(self):
        html = _conviction_by_direction(views(COMMITTED, DECLINED),
                                        Decimal("0.72"), "conv")
        assert "0.72" in html, (
            "the panel invented a floor instead of showing the live one")
