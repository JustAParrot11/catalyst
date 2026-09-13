"""A closed position has nothing left to protect.

OWNER-REPORTED 2026-09-13, on the Overview, about a position sold a
fortnight earlier:

    OPERATIONAL ALERTS AND ADAPTATION
    position 85fb5edc-a8f5-4bd8-a6a1-0b91c4953b4c is unprotected
    (checked 2026-08-31T13:27:33.148717+00:00)
    []

THE SAME POSITION ID WAS REPORTED ON 2026-08-17. That fix - "only the
latest check per position counts" - is right and was not enough:

* it rescues a gap that was RESOLVED on an OPEN position, because a
  later check overwrites the verdict;
* it can never rescue a CLOSED one. `confirm_stops_resting` runs over
  `_open_position_dicts`, whose SQL ends `WHERE p.status = 'open'`, so
  the moment a position closes **no further check is ever written** and
  its last verdict is frozen for good.

And that last verdict is very often `unprotected` for an entirely
innocent reason: it is taken while the position is being sold, after the
resting stop has been cancelled to make way for the market sell. So the
final check on a **correct, normal exit** alarms forever.

The position's own status is the fact that settles it and was one join
away - the same shape as every other report this week: a historical fact
rendered as a live one.

WHAT MUST NOT CHANGE. This alarm is a real safety signal and silencing
it wrongly is the dangerous direction, so the tests below pin the cases
that must STILL fire: an open position with no stop, an open position
with two stops, and a confirmation whose position row does not exist at
all. Unknown is not closed.
"""
import sqlite3

import pytest

from catalyst.dashboard.db import Db

POSITION_ID = "85fb5edc-a8f5-4bd8-a6a1-0b91c4953b4c"
CHECKED = "2026-08-31T13:27:33.148717+00:00"


@pytest.fixture
def db(tmp_path):
    """init_db, so PRAGMA foreign_keys matches production - a fixture
    that differs from production agrees with bugs (section 14)."""
    from catalyst.storage import init_db

    path = str(tmp_path / "t.db")
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()
    return path


def _seed(path, *, position_status="closed", confirmation="unprotected",
          ticker="EMBC", with_position=True, checked=CHECKED,
          position_id=POSITION_ID):
    conn = sqlite3.connect(path)
    try:
        if with_position:
            conn.execute(
                "INSERT INTO positions (id, ticker, entry_order_ids, "
                "stop_order_id, opened_at, planned_exit_date, status) "
                "VALUES (?,?,?,?,?,?,?)",
                (position_id, ticker, "[]", None,
                 "2026-08-20T14:00:00+00:00", "2026-08-31", position_status))
        conn.execute("INSERT INTO stop_confirmations VALUES (?,?,?,?)",
                     (position_id, checked, "[]", confirmation))
        conn.commit()
    finally:
        conn.close()


def _alarms(path):
    from catalyst.dashboard import queries

    return [text for kind, text, _raw in queries.alerts(Db(path)).items
            if kind == "alarm"]


class TestTheOwnersAlert:

    def test_a_closed_position_does_not_alarm(self, db):
        """THE BUG, in the owner's own shape."""
        _seed(db)
        said = _alarms(db)
        assert said == [], (
            "a position sold a fortnight ago is still reported as "
            "unprotected, which is what the owner saw twice:\n"
            + "\n".join(said))

    def test_the_confirmation_row_is_NOT_deleted(self, db):
        """It is real, it happened, and the trade timeline shows it. What
        changes is that it stops being an ALARM."""
        _seed(db)
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT status FROM stop_confirmations").fetchall()
        finally:
            conn.close()
        assert rows == [("unprotected",)], (
            "the history was rewritten instead of the alarm being scoped")


class TestWhatMustStillAlarm:
    """Silencing this wrongly is the dangerous direction."""

    def test_an_open_position_with_no_stop_still_alarms(self, db):
        _seed(db, position_status="open")
        said = _alarms(db)
        assert said, "an OPEN position with no stop went silent"
        assert "EMBC" in said[0]
        assert "NO protective stop" in said[0]

    def test_an_open_position_with_two_stops_still_alarms(self, db):
        """A different fault needing the opposite response: not an
        unbounded downside, a possible double sale."""
        _seed(db, position_status="open", confirmation="duplicate_stops")
        said = _alarms(db)
        assert said, "duplicate stops went silent"
        assert "MORE THAN ONE" in said[0], said[0]
        assert "sold twice" in said[0], said[0]

    def test_a_confirmation_with_no_position_row_still_alarms(self, db):
        """UNKNOWN IS NOT CLOSED. No row means the record is wrong, which
        is worse than an unprotected position rather than better."""
        _seed(db, with_position=False)
        said = _alarms(db)
        assert said, (
            "a confirmation whose position does not exist went silent - "
            "absence of evidence read as evidence the stop is fine")
        assert "record itself is wrong" in said[0], said[0]

    def test_a_still_open_position_alarms_even_beside_a_closed_one(self, db):
        """The filter must scope by row, not switch the panel off."""
        _seed(db, position_status="closed")
        _seed(db, position_status="open", ticker="NVDA",
              position_id="open-position-2", checked="2026-09-13T10:00:00+00:00")
        said = _alarms(db)
        assert len(said) == 1, said
        assert "NVDA" in said[0], said[0]


class TestItNamesTheStockNotTheRowId:
    """A uuid is a machine reference and tells the reader nothing - the
    same defect section 23 fixed on the decision card, in another panel."""

    def test_the_ticker_is_in_the_sentence(self, db):
        _seed(db, position_status="open", ticker="NVDA")
        assert "NVDA" in _alarms(db)[0]

    def test_the_uuid_is_not_in_the_sentence(self, db):
        _seed(db, position_status="open")
        assert POSITION_ID not in _alarms(db)[0], (
            "the alert still leads with a uuid the owner cannot read")

    def test_a_missing_ticker_falls_back_to_the_id_rather_than_blank(self, db):
        """Naming nothing at all would be worse than naming the id."""
        _seed(db, with_position=False)
        assert POSITION_ID in _alarms(db)[0]


class TestTheAlarmSaysWhatItMeans:
    """"is unprotected" plus a bare [] does not tell the reader what is
    at stake or what to do about it."""

    def test_it_says_what_being_unprotected_costs(self, db):
        _seed(db, position_status="open")
        assert "downside is unbounded" in _alarms(db)[0]

    def test_an_unknown_status_cannot_be_stored_at_all(self, db):
        """WHY THE THIRD BRANCH IS UNREACHABLE, recorded rather than
        faked.

        The sentence map has a fallback for a status it does not know, on
        house rule 7 - but the schema's own CHECK constraint refuses any
        value outside the three, and it holds even under
        `PRAGMA writable_schema = ON`. So that branch is defence against
        arithmetic, not against an input that can occur.

        This test is the pair to it: if somebody relaxes the constraint
        later the branch becomes reachable, and this goes red to say so
        rather than the fallback quietly becoming load-bearing untested.
        """
        conn = sqlite3.connect(db)
        try:
            with pytest.raises(sqlite3.IntegrityError) as caught:
                conn.execute("INSERT INTO stop_confirmations VALUES (?,?,?,?)",
                             (POSITION_ID, CHECKED, "[]", "something_new"))
            assert "CHECK constraint failed" in str(caught.value)
        finally:
            conn.close()
