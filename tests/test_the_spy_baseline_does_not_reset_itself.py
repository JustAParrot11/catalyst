"""The SPY comparison must not restart on its own. OWNER-REPORTED.

OWNER-REPORTED 2026-09-12: *"something has gone wrong, it has reset my
SPY track, it has randomly gone to tracking from 11/09 when it started
14/08, figure out why?"*

THREE SEPARATE CAUSES, all measured, all the same mistake: **absence of
evidence read as evidence that the account had changed.**

    1. An OWNER-SET baseline stores no fingerprint (the form cannot know
       the broker's account id), so "" != the live hash and it was
       overwritten ON THE VERY NEXT CYCLE - with a reason claiming the
       account had changed "fingerprint  -> 5a8297...", from a blank to a
       real one. `sync_with_account`'s own docstring has always promised
       the opposite: "AN OWNER-SET BASELINE IS NOT OVERWRITTEN."
    2. The fingerprint came from `id or account_number`, so a single read
       that omitted `id` hashed `account_number` instead, matched
       nothing, and looked like a different account.
    3. A row that existed but would not parse reported source "unset",
       which `sync_with_account` reads as "nothing has ever been stored"
       and answers by striking a new baseline at TODAY.

The rule now: **a restart needs positive evidence of a different
account.** A blank fingerprint, an unreadable row, and a payload missing
the canonical identifier are all inconclusive, and inconclusive changes
nothing.

WHAT MUST STILL WORK, because it is the owner's own instruction ("when I
change the Alpaca keys i want it to register there is a new account and
restart the SPY tracker"): a genuinely different account still restarts.
Every test that protects that is in test_benchmark_baseline.py and stays
green; this file adds the three that were missing.

Fully offline.
"""

import sqlite3
from datetime import date
from decimal import Decimal

import pytest

from catalyst import benchmark
from catalyst.benchmark import current, record, sync_with_account

AUG14 = date(2026, 8, 14)
SEP11 = date(2026, 9, 11)


@pytest.fixture
def db(tmp_path):
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "t.db"))
    yield conn
    conn.close()


class TestAnOwnerSetBaselineSurvives:
    """Cause 1, and the one that matches the owner's report exactly."""

    def test_a_healthy_cycle_does_not_overwrite_it(self, db):
        record(db, capital_cents=Decimal("200000"), start_date=AUG14,
               source="owner_set", account_fingerprint="",
               reason="owner set it: $2,000 in SPY on 14 August")
        after, changed = sync_with_account(
            db, {"id": "acct-real", "equity": "1950.00"}, SEP11)
        assert changed is False, (
            "the owner's baseline was replaced by a routine account read")
        assert after.start_date == AUG14
        assert after.capital_cents == Decimal("200000")
        assert after.source == "owner_set"

    def test_it_adopts_the_fingerprint_so_it_is_asked_only_once(self, db):
        """Otherwise every cycle forever would re-examine it, and the
        history would fill with identical rows."""
        record(db, capital_cents=Decimal("200000"), start_date=AUG14,
               source="owner_set", account_fingerprint="", reason="owner set")
        sync_with_account(db, {"id": "acct-real", "equity": "1950"}, SEP11)
        assert current(db).account_fingerprint == \
            benchmark.fingerprint_account("acct-real")
        rows_before = db.execute(
            "SELECT COUNT(*) FROM benchmark_baselines").fetchone()[0]
        for _ in range(3):
            sync_with_account(db, {"id": "acct-real", "equity": "1960"}, SEP11)
        assert db.execute(
            "SELECT COUNT(*) FROM benchmark_baselines").fetchone()[0] == \
            rows_before, "a settled baseline is still appending rows"

    def test_the_owners_dates_and_money_are_what_survive(self, db):
        """Adopting the fingerprint must not quietly re-strike the
        capital from the account's current equity - that would answer a
        different question from the one the owner asked."""
        record(db, capital_cents=Decimal("200000"), start_date=AUG14,
               source="owner_set", account_fingerprint="", reason="owner set")
        sync_with_account(db, {"id": "acct-real", "equity": "99999"}, SEP11)
        b = current(db)
        assert b.capital_cents == Decimal("200000")
        assert b.start_date == AUG14

    def test_the_reason_says_it_was_not_restarted(self, db):
        record(db, capital_cents=Decimal("200000"), start_date=AUG14,
               source="owner_set", account_fingerprint="", reason="owner set")
        sync_with_account(db, {"id": "acct-real", "equity": "1950"}, SEP11)
        assert "NOT restarted" in current(db).reason


class TestAPayloadMissingAFieldIsNotANewAccount:
    """Cause 2. The same real account, reported differently."""

    def test_a_read_that_omits_id_keeps_the_baseline(self, db):
        sync_with_account(db, {"id": "A1", "account_number": "N1",
                               "equity": "2000"}, AUG14)
        after, changed = sync_with_account(
            db, {"account_number": "N1", "equity": "1950"}, SEP11)
        assert changed is False, (
            "a read without `id` was treated as a different account")
        assert after.start_date == AUG14

    def test_a_read_that_omits_account_number_keeps_the_baseline(self, db):
        sync_with_account(db, {"id": "A1", "account_number": "N1",
                               "equity": "2000"}, AUG14)
        after, changed = sync_with_account(db, {"id": "A1", "equity": "1950"},
                                           SEP11)
        assert changed is False
        assert after.start_date == AUG14

    def test_an_old_row_fingerprinted_on_account_number_still_matches(self, db):
        """Backwards compatibility with rows the previous code wrote:
        when `id` was absent it hashed `account_number`, so such a row
        exists in the owner's database and must not be orphaned."""
        record(db, capital_cents=Decimal("200000"), start_date=AUG14,
               source="first_run",
               account_fingerprint=benchmark.fingerprint_account("N1"),
               reason="written by the older code from account_number")
        after, changed = sync_with_account(
            db, {"id": "A1", "account_number": "N1", "equity": "1950"}, SEP11)
        assert changed is False
        assert after.start_date == AUG14

    def test_a_payload_with_no_identifier_at_all_changes_nothing(self, db):
        sync_with_account(db, {"id": "A1", "equity": "2000"}, AUG14)
        after, changed = sync_with_account(db, {"equity": "1950"}, SEP11)
        assert changed is False
        assert after.start_date == AUG14


class TestAnUnreadableBaselineIsNeverReplaced:
    """Cause 3. There IS a baseline; we simply cannot read it."""

    def test_one_unparseable_row_does_not_restart_the_comparison(self, db):
        sync_with_account(db, {"id": "A1", "equity": "2000"}, AUG14)
        db.execute("UPDATE benchmark_baselines SET start_date = 'not-a-date'")
        db.commit()
        _after, changed = sync_with_account(
            db, {"id": "A1", "equity": "1950"}, SEP11)
        assert changed is False
        assert db.execute(
            "SELECT COUNT(*) FROM benchmark_baselines").fetchone()[0] == 1, (
            "a new baseline was written over a row that merely would not "
            "parse")

    def test_a_missing_table_does_not_write_a_baseline(self, tmp_path):
        bare = sqlite3.connect(str(tmp_path / "old.db"))
        try:
            _after, changed = sync_with_account(
                bare, {"id": "A1", "equity": "2000"}, SEP11)
            assert changed is False
        finally:
            bare.close()

    def test_unreadable_and_absent_are_different_facts(self, db):
        assert current(db).is_placeholder is True
        assert current(db).is_unreadable is False
        sync_with_account(db, {"id": "A1", "equity": "2000"}, AUG14)
        db.execute("UPDATE benchmark_baselines SET capital_cents = 'nope'")
        db.commit()
        assert current(db).is_unreadable is True
        assert current(db).is_placeholder is False, (
            "reporting an unreadable baseline as absent is what let a "
            "failed read overwrite a month of tracking")

    def test_the_scheduler_says_so_loudly(self, tmp_path, caplog):
        """Refusing to overwrite it must not become silence - the
        comparison cannot work until somebody looks."""
        import logging

        from catalyst.orchestrator import scheduler

        conn = sqlite3.connect(str(tmp_path / "u.db"))
        try:
            class Broker:
                @staticmethod
                def get_account():
                    return {"id": "A1", "equity": "2000"}

            with caplog.at_level(logging.INFO):
                assert scheduler._sync_benchmark_baseline(
                    conn, Broker()) is False
            assert any(r.levelno >= logging.ERROR for r in caplog.records)
            assert any("LEFT EXACTLY AS IT IS" in r.getMessage()
                       for r in caplog.records)
        finally:
            conn.close()


class TestAGenuinelyNewAccountStillRestarts:
    """The owner's own instruction, and the property none of the above
    may break: "when I change the Alpaca keys i want it to register there
    is a new account and restart the SPY tracker"."""

    def test_a_different_id_restarts_from_today(self, db):
        sync_with_account(db, {"id": "A1", "equity": "2000"}, AUG14)
        after, changed = sync_with_account(
            db, {"id": "COMPLETELY-DIFFERENT", "equity": "5000"}, SEP11)
        assert changed is True
        assert after.source == "account_changed"
        assert after.start_date == SEP11
        assert after.capital_cents == Decimal("500000")

    def test_the_old_baseline_stays_in_the_history(self, db):
        sync_with_account(db, {"id": "A1", "equity": "2000"}, AUG14)
        sync_with_account(db, {"id": "A2", "equity": "5000"}, SEP11)
        starts = [r[0] for r in db.execute(
            "SELECT start_date FROM benchmark_baselines ORDER BY rowid")]
        assert starts == [AUG14.isoformat(), SEP11.isoformat()], (
            "the previous baseline must survive so the owner can read - "
            "and restore - what it was")

    def test_a_first_baseline_is_still_struck_on_a_fresh_install(self, db):
        after, changed = sync_with_account(
            db, {"id": "A1", "equity": "2000"}, AUG14)
        assert changed is True
        assert after.source == "first_run"
        assert after.start_date == AUG14
