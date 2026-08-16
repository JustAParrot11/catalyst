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


def _page(path):
    db = Db(path)
    try:
        return panels.funnel_panel(db, p="fun")
    finally:
        db.close()


def _drops_block(path_or_html):
    """The CURRENT list only - the drop section up to the end of its
    first <ul>. Anything after that is the collapsed legacy disclosure,
    which is a different question and has its own helper."""
    html = path_or_html if str(path_or_html).lstrip().startswith("<") \
        else _page(path_or_html)
    m = re.search(
        r'<div class="funnel-why" id="fun-drops-researched">.*?</ul>',
        html, re.S)
    assert m, "the researched-stage drop list did not render"
    return m.group(0)


def _older_block(path_or_html):
    html = path_or_html if str(path_or_html).lstrip().startswith("<") \
        else _page(path_or_html)
    m = re.search(r'<details id="fun-drops-old-researched">.*?</details>',
                  html, re.S)
    return m.group(0) if m else ""


@pytest.fixture
def funnel_with_a_live_fault(tmp_path):
    """The owner's rows, plus an HTTP 400 that happened TODAY - so the
    ordering of a current fault can still be tested now that settled
    ones are filed away."""
    path = str(tmp_path / "live.db")
    conn = init_db(path)
    rows = list(OWNER_ROWS) + [("transport_error: HTTP 400 right now", 2, 0, 0)]
    i = 0
    for reason, n, first_ago, last_ago in rows:
        for k in range(n):
            when = (TODAY - timedelta(days=last_ago if k == 0
                                      else first_ago)).isoformat()
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"c{i}", "AAA", "insider_cluster", "2026-09-01",
                          "estimated", "[]", when, "tech", "[]"))
            conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"r{i}", f"c{i}", "", "", "[]", "0", 0, reason, when))
            i += 1
    conn.commit()
    conn.close()
    return path


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
            self, funnel_with_a_live_fault):
        """Sorting by count put a real HTTP 400 below 'the market was
        closed'. Frequency is not importance.

        Uses a fault seen TODAY: the owner's own 400s are settled and now
        live behind the disclosure, so they cannot test this ordering."""
        block = _drops_block(funnel_with_a_live_fault)
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
        is a measurement; 'five days ago' is not.

        Read from the WHOLE page: these particular faults are settled, so
        they now sit behind the disclosure - but the sentence that dates
        them has to be there either way, or the record is useless when
        someone opens it to write a bug report."""
        block = _older_block(owners_funnel)
        assert block, "the settled faults have no disclosure to date them in"
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
    def test_every_row_carries_a_TEXT_tag(self, funnel_with_a_live_fault):
        """Greyscale, colour blindness, a printed screenshot: the kind
        has to survive all of them."""
        block = _drops_block(funnel_with_a_live_fault)
        rows = re.findall(r"<li class=\"drop-[a-z]+\">.*?</li>", block, re.S)
        assert rows, "no drop rows rendered"
        for row in rows:
            assert 'class="drop-tag' in row, (
                f"a row is distinguished by colour alone: {row[:120]}")


# ------------------------------------------------- legacy out of the way
#
# OWNER-REPORTED: "If these errors are legacy why are they still visible
# taking space? I want them if relevant not legacy."
#
# Deleting them is not the answer - this project's own rule is that "a
# fault that vanishes silently is indistinguishable from one that never
# happened", and the owner still has to be able to find it when writing
# a bug report. So a settled reason outside the fault window collapses
# behind a disclosure: the page shows what is current, the record stays
# one click away.
#
# SETTLED means the bot has demonstrably worked past it - successful
# research calls landed after the last occurrence. An old fault with
# NOTHING successful after it is not settled, it is untested, and it
# stays on the page.

class TestLegacyReasonsLeaveThePage:
    def test_the_old_400s_are_no_longer_in_the_main_list(self, owners_funnel):
        block = _drops_block(owners_funnel)
        assert "400" not in block, (
            "a fault last seen 5-6 days ago, with successful research "
            "since, is still taking space in the current list")

    def test_what_is_still_happening_STAYS_on_the_page(self, owners_funnel):
        """The other half. Collapsing everything would be just as
        useless as collapsing nothing."""
        block = _drops_block(owners_funnel)
        for reason in ("deferred_max_research_per_cycle", "market_closed",
                       "budget_denied"):
            assert reason in block, f"{reason} was hidden but is current"

    def test_they_are_COLLAPSED_not_deleted(self, owners_funnel):
        older = _older_block(owners_funnel)
        assert older, "no disclosure for the older reasons - were they deleted?"
        assert "400" in older, "the old faults are gone entirely"
        assert "kept for the record" in older

    def test_the_summary_says_how_many_and_why(self, owners_funnel):
        m = re.search(r"<summary>(.*?)</summary>", _older_block(owners_funnel),
                      re.S)
        assert m, "the disclosure has no summary to click"
        text = re.sub(r"<[^>]+>", "", m.group(1))
        assert "2 older reason" in text, text
        assert "settled" in text and "kept for the record" in text, text

    def test_an_UNTESTED_fault_is_not_filed_away(self, tmp_path):
        """The dangerous case. Old, and nothing has succeeded since - so
        nothing has proved it gone. That must stay in front of the
        owner, however old it is."""
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        when = (TODAY - timedelta(days=20)).isoformat()
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     ("c1", "AAA", "insider_cluster", "2026-09-01",
                      "estimated", "[]", when, "tech", "[]"))
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     ("r1", "c1", "", "", "[]", "0", 0,
                      "transport_error: HTTP 400", when))
        conn.commit()
        conn.close()
        block = _drops_block(path)
        assert "HTTP 400" in block, (
            "a 20-day-old fault with NOTHING successful since was filed "
            "away as settled - it is untested, not resolved")
        assert "treat it as live" in block


class TestEverythingSettledIsNotEverythingGone:
    """FOUND BY A TEST FAILING, not by reading: the disclosure was
    nested inside `if stage.drops:`, so the moment EVERY reason settled
    the whole record vanished - the exact "a fault that disappears
    silently" failure this feature exists to prevent, reintroduced by
    the feature itself."""

    @pytest.fixture
    def all_settled(self, tmp_path):
        path = str(tmp_path / "quiet.db")
        conn = init_db(path)
        old = (TODAY - timedelta(days=9)).isoformat()
        for i in range(3):
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"c{i}", "AAA", "insider_cluster", "2026-09-01",
                          "estimated", "[]", old, "tech", "[]"))
            conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"r{i}", f"c{i}", "", "", "[]", "0", 0,
                          "not_attempted: market_closed", old))
        recent = (TODAY - timedelta(days=1)).isoformat()
        for k in range(4):
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"ok{k}", "BBB", "insider_cluster", "2026-09-01",
                          "estimated", "[]", recent, "tech", "[]"))
            conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                         (f"rok{k}", f"ok{k}", "m", "p", "[]", "5", 100,
                          None, recent))
        conn.commit()
        conn.close()
        return path

    def test_the_record_survives_when_nothing_is_current(self, all_settled):
        older = _older_block(all_settled)
        assert older, (
            "every reason settled and the entire record disappeared")
        assert "market_closed" in older

    def test_and_the_page_SAYS_nothing_is_current(self, all_settled):
        """A silent gap where a list used to be reads as a broken
        query, which is the diagnosis this project keeps having to make."""
        block = _drops_block(all_settled)
        assert "Nothing is currently stopping candidates here" in block
