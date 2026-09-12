"""Is the Form 4 / insider data actually helping, and the hunt's rate.

OWNER-ASKED 2026-09-12: *"how do we know if the form 4 and insider data
is actually helping or not? is it easy to determine this? Can we get a
trade purely from claude research and one as normal, e.g. if we have 10
trades in 2 weeks at least 5 are fully claude research from news and
trade deals etc"* and, when told a comparison page would be full of
zeros, *"add the comparison page 0s are fine if it means itll populate
it it goes on"*.

TWO HALVES, and they answer different questions.

THE PAGE answers "is it helping": each arm from nomination to banked
money, beside its own out-of-sample grade read from this database's own
`backtest_results` rows. Built with zeros showing because the owner
asked for that explicitly. The one thing deliberately NOT shown as zero
is a ratio with no denominator - "no calls yet" and "0% conversion" are
different facts and only one of them is a verdict.

THE HUNT RATE answers "can we get 5 of 10 from Claude's own research":
the allocation the owner wants already exists, because research slots
rotate one per arm per round. What was missing was SUPPLY - and the
thing limiting it was a typed number.

MEASURED 2026-09-12: `hunts_per_day` returned `min(4, 333c // 23.2c)` at
the owner's $100 cap. The BUDGET afforded fourteen; a hard-coded 4 was
the limiter, and it was also capping the $300 row at 4 so tripling the
cap bought nothing. Removing it removed the bound too - a 1c measured
hunt returned 166 a day - so the ceiling is now the CADENCE (a hunt runs
once per cycle at most) and the arm's own record (no directional view in
40+ paid calls drops it to a probe share, the same measured rule that
cut the conjunction allowance).

Fully offline.
"""

import json
import pathlib
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    """PRODUCTION SETTINGS: `init_db` enables PRAGMA foreign_keys, which a
    raw connection does not. This file seeds a full
    candidate -> decision -> order -> position -> closed_trade chain,
    so a fixture that skipped a parent row would look fine with FKs
    off and be impossible in production.
    """
    from catalyst.storage import init_db

    conn = init_db(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
        "this fixture exists to match production; foreign keys are off")
    yield conn
    conn.close()


def _dbview(conn, tmp_path):
    from catalyst.dashboard.db import Db

    conn.commit()
    return Db(str(tmp_path / "t.db"))


def seed_candidate(conn, cid, origin, ticker="AAA", direction=None,
                   conviction=0.7):
    conn.execute(
        "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, ticker, "insider_cluster", "2026-09-20", "estimated",
         json.dumps(["e1"]), NOW.isoformat(), "tech",
         json.dumps(["tech"])))
    conn.execute(
        "INSERT OR REPLACE INTO candidate_origin VALUES (?,?,?,?)",
        (cid, origin, "because", NOW.isoformat()))
    if direction is not None:
        conn.execute(
            "INSERT OR REPLACE INTO research_views VALUES (?,?,?,?,?,?,?,?)",
            (cid, direction, conviction, "t", "i", 10, 0, "r"))
    conn.commit()


def seed_paid_call(conn, cid, cents):
    """Columns read from the table rather than typed, so a schema change
    fails this loudly instead of silently seeding the wrong shape."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(research_calls)")]
    row = {"id": str(uuid.uuid4()), "candidate_id": cid,
           "model": "claude-sonnet-5", "prompt_rendered": "p",
           "tools_offered": "[]", "cost_cents": str(cents),
           "latency_ms": 100, "skipped_reason": None,
           "called_at": NOW.isoformat()}
    missing = [c for c in cols if c not in row]
    assert not missing, f"research_calls gained columns: {missing}"
    conn.execute(
        f"INSERT INTO research_calls ({','.join(cols)}) VALUES "
        f"({','.join('?' * len(cols))})", [row[c] for c in cols])
    conn.commit()


def seed_closed_trade(conn, cid, pnl_cents, ticker="AAA"):
    """A full chain: decision -> order -> position -> closed trade. The
    arms page has to walk all of it to attribute money to an arm."""
    did, oid, pid = (str(uuid.uuid4()) for _ in range(3))
    conn.execute(
        "INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (did, cid, "trade", "long", "400", "8", "45", "2026-09-25",
         json.dumps([]), json.dumps({}), NOW.isoformat()))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)")]
    # decision_id CARRIES THE CANDIDATE ID. The column name lies; the
    # foreign key on it is REFERENCES candidates(id), and
    # execution/orders.py passes decision.candidate_id. With this fixture
    # on production settings (init_db, foreign keys ON) seeding it any
    # other way is impossible - which is what caught the arms query
    # joining it as a decision id and matching nothing.
    row = {"id": oid, "decision_id": cid, "broker_order_id": "b1",
           "client_order_id": "c1", "side": "buy", "qty": "8",
           "order_type": "market", "time_in_force": "day",
           "status": "filled", "submitted_at": NOW.isoformat(),
           "raw_response": "{}", "kind": "entry", "limit_price": None,
           "stop_price": None, "replaced_at": None, "confirmed_at": None,
           "account_mode": "paper"}
    conn.execute(
        f"INSERT INTO orders ({','.join(cols)}) VALUES "
        f"({','.join('?' * len(cols))})",
        [row.get(c) for c in cols])
    conn.execute(
        "INSERT INTO positions VALUES (?,?,?,?,?,?,?)",
        (pid, ticker, json.dumps([oid]), None, NOW.isoformat(),
         "2026-09-25", "closed"))
    conn.execute(
        "INSERT INTO closed_trades VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, "paper", "50", "52", "hard_exit", int(pnl_cents), 10, 9,
         NOW.isoformat()))
    conn.commit()
    return did


def seed_graded_run(conn, name, *, n, hit, maxdd, excess):
    rid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO backtest_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, name, "structural", "2024-01-01", "2026-01-01", "0.6872",
         "0.7545", excess, json.dumps({}), "notes", NOW.isoformat()))
    conn.execute(
        "INSERT INTO backtest_sample_stats VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, "out_of_sample", n, hit, "0.0087", "0.0050", "-0.574",
         maxdd, "0.001"))
    conn.commit()
    return rid


class TestTheArmsPageWithNothingOnRecord:
    """Zeros are fine - the owner said so - but they have to be LABELLED
    zeros rather than a verdict dressed as a measurement."""

    def test_every_wired_arm_appears_from_day_one(self, db, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.orchestrator.cycle import ARM_ROTATION

        html = panels.arms_panel(_dbview(db, tmp_path))
        from catalyst.dashboard.panels import _ORIGIN_NAMES

        for arm in ARM_ROTATION:
            label = _ORIGIN_NAMES.get(arm, arm)
            assert label.replace("'", "&#x27;") in html, (
                f"{arm} is missing from the page, which is "
                "indistinguishable from it not existing")

    def test_it_says_plainly_that_nothing_can_be_concluded_yet(
            self, db, tmp_path):
        from catalyst.dashboard import panels

        html = panels.arms_panel(_dbview(db, tmp_path))
        assert "No arm has closed a trade yet" in html
        assert "insider data is helping" in html

    def test_a_ratio_with_no_denominator_is_a_dash_not_zero_percent(self):
        """The defect this page exists to avoid. "0% conversion" reads as
        a measured failure; "no calls yet" is an absence of evidence."""
        from catalyst.dashboard.panels import _pct_or_dash

        assert _pct_or_dash(None) == "—"
        assert _pct_or_dash(0.0) == "0%"
        assert _pct_or_dash(0.125, 1) == "12.5%"

    def test_an_ungraded_arm_says_never_replayed_rather_than_zero(
            self, db, tmp_path):
        from catalyst.dashboard import panels

        html = panels.arms_panel(_dbview(db, tmp_path))
        assert "never replayed" in html

    def test_a_broken_query_is_not_reported_as_zeros(self, db, tmp_path):
        """House rule 3, and it matters most on a page of zeros: "nothing
        happened" and "the query is broken" look identical otherwise."""
        from catalyst.dashboard.panels import _arm_provenance

        class Bad:
            sql = "SELECT 1"
            params = ()
            rows: list = []
            row_count = 0
            error = "no such table: candidate_origin"

        out = _arm_provenance("x", [("candidates", Bad())])
        assert "query FAILED" in out
        assert "NOT a measurement" in out


class TestItAttributesRealMoneyToTheRightArm:
    def test_a_closed_trade_lands_against_its_own_arm(self, db, tmp_path):
        from catalyst.dashboard.queries import arm_records

        seed_candidate(db, "c-hunt", "hunt", "HHH", "long")
        seed_paid_call(db, "c-hunt", 12)
        seed_closed_trade(db, "c-hunt", 250, "HHH")

        seed_candidate(db, "c-scr", "screen", "SSS", "long")
        seed_paid_call(db, "c-scr", 18)
        seed_closed_trade(db, "c-scr", -80, "SSS")

        rows = {r.origin: r for r in arm_records(_dbview(db, tmp_path)).rows}
        assert rows["hunt"].realised_cents == 250
        assert rows["hunt"].closed == 1 and rows["hunt"].wins == 1
        assert rows["screen"].realised_cents == -80
        assert rows["screen"].closed == 1 and rows["screen"].wins == 0
        assert rows["conjunction"].realised_cents == 0

    def test_conversion_and_hit_rate_are_counted_not_modelled(
            self, db, tmp_path):
        from catalyst.dashboard.queries import arm_records

        # Four paid calls on the screen arm, one of which produced a view.
        for i in range(4):
            cid = f"s{i}"
            seed_candidate(db, cid, "screen", "SSS",
                           "long" if i == 0 else "no_trade")
            seed_paid_call(db, cid, 18)
        rows = {r.origin: r for r in arm_records(_dbview(db, tmp_path)).rows}
        assert rows["screen"].paid_calls == 4
        assert rows["screen"].directional == 1
        assert rows["screen"].conversion == pytest.approx(0.25)
        assert rows["screen"].hit_rate is None, (
            "a hit rate with no closed trades must be None, not 0.0")

    def test_an_open_position_is_not_counted_as_realised(self, db, tmp_path):
        """Paper gains on an open position are not money."""
        from catalyst.dashboard.queries import arm_records

        seed_candidate(db, "c-open", "screen", "OOO", "long")
        seed_paid_call(db, "c-open", 18)
        did = seed_closed_trade(db, "c-open", 999, "OOO")
        db.execute("DELETE FROM closed_trades")       # still open
        db.execute("UPDATE positions SET status='open'")
        db.commit()
        rows = {r.origin: r for r in arm_records(_dbview(db, tmp_path)).rows}
        assert rows["screen"].orders == 1
        assert rows["screen"].closed == 0
        assert rows["screen"].realised_cents == 0
        assert did


class TestTheGradeComesFromTheDatabase:
    def test_an_arms_own_out_of_sample_grade_is_read_back(self, db, tmp_path):
        """Read from `backtest_results`, never copied off a document -
        so the page cannot disagree with the run that produced it."""
        from catalyst.dashboard.queries import arm_records

        seed_graded_run(
            db, "C-insider-cluster-pre(2x10d,50k,hold12,liq5/1M)|oos|api8|c15bp",
            n=203, hit="0.493", maxdd="0.412", excess="0.0673")
        rows = {r.origin: r for r in arm_records(_dbview(db, tmp_path)).rows}
        g = rows["screen"].graded
        assert g is not None, "the graded run was not matched to its arm"
        assert g["n"] == 203
        assert Decimal(str(g["hit_rate"])) == Decimal("0.493")
        assert rows["hunt"].graded is None, (
            "the hunt cannot be graded by a replay and must not claim to be")

    def test_an_in_sample_run_is_not_used_as_the_grade(self, db, tmp_path):
        """In-sample is the figure every tuned variant flattered itself
        with, and this project measured tuning making both graded arms
        WORSE out of sample every single time it was tried.

        THE IN-SAMPLE ROW IS DELIBERATELY THE NEWER ONE, so `ORDER BY
        created_at DESC` puts it first and correct code has to skip past
        it. A first attempt at this test inserted both kinds for the SAME
        run and passed no matter what the query did, because the
        out-of-sample row happened to be inserted first - unfailable from
        the day it was written, which is exactly the pattern
        docs/WHAT-WE-TRIED.md section 6 opens with. Found by sabotage."""
        from catalyst.dashboard.queries import arm_records

        old_run = seed_graded_run(
            db, "C-insider-cluster-pre|oos|api8|c15bp",
            n=203, hit="0.493", maxdd="0.412", excess="0.0673")
        db.execute("UPDATE backtest_results SET created_at = ? WHERE id = ?",
                   ("2026-01-01T00:00:00+00:00", old_run))
        newer = seed_graded_run(
            db, "C-insider-cluster-pre|oos|api8|c15bp",
            n=999, hit="0.999", maxdd="0.001", excess="9.999")
        db.execute("UPDATE backtest_results SET created_at = ? WHERE id = ?",
                   ("2026-09-01T00:00:00+00:00", newer))
        # The newer run carries ONLY in-sample stats.
        db.execute("DELETE FROM backtest_sample_stats WHERE result_id = ?",
                   (newer,))
        db.execute(
            "INSERT INTO backtest_sample_stats VALUES (?,?,?,?,?,?,?,?,?)",
            (newer, "in_sample", 710, "0.531", "0.0028", "0.002", "-0.896",
             "0.526", "0.001"))
        db.commit()

        rows = {r.origin: r for r in arm_records(_dbview(db, tmp_path)).rows}
        g = rows["screen"].graded
        assert g["sample_kind"] == "out_of_sample", (
            "an in-sample figure was used as the grade")
        assert g["n"] == 203, (
            "the newer in-sample run was used even though it has no "
            "out-of-sample stats")

    def test_the_page_names_a_graded_arm_that_never_fires(self, db, tmp_path):
        """The finding that matters most and is easiest to miss: a
        measured edge that never produces a tradeable view earns
        nothing, and that needs a different fix from a losing arm."""
        from catalyst.dashboard import panels

        seed_graded_run(db, "A-earnings-drift-pre|oos|api8|c15bp",
                        n=84, hit="0.571", maxdd="0.088", excess="-0.641")
        html = panels.arms_panel(_dbview(db, tmp_path))
        assert "graded and has produced no tradeable view" in html


class TestTheHuntRateIsNoLongerATypedNumber:
    def test_raising_the_cap_now_actually_raises_hunting(self):
        """The measured defect: min(4, ...) capped the $300 row at 4, so
        tripling the budget bought nothing. Throttles derive from the
        budget."""
        from catalyst.discovery.hunt import hunts_per_day

        assert hunts_per_day(30000) > hunts_per_day(10000) > hunts_per_day(2000)
        assert hunts_per_day(30000) == 8

    def test_a_measured_cheaper_hunt_raises_the_rate(self, db):
        from catalyst.cost.observed import MIN_OBSERVED_CALLS
        from catalyst.discovery.hunt import hunts_per_day

        for _ in range(MIN_OBSERVED_CALLS):
            db.execute(
                "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
                " component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "{}", "claude-sonnet-5", "scheduled",
                 "hunt", "12", NOW.isoformat()))
        db.commit()
        assert hunts_per_day(10000, db) > hunts_per_day(10000), (
            "the measured cost did not raise the rate, so the seed is "
            "still the authority"
        )

    def test_the_bound_is_the_cadence_and_it_is_derived(self, monkeypatch):
        """Bounded, but by something real. A hunt is asked once per cycle
        at most, so a day cannot hold more hunts than cycles - and it
        moves on its own if the interval changes."""
        from catalyst.discovery import hunt as hunt_mod

        assert hunt_mod._hunts_the_cadence_allows() == 96      # 900s cycles
        monkeypatch.setenv("CATALYST_CYCLE_SECONDS", "3600")
        assert hunt_mod._hunts_the_cadence_allows() == 24
        monkeypatch.setenv("CATALYST_CYCLE_SECONDS", "0")
        assert hunt_mod._hunts_the_cadence_allows() == 96      # refuses a zero
        monkeypatch.setenv("CATALYST_CYCLE_SECONDS", "nonsense")
        assert hunt_mod._hunts_the_cadence_allows() == 96

    def test_an_absurd_cap_is_still_bounded(self):
        from catalyst.discovery.hunt import (
            _hunts_the_cadence_allows, hunts_per_day,
        )

        assert hunts_per_day(10 ** 12) == _hunts_the_cadence_allows()

    def test_a_proven_non_converter_drops_to_a_probe_share(self, db):
        """THE BOUND THAT REPLACED THE TYPED CEILING. 40 paid calls with
        no directional view is the same measured test that cut the
        conjunction allowance; it was bounding RESEARCH slots only, so a
        non-converting hunt kept nominating at full rate while its
        nominations were rationed."""
        from catalyst.discovery.hunt import hunts_per_day
        from catalyst.orchestrator.cycle import ARM_PROBE_MIN_CALLS

        full = hunts_per_day(30000, db)
        for i in range(ARM_PROBE_MIN_CALLS):
            cid = f"h{i}"
            seed_candidate(db, cid, "hunt", "HHH", "no_trade")
            seed_paid_call(db, cid, 12)
        demoted = hunts_per_day(30000, db)
        assert demoted < full, (
            "40 paid hunt calls with no directional view did not reduce "
            "the hunt allowance")
        assert demoted >= 1, (
            "an arm on a probe share must never drop to zero - it has to "
            "keep generating the evidence that would restore it")

    def test_one_directional_view_restores_the_full_allowance(self, db):
        """The demotion is a reading of the record, never a stored flag,
        so it lifts on the cycle after the arm finally converts."""
        from catalyst.discovery.hunt import hunts_per_day
        from catalyst.orchestrator.cycle import ARM_PROBE_MIN_CALLS

        for i in range(ARM_PROBE_MIN_CALLS):
            cid = f"h{i}"
            seed_candidate(db, cid, "hunt", "HHH", "no_trade")
            seed_paid_call(db, cid, 12)
        demoted = hunts_per_day(30000, db)
        seed_candidate(db, "h-good", "hunt", "GGG", "long")
        seed_paid_call(db, "h-good", 12)
        assert hunts_per_day(30000, db) > demoted


class TestAHuntIsNotPaidForTwiceOnTheSameFeed:
    def test_a_feed_with_nothing_new_refuses(self, db):
        from catalyst.discovery.hunt import feed_changed_since_last_hunt

        db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            " component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "{}", "claude-sonnet-5", "scheduled",
             "hunt", "12", NOW.isoformat()))
        db.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                   ("edgar_form4", "old", (NOW - timedelta(hours=2)).isoformat(),
                    "{}"))
        db.commit()
        changed, why = feed_changed_since_last_hunt(db)
        assert changed is False
        assert "identical digest" in why

    def test_one_new_event_is_enough(self, db):
        from catalyst.discovery.hunt import feed_changed_since_last_hunt

        db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            " component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "{}", "claude-sonnet-5", "scheduled",
             "hunt", "12", (NOW - timedelta(hours=1)).isoformat()))
        db.execute("INSERT INTO raw_events VALUES (?,?,?,?)",
                   ("edgar_form4", "new", NOW.isoformat(), "{}"))
        db.commit()
        changed, why = feed_changed_since_last_hunt(db)
        assert changed is True
        assert "1 event(s) arrived" in why

    def test_the_first_hunt_ever_is_always_worth_paying_for(self, db):
        from catalyst.discovery.hunt import feed_changed_since_last_hunt

        changed, why = feed_changed_since_last_hunt(db)
        assert changed is True
        assert "has ever been billed" in why

    def test_an_unreadable_ledger_is_not_a_veto(self, db):
        """Never raises, and errs toward hunting: a ledger that cannot
        answer must not silently stop discovery."""
        from catalyst.discovery.hunt import feed_changed_since_last_hunt

        db.execute("DROP TABLE cost_events")
        db.commit()
        changed, _why = feed_changed_since_last_hunt(db)
        assert changed is True

    def test_it_is_wired_into_the_due_check(self):
        """THE CALL SITE, not the function - a helper nobody calls has
        passed its own tests four times in this project."""
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler._hunt_due)
        assert "feed_changed_since_last_hunt" in src

    def test_a_declined_hunt_does_not_consume_the_days_allowance(self, db):
        """It used to increment before every reason to decline had been
        checked, which would spend the day's hunts on hunts that never
        ran."""
        from catalyst.orchestrator.scheduler import _hunt_due

        db.execute(
            "INSERT INTO cost_events (id, raw_usage_json, model, kind, "
            " component, priced_cents, priced_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "{}", "claude-sonnet-5", "scheduled",
             "hunt", "12", NOW.isoformat()))
        db.commit()                      # no raw_events at all -> nothing new
        # A NON-EMPTY state dict on purpose: an empty one is falsy, so a
        # regression written as `daily_state or {}` would quietly operate
        # on a throwaway dict and this test would pass for the wrong
        # reason. Found by sabotage.
        state: dict = {"hunt_day": NOW.date().isoformat(), "hunt_count": 0}
        assert _hunt_due(state, 10000, NOW, db) is False
        assert state["hunt_count"] == 0, (
            "a declined hunt consumed the allowance")


class TestTheWeekendHuntGetsADifferentJob:
    def test_a_closed_market_adds_the_brief(self):
        from catalyst.discovery.hunt import render_hunt_prompt

        shut = render_hunt_prompt([], NOW, market_open=False)
        assert "THE EXCHANGE IS SHUT RIGHT NOW" in shut
        assert "reason outward FROM" in shut

    def test_an_open_market_does_not(self):
        from catalyst.discovery.hunt import render_hunt_prompt

        assert "EXCHANGE IS SHUT" not in render_hunt_prompt(
            [], NOW, market_open=True)

    def test_unknown_keeps_the_ordinary_brief(self):
        """None means the clock could not be read. The conservative
        direction is the brief every hunt got before this existed."""
        from catalyst.discovery.hunt import render_hunt_prompt

        assert "EXCHANGE IS SHUT" not in render_hunt_prompt([], NOW)

    def test_the_brief_does_not_hand_out_more_searches(self):
        """Conjunctions were given ten searches instead of three on the
        argument that the answer lived in reporting the feeds do not
        carry. After 89 paid calls at the larger allowance and zero
        directional views the allowance was cut back. Evidence buys
        budget; hope does not - so the weekend gets a different JOB at
        the same price."""
        from catalyst.discovery.hunt import closed_market_brief
        from catalyst.discovery.hunt_tools import HUNT_SEARCHES

        brief = closed_market_brief()
        assert str(HUNT_SEARCHES + 1) not in brief
        assert "more search" not in brief.lower()

    def test_it_still_states_the_date_rule(self):
        """88% of hunt nominations were once rejected for a past catalyst
        date. A brief that quietly dropped the rule would bring that
        back, and "the price will react on Monday" is exactly the shape
        of nomination this brief could invite."""
        from catalyst.discovery.hunt import closed_market_brief

        brief = closed_market_brief()
        assert "today or later" in brief
        assert "not itself an event" in brief

    def test_the_market_state_reaches_the_hunt(self):
        import inspect

        from catalyst.discovery import hunt as hunt_mod
        from catalyst.orchestrator import scheduler

        assert "market_open" in inspect.signature(hunt_mod.hunt).parameters
        src = inspect.getsource(scheduler._run_one_cycle)
        assert "market_open=market_open" in src, (
            "the scheduler never tells the hunt whether the market is shut")

    def test_the_state_comes_from_the_broker_not_from_the_weekday(self):
        """House rule 7: a market holiday is the case nobody thinks of,
        and `weekday() < 5` calls it open."""
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler._run_one_cycle)
        window = src[src.index("market_open = None"):]
        window = window[:window.index("res = hunt(")]
        assert "get_clock" in window
        assert "weekday" not in window
