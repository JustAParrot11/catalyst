"""Half the research budget went to an arm that has never produced a view.

OWNER, 2026-09-11: "do we need to make this more aggressive ... find
more options and reasons to trade while remaining profitable".

MEASURED, per arm, from the owner's own 7-day window joined to the
pricing bundle. Paid research calls, spend, cost per call, and lifetime
directional views:

    arm              calls   spend   $/call   directional views
    conjunction      33      $9.15   0.277    0 of 89 EVER
    insider (screen) 23      $4.10   0.178    23 of 189
    earnings_drift   13      $2.45   0.189    0 of 13 (2 shorts)
    hunt              0        -       -      0 of 2

So the most expensive arm per call took 48% of the calls and 58% of the
money for a lifetime record of zero tradeable views, while the
BEST-GRADED arm on the bake-off - drift, 57.1% hit out of sample against
insider's 49.3%, 8.8% max drawdown against 41.2% - got thirteen calls.

THERE WAS NO JUDGEMENT BEHIND THAT. `fresh[:max_research]` took the
first six of a list built insider -> conjunctions -> drift -> hunt, so
allocation was by LIST POSITION, and conjunctions emit across fifteen
catalyst types. Whichever builder is most prolific wins the budget.

AND THE BUDGET IS FULLY COMMITTED, which is what makes it matter. On the
four days it was actually active the bot spent $13.98, $3.50 a day
against the $3.33 the $100 cap sustains - 105% of the rate, projecting
to $105 a month. Every conjunction call displaces a call on an arm that
converts; there is no spare money to grow into.

Fully offline. No calendar dates (house rule 6).
"""

import sqlite3
import uuid
from collections import Counter
from types import SimpleNamespace

import pytest

from catalyst.orchestrator.cycle import (
    ARM_PROBE_EVERY, ARM_PROBE_MIN_CALLS, ARM_ROTATION, arm_conversion,
    demoted_arms, interleave_by_arm,
)
from catalyst.storage import init_db


def cand(arm, i):
    return SimpleNamespace(id=f"{arm}#{i}", ticker=f"{arm[:4].upper()}{i}")


def mix(**spec):
    """(candidates, origins) for a per-arm count, in the order the
    builders really produce them: insider, conjunctions, drift, hunt."""
    order = ["screen", "conjunction", "earnings_drift", "hunt"]
    cands, origins = [], {}
    for arm in order + [a for a in spec if a not in order]:
        for i in range(spec.get(arm, 0)):
            c = cand(arm, i)
            cands.append(c)
            origins[c.id] = arm
    return cands, origins


#: The owner's window, rounded to the candidate counts on record:
#: 29 insider, ~70 conjunction across fifteen types, 32 drift, 8 hunt.
OWNERS_WINDOW = dict(screen=29, conjunction=70, earnings_drift=32, hunt=8)


class TestTheOwnersWindowWouldHaveBeenAllocatedDifferently:
    def test_the_old_order_gave_the_whole_cycle_to_one_arm(self):
        """House rule 4 in the same file: the shipped behaviour, so the
        tests below are measuring a change and not a tautology."""
        cands, origins = mix(**OWNERS_WINDOW)
        first_six = [origins[c.id] for c in cands[:6]]
        assert set(first_six) == {"screen"}, (
            "the fixture no longer reproduces list-position allocation")

    def test_now_every_arm_present_gets_a_turn(self):
        cands, origins = mix(**OWNERS_WINDOW)
        got = interleave_by_arm(cands, origins)
        assert len({origins[c.id] for c in got[:4]}) == 4

    def test_the_best_graded_arm_goes_first(self):
        """Drift: 57.1% out of sample against insider's 49.3%, 8.8% max
        drawdown against 41.2%."""
        cands, origins = mix(**OWNERS_WINDOW)
        got = interleave_by_arm(cands, origins)
        assert origins[got[0].id] == "earnings_drift"

    def test_the_hunt_is_never_crowded_out_again(self):
        """It got zero paid calls in the window. It produces a handful
        of candidates, so in a round-robin it cannot be starved."""
        cands, origins = mix(**OWNERS_WINDOW)
        got = interleave_by_arm(cands, origins)
        assert "hunt" in {origins[c.id] for c in got[:4]}

    def test_the_zero_for_89_arm_drops_from_half_the_calls_to_a_probe(self):
        cands, origins = mix(**OWNERS_WINDOW)
        got = interleave_by_arm(cands, origins, demoted={"conjunction"})
        share = Counter(origins[c.id] for c in got[:12])
        assert share["conjunction"] <= 12 // ARM_PROBE_EVERY + 1
        assert share["earnings_drift"] >= 3 and share["screen"] >= 3

    def test_no_candidate_is_ever_discarded(self):
        """Reordering only. A candidate pushed past the belt returns on
        the next cycle; one dropped here is gone for good."""
        cands, origins = mix(**OWNERS_WINDOW)
        for demoted in (set(), {"conjunction"}, set(ARM_ROTATION)):
            got = interleave_by_arm(cands, origins, demoted=demoted)
            assert sorted(c.id for c in got) == sorted(c.id for c in cands)

    def test_an_arm_alone_on_probation_still_gets_researched(self):
        """On a quiet cycle a demoted arm is the only thing there is,
        and a probe share must not become an exclusion."""
        cands, origins = mix(conjunction=5)
        got = interleave_by_arm(cands, origins, demoted={"conjunction"})
        assert [c.id for c in got] == [c.id for c in cands]

    def test_order_within_an_arm_is_preserved(self):
        cands, origins = mix(**OWNERS_WINDOW)
        got = interleave_by_arm(cands, origins, demoted={"conjunction"})
        for arm in OWNERS_WINDOW:
            seen = [c.id for c in got if origins[c.id] == arm]
            assert seen == sorted(seen, key=lambda x: int(x.split("#")[1]))


class TestItIsActuallyWiredIn:
    """A rotation nobody calls is a rotation that changes nothing.

    This project has already shipped exactly that once - the adaptation
    loop was a helper no caller reached for a week - so the wiring gets
    its own tests rather than being assumed from the unit tests above.
    """

    def test_run_cycle_reorders_before_it_slices(self):
        import inspect

        from catalyst.orchestrator import cycle

        src = inspect.getsource(cycle.run_cycle)
        assert "interleave_by_arm(" in src, (
            "the rotation is never called, so allocation is still by "
            "list position")
        assert "demoted_arms(" in src
        # The order matters: reordering AFTER the slice would reorder the
        # six that were already chosen and change nothing at all.
        assert src.index("interleave_by_arm(") < src.index(
            "fresh[max_research:]")

    def test_the_slice_still_bounds_the_cycle(self):
        """Reordering must not have quietly widened the belt: the
        governor and this slice are what bound spend per cycle."""
        import inspect

        from catalyst.orchestrator import cycle

        src = inspect.getsource(cycle.run_cycle)
        assert "fresh[:max_research]" in src

    def test_the_conjunction_arm_is_stamped_as_its_own_origin(self, tmp_path):
        """Without a separate stamp every mechanical candidate is
        'screen', every arm looks identical, and the whole allocation is
        inert however well the rotation works.

        RE-PINNED 2026-09-15. This asserted the conjunction stamp appeared
        EARLIER IN THE SOURCE than the blanket sweep, because with
        `INSERT OR IGNORE` everywhere the ordering was the only thing
        keeping the right origin. It is not any more: a specific arm
        upserts and the blanket sweep cannot take a claimed candidate, so
        the ordering is no longer the mechanism and asserting it broke on
        a change that makes the property STRONGER. The property is
        asserted directly now - run the sweep second and see whose stamp
        survives - which also covers the case source order never could: a
        candidate the sweep reached FIRST, on an earlier cycle.
        """
        import inspect
        from datetime import datetime, timezone

        from catalyst.orchestrator import scheduler
        from catalyst.storage import init_db

        src = inspect.getsource(scheduler)
        assert '"conjunction"' in src, (
            "conjunctions are not stamped, so they cannot be told apart "
            "from insider clusters and cannot be held to their record")

        class C:
            id = "conj-0123456789abcdef0123"

        now = datetime.now(timezone.utc)
        conn = init_db(str(tmp_path / "stamp.db"))
        try:
            conn.execute(
                "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                (C.id, "CHYM", "credit_amendment", now.date().isoformat(),
                 "estimated", "[]", now.isoformat(), "fin", "[]"))
            conn.commit()

            def stamped():
                return conn.execute(
                    "SELECT origin FROM candidate_origin WHERE "
                    "candidate_id = ?", (C.id,)).fetchone()[0]

            # The sweep running after the arm cannot take it.
            scheduler._record_origin(conn, [C], "conjunction", None, now)
            scheduler._record_origin(conn, [C], scheduler.BLANKET_ORIGIN,
                                     {}, now)
            assert stamped() == "conjunction"

            # And the sweep having got there FIRST on an earlier cycle
            # does not make it permanent, which source order cannot say.
            conn.execute("DELETE FROM candidate_origin")
            conn.commit()
            scheduler._record_origin(conn, [C], scheduler.BLANKET_ORIGIN,
                                     {}, now)
            assert stamped() == scheduler.BLANKET_ORIGIN
            scheduler._record_origin(conn, [C], "conjunction", None, now)
            assert stamped() == "conjunction"
        finally:
            conn.close()

    def test_every_rotation_name_is_an_origin_something_really_writes(self):
        """Classified by the rule, not by enumeration: a name in the
        rotation that no builder ever stamps is a silent no-op."""
        import inspect

        from catalyst.orchestrator import cycle, scheduler

        src = inspect.getsource(scheduler)
        for arm in cycle.ARM_ROTATION:
            assert f'"{arm}"' in src, (
                f"{arm!r} is in the rotation but nothing stamps it")


class TestDemotionIsMeasuredNotAsserted:
    def test_the_conjunction_record_earns_a_probe_share(self):
        assert demoted_arms({"conjunction": (89, 0)}) == {"conjunction"}

    def test_a_new_arm_is_not_punished_for_being_new(self):
        """The hunt sits at 2 paid calls. Zero views out of two is not
        evidence of anything, and demoting it would silence the half of
        discovery the owner most asked for."""
        assert demoted_arms({"hunt": (2, 0)}) == set()

    def test_one_view_is_enough_to_keep_a_full_share(self):
        """Drift has produced two directional views. They were shorts a
        cash account cannot take, but the arm demonstrably turns money
        into a directional call, which is what is being measured."""
        assert demoted_arms({"earnings_drift": (13, 2)}) == set()

    def test_the_sample_minimum_is_stated_and_defensible(self):
        """At the insider arm's measured 12.2% conversion, zero views in
        40 calls has probability 0.878^40 ~ 0.6%."""
        assert ARM_PROBE_MIN_CALLS >= 40
        assert 0.878 ** ARM_PROBE_MIN_CALLS < 0.01

    def test_just_under_the_minimum_is_not_demoted(self):
        assert demoted_arms(
            {"x": (ARM_PROBE_MIN_CALLS - 1, 0)}) == set()
        assert demoted_arms({"x": (ARM_PROBE_MIN_CALLS, 0)}) == {"x"}

    def test_a_probe_is_a_share_and_not_an_exclusion(self):
        """An arm that can never be researched can never produce the
        view that would restore it, which is a permanent decision
        wearing a measurement's clothes."""
        assert ARM_PROBE_EVERY >= 2
        cands, origins = mix(conjunction=20, screen=20)
        got = interleave_by_arm(cands, origins, demoted={"conjunction"})
        assert any(origins[c.id] == "conjunction" for c in got[:8])

    def test_nothing_is_demoted_on_an_empty_or_broken_record(self):
        assert demoted_arms({}) == set()
        assert demoted_arms(None) == set()


class TestConversionIsReadFromTheRecord:
    @pytest.fixture
    def db(self, tmp_path):
        conn = init_db(str(tmp_path / "a.db"))
        yield conn
        conn.close()

    def seed(self, conn, arm, *, paid=0, skipped=0, longs=0, no_trades=0):
        for n in range(max(paid + skipped, longs + no_trades)):
            cid = f"{arm}-{n}-{uuid.uuid4().hex[:6]}"
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                         (cid, "AAA", "t", "2026-01-01", "confirmed", "[]",
                          "2026-01-01T00:00:00+00:00", "u", "[]"))
            conn.execute(
                "INSERT INTO candidate_origin (candidate_id, origin, "
                "rationale, nominated_at) VALUES (?,?,?,?)",
                (cid, arm, None, "2026-01-01T00:00:00+00:00"))
            if n < paid + skipped:
                conn.execute(
                    "INSERT INTO research_calls (id, candidate_id, model, "
                    "prompt_rendered, tools_offered, cost_cents, latency_ms, "
                    "skipped_reason, called_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), cid, "m", "p", "[]", "1", 1,
                     None if n < paid else "not_attempted: deferred",
                     "2026-01-01T00:00:00+00:00"))
            if n < longs + no_trades:
                conn.execute(
                    "INSERT INTO research_views VALUES (?,?,?,?,?,?,?,?)",
                    (cid, "long" if n < longs else "no_trade", 0.6, "t", "i",
                     12, 0, "w"))
        conn.commit()

    def test_it_counts_paid_calls_and_directional_views(self, db):
        self.seed(db, "conjunction", paid=5, no_trades=5)
        self.seed(db, "screen", paid=4, longs=2, no_trades=2)
        got = arm_conversion(db)
        assert got["conjunction"] == (5, 0)
        assert got["screen"] == (4, 2)

    def test_a_deferred_call_cost_nothing_and_does_not_count(self, db):
        """`not_attempted` rows never reached the API. Counting them
        would demote an arm for cycles it was never given."""
        self.seed(db, "conjunction", paid=1, skipped=50, no_trades=51)
        assert arm_conversion(db)["conjunction"][0] == 1
        assert demoted_arms(arm_conversion(db)) == set()

    def test_a_no_trade_is_not_a_directional_view(self, db):
        """The distinction the whole allocation rests on: a no_trade
        cost the same money and produced nothing a risk engine can act
        on."""
        self.seed(db, "conjunction", paid=ARM_PROBE_MIN_CALLS,
                  no_trades=ARM_PROBE_MIN_CALLS)
        assert arm_conversion(db)["conjunction"][1] == 0
        assert "conjunction" in demoted_arms(arm_conversion(db))

    def test_a_short_counts_because_it_is_a_directional_call(self, db):
        """Drift's only two views were shorts a cash account cannot
        take. The arm still demonstrably turns money into a directional
        call, and that is what this allocation measures - the engine
        refusing the side is a separate fact."""
        self.seed(db, "earnings_drift", paid=ARM_PROBE_MIN_CALLS,
                  no_trades=ARM_PROBE_MIN_CALLS)
        assert "earnings_drift" in demoted_arms(arm_conversion(db)), (
            "fixture sanity: all-no_trade should demote")
        cid = db.execute(
            "SELECT candidate_id FROM candidate_origin "
            "WHERE origin='earnings_drift'").fetchone()[0]
        db.execute("UPDATE research_views SET direction='short' "
                   "WHERE candidate_id=?", (cid,))
        db.commit()
        assert arm_conversion(db)["earnings_drift"][1] == 1
        assert demoted_arms(arm_conversion(db)) == set(), (
            "a single short restored nothing, so a two-sided arm can be "
            "demoted for producing exactly what it was built to produce")

    def test_it_survives_a_database_with_no_origin_table(self, tmp_path):
        conn = sqlite3.connect(str(tmp_path / "old.db"))
        try:
            assert arm_conversion(conn) == {}
            assert demoted_arms(arm_conversion(conn)) == set()
        finally:
            conn.close()


class TestTheConjunctionArmPaysTheSamePriceAsEveryoneElse:
    def test_its_search_allowance_is_no_longer_privileged(self):
        from catalyst.research.prompts import (
            BASE_SEARCHES, CONJUNCTION_SEARCHES,
        )

        assert CONJUNCTION_SEARCHES == BASE_SEARCHES

    def test_the_old_allowance_is_what_made_it_the_dearest_arm(self):
        """House rule 4: at ten searches it cost $0.277 a call against
        $0.178 for insider clusters, and the gap was the searches plus
        their results arriving as input tokens."""
        from catalyst.research.prompts import BASE_SEARCHES

        assert BASE_SEARCHES < 10, (
            "BASE_SEARCHES has moved; this file's arithmetic about the "
            "conjunction premium no longer describes the code")

    def test_a_conjunction_still_searches_as_much_as_anything_else(self):
        """Cut to the base allowance, not to zero. The arm keeps a real
        chance to produce the view that would restore its budget."""
        from catalyst.research.prompts import BASE_SEARCHES, searches_for

        signals = [SimpleNamespace(source="edgar"),
                   SimpleNamespace(source="federal_register")]
        assert searches_for(signals=signals) == BASE_SEARCHES
        assert searches_for(signals=signals) == searches_for(signals=None)

    def test_the_same_money_now_buys_more_calls(self):
        """$15.70 bought 69 calls at a blended $0.228. At the insider
        price it buys 88."""
        assert 15.70 / 0.178 > 69 * 1.25
