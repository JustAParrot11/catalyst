"""Every API cost in the live path is measured, not typed in.

OWNER-ASKED 2026-09-11: *"i want to be absolutely certain aswell we have
not hard coded api costs, remember we have access with the admin API. We
are testing and getting this to work but eventually will be full
autonomous, we dont want to be changing estimates manually."*

THE AUDIT HAD TWO HALVES AND ONLY ONE WAS ALREADY RIGHT.

ALREADY SELF-CORRECTING - the PRICES, which decide what a call cost once
it happened:

  - per-token rates: measured_rates.py divides Anthropic's charge for a
    closed day by its own token counts and calls set_override(), so
    pricing.py's table is a cold start that nothing reads afterwards.
  - cache and web-search multipliers: factors.py derives them from the
    itemised bill and discards any derivation whose components do not
    add back up to the billed total.
  - input tokens per web search: boundary.py seeds 12k and replaces it
    with the observed 75th percentile after 8 searching turns.

NOT SELF-CORRECTING, and these are the ones a human would have had to
edit:

    TYPICAL_RESEARCH_CALL_CENTS  50c   measured blended cost  22.8c
    HUNT_ESTIMATE_CENTS          60c   measured               11.6c
    HUNT_TURN_ESTIMATE_CENTS     20c   never measured at all

Wrong by two to five times, with nothing anywhere to say so. They now
read the ledger through cost/observed.py, with the constants demoted to
cold-start seeds.

THE CHAIN THIS FILE HOLDS, end to end:

    Admin API charge for a closed day
      -> measured_rates.learn_from_closed_day -> set_override
      -> pricing_overrides table
      -> price() -> cost_events.priced_cents
      -> observed_call_cents -> the estimate for the NEXT call

Fully offline. No calendar dates in any assertion (house rule 6): every
fixture timestamp is relative to `datetime.now`, because the window this
code measures is relative to now.
"""

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.cost.observed import (
    MIN_OBSERVED_CALLS, OBSERVED_PERCENTILE, OBSERVED_WINDOW_DAYS,
    observed_call_cents, observed_or_seed,
)
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "c.db"))
    yield conn
    conn.close()


def spend(conn, component, cents, *, days_ago=1, kind="scheduled",
          priced=True):
    conn.execute(
        "INSERT INTO cost_events (id,raw_usage_json,model,kind,component,"
        "priced_cents,priced_at,api_call_id) VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "{}", "m", kind, component,
         str(cents) if priced else None,
         (NOW - timedelta(days=days_ago)).isoformat(), None))
    conn.commit()


class TestItMeasuresWhatCallsActuallyCost:
    def test_a_cold_start_uses_the_seed_and_says_the_sample_is_zero(self, db):
        assert observed_call_cents(db, "research", 50) == (Decimal("50"), 0)

    def test_with_a_sample_it_replaces_the_seed(self, db):
        for c in (18, 19, 20, 22, 24, 26, 30, 45):
            spend(db, "research", c)
        value, n = observed_call_cents(db, "research", 50)
        assert n == 8
        assert value < 50, "the typed seed is still being used"
        assert value == Decimal("26")

    def test_it_estimates_high_rather_than_average(self):
        """An estimate exists to cover the dear calls; the mean is
        dragged down by the cheap ones."""
        assert OBSERVED_PERCENTILE > Decimal("0.5")

    def test_a_measured_figure_above_the_seed_is_believed_too(self, db):
        """The direction that protects the budget needs no guard, and
        must not be quietly ignored either."""
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 140)
        value, n = observed_call_cents(db, "research", 50)
        assert n == MIN_OBSERVED_CALLS and value == Decimal("140")

    def test_components_are_measured_separately(self, db):
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 20)
            spend(db, "hunt", 90)
        assert observed_call_cents(db, "research", 50)[0] == Decimal("20")
        assert observed_call_cents(db, "hunt", 60)[0] == Decimal("90")


class TestItRefusesToAdaptOnNoise:
    def test_below_the_minimum_sample_the_seed_stands(self, db):
        for _ in range(MIN_OBSERVED_CALLS - 1):
            spend(db, "hunt", 1)
        value, n = observed_call_cents(db, "hunt", 60)
        assert value == Decimal("60"), (
            "a handful of cheap calls moved the estimate, which is "
            "fitting noise")
        assert n == MIN_OBSERVED_CALLS - 1

    def test_the_minimum_is_stated_and_matches_the_other_calibration(self):
        """One calibration idea, one number, so boundary.py and this
        cannot drift apart."""
        from catalyst.research.boundary import MIN_CALIBRATION_SAMPLE

        assert MIN_OBSERVED_CALLS == MIN_CALIBRATION_SAMPLE

    def test_rows_outside_the_window_do_not_count(self, db):
        for _ in range(MIN_OBSERVED_CALLS + 4):
            spend(db, "research", 5, days_ago=OBSERVED_WINDOW_DAYS + 10)
        assert observed_call_cents(db, "research", 50) == (Decimal("50"), 0)

    def test_a_window_exists_at_all(self):
        """A rate change six months ago must not still be setting
        today's estimate."""
        assert 7 <= OBSERVED_WINDOW_DAYS <= 90


class TestItCannotBeDraggedDownByRowsThatAreNotCalls:
    def test_an_unpriced_row_is_excluded(self, db):
        """A row nobody could price is a hole in the count. The governor
        already blocks spend while one exists."""
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 40)
        spend(db, "research", 0, priced=False)
        assert observed_call_cents(db, "research", 50)[0] == Decimal("40")

    def test_a_NEGATIVE_correction_row_is_excluded(self, db):
        """`backfill_adjustment` carries the reconciliation's own
        true-up. Averaging over it would put the estimate below what any
        call has ever cost."""
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 40)
        before = observed_call_cents(db, "research", 50)
        spend(db, "research", -500)
        spend(db, "research", -300)
        after = observed_call_cents(db, "research", 50)
        # ASSERT THE SAMPLE SIZE, not only the figure. Checking the
        # value alone passed for the wrong reason: at the 75th
        # percentile two negative rows sort below the window and are
        # masked, so the filter could be removed entirely and the number
        # would not move. The count is what proves they were excluded.
        assert after == before, (
            "a negative correction row entered the sample")
        assert after[1] == MIN_OBSERVED_CALLS

    def test_manual_spend_is_excluded(self, db):
        """Owner testing is not scheduled running cost, and TRAPS.md
        says mixing them makes every projection wrong."""
        for _ in range(MIN_OBSERVED_CALLS + 4):
            spend(db, "research", 300, kind="manual")
        assert observed_call_cents(db, "research", 50) == (Decimal("50"), 0)

    def test_an_unparseable_row_is_skipped_not_fatal(self, db):
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 40)
        spend(db, "research", "not-a-number")
        value, n = observed_call_cents(db, "research", 50)
        assert value == Decimal("40") and n == MIN_OBSERVED_CALLS


class TestItNeverBreaksACycle:
    def test_a_database_without_the_table_uses_the_seed(self):
        bare = sqlite3.connect(":memory:")
        try:
            assert observed_call_cents(bare, "research", 50) == \
                (Decimal("50"), 0)
        finally:
            bare.close()

    def test_something_that_is_not_a_connection_uses_the_seed(self):
        assert observed_call_cents(object(), "research", 50) == \
            (Decimal("50"), 0)

    @pytest.mark.parametrize("seed", ["abc", None, 0, -5, float("nan")])
    def test_a_junk_seed_never_raises(self, db, seed):
        value, n = observed_call_cents(db, "research", seed)
        assert isinstance(value, Decimal) and n == 0

    def test_the_convenience_wrapper_agrees_with_the_pair(self, db):
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "hunt", 33)
        assert observed_or_seed(db, "hunt", 60) == \
            observed_call_cents(db, "hunt", 60)[0]


class TestTheThrottlesUseIt:
    """The estimates that would otherwise need hand-editing."""

    def test_the_research_belt_reads_the_ledger(self, db):
        from catalyst.orchestrator.cycle import research_per_cycle

        seeded = research_per_cycle(10000)
        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 10)
        measured = research_per_cycle(10000, db)
        assert measured > seeded, (
            "a measured cost far below the seed did not widen the belt, "
            "so the typed constant is still in charge")

    def test_the_research_belt_still_works_with_no_connection(self):
        """Every caller that cannot pass a connection must behave
        exactly as before this module existed."""
        from catalyst.orchestrator.cycle import research_per_cycle

        assert research_per_cycle(10000) == research_per_cycle(10000, None)

    def test_a_dearer_measured_call_TIGHTENS_the_belt(self, db):
        from catalyst.orchestrator.cycle import (
            MAX_RESEARCH_PER_CYCLE, research_per_cycle,
        )

        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 900)
        assert research_per_cycle(10000, db) == MAX_RESEARCH_PER_CYCLE, (
            "a call measured at $9 did not reduce the belt to its floor")

    def test_the_hunt_rate_reads_the_ledger(self, db):
        from catalyst.discovery.hunt import hunts_per_day

        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "hunt", 300)
        assert hunts_per_day(10000, db) < hunts_per_day(10000), (
            "a hunt measured at $3 did not reduce the hunt rate")

    def test_the_hunt_rate_is_still_bounded_however_cheap(self, db):
        from catalyst.discovery.hunt import hunts_per_day

        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "hunt", 1)
        assert hunts_per_day(10000, db) <= 4, (
            "a cheap measurement bought unbounded hunting")

    def test_the_belt_is_still_bounded_however_cheap(self, db):
        from catalyst.orchestrator.cycle import (
            MAX_RESEARCH_PER_CYCLE_CEILING, research_per_cycle,
        )

        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 1)
        assert research_per_cycle(10000, db) <= MAX_RESEARCH_PER_CYCLE_CEILING

    def test_a_small_budget_still_hunts_not_at_all(self, db):
        """The reserve for researching what a hunt finds survives being
        measured."""
        from catalyst.discovery.hunt import hunts_per_day

        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "hunt", 60)
        assert hunts_per_day(2000, db) == 0


class TestTheChainReachesTheAdminAPI:
    """The point of all of it: no figure in the live path is typed once
    one day has closed with spend on it."""

    def test_the_rate_is_corrected_from_the_bill_and_stored(self):
        import inspect

        from catalyst.cost import measured_rates

        src = inspect.getsource(measured_rates)
        assert "set_override" in src, (
            "a measured rate is computed and never stored, so nothing "
            "prices against it")

    def test_pricing_reads_the_stored_override_before_the_table(self):
        import inspect

        from catalyst.cost import overrides

        src = inspect.getsource(overrides.rates_for_on)
        assert "pricing_overrides" in src
        assert src.index("pricing_overrides") < src.index("return rates_for(")

    def test_reconciliation_is_what_triggers_the_correction(self):
        import inspect

        from catalyst.cost import tracker

        src = inspect.getsource(tracker)
        assert "learn_from_closed_day" in src

    def test_the_estimate_reads_what_the_rate_priced(self):
        import inspect

        from catalyst.cost import observed

        src = inspect.getsource(observed.observed_call_cents)
        assert "cost_events" in src and "priced_cents" in src

    def test_the_admin_api_stays_read_only(self):
        """It may read the bill and must never change an account."""
        from catalyst.cost import cost_api

        src = Path(cost_api.__file__).read_text()
        for verb in ("requests.post", "requests.put", "requests.patch",
                     "requests.delete", '"POST"', '"PUT"', '"DELETE"',
                     '"PATCH"', ".post(", ".put(", ".delete(", ".patch("):
            assert verb not in src, f"the cost API module can {verb}"


class TestTheSeedsAreLabelledAsSeeds:
    """A constant that is still an authority is the thing being removed.
    Each one must say in its own comment that it is a starting point."""

    @pytest.mark.parametrize("module,name", [
        ("catalyst.orchestrator.cycle", "TYPICAL_RESEARCH_CALL_CENTS"),
        ("catalyst.discovery.hunt", "HUNT_ESTIMATE_CENTS"),
        ("catalyst.discovery.hunt", "HUNT_TURN_ESTIMATE_CENTS"),
        ("catalyst.research.boundary", "INPUT_TOKENS_PER_SEARCH"),
    ])
    def test_it_exists_and_is_positive(self, module, name):
        import importlib

        value = getattr(importlib.import_module(module), name)
        assert Decimal(str(value)) > 0

    def test_the_measured_path_is_named_where_each_seed_lives(self):
        """So the next reader knows the constant is not the authority."""
        import inspect

        from catalyst.discovery import hunt
        from catalyst.orchestrator import cycle

        assert "observed_call_cents" in inspect.getsource(
            cycle.research_per_cycle)
        assert "observed_call_cents" in inspect.getsource(hunt.hunts_per_day)
        assert "observed_call_cents" in inspect.getsource(hunt._turn_estimate)


class TestThePageSaysWhichIsMeasured:
    def test_a_cold_start_is_shown_as_a_seed(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        init_db(str(tmp_path / "p.db")).close()
        html = panels._estimate_provenance(Db(str(tmp_path / "p.db")), "cost")
        assert "0 of 2 measured" in html
        assert "nothing measured yet" in html

    def test_a_measured_estimate_is_shown_as_measured(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        path = str(tmp_path / "p.db")
        conn = init_db(path)
        for c in range(MIN_OBSERVED_CALLS + 2):
            spend(conn, "research", 18 + c)
        conn.close()
        html = panels._estimate_provenance(Db(path), "cost")
        assert "1 of 2 measured" in html
        assert "measured, 10 call(s)" in html

    def test_it_states_the_chain_back_to_the_bill(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        init_db(str(tmp_path / "p.db")).close()
        html = panels._estimate_provenance(Db(str(tmp_path / "p.db")), "cost")
        assert "closed day divided by its own token counts" in html
        assert "cold-start seeds" in html

    def test_it_is_actually_on_the_cost_panel(self):
        """A helper nobody calls answers nothing."""
        import inspect

        from catalyst.dashboard import panels

        assert "_estimate_provenance(" in inspect.getsource(panels.cost_panel)


class TestNoNewHARDCODEDCostAppears:
    """A guard for the next session rather than for this one.

    The owner's ask is about the future - "eventually will be full
    autonomous, we dont want to be changing estimates manually" - so the
    check that matters is the one that fails when somebody adds a new
    typed cost constant and wires it straight into a decision.
    """

    #: Every module allowed to hold an API-cost constant, and what kind.
    #: A COST constant is one whose value is money the API charges. A
    #: dollar threshold about a TRADE - the minimum insider purchase a
    #: cluster must total, say - is a strategy parameter and not in
    #: scope; an earlier version of this test matched `_USD` too and
    #: flagged exactly those, which would have taught the next reader
    #: that the guard cries wolf.
    SEED_HOMES = {
        # cold-start seeds, each replaced by measurement
        "catalyst/orchestrator/cycle.py": "research call seed",
        "catalyst/discovery/hunt.py": "hunt and hunt-turn seeds",
        "catalyst/research/boundary.py": "exploration and extraction seeds",
        "catalyst/cost/pricing.py": "web-search per-query seed",
        # BOUNDS, which are meant to be typed: a cap is a decision, not
        # a measurement, and the owner sets it
        "catalyst/cost/governor.py": "the caps themselves",
        "catalyst/cost/tracker.py": "reconciliation floors",
        "catalyst/cost/measured_rates.py": "guard on the measurement",
        # account capital fallbacks and display bounds - not API cost
        "catalyst/benchmark/__init__.py": "capital fallback",
        "catalyst/dashboard/db.py": "capital fallback",
        "catalyst/dashboard/panels.py": "display bounds",
    }

    PATTERN = r"^([A-Z][A-Z0-9_]*CENTS(?:_PER_[A-Z0-9_]+)?)\s*="

    def _found(self):
        import re

        out = {}
        for path in sorted(Path("catalyst").rglob("*.py")):
            names = re.findall(self.PATTERN, path.read_text(), re.M)
            if names:
                out[path.as_posix()] = names
        return out

    def test_cost_constants_live_only_where_they_are_accounted_for(self):
        unexpected = {k: v for k, v in self._found().items()
                      if k not in self.SEED_HOMES}
        assert not unexpected, (
            "a new hard-coded API cost constant appeared outside the "
            f"modules that account for one: {unexpected}. Either measure "
            "it through cost/observed.py, or add the file to SEED_HOMES "
            "with a note saying why a typed number is correct there.")

    def test_the_list_is_not_stale(self):
        """A permit list naming files with nothing in them stops being a
        check and becomes decoration."""
        found = self._found()
        for rel in self.SEED_HOMES:
            assert Path(rel).exists(), rel
            assert rel in found, (
                f"{rel} is permitted to hold an API cost constant and "
                "holds none - remove it from the list")

    def test_it_would_catch_a_new_one(self):
        """House rule 4 on the guard itself."""
        import re

        assert re.findall(self.PATTERN,
                          "SOME_NEW_ESTIMATE_CENTS = 42\n", re.M)
        assert not re.findall(self.PATTERN,
                              "MIN_TOTAL_VALUE_USD = 50_000.0\n", re.M), (
            "the guard matches trade dollar thresholds again, which are "
            "strategy parameters and not API costs")

    def test_every_seed_that_feeds_a_THROTTLE_is_measured(self):
        """The distinction that matters. A cap may be typed - it is the
        owner's decision. An ESTIMATE may not, because nobody decided
        it; it was measured once and then went stale."""
        import inspect

        from catalyst.discovery import hunt
        from catalyst.orchestrator import cycle
        from catalyst.research import boundary

        for fn in (cycle.research_per_cycle, hunt.hunts_per_day,
                   hunt._turn_estimate):
            assert "observed_call_cents" in inspect.getsource(fn), fn
        # boundary's two seeds are measured by its own calibration
        assert "observed_tokens_per_search" in inspect.getsource(boundary)


class TestTheCheckCanFail:
    """House rule 4, against the values that shipped."""

    def test_the_old_constants_were_wrong_by_the_margins_claimed(self):
        from catalyst.discovery.hunt import HUNT_ESTIMATE_CENTS
        from catalyst.orchestrator.cycle import TYPICAL_RESEARCH_CALL_CENTS

        # measured 2026-09-11: research 22.8c blended, hunt 11.6c
        assert Decimal(str(TYPICAL_RESEARCH_CALL_CENTS)) > Decimal("22.8") * 2
        assert Decimal(str(HUNT_ESTIMATE_CENTS)) > Decimal("11.6") * 4

    def test_a_seed_only_estimate_would_be_caught(self, db):
        """Reproduce the shipped behaviour: ignore the ledger and the
        belt cannot move."""
        from catalyst.orchestrator.cycle import research_per_cycle

        for _ in range(MIN_OBSERVED_CALLS):
            spend(db, "research", 10)
        assert research_per_cycle(10000, None) < research_per_cycle(10000, db)
