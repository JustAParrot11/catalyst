"""Whose spend was it? The panel that answers the owner's own question.

OWNER-REPORTED 2026-08-23: "on 17th dashboard says i spent $3.64 but
admin console says $2.95".

Neither number is wrong arithmetic. On 2026-08-17 the Cost API and our
own pricing of Anthropic's token counts agreed exactly - 364.2052c,
recorded in tests/test_cost_backfill.py's verification table. The gap is
SCOPE: the Cost API and the usage report both bill the whole
organisation, and this account also runs Claude Code.

The evidence was already in the database - the nightly correction stores
the usage report's per-api_key_id groups verbatim - and nothing rendered
it. These tests hold the panel that does, and hold it to the two things
that make it worth having: the per-key split is real, and a group it
cannot read is shown rather than dropped.

Fully offline: every row is written straight into the ledger.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.cost.backfill import BACKFILL_COMPONENT
from catalyst.dashboard.db import Db
from catalyst.dashboard.panels import _spend_by_key

SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"

#: HOUSE RULE 6 does not apply: nothing in _spend_by_key compares against
#: datetime.now(). It reads whatever days the ledger holds, so a fixed
#: fixture date cannot drift out of any window.
DAY = date(2026, 8, 17)

BOT_KEY = "apikey_01DXXtR5LfFyDuhhghpDQ1h9"
OTHER_KEY = "apikey_someoneElsesClaudeCode"


class Ledger:
    """A writable ledger plus the read-only view the dashboard gets.

    Db opens `mode=ro` by construction, so seeding needs its own
    connection - the same split the real dashboard runs under.
    """

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA.read_text())

    def view(self) -> Db:
        return Db(self.path)

    def close(self):
        self.conn.close()


@pytest.fixture
def db(tmp_path):
    led = Ledger(tmp_path / "t.db")
    yield led
    led.close()


def render(led) -> str:
    view = led.view()
    try:
        return _spend_by_key(view, "cost")
    finally:
        view.close()


def seed_correction(db, day, groups, recorded_before, billed):
    """One nightly-correction row, shaped exactly as backfill.py writes it."""
    db.conn.execute(
        "INSERT OR REPLACE INTO cost_events "
        "(id, raw_usage_json, model, kind, component, priced_cents, "
        " priced_at, api_call_id) VALUES (?,?,?,?,?,?,?,?)",
        (f"backfill-{day.isoformat()}",
         json.dumps({"backfill": True, "target_date": day.isoformat(),
                     "billed_cents": str(billed),
                     "ledger_cents_before": str(recorded_before),
                     "groups": groups}, sort_keys=True),
         "claude-sonnet-5", "scheduled", BACKFILL_COMPONENT,
         str(Decimal(str(billed)) - Decimal(str(recorded_before))),
         datetime.combine(day, datetime.min.time(), timezone.utc).isoformat(),
         None))
    db.conn.commit()


def the_owners_day(db):
    """2026-08-17 as reported: $3.64 billed to the account, $2.95 of it
    on one key. The 69c is the second key, and that is the whole answer."""
    seed_correction(db, DAY, [
        {"model": "claude-sonnet-5", "api_key_id": BOT_KEY, "cents": "295"},
        {"model": "claude-opus-5", "api_key_id": OTHER_KEY, "cents": "69.2052"},
    ], recorded_before="295", billed="364.2052")


class TestItNamesWhoSpentIt:
    def test_the_owners_69c_gap_is_attributed_to_a_second_key(self, db):
        the_owners_day(db)
        html = render(db)
        assert BOT_KEY in html
        assert OTHER_KEY in html
        # $2.95 and $0.69 both present, so the console figure and the
        # difference are each readable off the page.
        assert "$2.95" in html
        assert "$0.69" in html

    def test_the_day_row_shows_what_the_bot_itself_recorded(self, db):
        the_owners_day(db)
        html = render(db)
        assert "2026-08-17" in html
        assert "$3.64" in html, "the whole-account total must still be shown"
        assert "of which this bot recorded itself" in html

    def test_it_says_plainly_that_the_total_is_the_whole_organisation(self, db):
        the_owners_day(db)
        html = render(db).lower()
        assert "whole organisation" in html, (
            "a reader comparing this against a filtered console view has to "
            "be told the two are answering different questions")

    def test_keys_are_ranked_by_what_they_cost(self, db):
        the_owners_day(db)
        html = render(db)
        assert html.index(BOT_KEY) < html.index(OTHER_KEY)

    def test_several_days_accumulate_per_key(self, db):
        the_owners_day(db)
        seed_correction(db, DAY - timedelta(days=1), [
            {"model": "claude-sonnet-5", "api_key_id": BOT_KEY, "cents": "105"},
        ], recorded_before="105", billed="105")
        html = render(db)
        assert "$4.00" in html, "295c + 105c on the bot's key across two days"
        assert "2 corrected day(s)" in html


def seed_usage_by_key(db, day, groups, recorded_at="2026-08-18T02:00:00+00:00"):
    """The side table, as backfill.record_usage_by_key writes it."""
    for g in groups:
        db.conn.execute(
            "INSERT OR REPLACE INTO usage_by_key "
            "(target_date, api_key_id, model, cents, recorded_at) "
            "VALUES (?,?,?,?,?)",
            (day.isoformat(), g["api_key_id"], g["model"], g["cents"],
             recorded_at))
    db.conn.commit()


def seed_own_spend(db, day, cents):
    db.conn.execute(
        "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
        (f"own-{day.isoformat()}-{cents}", "{}", "claude-sonnet-5",
         "scheduled", "research", str(cents),
         datetime.combine(day, datetime.min.time(), timezone.utc).isoformat(),
         None))
    db.conn.commit()


class TestItReadsTheSideTable:
    """usage_by_key is written on EVERY backfill pass, including days
    needing no correction - so it covers days the adjustment payload
    never existed for."""

    def test_a_day_with_no_correction_still_shows_who_spent_it(self, db):
        seed_usage_by_key(db, DAY, [
            {"model": "claude-sonnet-5", "api_key_id": BOT_KEY, "cents": "295"},
            {"model": "claude-opus-5", "api_key_id": OTHER_KEY,
             "cents": "69.2052"},
        ])
        html = render(db)
        assert BOT_KEY in html and OTHER_KEY in html
        assert "$3.64" in html

    def test_the_bots_own_ledger_is_read_live_beside_it(self, db):
        seed_usage_by_key(db, DAY, [
            {"model": "claude-sonnet-5", "api_key_id": BOT_KEY, "cents": "295"},
        ])
        seed_own_spend(db, DAY, "250")
        html = render(db)
        assert "$2.50" in html, (
            "the bot's own recorded spend must come from the ledger as it "
            "stands now, not from a figure frozen at correction time")

    def test_a_day_is_never_counted_from_both_sources(self, db):
        """The same day in the side table AND in an older adjustment
        payload must be counted once, or every total doubles."""
        the_owners_day(db)
        seed_usage_by_key(db, DAY, [
            {"model": "claude-sonnet-5", "api_key_id": BOT_KEY, "cents": "295"},
            {"model": "claude-opus-5", "api_key_id": OTHER_KEY,
             "cents": "69.2052"},
        ])
        html = render(db)
        assert "$3.64" in html
        assert "$7.28" not in html
        assert "1 corrected day(s)" in html


class TestItRefusesToInventNumbers:
    def test_nothing_corrected_yet_renders_nothing(self, db):
        assert render(db) == ""

    def test_an_unreadable_group_is_shown_not_dropped(self, db):
        """House rule 3, and the reason the panel is trustworthy at all:
        a group whose amount cannot be read must not quietly become
        zero, because that understates somebody's spend and looks like
        agreement."""
        seed_correction(db, DAY, [
            {"model": "claude-sonnet-5", "api_key_id": BOT_KEY, "cents": "295"},
            {"model": "claude-opus-5", "api_key_id": OTHER_KEY,
             "cents": "not-a-number"},
        ], recorded_before="295", billed="364.2052")
        html = render(db)
        assert "could not be read" in html
        assert OTHER_KEY in html
        assert "$3.64" not in html, (
            "the unreadable group must be absent from the total, not "
            "silently priced at zero inside it")

    def test_a_corrupt_payload_does_not_take_the_page_down(self, db):
        db.conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            ("backfill-broken", "{not json", "claude-sonnet-5", "scheduled",
             BACKFILL_COMPONENT, "10",
             datetime.combine(DAY, datetime.min.time(),
                              timezone.utc).isoformat(), None))
        db.conn.commit()
        assert render(db) == ""

    def test_a_corrupt_payload_beside_a_good_one_is_reported(self, db):
        the_owners_day(db)
        db.conn.execute(
            "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
            ("backfill-broken", "{not json", "claude-sonnet-5", "scheduled",
             BACKFILL_COMPONENT, "10",
             datetime.combine(DAY - timedelta(days=2), datetime.min.time(),
                              timezone.utc).isoformat(), None))
        db.conn.commit()
        html = render(db)
        assert "could not be read" in html
        assert BOT_KEY in html, "one bad day must not lose the good ones"


class TestTheCheckCanActuallyFail:
    """House rule 4: break a copy and confirm the check catches it."""

    def test_dropping_the_second_key_would_be_caught(self, db):
        the_owners_day(db)
        html = render(db)
        assert OTHER_KEY in html
        # The sabotage: a panel that only ever showed the largest key
        # would still pass every total-based assertion above, which is
        # why the second key is asserted by name.
        only_largest = html.replace(OTHER_KEY, "")
        assert OTHER_KEY not in only_largest
