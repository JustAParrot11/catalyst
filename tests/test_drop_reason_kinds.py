"""A working bot must not read as a broken one.

OWNER-REPORTED, twice. The second time with the diagnosis attached:
"Is this old or still relevant? If so its confusing me by being there."

What they were looking at was one flat list, sorted by count, in one
style, containing all of:

    74  research skipped: budget_denied
    63  research skipped: not_attempted: deferred_max_research_per_cycle
     5  research skipped: not_attempted: market_closed
     1  research skipped: transport_error: HTTP 400 ... tool_use ids ...

Three unrelated kinds of news:

  ROUTINE  the per-cycle research cap deferring a candidate, the market
           being closed. Nothing broke. These recur every day BY DESIGN.
  LIMIT    the budget governor refusing to spend. A bound the owner set,
           doing its job. A decision, not a failure.
  FAULT    an HTTP 400. Something actually broke.

Sorting by count buried the single real fault under "the market was
closed", and identical styling made routine operation look like damage.

THE DATING WAS ALSO A GUESS DRESSED AS A FINDING. Every row older than
a day carried "NOT since, so this may be history rather than a live
fault". The commit that fixed the tool_result 400 records precisely why
that is wrong:

    "The dashboard called it 'may be history rather than a live fault'
     because none had recurred - but the defect was still in the code,
     waiting for the next malformed early submission."

Absence of recurrence is not evidence of a fix. What the database can
actually prove is how much work has succeeded since, so that is what is
printed now - a number the reader draws their own conclusion from.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.storage import init_db

TODAY = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)

TOOL_400 = (
    "transport_error: AnthropicHTTPError: HTTP 400 from the Messages API: "
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"messages.2: `tool_use` ids were found without `tool_result` blocks"}}')

#: The owner's real funnel: (reason, count, first_days_ago, last_days_ago)
OWNER_ROWS = [
    ("budget_denied", 74, 4, 3),
    ("not_attempted: deferred_max_research_per_cycle", 63, 2, 0),
    ("not_attempted: market_closed", 5, 1, 0),
    ("transport_error: HTTPStatusError: Client error '400 Bad Request'",
     1, 6, 6),
    (TOOL_400, 5, 5, 5),
]


@pytest.fixture
def owners_funnel(tmp_path):
    """Their database, as reported, plus some successful research after."""
    path = str(tmp_path / "c.db")
    conn = init_db(path)
    i = 0
    for reason, n, first_ago, last_ago in OWNER_ROWS:
        for k in range(n):
            when = (TODAY - timedelta(days=last_ago if k == 0
                                      else first_ago)).isoformat()
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"c{i}", "AAA", "insider_cluster", "2026-09-01",
                          "estimated", "[]", when, "tech", "[]"))
            conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"r{i}", f"c{i}", "", "", "[]", "0", 0, reason, when))
            i += 1
    for k in range(9):
        when = (TODAY - timedelta(days=1)).isoformat()
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"ok{k}", "BBB", "insider_cluster", "2026-09-01",
                      "estimated", "[]", when, "tech", "[]"))
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"rok{k}", f"ok{k}", "m", "p", "[]", "5", 100, None, when))
    conn.commit()
    conn.close()
    return path


def _drops_block(path):
    db = Db(path)
    try:
        html = panels.funnel_panel(db, p="fun")
    finally:
        db.close()
    m = re.search(r'<div class="funnel-why" id="fun-drops-researched">.*?</div>',
                  html, re.S)
    assert m, "the researched-stage drop list did not render"
    return m.group(0)


class TestTheThreeKindsAreToldApart:
    @pytest.mark.parametrize("reason,kind", [
        ("not_attempted: deferred_max_research_per_cycle", "ROUTINE"),
        ("not_attempted: market_closed", "ROUTINE"),
        ("budget_denied", "LIMIT"),
        ("budget_denied: month cap reached", "LIMIT"),
        ("transport_error: HTTPStatusError: 400 Bad Request", "FAULT"),
        (TOOL_400, "FAULT"),
        ("something nobody has seen before", "FAULT"),
    ])
    def test_each_reason_gets_the_right_kind(self, reason, kind):
        assert queries.skip_kind(reason) == kind

    def test_an_unknown_reason_defaults_to_FAULT(self):
        """Fail loud, not quiet. A new failure mode miscategorised as
        routine is invisible; miscategorised as a fault it is merely
        noisy, and someone looks at it."""
        assert queries.skip_kind("kablooie") == "FAULT"
        assert queries.skip_kind("") == "FAULT"
        assert queries.skip_kind(None) == "FAULT"


class TestTheOwnersPageReadsCorrectly:
    def test_the_fault_is_listed_FIRST_not_buried_under_the_routine(
            self, owners_funnel):
        """Sorting by count put a real HTTP 400 below 'the market was
        closed'. Frequency is not importance."""
        block = _drops_block(owners_funnel)
        classes = re.findall(r'<li class="(drop-[a-z]+)"', block)
        assert classes[0] == "drop-live", (
            f"the list opens with {classes[0]}, not the fault: {classes}")
        first_routine = classes.index("drop-routine")
        last_fault = max(i for i, c in enumerate(classes) if c == "drop-live")
        assert last_fault < first_routine, (
            f"a fault is listed after routine attrition: {classes}")

    def test_routine_operation_is_not_dressed_as_damage(self, owners_funnel):
        block = _drops_block(owners_funnel)
        for reason in ("deferred_max_research_per_cycle", "market_closed"):
            i = block.find(reason)
            assert i > 0, f"{reason} vanished from the page"
            before = block[max(0, i - 400):i]
            assert "drop-routine" in before, (
                f"{reason} is not tagged routine - it reads as a fault")

    def test_a_bound_doing_its_job_says_so(self, owners_funnel):
        block = _drops_block(owners_funnel)
        i = block.find("budget_denied")
        assert i > 0
        assert "a limit you set" in block[max(0, i - 400):i], (
            "budget_denied is not identified as a limit the owner set")

    def test_the_page_explains_the_three_tags(self, owners_funnel):
        block = _drops_block(owners_funnel)
        for word in ("routine", "a limit you set", "fault"):
            assert word in block, f"the key does not explain {word!r}"


class TestTheDatingStopsGuessing:
    def test_the_weasel_wording_is_gone(self, owners_funnel):
        """"This MAY be history" told the owner nothing and was wrong at
        least once - the code was still broken while it said so."""
        block = _drops_block(owners_funnel)
        assert "may be history" not in block, (
            "the page still guesses at whether a fault is live")

    def test_a_fault_is_dated_against_WORK_DONE_not_the_calendar(
            self, owners_funnel):
        """9 successful research calls landed after the last 400. That
        is a measurement; 'five days ago' is not."""
        block = _drops_block(owners_funnel)
        assert "9 research call(s) have succeeded since" in block, (
            "the fault is not dated against successful work")
        assert "not proof it is fixed" in block, (
            "the page overclaims - absence of recurrence is not a fix")

    def test_a_fault_with_NO_success_since_is_called_live(self, tmp_path):
        """The other direction, and the one that matters. If nothing has
        worked since, it must not read as reassuring."""
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        when = (TODAY - timedelta(days=4)).isoformat()
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     ("c1", "AAA", "insider_cluster", "2026-09-01",
                      "estimated", "[]", when, "tech", "[]"))
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     ("r1", "c1", "", "", "[]", "0", 0,
                      "transport_error: HTTP 400", when))
        conn.commit()
        conn.close()
        block = _drops_block(path)
        assert "treat it as live" in block, (
            "a fault with nothing successful since is not flagged as live")
        assert "have succeeded since" not in block


class TestTheStylingIsNotColourAlone:
    def test_every_row_carries_a_TEXT_tag(self, owners_funnel):
        """Greyscale, colour blindness, a printed screenshot: the kind
        has to survive all of them."""
        block = _drops_block(owners_funnel)
        rows = re.findall(r"<li class=\"drop-[a-z]+\">.*?</li>", block, re.S)
        assert rows, "no drop rows rendered"
        for row in rows:
            assert 'class="drop-tag' in row, (
                f"a row is distinguished by colour alone: {row[:120]}")
