"""Conjunction took half the research budget while demoted. Measured.

OWNER-ASKED 2026-09-15: I proposed unwiring the conjunction arm and the
owner said *"if you think it improves do so"*. **Measuring it first said
DON'T** - see `TestUnwiringWouldHaveMadeItWORSE` below - and measuring
*why* conjunction was still taking half the calls found the real defect.

MEASURED from the owner's 09-14 bundle: **conjunction took 8 of 16 paid
research calls, 50%**, on a day its own lifetime record (~114 paid calls,
zero directional views) qualified it for a probe share of one round in
four. `demoted_arms` was correct. `interleave_by_arm` was correct. The
call site passed the demotion through. **The INPUT was wrong.**

A candidate id is a CONTENT HASH with no date in it - deliberately, so a
re-run is idempotent (`conjunctions._hash_id`: `"conj|TICKER|kinds"`).
Combined with `INSERT OR IGNORE` on `candidate_origin`, THE FIRST STAMP
WINS FOREVER. So every conjunction first seen before the 2026-09-11 fix
that gave conjunctions their own origin is stamped `screen` for the life
of the database, and no later cycle could correct it.

Two consequences, both of which cost money:

- conjunction's counted calls are UNDERSTATED, so the demotion fires
  late or never and the arm keeps a full share of a budget §5 measures
  as fully committed;
- the insider arm's counted calls are OVERSTATED by exactly those rows,
  so the Arms page's cost-per-view for the only arm that has ever
  produced a tradeable view is a blend of two arms - on the page built
  to answer "is the insider data helping".

Eleventh instance of this project's recurring shape, in its other form:
not a fact that never reached the page, but a fact the page read from a
column nothing could correct.
"""

from datetime import datetime, timedelta, timezone

import pytest

from catalyst.orchestrator import scheduler as S
from catalyst.orchestrator.cycle import (
    ARM_PROBE_EVERY,
    ARM_PROBE_MIN_CALLS,
    ARM_ROTATION,
    arm_conversion,
    demoted_arms,
    interleave_by_arm,
)
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc).replace(microsecond=0)


class Cand:
    def __init__(self, cid):
        self.id = cid

    def __repr__(self):
        return self.id


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "arms.db"))
    # §14: through init_db, so foreign keys are on as they are in
    # production and an impossible row is impossible here too.
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    yield c
    c.close()


def candidate(c, cid, ticker="CHYM", when=None):
    c.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
              (cid, ticker, "credit_amendment",
               (NOW + timedelta(days=5)).date().isoformat(), "estimated",
               "[]", (when or NOW).isoformat(), "fin", "[]"))
    c.commit()


def origin_of(c, cid):
    row = c.execute("SELECT origin FROM candidate_origin "
                    "WHERE candidate_id = ?", (cid,)).fetchone()
    return row[0] if row else None


class TestTheOwnersCase:
    """A conjunction stamped by the old blanket sweep, corrected by the
    arm that owns it, with no backfill and nothing to run by hand."""

    def test_a_stale_screen_stamp_is_corrected_on_the_next_cycle(self, conn):
        candidate(conn, "conj-abc")
        S._record_origin(conn, [Cand("conj-abc")], S.BLANKET_ORIGIN, {}, NOW)
        assert origin_of(conn, "conj-abc") == "screen", "the pre-fix state"

        S._record_origin(conn, [Cand("conj-abc")], "conjunction", None, NOW)
        assert origin_of(conn, "conj-abc") == "conjunction", (
            "the arm that owns this candidate could not correct a stale "
            "stamp - which is the whole defect")

    def test_the_blanket_sweep_cannot_TAKE_IT_BACK(self, conn):
        """The sweep runs LAST every cycle over everything that
        survived. If it could overwrite, the correction would last
        milliseconds."""
        candidate(conn, "conj-abc")
        S._record_origin(conn, [Cand("conj-abc")], "conjunction", None, NOW)
        S._record_origin(conn, [Cand("conj-abc")], S.BLANKET_ORIGIN, {}, NOW)
        assert origin_of(conn, "conj-abc") == "conjunction"

    def test_one_specific_arm_does_not_steal_from_another(self, conn):
        """`hunt` and `earnings_drift` both upsert. A candidate cannot
        belong to two arms, but if it ever did, the LAST builder to run
        would win - which is deterministic and is what the cycle order
        already decides. Asserted so it is a decision rather than an
        accident."""
        candidate(conn, "hunt-x", ticker="MU")
        S._record_origin(conn, [Cand("hunt-x")], "earnings_drift", None, NOW)
        S._record_origin(conn, [Cand("hunt-x")], "hunt", None, NOW)
        assert origin_of(conn, "hunt-x") == "hunt"

    def test_a_rationale_is_never_erased_by_a_later_stamp(self, conn):
        """The hunt records WHY it nominated something; the conjunction
        builder passes None. A plain assignment would wipe a real
        rationale on the cycle a candidate qualified for both."""
        candidate(conn, "hunt-x", ticker="MU")
        S._record_origin(conn, [Cand("hunt-x")], "hunt",
                         {"hunt-x": "war -> supplier -> beneficiary"}, NOW)
        S._record_origin(conn, [Cand("hunt-x")], "hunt", None, NOW)
        row = conn.execute("SELECT rationale FROM candidate_origin "
                           "WHERE candidate_id='hunt-x'").fetchone()
        assert row[0] == "war -> supplier -> beneficiary"

    def test_a_rationale_arriving_later_is_recorded(self, conn):
        candidate(conn, "hunt-x", ticker="MU")
        S._record_origin(conn, [Cand("hunt-x")], "hunt", None, NOW)
        S._record_origin(conn, [Cand("hunt-x")], "hunt",
                         {"hunt-x": "the chain"}, NOW)
        row = conn.execute("SELECT rationale FROM candidate_origin "
                           "WHERE candidate_id='hunt-x'").fetchone()
        assert row[0] == "the chain"

    def test_it_never_raises_on_a_database_that_cannot_take_the_write(
            self, tmp_path):
        """Provenance is observability. Losing it must not cost a trade -
        the function's own docstring promises that, and §'s own history
        has this exact function raising NameError while handling an
        error."""
        import sqlite3

        c = sqlite3.connect(str(tmp_path / "bare.db"))
        try:
            S._record_origin(c, [Cand("x")], "hunt", None, NOW)
        finally:
            c.close()


class TestTheCorrectionREACHESTheDemotion:
    """The point of the fix. A corrected stamp has to move the number the
    allocation reads, or it is cosmetic."""

    def _seed_calls(self, conn, cid, n, views=0):
        for i in range(n):
            conn.execute(
                "INSERT INTO research_calls (id,candidate_id,model,"
                "prompt_rendered,tools_offered,cost_cents,latency_ms,"
                "skipped_reason,called_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{cid}-call-{i}", cid, "m", "p", "[]", "10", 1, None,
                 NOW.isoformat()))
        if views:
            conn.execute("INSERT OR REPLACE INTO research_views "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (cid, "long", 0.6, "t", "i", 10, 0, "w"))
        conn.commit()

    def test_a_stale_stamp_hides_the_calls_from_the_demotion(self, conn):
        """The measured defect, reproduced: with the stamp wrong, the
        arm's own calls are counted against `screen` and conjunction is
        not demoted."""
        candidate(conn, "conj-abc")
        S._record_origin(conn, [Cand("conj-abc")], S.BLANKET_ORIGIN, {}, NOW)
        self._seed_calls(conn, "conj-abc", ARM_PROBE_MIN_CALLS + 5)

        conv = arm_conversion(conn)
        assert conv.get("conjunction") is None, (
            "the fixture does not reproduce the stale stamp")
        assert "conjunction" not in demoted_arms(conv), (
            "with the stamp wrong, conjunction escapes demotion - which "
            "is exactly what the 09-14 bundle measured")

    def test_correcting_the_stamp_demotes_it(self, conn):
        candidate(conn, "conj-abc")
        S._record_origin(conn, [Cand("conj-abc")], S.BLANKET_ORIGIN, {}, NOW)
        self._seed_calls(conn, "conj-abc", ARM_PROBE_MIN_CALLS + 5)

        S._record_origin(conn, [Cand("conj-abc")], "conjunction", None, NOW)
        conv = arm_conversion(conn)
        calls, views = conv["conjunction"]
        assert calls >= ARM_PROBE_MIN_CALLS and views == 0, conv
        assert "conjunction" in demoted_arms(conv), (
            "the corrected stamp did not reach the demotion, so the fix "
            "is cosmetic")

    def test_an_arm_with_a_view_is_never_demoted_however_many_calls(
            self, conn):
        """The direction that must not break: the demotion is for an arm
        that has produced NOTHING, not for one that is merely busy."""
        candidate(conn, "conj-abc")
        S._record_origin(conn, [Cand("conj-abc")], "conjunction", None, NOW)
        self._seed_calls(conn, "conj-abc", ARM_PROBE_MIN_CALLS + 50, views=1)
        assert "conjunction" not in demoted_arms(arm_conversion(conn))

    def test_the_insider_arms_count_stops_carrying_conjunction_calls(
            self, conn):
        """The other half of the cost, and the one on a page the owner
        reads: the Arms table's cost-per-view for insider clusters was a
        blend of two arms."""
        candidate(conn, "conj-abc")
        candidate(conn, "insider_cluster-RLMD-1", ticker="RLMD")
        S._record_origin(conn, [Cand("conj-abc"),
                                Cand("insider_cluster-RLMD-1")],
                         S.BLANKET_ORIGIN, {}, NOW)
        self._seed_calls(conn, "conj-abc", 10)
        self._seed_calls(conn, "insider_cluster-RLMD-1", 3)
        before = arm_conversion(conn)["screen"][0]

        S._record_origin(conn, [Cand("conj-abc")], "conjunction", None, NOW)
        after = arm_conversion(conn)["screen"][0]
        assert before == 13 and after == 3, (before, after)


class TestUnwiringWouldHaveMadeItWORSE:
    """TRIED, MEASURED AND REJECTED. I proposed unwiring the conjunction
    arm and the owner agreed. The measurement says do not.

    With conjunction demoted - which it already is, once its stamp is
    right - its slots do NOT go to the hunt. The rotation is one per arm
    per round, and the hunt already takes its full share every round, so
    removing a competitor frees capacity for whichever arm still has
    candidates left. That is `screen`: the arm that graded WORST out of
    sample (49.3% hit rate, 41.2% max drawdown).
    """

    def _share(self, arms, demoted, slots=6, per_arm=30):
        cands, origins = [], {}
        for arm in arms:
            for i in range(per_arm):
                c = Cand(f"{arm}-{i}")
                cands.append(c)
                origins[c.id] = arm
        belt = interleave_by_arm(cands, origins, demoted=demoted)[:slots]
        out: dict = {}
        for c in belt:
            out[origins[c.id]] = out.get(origins[c.id], 0) + 1
        return out

    FOUR = ("screen", "conjunction", "earnings_drift", "hunt")
    THREE = ("screen", "earnings_drift", "hunt")

    def test_the_hunt_gains_NOTHING_from_unwiring(self):
        with_conj = self._share(self.FOUR, {"conjunction"})
        without = self._share(self.THREE, set())
        assert with_conj.get("hunt") == without.get("hunt"), (
            "the premise of my own proposal - that unwiring feeds the "
            f"hunt - is false: {with_conj} vs {without}")

    def test_the_freed_slots_go_to_the_WORST_GRADED_arm(self):
        with_conj = self._share(self.FOUR, {"conjunction"})
        without = self._share(self.THREE, set())
        assert without["screen"] > with_conj["screen"], (
            with_conj, without)

    def test_the_probe_share_already_bounds_it(self):
        """One round in four, and the arm keeps generating the evidence
        that could restore it. Unwiring takes that to zero and loses the
        evidence with it."""
        assert ARM_PROBE_EVERY == 4
        demoted = self._share(self.FOUR, {"conjunction"}, slots=24)
        full = self._share(self.FOUR, set(), slots=24)
        assert demoted["conjunction"] < full["conjunction"], (
            demoted, full)

    def test_conjunction_is_still_in_the_rotation(self):
        """The decision, asserted so it is not quietly reversed: the arm
        stays wired and is throttled by its own record."""
        assert "conjunction" in ARM_ROTATION


class TestTheCallSiteAndThePair:
    """Three sabotages came back GREEN and each was a real gap."""

    def test_the_blanket_sweep_PASSES_the_constant(self):
        """GREEN #1: every test above calls `_record_origin` directly with
        `S.BLANKET_ORIGIN`, so none of them exercises the CALL SITE - and
        a sweep passing a bare `"screens"` would upsert, reclaiming every
        candidate a specific arm had named. §6: assert the call site."""
        from source_guard import source_matches

        hits = source_matches("_record_origin(conn, kept, BLANKET_ORIGIN",
                              "catalyst/orchestrator")
        assert hits, (
            "the blanket sweep no longer passes BLANKET_ORIGIN, so its "
            "string and the rule that exempts it can drift - which is the "
            "same two-copies defect that produced the stale stamp")

    def test_no_call_site_passes_a_bare_screen_string(self):
        """The other half: a second sweep added later must not
        reintroduce the literal."""
        from source_guard import source_matches

        hits = [h for h in source_matches('_record_origin(conn, kept, "',
                                          "catalyst/orchestrator")]
        assert hits == [], (
            "an origin call site passes a bare string where the constant "
            f"belongs:\n{hits}")

    def test_the_rationale_is_protected_by_TWO_things_and_needs_both(
            self, conn):
        """GREEN #2 and #3, recorded as DEFENCE IN DEPTH rather than as
        uncaught (§14, §17, §24).

        A later stamp with no rationale cannot erase one because of
        `COALESCE` **and** because the `WHERE` clause finds nothing to
        change when the origin already matches and the rationale is
        already set. Breaking either alone is invisible; the right way to
        show a pair is load-bearing is to break BOTH, which is what this
        asserts by running the query the sabotage would produce.
        """
        candidate(conn, "hunt-x", ticker="MU")
        S._record_origin(conn, [Cand("hunt-x")], "hunt",
                         {"hunt-x": "war -> supplier"}, NOW)

        # Both guards removed at once: assignment instead of COALESCE,
        # and no WHERE to skip the no-op row.
        conn.execute(
            "INSERT INTO candidate_origin "
            "(candidate_id, origin, rationale, nominated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(candidate_id) DO UPDATE SET "
            "  origin = excluded.origin, rationale = excluded.rationale",
            ("hunt-x", "hunt", None, NOW.isoformat()))
        conn.commit()
        erased = conn.execute("SELECT rationale FROM candidate_origin "
                              "WHERE candidate_id='hunt-x'").fetchone()[0]
        assert erased is None, (
            "this test no longer demonstrates what the pair prevents")

        # And the shipped statement, on the same row, does not.
        conn.execute("UPDATE candidate_origin SET rationale = ? "
                     "WHERE candidate_id = 'hunt-x'", ("war -> supplier",))
        conn.commit()
        S._record_origin(conn, [Cand("hunt-x")], "hunt", None, NOW)
        kept = conn.execute("SELECT rationale FROM candidate_origin "
                            "WHERE candidate_id='hunt-x'").fetchone()[0]
        assert kept == "war -> supplier"

    def test_a_settled_row_IS_NOT_WRITTEN_AT_ALL_on_a_later_cycle(
            self, conn):
        """What the WHERE clause is for, made MEASURABLE.

        My first version of this asserted that `nominated_at` survived a
        second stamp - which the SET clause guarantees anyway, since it
        only touches `origin` and `rationale`. So it passed with the
        WHERE clause deleted and was testing nothing. `total_changes`
        counts rows actually written, which is the only observable
        difference: thousands of candidates are re-stamped every fifteen
        minutes, and a row that is already right must cost no write.
        """
        candidate(conn, "hunt-x", ticker="MU")
        S._record_origin(conn, [Cand("hunt-x")], "hunt",
                         {"hunt-x": "the chain"}, NOW)

        before = conn.total_changes
        S._record_origin(conn, [Cand("hunt-x")], "hunt",
                         {"hunt-x": "the chain"}, NOW)
        assert conn.total_changes == before, (
            "a settled row was rewritten: re-stamping an unchanged "
            "candidate cost "
            f"{conn.total_changes - before} write(s), and this runs over "
            "every candidate every cycle")

        # And a row that HAS changed must still be written, or the
        # correction this whole change exists for would never happen.
        moved = conn.total_changes
        S._record_origin(conn, [Cand("hunt-x")], "conjunction", None, NOW)
        assert conn.total_changes > moved, (
            "a changed origin was not written - the WHERE clause is now "
            "too tight and no stale stamp can ever be corrected")
        assert origin_of(conn, "hunt-x") == "conjunction"


class TestTheSTALESTAMPDEMOTEDTheONLYARMTHATCONVERTS:
    """The harm, measured rather than argued.

    `demoted_arms` reads `candidate_origin`. With every conjunction
    stamped `screen`, the calls it counts against `screen` are
    conjunction's - so the arm the record demotes is `screen`: the arm
    that has produced 24 of the 26 directional views this bot has ever
    formed and the only order it has ever placed.

    Measured on the owner's own database shape: before the fix
    `demoted == {'screen'}`; after one cycle, `{'conjunction'}`.
    """

    def _share(self, demoted, supply, slots=6):
        cands, origins = [], {}
        for arm, n in supply.items():
            for i in range(n):
                c = Cand(f"{arm}-{i}")
                cands.append(c)
                origins[c.id] = arm
        out: dict = {}
        for c in interleave_by_arm(cands, origins, demoted=demoted)[:slots]:
            out[origins[c.id]] = out.get(origins[c.id], 0) + 1
        return out

    #: The shape the builders actually produce, from the owner's 09-14
    #: bundle: conjunctions emit across fifteen catalyst types and build
    #: the most, drift and the hunt are thin, insider clusters are in
    #: between. The even-supply case cannot see this defect at all - with
    #: six slots and four arms each holding stock, every arm takes one a
    #: round and a probe share changes nothing. It only bites when supply
    #: is lopsided, which is every real cycle.
    REAL = {"earnings_drift": 1, "hunt": 1, "screen": 6, "conjunction": 30}

    def test_the_even_supply_case_CANNOT_see_it(self):
        """Recorded so a later session does not 'simplify' the fixture
        above into an even one and conclude the demotion does nothing."""
        even = {a: 10 for a in ("earnings_drift", "hunt",
                                "screen", "conjunction")}
        assert (self._share({"screen"}, even)
                == self._share({"conjunction"}, even)
                == self._share(set(), even)), (
            "an even supply now distinguishes the demotions, so this "
            "test's reason for existing has changed")

    def test_demoting_the_WRONG_arm_hands_slots_to_the_non_converter(self):
        wrong = self._share({"screen"}, self.REAL)
        right = self._share({"conjunction"}, self.REAL)
        assert wrong["conjunction"] > wrong["screen"], (
            "the defect no longer reproduces: with `screen` demoted the "
            f"non-converting arm should take more slots, got {wrong}")
        assert right["screen"] > right["conjunction"], (
            f"the fix does not reallocate: {right}")
        # And the direction of the swing, so a shrinking effect is visible
        # rather than merely still-positive.
        assert right["screen"] >= 3 * right["conjunction"], right

    def test_the_owners_database_demotes_screen_until_the_stamp_is_fixed(
            self, conn):
        """End to end on rows, not on the belt: a database whose
        conjunctions are all stamped `screen` demotes `screen`."""
        for i in range(ARM_PROBE_MIN_CALLS + 5):
            cid = f"conj-{i:020x}"
            candidate(conn, cid)
            # The pre-09-11 state: the blanket sweep got there first.
            S._record_origin(conn, [Cand(cid)], S.BLANKET_ORIGIN, {}, NOW)
            conn.execute(
                "INSERT INTO research_calls "
                "(id, candidate_id, model, prompt_rendered, tools_offered, "
                " cost_cents, latency_ms, skipped_reason, called_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"call-{i}", cid, "m", "p", "[]", 18, 100, None,
                 NOW.isoformat()))
        conn.commit()

        stale = demoted_arms(arm_conversion(conn))
        assert stale == {S.BLANKET_ORIGIN}, (
            "the owner's state does not reproduce: expected the blanket "
            f"origin to be the demoted one, got {stale}")

        # One cycle with the fix: the conjunction builder re-stamps its
        # own candidates and the demotion follows the real arm.
        for i in range(ARM_PROBE_MIN_CALLS + 5):
            S._record_origin(conn, [Cand(f"conj-{i:020x}")],
                             "conjunction", {}, NOW)
        after = demoted_arms(arm_conversion(conn))
        assert after == {"conjunction"}, after
        assert S.BLANKET_ORIGIN not in after, (
            "the only arm that has ever converted is still demoted")


class TestABundleCanCheckWhichArmSpentTheMoney:
    """The logic bundle's own `why` promises the funnel, and section 3
    says the funnel means per stage AND PER ARM.

    Measured on the owner's 09-14 logic bundle: `candidate_origin` was
    not in it, so answering "which arm took the 16 paid calls" meant
    inferring the arm from each candidate id's prefix. That works only
    by luck of the id format, and it is blind to exactly the defect this
    change fixes - a stale stamp is invisible when you read the id.
    """

    def test_the_logic_scope_carries_the_arm(self):
        from catalyst.dashboard.server import DIAGNOSTIC_SCOPES
        assert "candidate_origin" in DIAGNOSTIC_SCOPES["logic"]["tables"], (
            "the scope that promises the funnel cannot say which arm "
            "spent the money")

    def test_the_arm_table_is_windowed_like_every_other(self):
        """A table with no registered time column is exported WHOLE
        behind a window the bundle claims (section 13). `nominated_at`
        must be the one picked."""
        from catalyst.dashboard.server import _TIME_COLUMNS, _time_column
        from catalyst.dashboard.queries import Db
        import tempfile, pathlib
        assert "nominated_at" in _TIME_COLUMNS
        with tempfile.TemporaryDirectory() as d:
            p = str(pathlib.Path(d) / "w.db")
            init_db(p).close()
            assert _time_column(Db(p), "candidate_origin") == "nominated_at"


class TestASETTLEDROWCOSTSNOWRITEForEVERYBuilderShape:
    """FOUND BY THE UPGRADE RUN, NOT BY A TEST, and it was mine.

    `test_a_settled_row_IS_NOT_WRITTEN_AT_ALL_on_a_later_cycle` above
    passes a rationale, because the hunt does. **The conjunction builder
    passes None** - and the first WHERE clause said
    `candidate_origin.rationale IS NULL`, which for a row that never had
    one is permanently TRUE. So every conjunction row was rewritten on
    every cycle: roughly 7,000 pointless UPDATEs every fifteen minutes,
    on the arm with the most rows, while the code comment claimed a
    settled row cost no write.

    Measured in isolation, 10 re-stamps of one settled row:
        with a rationale (the old fixture)  ->  0 writes
        with none (what conjunctions pass)  -> 10 writes

    Third instance of a fixture that cannot produce the owner's state
    agreeing with the bug (§14 foreign keys off, §23 `g1` entity ids,
    §24 the 553-day cache).
    """

    #: Every shape a real builder passes. The conjunction builder and the
    #: drift builder pass no rationale; the hunt passes its chain. Both
    #: are exercised, so neither can be the only one covered again.
    SHAPES = (("no rationale, as conjunctions and drift pass", None),
              ("a rationale, as the hunt passes", "war -> freight -> XYZ"))

    @pytest.mark.parametrize("label,rationale", SHAPES,
                             ids=[s[0] for s in SHAPES])
    def test_re_stamping_an_unchanged_row_costs_nothing(
            self, conn, label, rationale):
        candidate(conn, "conj-x")
        rats = {"conj-x": rationale} if rationale else {}
        S._record_origin(conn, [Cand("conj-x")], "conjunction", rats, NOW)

        before = conn.total_changes
        for _ in range(10):
            S._record_origin(conn, [Cand("conj-x")], "conjunction",
                             rats, NOW)
        assert conn.total_changes == before, (
            f"{label}: ten re-stamps of a settled row cost "
            f"{conn.total_changes - before} write(s). This runs over every "
            "candidate every fifteen minutes")

    def test_a_rationale_arriving_LATER_is_still_recorded(self):
        """What the `IS NULL` test was written for, and it must survive
        the tighter clause: a candidate stamped with no reason, then
        re-nominated by a builder that has one."""
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            c = init_db(str(pathlib.Path(d) / "later.db"))
            try:
                candidate(c, "conj-x")
                S._record_origin(c, [Cand("conj-x")], "conjunction",
                                 {}, NOW)
                assert c.execute(
                    "SELECT rationale FROM candidate_origin "
                    "WHERE candidate_id='conj-x'").fetchone()[0] is None

                n = c.total_changes
                S._record_origin(c, [Cand("conj-x")], "conjunction",
                                 {"conj-x": "the chain"}, NOW)
                assert c.total_changes > n, (
                    "a rationale arriving later was not written - the "
                    "clause is now too tight")
                assert c.execute(
                    "SELECT rationale FROM candidate_origin "
                    "WHERE candidate_id='conj-x'").fetchone()[0] == "the chain"
            finally:
                c.close()

    def test_a_CHANGED_rationale_from_the_same_arm_is_written(self):
        """A builder is authoritative about its own candidate, so a
        fresher reason replaces an older one. COALESCE only ever protects
        against `None` erasing a real value."""
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            c = init_db(str(pathlib.Path(d) / "changed.db"))
            try:
                candidate(c, "hunt-x")
                S._record_origin(c, [Cand("hunt-x")], "hunt",
                                 {"hunt-x": "first read"}, NOW)
                S._record_origin(c, [Cand("hunt-x")], "hunt",
                                 {"hunt-x": "second read"}, NOW)
                assert c.execute(
                    "SELECT rationale FROM candidate_origin "
                    "WHERE candidate_id='hunt-x'").fetchone()[0] \
                    == "second read"
            finally:
                c.close()
