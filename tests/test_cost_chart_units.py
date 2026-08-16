"""The spend chart is read in dollars, because everything beside it is.

OWNER-ASKED: "the API costs is in cents can you enter i dollars on the
measuring graph".

WHY IT WAS EVER IN CENTS. The Anthropic Cost API reports cents
(TRAPS.md), and the ledger stores cents deliberately - integer cents
never lose a fraction the way a float dollar does. That is correct for
STORAGE. It was wrong for the axis: the cap, the budget, the account and
every other figure on that page are in dollars, so a chart whose y-axis
counted something else sat in the middle of them inviting a factor-of-100
misreading.

The stored value is untouched. Only the presentation divides by 100, and
the raw cents stay in the hover text so the number that was billed is
never lost.

FOUR DECIMALS, NOT TWO. A quiet day genuinely costs under a cent - the
project's own measured figure is $0.415/day across a whole day's work,
and a single reconciliation row can be a fraction of that. At two
decimals every bar would print "$0.00" and the chart would look like a
broken feed, which is the exact failure this project has a house rule
about.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import panels
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc)


def _seed(tmp_path, cents_per_day):
    path = str(tmp_path / "c.db")
    conn = init_db(path)
    for i, cents in enumerate(cents_per_day):
        day = (NOW.date() - timedelta(days=i + 1)).isoformat()
        conn.execute(
            "INSERT INTO cost_reconciliation_events (id,target_date,kind,"
            "component,local_total_cents,cost_api_total_cents,"
            "discrepancy_cents,threshold_cents,api_raw_response,"
            "api_record_count,action_taken,reconciled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"r{i}", day, "scheduled", "anthropic", str(cents), str(cents),
             "0", "50", "{}", 1, "none", NOW.isoformat()))
    conn.commit()
    conn.close()
    return path


def _chart(tmp_path, cents_per_day):
    db = Db(_seed(tmp_path, cents_per_day))
    try:
        html = panels.cost_panel(db, p="cost")
    finally:
        db.close()
    m = re.search(r'<svg id="cost-daily-chart".*?</svg>', html, re.S)
    assert m, "the daily spend chart did not render at all"
    return m.group(0)


def _ticks(svg):
    return re.findall(r">(\$?[\d,.]+)<", svg)


class TestTheAxisIsInDollars:
    def test_every_tick_carries_a_dollar_sign(self, tmp_path):
        svg = _chart(tmp_path, [41.5, 38.2, 55.0])
        ticks = [t for t in _ticks(svg) if any(ch.isdigit() for ch in t)]
        assert ticks, "the axis has no numeric ticks"
        assert all(t.startswith("$") for t in ticks), (
            f"ticks {ticks} are not all in dollars")

    def test_the_title_no_longer_says_cents(self, tmp_path):
        svg = _chart(tmp_path, [41.5, 38.2])
        title = re.search(r">([^<]*Billed spend[^<]*)<", svg)
        assert title, "the chart lost its title"
        assert "cents" not in title.group(1).lower(), (
            f"the title still says cents: {title.group(1)!r}")

    def test_the_scale_really_divided_by_a_hundred(self, tmp_path):
        """The test that catches a relabelled axis. 55 cents must plot
        as $0.55, not as $55 with a dollar sign painted on."""
        svg = _chart(tmp_path, [55.0])
        values = [float(t.lstrip("$").replace(",", ""))
                  for t in _ticks(svg)
                  if t.startswith("$")]
        assert values, "no dollar ticks to read"
        top = max(values)
        assert top < 1.0, (
            f"the axis tops out at ${top} for a 55-cent day - the label "
            "changed but the scale did not")
        assert top > 0.5, f"axis top ${top} is below the largest bar"

    def test_a_sub_penny_day_is_not_rendered_as_zero(self, tmp_path):
        """The reason for four decimals. A day costing 0.6 cents must
        not print as $0.00 on every tick - that reads as a dead feed."""
        svg = _chart(tmp_path, [0.6])
        values = [float(t.lstrip("$").replace(",", ""))
                  for t in _ticks(svg) if t.startswith("$")]
        assert any(v > 0 for v in values), (
            "every axis tick rounded to zero for a sub-penny day")

    def test_the_cap_line_is_converted_too(self, tmp_path):
        """A reference line left in cents beside a dollar axis would sit
        off the top of the chart forever and read as 'never near the
        cap' - the most dangerous possible way to be wrong here."""
        svg = _chart(tmp_path, [41.5, 38.2, 55.0])
        values = [float(t.lstrip("$").replace(",", ""))
                  for t in _ticks(svg) if t.startswith("$")]
        assert max(values) < 5.0, (
            f"axis tops out at ${max(values)} - the pro-rata cap line was "
            "probably left in cents, which drags the scale up and makes "
            "every real bar invisible")


class TestTheBilledCentsAreStillThere:
    def test_the_hover_keeps_the_raw_cents(self, tmp_path):
        """House rule 3 in spirit: the figure that was actually billed
        must stay beside the one that is drawn."""
        svg = _chart(tmp_path, [41.5])
        tips = re.findall(r"<title>([^<]+)</title>", svg)
        assert tips, "the bars carry no hover text"
        joined = " ".join(tips)
        assert "41.5" in joined and "cents" in joined, (
            f"the billed cents are gone from the tooltip: {tips}")
        assert "$0.41" in joined, (
            f"the tooltip does not also give the dollar figure: {tips}")


class TestThisCheckCanFail:
    """House rule 4 - break a copy and confirm it catches it."""

    def test_a_cents_axis_would_be_caught(self, tmp_path, monkeypatch):
        """Put the defect back: plot cents under a dollar label."""
        import catalyst.dashboard.charts as charts

        real = charts.bar_chart

        def cents_again(bars, **kw):
            return real([(a, v * 100.0, t) for a, v, t in bars], **kw)

        monkeypatch.setattr(panels.charts, "bar_chart", cents_again)
        svg = _chart(tmp_path, [55.0])
        values = [float(t.lstrip("$").replace(",", ""))
                  for t in _ticks(svg) if t.startswith("$")]
        assert max(values) > 1.0, (
            "the sabotage did not take, so the test below proves nothing")
        with pytest.raises(AssertionError):
            assert max(values) < 1.0
