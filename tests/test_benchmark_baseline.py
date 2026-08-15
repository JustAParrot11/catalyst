"""catalyst/benchmark - the SPY comparison is struck against DATA, not a
hardcoded $1,000.

Before this module, `START_CAPITAL_CENTS = 100_000` in dashboard/db.py
drove every performance figure on the dashboard - net equity, the SPY
index, the annual-hurdle arithmetic - regardless of which Alpaca account
was actually connected. Point the bot at a $2,000 account and every one
of those numbers silently compares the new account against the old base.

This file holds the module to the six behaviours the owner asked for and
verified by hand:

    fresh install    -> source 'unset', is_placeholder True
    first account    -> source 'first_run', capital from the account's
                         own equity
    same account     -> changed=False (no churn on repeat reads)
    NEW account      -> source 'account_changed', new capital, new start
                         date
    owner-set        -> a routine account read does NOT overwrite it
    history          -> every previous baseline is kept, with its reason

...plus the attack surface: a payload with no usable id, unreadable
equity in its various shapes, a malformed stored row (must degrade to a
placeholder, never raise), and the fingerprint's stability, uniqueness,
and non-reversibility.

Sabotage log (house rule 4) is a class docstring at the bottom of this
file - each test above was proven capable of catching a specific broken
copy of catalyst/benchmark/__init__.py before being trusted.
"""

import sqlite3
from datetime import date, timezone, datetime
from decimal import Decimal

import pytest

from catalyst.benchmark import (
    FALLBACK_CAPITAL_CENTS, Baseline, current, fingerprint_account, record,
    sync_with_account,
)


def all_rows(conn):
    """Every stored baseline row, oldest first - the raw history, not
    just what current() surfaces."""
    return conn.execute(
        "SELECT capital_cents, start_date, source, account_fingerprint, "
        "reason, set_at FROM benchmark_baselines ORDER BY rowid ASC"
    ).fetchall()


# ---------------------------------------------------------------------
# The six owner-verified behaviours
# ---------------------------------------------------------------------

class TestFreshInstall:
    def test_a_database_that_has_never_seen_a_baseline_reports_unset(
            self, tmp_db):
        baseline = current(tmp_db)
        assert baseline.source == "unset"
        assert baseline.is_placeholder is True

    def test_the_placeholder_uses_the_documented_fallback_capital(
            self, tmp_db):
        baseline = current(tmp_db)
        assert baseline.capital_cents == FALLBACK_CAPITAL_CENTS

    def test_the_placeholder_carries_no_account_fingerprint(self, tmp_db):
        baseline = current(tmp_db)
        assert baseline.account_fingerprint == ""

    def test_fresh_install_writes_nothing(self, tmp_db):
        """Reading the placeholder must not itself create a row - it is
        a fact about absence, not a value to persist."""
        current(tmp_db)
        assert all_rows(tmp_db) == []


class TestFirstAccount:
    def test_first_sync_takes_its_source_from_first_run(self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 8, 15))
        assert changed is True
        assert baseline.source == "first_run"

    def test_first_sync_strikes_capital_from_the_accounts_own_equity(
            self, tmp_db):
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 8, 15))
        # equity is dollars; capital_cents is cents.
        assert baseline.capital_cents == Decimal("100000")

    def test_first_sync_starts_from_the_day_the_account_was_first_seen(
            self, tmp_db):
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 8, 15))
        assert baseline.start_date == date(2026, 8, 15)

    def test_first_sync_records_a_fingerprint_of_the_account_seen(
            self, tmp_db):
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 8, 15))
        assert baseline.account_fingerprint == fingerprint_account("acct-1")

    def test_a_two_thousand_dollar_account_is_struck_at_two_thousand(
            self, tmp_db):
        """Not the $1,000 fallback - this is exactly the owner's stated
        scenario (moving from a $1,000 to a $2,000 account)."""
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-2", "equity": "2000.00"},
            today=date(2026, 8, 15))
        assert baseline.capital_cents == Decimal("200000")
        assert baseline.capital_cents != FALLBACK_CAPITAL_CENTS


class TestSameAccountNoChurn:
    def test_a_repeat_read_of_the_same_account_reports_unchanged(
            self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 15))
        _, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 8, 16))
        assert changed is False

    def test_a_repeat_read_writes_no_second_row(self, tmp_db):
        """This is the churn the docstring warns about: 'called on
        every healthy cycle... cheap to call often' only holds if a
        same-account cycle does not append a fresh row each time."""
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 15))
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 16))
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 17))
        assert len(all_rows(tmp_db)) == 1

    def test_a_repeat_read_does_not_restrike_capital_even_if_equity_moved(
            self, tmp_db):
        """The baseline is struck once, at first sight of the account -
        a routine cycle reporting updated equity must not silently
        re-base the SPY comparison every day the account value moves."""
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 15))
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1234.56"},
            today=date(2026, 8, 16))
        assert changed is False
        assert baseline.capital_cents == Decimal("100000")


class TestNewAccountRestartsTheTracker:
    def test_a_different_account_id_is_reported_as_account_changed(
            self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 1))
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-2", "equity": "2000.00"},
            today=date(2026, 8, 15))
        assert changed is True
        assert baseline.source == "account_changed"

    def test_a_new_account_is_struck_at_its_own_equity_not_the_olds(
            self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 1))
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-2", "equity": "2000.00"},
            today=date(2026, 8, 15))
        assert baseline.capital_cents == Decimal("200000")

    def test_a_new_account_restarts_the_start_date_from_today(self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 1))
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-2", "equity": "2000.00"},
            today=date(2026, 8, 15))
        assert baseline.start_date == date(2026, 8, 15)
        assert baseline.start_date != date(2026, 8, 1)

    def test_a_new_account_carries_the_new_accounts_fingerprint(
            self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 8, 1))
        baseline, _ = sync_with_account(
            tmp_db, {"id": "acct-2", "equity": "2000.00"},
            today=date(2026, 8, 15))
        assert baseline.account_fingerprint == fingerprint_account("acct-2")
        assert baseline.account_fingerprint != fingerprint_account("acct-1")


class TestOwnerSetSurvivesRoutineReads:
    def test_a_routine_sync_of_the_same_account_does_not_touch_an_owner_set_baseline(
            self, tmp_db):
        """The owner said: track SPY as if $2,000 had gone in on 1 July.
        A background cycle reading the same connected account's real
        equity must not silently answer a different question."""
        _, _ = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 7, 1))
        fp = fingerprint_account("acct-1")
        record(tmp_db, capital_cents=Decimal("200000"),
              start_date=date(2026, 7, 1), source="owner_set",
              account_fingerprint=fp,
              reason="owner asked to emulate a $2,000 stake from 1 July")

        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1005.42"},
            today=date(2026, 8, 15))

        assert changed is False
        assert baseline.source == "owner_set"
        assert baseline.capital_cents == Decimal("200000")
        assert baseline.start_date == date(2026, 7, 1)

    def test_only_a_genuine_account_change_can_override_an_owner_set_baseline(
            self, tmp_db):
        """The other half of the same rule: it is not a lock forever,
        only immune to routine reads of the SAME account."""
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 7, 1))
        fp1 = fingerprint_account("acct-1")
        record(tmp_db, capital_cents=Decimal("200000"),
              start_date=date(2026, 7, 1), source="owner_set",
              account_fingerprint=fp1, reason="owner override")

        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-2", "equity": "3000.00"},
            today=date(2026, 8, 15))

        assert changed is True
        assert baseline.source == "account_changed"
        assert baseline.account_fingerprint == fingerprint_account("acct-2")


class TestHistoryIsKeptNotOverwritten:
    def test_every_previous_baseline_row_survives_a_new_one(self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 7, 1))
        sync_with_account(tmp_db, {"id": "acct-2", "equity": "2000.00"},
                          today=date(2026, 8, 15))
        rows = all_rows(tmp_db)
        assert len(rows) == 2
        sources = [r[2] for r in rows]
        assert sources == ["first_run", "account_changed"]

    def test_each_row_keeps_its_own_reason_text(self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 7, 1))
        sync_with_account(tmp_db, {"id": "acct-2", "equity": "2000.00"},
                          today=date(2026, 8, 15))
        reasons = [r[4] for r in all_rows(tmp_db)]
        assert "first broker account seen" in reasons[0]
        assert "account changed" in reasons[1]

    def test_current_always_reports_the_latest_row_not_the_first(
            self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "1000.00"},
                          today=date(2026, 7, 1))
        sync_with_account(tmp_db, {"id": "acct-2", "equity": "2000.00"},
                          today=date(2026, 8, 15))
        assert current(tmp_db).capital_cents == Decimal("200000")

    def test_history_survives_even_when_set_at_timestamps_collide(
            self, tmp_db):
        """set_at is a wall-clock timestamp; two inserts inside the same
        clock tick would tie on it. rowid must break the tie so
        current() still reports the row written second, not whichever
        SQLite happens to return first on a tie."""
        now = datetime.now(timezone.utc).isoformat()
        conn = tmp_db
        conn.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, "
            "start_date, source, account_fingerprint, reason, set_at) "
            "VALUES ('r1','100000','2026-07-01','first_run','fpA',"
            "'first', ?)", (now,))
        conn.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, "
            "start_date, source, account_fingerprint, reason, set_at) "
            "VALUES ('r2','200000','2026-08-15','account_changed','fpB',"
            "'second', ?)", (now,))
        conn.commit()
        baseline = current(conn)
        assert baseline.capital_cents == Decimal("200000")
        assert baseline.account_fingerprint == "fpB"


# ---------------------------------------------------------------------
# Attack surface named in the brief
# ---------------------------------------------------------------------

class TestAccountWithNoUsableId:
    def test_an_account_payload_with_neither_id_nor_account_number_is_ignored(
            self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"equity": "1000.00"}, today=date(2026, 8, 15))
        assert changed is False
        assert baseline.is_placeholder is True

    def test_an_account_payload_with_no_usable_id_writes_no_row(
            self, tmp_db):
        sync_with_account(tmp_db, {"equity": "1000.00"},
                          today=date(2026, 8, 15))
        assert all_rows(tmp_db) == []

    def test_an_empty_string_id_falls_back_to_account_number(self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"id": "", "account_number": "PA-9",
                     "equity": "1000.00"},
            today=date(2026, 8, 15))
        assert changed is True
        assert baseline.account_fingerprint == fingerprint_account("PA-9")

    def test_a_null_id_is_treated_the_same_as_a_missing_one(self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"id": None, "equity": "1000.00"},
            today=date(2026, 8, 15))
        assert changed is False
        assert baseline.is_placeholder is True


class TestUnreadableEquity:
    def test_a_missing_equity_key_is_declined_without_a_crash(
            self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1"}, today=date(2026, 8, 15))
        assert changed is False
        assert baseline.is_placeholder is True

    def test_a_non_numeric_equity_string_is_declined_without_a_crash(
            self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "not-a-number"},
            today=date(2026, 8, 15))
        assert changed is False
        assert baseline.is_placeholder is True

    def test_a_null_equity_is_declined_without_a_crash(self, tmp_db):
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": None},
            today=date(2026, 8, 15))
        assert changed is False
        assert baseline.is_placeholder is True

    def test_unreadable_equity_writes_no_row(self, tmp_db):
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "garbage"},
                          today=date(2026, 8, 15))
        sync_with_account(tmp_db, {"id": "acct-1"}, today=date(2026, 8, 16))
        assert all_rows(tmp_db) == []

    def test_a_later_readable_sync_still_succeeds_after_an_earlier_failure(
            self, tmp_db):
        """An unreadable equity must not poison the account fingerprint
        so a subsequent healthy read is silently treated as 'no
        change'."""
        sync_with_account(tmp_db, {"id": "acct-1", "equity": "garbage"},
                          today=date(2026, 8, 15))
        baseline, changed = sync_with_account(
            tmp_db, {"id": "acct-1", "equity": "1000.00"},
            today=date(2026, 8, 16))
        assert changed is True
        assert baseline.source == "first_run"


class TestMalformedStoredRowDegradesInsteadOfRaising:
    def test_unreadable_capital_cents_degrades_to_a_placeholder(
            self, tmp_db):
        tmp_db.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, "
            "start_date, source, account_fingerprint, reason, set_at) "
            "VALUES ('r1','not-a-number','2026-07-01','first_run','fpA',"
            "'ok', ?)", (datetime.now(timezone.utc).isoformat(),))
        tmp_db.commit()
        baseline = current(tmp_db)  # must not raise
        assert baseline.is_placeholder is True

    def test_unreadable_start_date_degrades_to_a_placeholder(self, tmp_db):
        tmp_db.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, "
            "start_date, source, account_fingerprint, reason, set_at) "
            "VALUES ('r1','100000','not-a-date','first_run','fpA',"
            "'ok', ?)", (datetime.now(timezone.utc).isoformat(),))
        tmp_db.commit()
        baseline = current(tmp_db)  # must not raise
        assert baseline.is_placeholder is True

    def test_the_degraded_placeholder_names_the_bad_row_in_its_reason(
            self, tmp_db):
        """'never raise' is not enough on its own - a dashboard that
        silently shows the $1,000 fallback with no explanation is as
        unusable as a crash. The reason must say the stored row could
        not be read."""
        tmp_db.execute(
            "INSERT INTO benchmark_baselines (id, capital_cents, "
            "start_date, source, account_fingerprint, reason, set_at) "
            "VALUES ('r1','not-a-number','2026-07-01','first_run','fpA',"
            "'ok', ?)", (datetime.now(timezone.utc).isoformat(),))
        tmp_db.commit()
        baseline = current(tmp_db)
        assert "could not be read" in baseline.reason
        assert "not-a-number" in baseline.reason

    def test_a_missing_benchmark_baselines_table_degrades_to_a_placeholder(
            self, tmp_path):
        """A database that predates this migration, or one opened
        against the wrong schema file, must not crash dashboard
        rendering - it must say the baseline is unset."""
        conn = sqlite3.connect(str(tmp_path / "no_table.db"))
        baseline = current(conn)  # must not raise
        assert baseline.source == "unset"
        assert baseline.is_placeholder is True
        conn.close()


class TestFingerprint:
    def test_the_same_account_id_always_produces_the_same_fingerprint(
            self):
        assert fingerprint_account("acct-1") == fingerprint_account(
            "acct-1")

    def test_different_account_ids_produce_different_fingerprints(self):
        assert fingerprint_account("acct-1") != fingerprint_account(
            "acct-2")

    def test_the_fingerprint_never_contains_the_raw_account_id(self):
        account_id = "PKLIVEFAKEKEYDONOTUSE9988"
        fp = fingerprint_account(account_id)
        assert account_id not in fp
        assert fp != account_id

    def test_the_fingerprint_is_not_the_account_id_lightly_encoded(self):
        """Guards against a 'fingerprint' that is really just the id in
        a different case, base64, or with characters stripped -
        anything short of an actual one-way hash."""
        import base64
        account_id = "acct-real-12345"
        fp = fingerprint_account(account_id)
        assert fp != account_id.lower()
        assert fp != account_id.upper()
        assert fp != base64.b64encode(account_id.encode()).decode()
        assert fp != account_id.replace("-", "")

    def test_the_fingerprint_matches_a_truncated_sha256_of_the_id(self):
        """Pins the documented construction (sha256, truncated to 16 hex
        chars) so a change to the algorithm - which would silently
        detach every stored fingerprint from the accounts it names - is
        caught rather than waved through."""
        import hashlib
        account_id = "acct-42"
        expected = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
        assert fingerprint_account(account_id) == expected

    def test_the_fingerprint_is_short_and_looks_like_a_hash_not_a_secret(
            self):
        import re
        fp = fingerprint_account("acct-1")
        assert re.fullmatch(r"[0-9a-f]{16}", fp)

    def test_blank_and_missing_ids_both_produce_no_fingerprint(self):
        assert fingerprint_account(None) == ""
        assert fingerprint_account("") == ""
        assert fingerprint_account("   ") == ""

    def test_a_non_string_id_is_still_hashed_consistently(self):
        """Alpaca account payloads are JSON; an id could arrive as an
        int from a hand-crafted test fixture or a future API change."""
        assert fingerprint_account(12345) == fingerprint_account("12345")

    def test_leading_and_trailing_whitespace_does_not_change_the_fingerprint(
            self):
        assert fingerprint_account(" acct-1 ") == fingerprint_account(
            "acct-1")


class TestRecordAndCurrentRoundTrip:
    def test_a_recorded_baseline_reads_back_with_the_same_values(
            self, tmp_db):
        baseline = record(
            tmp_db, capital_cents=Decimal("150000"),
            start_date=date(2026, 8, 10), source="owner_set",
            account_fingerprint="deadbeefcafef00d",
            reason="owner emulation")
        assert baseline.capital_cents == Decimal("150000")
        assert baseline.start_date == date(2026, 8, 10)
        assert baseline.source == "owner_set"
        assert baseline.account_fingerprint == "deadbeefcafef00d"
        assert baseline.reason == "owner emulation"
        assert baseline.is_placeholder is False

    def test_record_is_visible_to_a_fresh_current_call_not_just_its_own_return(
            self, tmp_db):
        record(tmp_db, capital_cents=Decimal("150000"),
              start_date=date(2026, 8, 10), source="owner_set",
              account_fingerprint="deadbeefcafef00d", reason="x")
        assert current(tmp_db).capital_cents == Decimal("150000")


@pytest.mark.sabotage
class TestSabotageNegativeControls:
    """House rule 4: every group above was PROVEN able to fail, live.

    Method for each of the 6 breaks below: `cp` the real
    catalyst/benchmark/__init__.py to the scratchpad, edit the live
    copy with the one change described, `pytest tests/test_benchmark_baseline.py`
    (full file, not just the targeted class - to also check for
    unexpected collateral passes/failures), record the exact failures,
    `cp` the scratchpad copy back, re-run the full file to confirm 46
    passed again and `diff` the restored file against the saved copy
    to confirm it is byte-identical. All six were run this way, in
    order, before this file was considered done.

    1. Removed the `if not fp: return current(conn), False` early exit
       in sync_with_account. A payload with no id/account_number
       (fingerprint "") then fell through and, for `{"equity": "..."}`
       with a readable equity, WROTE a baseline row with an empty
       account_fingerprint instead of being ignored.
       Caught: 3 failures in TestAccountWithNoUsableId - rows went
       from 0 to 1, and is_placeholder flipped True -> False.

    2. Changed `except (ValueError, ArithmeticError)` in current() to
       `except ValueError` only. A non-numeric stored capital_cents
       ("not-a-number" -> Decimal(...) -> decimal.InvalidOperation,
       which is an ArithmeticError, not a ValueError) then propagated
       out of current() as an unhandled exception instead of degrading
       to a placeholder.
       Caught: 2 failures in TestMalformedStoredRowDegradesInsteadOfRaising
       - current(tmp_db) raised decimal.InvalidOperation instead of
       returning.

    3. Changed the ORDER BY in current() from
       "set_at DESC, rowid DESC" to "set_at DESC" alone. Two rows
       inserted with an identical set_at (forced in the test to
       simulate a same-tick collision) then returned in SQLite's
       tie order rather than most-recently-written order.
       Caught: test_history_survives_even_when_set_at_timestamps_collide
       - asserted the second-written row's capital (200000) and
       fingerprint (fpB); the sabotaged query returned the first
       row's stale values (100000, fpA) instead.

    4. Inverted the owner-set guard in sync_with_account from
       `if now.account_fingerprint == fp and not now.is_placeholder:
       return now, False` to
       `if now.account_fingerprint != fp or now.is_placeholder:
       return now, False`. This is deliberately backwards: it made a
       routine SAME-account sync fall through and re-strike the
       baseline (destroying an owner_set row), while a genuinely
       DIFFERENT account was now treated as "no change".
       Caught: both tests in TestOwnerSetSurvivesRoutineReads failed
       (in opposite directions, matching the inversion), plus 12
       further failures across TestFirstAccount, TestSameAccountNoChurn,
       TestNewAccountRestartsTheTracker, TestHistoryIsKeptNotOverwritten,
       TestAccountWithNoUsableId and TestUnreadableEquity - this guard
       is load-bearing for nearly every scenario, not just owner-set.

    5. Changed `equity = Decimal(str(account["equity"])) * 100` to
       drop the `* 100`, so a $1,000 account (equity="1000.00") was
       struck as a 1000-cent ($10) baseline instead of 100000 cents.
       Caught: 5 failures across TestFirstAccount, TestSameAccountNoChurn,
       TestNewAccountRestartsTheTracker and TestHistoryIsKeptNotOverwritten
       - every assertion on the numeric value of capital_cents.

    6. Changed fingerprint_account's truncation from `[:16]` to `[:8]`.
       No single-account test noticed (a shorter deterministic hash is
       still stable and still distinct for "acct-1" vs "acct-2").
       Caught: test_the_fingerprint_matches_a_truncated_sha256_of_the_id
       (pins the literal expected digest, mismatched at 8 chars) and
       test_the_fingerprint_is_short_and_looks_like_a_hash_not_a_secret
       (regex requires exactly 16 hex characters, got 8).
    """
