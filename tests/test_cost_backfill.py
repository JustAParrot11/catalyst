"""Correcting history so it reflects what was actually paid.

OWNER-REPORTED 2026-08-20: "it doesnt accurately reflect my costings, i
need it updating historically so it looks correct".

The gap was real. On 2026-08-15 Anthropic billed 45.7446c against a
single API key; the local ledger recorded $0.00. Every historical figure
on the dashboard was short by that day, and so was the governor's idea
of what the month had cost.

WHY RECONSTRUCTION IS LEGITIMATE HERE. price() reproduces Anthropic's
own charges to the cent when given their own token counts - verified
live against five separate days, including one with three API keys:

    2026-08-04   525.64452c vs   525.6445c   (rounding only)
    2026-08-11    670.294c  vs   670.2940c
    2026-08-15    45.7446c  vs    45.7446c
    2026-08-17   364.2052c  vs   364.2052c
    2026-08-18   306.1296c  vs   306.1296c

So the Usage API's counts, priced by our own code, ARE the bill. A
missing day can be rebuilt rather than guessed at.

The fixtures below are those real payloads. Everything is offline.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.cost.backfill import (
    BACKFILL_COMPONENT, BackfillError, backfill_day, backfill_range,
    local_real_total, price_usage_day,
)

SCHEMA = Path(__file__).resolve().parents[1] / "catalyst" / "storage" / "schema.sql"
#: PINNED, deliberately - the opposite of house rule 6, for the opposite
#: reason. Nothing here measures against datetime.now(): every call below
#: injects now=NOW, and backfill.py only defaults when it is omitted. So
#: there is no live clock to drift against, and pinning is safe.
#:
#: It is also REQUIRED. REAL_BILLED is what Anthropic actually charged
#: for 2026-08-15, and a bill is inseparable from the rates in force on
#: its day. DAY used to slide with the real clock, so once it drifted
#: past 2026-08-31 the same verbatim usage repriced at Sonnet 5's
#: standard rates - 64.1169c against a recorded 45.7446c - and eight
#: tests failed on pricing the code had exactly right. Found by moving
#: the system clock forward; they were green today and would have gone
#: red in mid-September, blocking the upgrade.
#:
#: DAY is now the real day the sample came from, which is what it should
#: always have been: a fixture of real observed data has a real date.
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
DAY = (NOW - timedelta(days=3)).date()          # 2026-08-15

#: The real 2026-08-15 usage group, verbatim from the Usage API.
REAL_GROUP = {
    "uncached_input_tokens": 169734,
    "output_tokens": 2328,
    "cache_creation": {"ephemeral_1h_input_tokens": 0,
                       "ephemeral_5m_input_tokens": 1620},
    "cache_read_input_tokens": 3240,
    "server_tool_use": {"web_search_requests": 9},
    "api_key_id": "apikey_01DXXtR5LfFyDuhhghpDQ1h9",
    "model": "claude-sonnet-5",
}
REAL_BILLED = Decimal("45.7446")     # cost_report agreed exactly


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(SCHEMA.read_text())
    yield conn
    conn.close()


def seed_local(conn, day, cents, component="research"):
    conn.execute(
        "INSERT INTO cost_events VALUES (?,?,?,?,?,?,?,?)",
        (f"e-{component}-{cents}", "{}", "claude-sonnet-5", "scheduled",
         component, str(cents),
         datetime.combine(day, datetime.min.time(),
                          timezone.utc).isoformat(), None))
    conn.commit()


def ledger_total(conn, day):
    rows = conn.execute(
        "SELECT priced_cents FROM cost_events WHERE date(priced_at) = ?",
        (day.isoformat(),)).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


def fetch(groups):
    return lambda d: list(groups)


class TestItRebuildsTheBillFromAnthropicsOwnCounts:
    def test_the_real_day_prices_to_the_real_bill(self):
        """The claim the whole module rests on."""
        total, items = price_usage_day([REAL_GROUP], date(2026, 8, 15))
        assert total == REAL_BILLED
        assert items[0][1] == "apikey_01DXXtR5LfFyDuhhghpDQ1h9"

    def test_a_missing_day_is_corrected_to_the_penny(self, db):
        """THE OWNER'S CASE: billed 45.7446c, ledger empty."""
        r = backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert r.applied
        assert r.local_before_cents == Decimal("0")
        assert r.billed_cents == REAL_BILLED
        assert ledger_total(db, DAY) == REAL_BILLED

    def test_a_partly_recorded_day_is_topped_up_not_replaced(self, db):
        seed_local(db, DAY, "20")
        r = backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert r.adjustment_cents == REAL_BILLED - Decimal("20")
        assert ledger_total(db, DAY) == REAL_BILLED
        # the original row is untouched
        assert db.execute(
            "SELECT priced_cents FROM cost_events WHERE component='research'"
        ).fetchone()[0] == "20"

    def test_an_over_recorded_day_is_corrected_downwards_too(self, db):
        """Direction matters both ways: a ledger claiming more than was
        billed makes the governor refuse work it could afford."""
        seed_local(db, DAY, "100")
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert ledger_total(db, DAY) == REAL_BILLED

    def test_a_day_that_already_agrees_gets_no_adjustment(self, db):
        seed_local(db, DAY, str(REAL_BILLED))
        r = backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert not r.applied
        assert db.execute(
            "SELECT COUNT(*) FROM cost_events WHERE component = ?",
            (BACKFILL_COMPONENT,)).fetchone()[0] == 0

    def test_several_api_keys_on_one_day_all_count(self, db):
        """2026-08-04 really did have three. Dropping the ones that are
        not the bot's would understate the bill actually paid."""
        # A near-copy under a different key: same tokens, 1000 output
        # instead of 2328, so it costs REAL_BILLED - 2.328c + 1.0c.
        second = dict(REAL_GROUP, api_key_id="apikey_other",
                      output_tokens=1000)
        r = backfill_day(db, DAY, fetch=fetch([REAL_GROUP, second]), now=NOW)
        assert len(r.groups) == 2
        expected = REAL_BILLED + REAL_BILLED - Decimal("2.328") + Decimal("1")
        assert r.billed_cents == expected
        assert {k for _, k, _ in r.groups} == {
            "apikey_01DXXtR5LfFyDuhhghpDQ1h9", "apikey_other"}


class TestItKeepsWhoseSpendItWas:
    """OWNER-REPORTED 2026-08-23: "on 17th dashboard says i spent $3.64
    but admin console says $2.95".

    Both were right. The Cost API bills the whole ORGANISATION and
    cannot be filtered to one key; a console view can be. The usage
    report is already fetched grouped by api_key_id, so the split was
    being priced, summed and then discarded. These hold that it is kept.
    """

    def test_the_per_key_split_is_kept(self, db):
        second = dict(REAL_GROUP, api_key_id="apikey_other",
                      output_tokens=1000)
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP, second]), now=NOW)
        rows = db.execute(
            "SELECT api_key_id, model, cents FROM usage_by_key "
            "WHERE target_date = ? ORDER BY api_key_id",
            (DAY.isoformat(),)).fetchall()
        assert [r[0] for r in rows] == [
            "apikey_01DXXtR5LfFyDuhhghpDQ1h9", "apikey_other"]
        assert sum(Decimal(r[2]) for r in rows) == (
            REAL_BILLED + REAL_BILLED - Decimal("2.328") + Decimal("1"))

    def test_a_day_needing_no_correction_still_records_who_spent_it(self, db):
        """The evidence must exist for every closed day, not only the
        ones that disagreed - otherwise the days that look fine are
        exactly the ones nobody can check."""
        seed_local(db, DAY, str(REAL_BILLED))
        r = backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert not r.applied, "this day needed no adjustment"
        assert db.execute(
            "SELECT COUNT(*) FROM usage_by_key WHERE target_date = ?",
            (DAY.isoformat(),)).fetchone()[0] == 1

    def test_re_running_restates_rather_than_doubles(self, db):
        for _ in range(3):
            backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        rows = db.execute(
            "SELECT cents FROM usage_by_key WHERE target_date = ?",
            (DAY.isoformat(),)).fetchall()
        assert len(rows) == 1
        assert Decimal(rows[0][0]) == REAL_BILLED

    def test_a_database_without_the_table_still_gets_its_correction(
            self, tmp_path):
        """The evidence is worth having; it is not worth losing the day's
        correction over, which is the half that touches money."""
        conn = sqlite3.connect(tmp_path / "old.db")
        conn.executescript(SCHEMA.read_text())
        conn.execute("DROP TABLE usage_by_key")
        conn.commit()
        r = backfill_day(conn, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert r.applied
        assert ledger_total(conn, DAY) == REAL_BILLED
        conn.close()


class TestItCorrectsRatherThanRewrites:
    def test_nothing_already_recorded_is_altered(self, db):
        seed_local(db, DAY, "20")
        before = db.execute(
            "SELECT id, raw_usage_json, priced_cents, priced_at FROM "
            "cost_events").fetchall()
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        after = db.execute(
            "SELECT id, raw_usage_json, priced_cents, priced_at FROM "
            "cost_events WHERE component != ?",
            (BACKFILL_COMPONENT,)).fetchall()
        assert before == after, (
            "history was edited; the correction must be a NEW row or the "
            "evidence of what the bot believed at the time is destroyed")

    def test_the_correction_is_visibly_a_correction(self, db):
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        row = db.execute(
            "SELECT component, raw_usage_json FROM cost_events "
            "WHERE component = ?", (BACKFILL_COMPONENT,)).fetchone()
        assert row is not None
        stored = json.loads(row[1])
        assert stored["backfill"] is True
        assert stored["billed_cents"] == str(REAL_BILLED)
        assert stored["groups"][0]["api_key_id"].startswith("apikey_")

    def test_running_it_twice_does_not_double(self, db):
        for _ in range(3):
            backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert ledger_total(db, DAY) == REAL_BILLED
        assert db.execute(
            "SELECT COUNT(*) FROM cost_events WHERE component = ?",
            (BACKFILL_COMPONENT,)).fetchone()[0] == 1

    def test_it_shrinks_when_the_real_rows_finally_arrive(self, db):
        """A day corrected while events were missing must un-correct
        itself if those events are later recorded, or the day ends up
        counted twice."""
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        seed_local(db, DAY, str(REAL_BILLED))          # the real rows land
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert ledger_total(db, DAY) == REAL_BILLED
        assert db.execute(
            "SELECT COUNT(*) FROM cost_events WHERE component = ?",
            (BACKFILL_COMPONENT,)).fetchone()[0] == 0

    def test_the_gap_ignores_its_own_previous_adjustment(self, db):
        seed_local(db, DAY, "20")
        backfill_day(db, DAY, fetch=fetch([REAL_GROUP]), now=NOW)
        assert local_real_total(db, DAY) == Decimal("20"), (
            "the adjustment is counted as real spend, so the next run "
            "measures a gap it has already closed")


class TestItRefusesRatherThanGuesses:
    def test_today_is_not_a_closed_day(self, db):
        with pytest.raises(ValueError, match="whole days"):
            backfill_day(db, NOW.date(), fetch=fetch([REAL_GROUP]), now=NOW)

    def test_a_group_with_no_model_is_never_priced_at_zero(self, db):
        bad = dict(REAL_GROUP)
        bad.pop("model")
        with pytest.raises(BackfillError, match="cannot be priced"):
            backfill_day(db, DAY, fetch=fetch([bad]), now=NOW)

    def test_an_unknown_model_is_loud(self, db):
        bad = dict(REAL_GROUP, model="claude-something-new")
        with pytest.raises(Exception):
            backfill_day(db, DAY, fetch=fetch([bad]), now=NOW)

    def test_an_empty_day_reports_zero_rather_than_erasing_the_ledger(
            self, db):
        """A day Anthropic reports nothing for, where the ledger DOES
        have rows, is a discrepancy to surface - not a licence to write
        a negative adjustment silently. It corrects, and says so."""
        seed_local(db, DAY, "50")
        r = backfill_day(db, DAY, fetch=fetch([]), now=NOW)
        assert r.billed_cents == Decimal("0")
        assert r.adjustment_cents == Decimal("-50")
        assert "billed 0c" in r.reason or "billed 0" in r.reason

    def test_one_bad_day_does_not_abandon_the_month(self, db):
        def flaky(d):
            if d.day % 2 == 0:
                raise BackfillError("upstream had a moment")
            return [REAL_GROUP]

        start = (NOW - timedelta(days=6)).date()
        end = (NOW - timedelta(days=1)).date()
        out = backfill_range(db, start, end, fetch=flaky, now=NOW)
        assert len(out) == 6
        assert any("could not be corrected" in r.reason for r in out)
        assert any(r.applied for r in out)

    def test_a_range_stops_at_the_first_unclosed_day(self, db):
        out = backfill_range(db, (NOW - timedelta(days=2)).date(),
                             (NOW + timedelta(days=5)).date(),
                             fetch=fetch([REAL_GROUP]), now=NOW)
        assert all(r.target_date < NOW.date() for r in out)
        assert len(out) == 2


class TestTheUpstreamCallIsReadOnly:
    def test_only_the_usage_report_is_ever_called(self):
        import inspect

        from catalyst.cost import backfill

        src = inspect.getsource(backfill)
        assert "usage_report/messages" in src
        for forbidden in ("httpx.post", "httpx.delete", "httpx.put",
                          '"POST"', '"DELETE"', '"PUT"', "spend_limit"):
            assert forbidden not in src, (
                f"{forbidden} appears in a module that must never modify "
                "anything in the owner's Anthropic account")

    def test_a_truncated_page_is_refused_not_used(self):
        import httpx

        from catalyst.cost.backfill import fetch_usage_day

        def get(url, **kw):
            return httpx.Response(
                200, json={"data": [], "has_more": True},
                request=httpx.Request("GET", url))

        with pytest.raises(BackfillError, match="has_more"):
            fetch_usage_day(date(2026, 8, 15), admin_key="k", http_get=get)

    def test_a_non_200_carries_the_raw_body(self):
        import httpx

        from catalyst.cost.backfill import fetch_usage_day

        def get(url, **kw):
            return httpx.Response(403, text="forbidden: wrong key type",
                                  request=httpx.Request("GET", url))

        with pytest.raises(BackfillError) as e:
            fetch_usage_day(date(2026, 8, 15), admin_key="k", http_get=get)
        assert "forbidden" in e.value.body
