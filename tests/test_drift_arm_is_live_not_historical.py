"""The drift arm queued seven years of earnings for paid research.

OWNER'S BUNDLE, 2026-08-30 - the arm's first day live. It ran from
02:38 and by 06:00 had produced 6,293 candidates:

    A-2019-02-22-AMH-51        candidate_origin: earnings_drift
    A-2019-05-03-AAT-54        candidate_origin: earnings_drift
    A-2020-02-14-AAT-65        candidate_origin: earnings_drift
    ...
    6,316 x "not_attempted: deferred_max_research_per_cycle"

and in steady state was offering 810 of them every fifteen minutes.

WHY. `earnings_drift.build_events` replays every earnings event a
company has ever filed. That is exactly right for grading ten years of
history and exactly wrong for deciding what to buy this morning, and
the wiring called it directly. Nothing downstream filters on the
catalyst date - the hunt's validator refuses past dates, but the hunt
is the one path these did not come through, and risk/evaluate.py never
reads catalyst_date at all.

WHAT IT WOULD HAVE COST. The market was closed all weekend, so nothing
was researched and nothing was spent. On the next trading day the
research loop takes fresh[:max_research] every cycle - six a cycle,
~26 cycles - and the queue in front of it was 6,316 earnings reports
filed up to seven years ago. About $30 against a $10 daily ceiling,
spent before lunch, on trades that cannot exist.

THREE DEFECTS, one wiring:

  1. no freshness filter        -> MAX_EVENT_AGE_DAYS
  2. discovered_at=2016-01-01   -> a backtest sentinel that made every
                                   one of the 6,293 invisible to every
                                   dashboard window
  3. the candidate id held the  -> the same event came back under a new
     loop INDEX                    id whenever the universe grew, so
                                   "already_researched" never matched
                                   and it would be paid for twice

Fully offline. No network, no clock anchored to a calendar date
(house rule 6): every date here is derived from a fixed AS_OF that the
code under test is given explicitly.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from catalyst.data.sources.edgar_xbrl import (
    FACTS_REFRESH_DAYS, MAX_EVENT_AGE_DAYS, DriftLiveness,
    live_drift_candidates,
)
from catalyst.discovery import Candidate

AS_OF = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
TODAY = AS_OF.date()


def cand(filed: date, ticker="ABC", end=date(2026, 6, 30)) -> Candidate:
    """The shape earnings_drift.build_candidates emits, sentinel and all."""
    return Candidate(
        id=f"A-{filed.isoformat()}-{ticker}-{end.isoformat()}",
        ticker=ticker, catalyst_type="earnings_drift",
        catalyst_date=filed, catalyst_date_confidence="confirmed",
        source_event_ids=(f"xbrl:{ticker}:{end.isoformat()}",),
        discovered_at=datetime(2016, 1, 1, tzinfo=timezone.utc),
        sector="unknown", correlation_tags=("type:earnings_drift",))


@pytest.fixture
def patched(monkeypatch):
    """live_drift_candidates with the graded builder stubbed, so these
    tests measure the LIVENESS rule and nothing about XBRL parsing."""
    def install(cands):
        table = {c.id: object() for c in cands}
        monkeypatch.setattr(
            "catalyst.data.sources.edgar_xbrl.drift_candidates",
            lambda *a, **k: (list(cands), dict(table)))
        return table
    return install


class TestOnlyEventsInsideTheDriftWindowSurvive:
    def test_the_2019_candidates_that_shipped_are_refused(self):
        """The literal ids from the owner's bundle."""
        cands = [cand(date(2019, 2, 22), "AMH"),
                 cand(date(2020, 2, 14), "AAT"),
                 cand(date(2022, 5, 6), "AMH")]
        live, _table, stats = _run(cands)
        assert live == []
        assert stats.too_old == 3

    def test_a_filing_from_today_is_kept(self):
        live, _t, stats = _run([cand(TODAY)])
        assert [c.catalyst_date for c in live] == [TODAY]
        assert stats.too_old == 0

    def test_the_oldest_day_inside_the_window_is_kept(self):
        edge = TODAY - timedelta(days=MAX_EVENT_AGE_DAYS)
        live, _t, _s = _run([cand(edge)])
        assert len(live) == 1, "the boundary day must be inclusive"

    def test_one_day_past_the_window_is_refused(self):
        past = TODAY - timedelta(days=MAX_EVENT_AGE_DAYS + 1)
        live, _t, stats = _run([cand(past)])
        assert live == [] and stats.too_old == 1

    def test_a_friday_filing_still_qualifies_on_the_following_wednesday(self):
        """The window exists to survive a weekend, which is the whole
        reason it is not one or two days."""
        friday = date(2026, 8, 28)
        wednesday = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
        live, _t, _s = _run([cand(friday)], as_of=wednesday)
        assert len(live) == 1

    def test_a_future_filed_date_is_refused_and_counted(self):
        """A filing dated tomorrow means the cache or the clock is
        wrong. Refusing is the safe direction; counting it is house
        rule 3."""
        live, _t, stats = _run([cand(TODAY + timedelta(days=1))])
        assert live == [] and stats.in_the_future == 1

    def test_the_mix_is_split_and_not_merely_truncated(self):
        cands = [cand(date(2019, 2, 22), "AMH"), cand(TODAY, "EMBC"),
                 cand(TODAY - timedelta(days=2), "ZNB"),
                 cand(date(2021, 11, 5), "AAT")]
        live, _t, stats = _run(cands)
        assert {c.ticker for c in live} == {"EMBC", "ZNB"}
        assert (stats.built, stats.live, stats.too_old) == (4, 2, 2)


class TestTheSignalTableFollowsTheCandidates:
    def test_every_surviving_candidate_keeps_its_table_entry(self, patched):
        """make_signal_fn looks the event up by candidate id; a
        candidate whose entry was dropped returns no_trade forever."""
        cands = [cand(TODAY, "EMBC"), cand(date(2019, 1, 1), "AMH")]
        table = patched(cands)
        live, out_table, _s = live_drift_candidates("d", ["EMBC"], AS_OF)
        assert len(live) == 1
        assert out_table[live[0].id] is table[live[0].id]

    def test_the_refused_ones_are_not_carried_in_the_table(self, patched):
        patched([cand(TODAY, "EMBC"), cand(date(2019, 1, 1), "AMH")])
        _live, out_table, _s = live_drift_candidates("d", ["EMBC"], AS_OF)
        assert len(out_table) == 1


class TestTheDiscoveryTimestampIsReal:
    def test_the_2016_sentinel_is_replaced_with_the_cycle_time(self):
        """Every window on the candidates table is keyed on
        discovered_at. With the sentinel the owner reads "no candidates
        today" while the queue fills."""
        live, _t, _s = _run([cand(TODAY)])
        assert live[0].discovered_at == AS_OF

    def test_nothing_else_about_the_candidate_changes(self):
        original = cand(TODAY)
        live, _t, _s = _run([original])
        got = live[0]
        assert (got.id, got.ticker, got.catalyst_type, got.catalyst_date,
                got.source_event_ids, got.correlation_tags) == (
            original.id, original.ticker, original.catalyst_type,
            original.catalyst_date, original.source_event_ids,
            original.correlation_tags)


class TestTheIdDoesNotMoveWhenTheUniverseGrows:
    """Defect 3. The id was the loop index, so adding one company
    renumbered every event filed after it."""

    def test_the_same_event_keeps_its_id_as_more_companies_are_cached(self):
        from catalyst.strategies.earnings_drift import EarningsEvent, build_candidates

        def ev(ticker, filed, end):
            return EarningsEvent(ticker=ticker, filed=filed, period_end=end,
                                 value=1.0, sue=2.0, form="10-Q")

        target = ev("EMBC", date(2026, 8, 28), date(2026, 6, 30))
        small, _ = build_candidates([target])
        large, _ = build_candidates(
            [ev("AAA", date(2019, 1, 1), date(2018, 12, 31)),
             ev("BBB", date(2020, 1, 1), date(2019, 12, 31)), target])
        assert small[0].id == large[-1].id, (
            "the id moved when the universe grew, so cycle.py cannot see "
            "that this candidate was already researched and pays again")

    def test_two_different_quarters_of_one_company_still_differ(self):
        from catalyst.strategies.earnings_drift import EarningsEvent, build_candidates

        cands, _ = build_candidates([
            EarningsEvent("EMBC", date(2026, 8, 28), date(2026, 6, 30),
                          1.0, 2.0, "10-Q"),
            EarningsEvent("EMBC", date(2026, 5, 8), date(2026, 3, 31),
                          1.0, 2.0, "10-Q")])
        assert len({c.id for c in cands}) == 2

    def test_the_id_carries_no_list_position(self):
        from catalyst.strategies.earnings_drift import EarningsEvent, build_candidates

        cands, _ = build_candidates([
            EarningsEvent("EMBC", date(2026, 8, 28), date(2026, 6, 30),
                          1.0, 2.0, "10-Q")])
        assert cands[0].id == "A-2026-08-28-EMBC-2026-06-30"


class TestTheRefreshCadenceCannotStrandTheWindow:
    def test_facts_are_refreshed_faster_than_the_window_closes(self):
        """THE JOINT RULE. A filing is invisible until its company's
        cache is refreshed. Refresh slower than the window and a real
        filing goes stale before it is ever seen, and the arm produces
        nothing while looking perfectly healthy."""
        assert FACTS_REFRESH_DAYS < MAX_EVENT_AGE_DAYS, (
            f"facts refresh every {FACTS_REFRESH_DAYS} days but a filing "
            f"is only tradeable for {MAX_EVENT_AGE_DAYS}; a filing can now "
            "expire before the cache that would reveal it is refreshed")

    def test_there_is_room_for_a_weekend_inside_that_gap(self):
        assert MAX_EVENT_AGE_DAYS - FACTS_REFRESH_DAYS >= 3


class TestTheOrchestratorUsesTheLiveOne:
    """The rule, not the instance: the plain builder must never be the
    one the cycle calls, whoever wires the next arm."""

    def test_the_scheduler_calls_live_drift_candidates(self):
        src = _scheduler_source()
        assert "live_drift_candidates" in src

    def test_the_scheduler_does_not_call_the_historical_builder(self):
        import re

        src = _scheduler_source()
        bare = re.findall(r"(?<!live_)drift_candidates\s*\(", src)
        assert not bare, (
            "scheduler.py calls drift_candidates() directly; that replays "
            "the whole XBRL history into the research queue")


class TestTheEmptyCaseExplainsItself:
    """House rule 3: a zero never stands alone."""

    def test_no_events_at_all_says_so(self):
        _l, _t, stats = _run([])
        assert "no surprise events at all" in stats.why_empty()

    def test_all_too_old_names_the_newest_it_saw(self):
        _l, _t, stats = _run([cand(date(2019, 2, 22), "AMH"),
                              cand(date(2021, 6, 1), "AAT")])
        why = stats.why_empty()
        assert "2021-06-01" in why and "drift window has closed" in why

    def test_a_live_candidate_produces_no_excuse(self):
        _l, _t, stats = _run([cand(TODAY)])
        assert stats.why_empty() == ""


class TestItNeverRaises:
    def test_a_broken_builder_is_a_quiet_arm_not_a_dead_cycle(self, monkeypatch):
        monkeypatch.setattr(
            "catalyst.data.sources.edgar_xbrl.drift_candidates",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            live_drift_candidates("d", ["X"], AS_OF)

    def test_the_real_builder_swallows_its_own_failures(self, tmp_path):
        """drift_candidates is the one that must not raise, and it is
        what the live wrapper calls - a missing directory is the
        ordinary cold-start case."""
        live, table, stats = live_drift_candidates(
            str(tmp_path / "nope"), ["EMBC"], AS_OF)
        assert (live, table, stats.built) == ([], {}, 0)


class TestTheCheckCanFail:
    """House rule 4, against the exact code that shipped."""

    def test_an_unfiltered_arm_would_be_caught(self):
        cands = [cand(date(2019, 2, 22), "AMH")]
        stats = DriftLiveness(built=1, live=1, max_age_days=MAX_EVENT_AGE_DAYS)
        assert stats.why_empty() == "", "sanity: a live count silences it"
        live, _t, real = _run(cands)
        assert (live, real.live) == ([], 0)

    def test_the_scheduler_scan_can_see_a_bare_call(self):
        import re

        sabotaged = "x = drift_candidates(facts_dir, names)\n"
        assert re.findall(r"(?<!live_)drift_candidates\s*\(", sabotaged), (
            "the scan cannot see a bare call, so it would not have caught "
            "the wiring that shipped")

    def test_the_scan_does_not_fire_on_the_live_call(self):
        import re

        ok = "a, b, c = live_drift_candidates(facts_dir, names, as_of)\n"
        assert not re.findall(r"(?<!live_)drift_candidates\s*\(", ok)


# --------------------------------------------------------------- helpers

def _run(cands, *, as_of=AS_OF):
    """live_drift_candidates over a fixed candidate list."""
    import catalyst.data.sources.edgar_xbrl as mod

    original = mod.drift_candidates
    mod.drift_candidates = lambda *a, **k: (list(cands),
                                            {c.id: object() for c in cands})
    try:
        return mod.live_drift_candidates("dir", ["X"], as_of)
    finally:
        mod.drift_candidates = original


def _scheduler_source() -> str:
    from pathlib import Path

    import catalyst.orchestrator.scheduler as sch

    return Path(sch.__file__).read_text()
