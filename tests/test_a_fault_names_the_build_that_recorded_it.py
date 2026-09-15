"""A recorded fault must say whether the running code still has it.

OWNER-REPORTED 2026-09-15, the day after the tool_result fix shipped:
"did you publish, ive got the same warning".

THE FIX WAS PUBLISHED AND THE WARNING WAS STILL CORRECT TO SHOW. The
funnel records a skipped research call as a row, and a row does not
disappear when the defect behind it is fixed - deliberately, because "a
fault that disappears silently cannot be told apart from one that never
happened". What the page could NOT say is the one thing that settles it:

    is this something the code running NOW would hit again, or something
    a commit this machine no longer runs did once?

`_fault_age` already said "N research calls have succeeded since without
hitting it. That is not proof it is fixed, only that it has not
recurred" - honest, and it cannot answer the question. Absence of
recurrence is not evidence of a fix; a CHANGE OF CODE is.

So the build is recorded against every research call, in a side table
(research_calls is written with positional INSERTs in several places),
and a FAULT no running build recorded is filed behind the disclosure the
same day rather than after three.

THE ASYMMETRY THAT MATTERS: saying a live fault is settled hides a real
problem, saying a settled one is live is only annoying. So every
uncertain reading goes the noisy way - no build recorded, a dirty
checkout, or a missing side table all keep the fault in NEEDS ATTENTION.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import catalyst
from catalyst.dashboard import panels, queries
from catalyst.dashboard.db import Db
from catalyst.dashboard.queries import (
    BUILD_NOT_RECORDED,
    BUILD_RUNNING,
    BUILD_SUPERSEDED,
    BUILD_UNCOMPARABLE,
    BUILD_UNSETTLED,
    build_verdict,
)
from catalyst.discovery import Candidate
from catalyst.research import prompts
from catalyst.research.boundary import CostContext, investigate
from catalyst.storage import init_db

#: The owner's own fault line, so the reproduction is theirs.
OWNER_FAULT = (
    "invalid_request_not_sent: message 3 calls tool_use "
    "toolu_015PG6wEFAvX7XHwsnfiWjXD and the next message carries no "
    "matching tool_result - the API rejects this outright")

#: NEVER a calendar date (house rule 6): everything is positioned
#: against the clock the code measures itself against.
NOW = datetime.now(timezone.utc)

RUNNING = "beefcafe1234"
OLDER = "0000deadbeef"

USAGE = {"input_tokens": 1000, "output_tokens": 500,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


@pytest.fixture(autouse=True)
def clean_running_build(monkeypatch):
    """A DEVELOPMENT CHECKOUT IS DIRTY, and a dirty build is deliberately
    uncomparable - so without pinning this, every test here would
    exercise one branch and agree with any bug in the other three."""
    monkeypatch.setattr(catalyst, "__build__", RUNNING)


def _seeded(tmp_path, name, build, *, ok_calls=0, age_days=0,
            reason=OWNER_FAULT):
    """A database through `init_db`, so foreign keys are ON exactly as
    they are in production (WHAT-WE-TRIED section 14: a fixture that
    differs from production in a way the code depends on will agree with
    a bug)."""
    path = str(tmp_path / name)
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 ("cand-1", "RLMD", "insider_cluster", "2026-09-10",
                  "confirmed", "[]", "insider_cluster", "{}",
                  NOW.isoformat()))
    conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                 ("call-fault", "cand-1", "m", "p", "[]", "0", 1, reason,
                  (NOW - timedelta(days=age_days)).isoformat()))
    if build is not None:
        conn.execute("INSERT INTO research_call_builds VALUES (?,?)",
                     ("call-fault", build))
    later = (NOW + timedelta(minutes=5)).isoformat()
    for i in range(ok_calls):
        conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"ok-{i}", "AAPL", "insider_cluster", "2026-09-10",
                      "confirmed", "[]", "insider_cluster", "{}",
                      NOW.isoformat()))
        conn.execute("INSERT INTO research_calls VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"call-ok-{i}", f"ok-{i}", "m", "p", "[]", "1", 1,
                      None, later))
    conn.commit()
    conn.close()
    return Db(path)


def _researched_stage(db):
    return next(s for s in queries.funnel(db).stages
                if s.key == "researched")


def _where_is_the_fault(db, needle="invalid_request"):
    """(in NEEDS ATTENTION, filed as history, the sentence beside it)."""
    st = _researched_stage(db)
    cur = [d for r, _, d in st.drops if needle in r]
    old = [d for r, _, d in st.stale_drops if needle in r]
    assert len(cur) + len(old) == 1, (st.drops, st.stale_drops)
    return bool(cur), bool(old), (cur + old)[0]


class TestTheVerdictItself:
    def test_the_running_build_recorded_it_so_it_is_live(self):
        assert build_verdict({RUNNING}, RUNNING) == BUILD_RUNNING

    def test_a_build_no_longer_running_is_superseded(self):
        assert build_verdict({OLDER}, RUNNING) == BUILD_SUPERSEDED

    def test_one_occurrence_from_the_running_build_is_enough_to_be_live(self):
        """A reason recurs. If ANY of its occurrences came from the code
        running now, the code running now produces it."""
        assert build_verdict({OLDER, RUNNING}, RUNNING) == BUILD_RUNNING

    def test_no_build_recorded_is_NOT_a_different_build(self):
        assert build_verdict(set(), RUNNING) == BUILD_NOT_RECORDED
        assert build_verdict({""}, RUNNING) == BUILD_NOT_RECORDED
        assert build_verdict({None}, RUNNING) == BUILD_NOT_RECORDED

    def test_a_DIRTY_checkout_can_never_settle_anything(self):
        """`abc+dirty` names a commit PLUS edits git cannot see, so it
        can never equal a recorded `abc` even when the defect is still
        there word for word - which is exactly the reading where
        "the code has changed since" would talk the reader out of a
        live fault."""
        dirty = OLDER + catalyst.DIRTY_SUFFIX
        assert build_verdict({OLDER}, dirty) == BUILD_UNCOMPARABLE
        assert build_verdict({RUNNING}, dirty) == BUILD_UNCOMPARABLE

    def test_an_unknown_running_build_settles_nothing_either(self):
        assert build_verdict({OLDER}, "") == BUILD_UNCOMPARABLE
        assert build_verdict({OLDER}, None) == BUILD_UNCOMPARABLE

    def test_only_superseded_is_outside_the_unsettled_set(self):
        """The classification the panel branches on, asserted as a
        property rather than by listing the same names twice."""
        assert BUILD_SUPERSEDED not in BUILD_UNSETTLED
        for verdict in (BUILD_RUNNING, BUILD_NOT_RECORDED,
                        BUILD_UNCOMPARABLE):
            assert verdict in BUILD_UNSETTLED


class TestWhereTheFaultIsFiled:
    def test_the_running_build_recorded_it_so_it_needs_attention(
            self, tmp_path):
        live, history, said = _where_is_the_fault(
            _seeded(tmp_path, "a.db", RUNNING, ok_calls=3))
        assert live and not history
        assert "the code running now" in said
        assert RUNNING in said

    def test_a_superseded_fault_is_history_THE_SAME_DAY(self, tmp_path):
        """THE OWNER'S CASE. Waiting for the three-day window is what
        showed them the same warning the day after the fix shipped."""
        live, history, said = _where_is_the_fault(
            _seeded(tmp_path, "b.db", OLDER, ok_calls=3))
        assert history and not live
        assert OLDER in said and RUNNING in said
        assert "the code has changed since" in said

    def test_a_superseded_fault_is_history_even_with_nothing_since(
            self, tmp_path):
        """No successful call behind it at all. The build evidence is
        about the CODE, so it does not need a call to confirm it."""
        live, history, said = _where_is_the_fault(
            _seeded(tmp_path, "c.db", OLDER, ok_calls=0))
        assert history and not live

    def test_an_unrecorded_build_keeps_the_fault_visible(self, tmp_path):
        """Every row on the owner's database today is in this state.
        Unknown is not settled, and the sentence says which it is."""
        live, history, said = _where_is_the_fault(
            _seeded(tmp_path, "d.db", None, ok_calls=3))
        assert live and not history
        assert "was not stored" in said

    def test_a_dirty_checkout_keeps_the_fault_visible(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setattr(catalyst, "__build__",
                            OLDER + catalyst.DIRTY_SUFFIX)
        live, history, said = _where_is_the_fault(
            _seeded(tmp_path, "e.db", OLDER, ok_calls=3))
        assert live and not history
        assert "uncommitted changes" in said
        assert "was not stored" not in said, (
            "the build WAS stored - saying otherwise sends the reader to "
            "fix the wrong thing")

    def test_a_missing_side_table_cannot_empty_the_drop_list(self, tmp_path):
        """AN OLDER DATABASE. Read as a join, a table that is not there
        comes back as an error with no rows and takes the whole panel's
        contents with it - so missing provenance has to degrade to "not
        recorded", never to "nothing stopped here"."""
        db = _seeded(tmp_path, "f.db", RUNNING, ok_calls=3)
        conn = sqlite3.connect(db.path)
        conn.execute("DROP TABLE research_call_builds")
        conn.commit()
        conn.close()
        live, history, said = _where_is_the_fault(db)
        assert live and not history
        assert "was not stored" in said

    def test_a_routine_reason_is_never_given_a_build_sentence(
            self, tmp_path):
        """Routine attrition recurs by design. Dating it as "history or
        live fault?" invites the reader to worry about the bot working
        correctly, which this project has already paid for twice."""
        db = _seeded(tmp_path, "g.db", OLDER,
                     reason="not_attempted: market_closed")
        st = _researched_stage(db)
        said = [d for r, _, d in st.drops + st.stale_drops
                if "market_closed" in r]
        assert said, (st.drops, st.stale_drops)
        assert "build" not in said[0].lower(), said[0]


class TestTheSentenceDoesNotContradictItself:
    def test_a_superseded_fault_is_not_also_called_live(self, tmp_path):
        """MY OWN DEFECT, found by rendering rather than by reading.
        Built from two independent halves, the line read "nothing has
        succeeded since, so treat it as live" immediately followed by
        "the code has changed since - filed as history"."""
        _, _, said = _where_is_the_fault(
            _seeded(tmp_path, "h.db", OLDER, ok_calls=0))
        assert "nothing has succeeded since" in said
        assert "treat it as live" not in said, said

    def test_no_evidence_in_any_direction_still_says_treat_it_as_live(
            self, tmp_path):
        """The noisy direction has to survive the rewrite: nothing
        recorded AND nothing succeeded since is the reading with no
        evidence at all."""
        _, _, said = _where_is_the_fault(
            _seeded(tmp_path, "i.db", None, ok_calls=0))
        assert "treat it as live" in said, said


class TestTheFilingDoesNotReadTheWORDING:
    def test_the_history_decision_survives_a_reworded_label(
            self, tmp_path, monkeypatch):
        """The old code decided the panel's contents with
        `"have succeeded since" in detail` - so rewording a label moved
        a fault between "act on this" and "history". Two tests in two
        days had already broken on a rewrite that improved the thing;
        this one asserts the classification ignores the words."""
        monkeypatch.setattr(queries, "_fault_age",
                            lambda *a, **k: "SOME OTHER WORDING ENTIRELY")
        # Outside the window, with successful calls behind it: settled.
        live, history, said = _where_is_the_fault(
            _seeded(tmp_path, "j.db", None, ok_calls=3,
                    age_days=queries.FEED_FAULT_WINDOW_DAYS + 5))
        assert said == "SOME OTHER WORDING ENTIRELY", said
        assert history and not live

    def test_an_unsettled_fault_outside_the_window_still_needs_attention(
            self, tmp_path, monkeypatch):
        """The other half of the same property: old, but nothing has
        ever succeeded since, so it is not settled by anything."""
        monkeypatch.setattr(queries, "_fault_age",
                            lambda *a, **k: "SOME OTHER WORDING ENTIRELY")
        live, history, _ = _where_is_the_fault(
            _seeded(tmp_path, "k.db", None, ok_calls=0,
                    age_days=queries.FEED_FAULT_WINDOW_DAYS + 5))
        assert live and not history


class TestThePanelSaysWhyEachOneIsFiled:
    def test_the_disclosure_does_not_claim_the_three_day_window(
            self, tmp_path):
        """A superseded fault is filed the same day, so a summary
        reading "not seen for over 3 day(s)" would be FALSE for exactly
        the rows this disclosure exists to absorb."""
        db = _seeded(tmp_path, "l.db", OLDER, ok_calls=3)
        html = panels.funnel_panel(db, p="fn")
        assert "older reason(s), settled" in html
        summary = html.split("older reason(s), settled")[1][:200]
        assert f"{queries.FEED_FAULT_WINDOW_DAYS} day" not in summary, summary

    def test_the_disclosure_says_a_build_can_be_the_reason(self, tmp_path):
        db = _seeded(tmp_path, "m.db", OLDER, ok_calls=3)
        html = panels.funnel_panel(db, p="fn")
        assert "build this machine no longer runs" in html


class TestTheBuildIsActuallyRECORDED:
    """The write site, driven through the real `investigate` - because a
    verdict computed from a table nothing writes to is a verdict that
    always reads "not recorded" (sections 12, 22, 24: assert the
    outcome, never a substring in a function)."""

    @pytest.fixture
    def conn(self, tmp_path):
        c = init_db(str(tmp_path / "w.db"))
        c.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                  ("cand-1", "TEST", "insider_cluster", "2026-08-20",
                   "estimated", "[]", "insider_cluster", "{}",
                   NOW.isoformat()))
        c.commit()
        yield c
        c.close()

    @pytest.fixture(autouse=True)
    def stub_prompts(self, monkeypatch):
        monkeypatch.setattr(prompts, "render_research_prompt",
                            lambda c, **kw: f"research {c.ticker}")
        monkeypatch.setattr(
            prompts, "exploration_tools",
            lambda max_searches=3, **kw: [
                {"type": "web_search_20250305", "name": "web_search",
                 "max_uses": max_searches}])

    def _candidate(self):
        return Candidate(
            id="cand-1", ticker="TEST", catalyst_type="insider_cluster",
            catalyst_date=date(2026, 8, 20),
            catalyst_date_confidence="estimated", source_event_ids=("e1",),
            discovered_at=NOW, sector="tech", correlation_tags=("tech",))

    def _ctx(self, conn):
        return CostContext(conn=conn, governor_profit_share=Decimal("0.10"),
                           cycle_id="cycle-1", kind="scheduled")

    def _recorded(self, conn, call_id):
        row = conn.execute(
            "SELECT build FROM research_call_builds WHERE call_id = ?",
            (call_id,)).fetchone()
        return row[0] if row else None

    def test_a_SKIPPED_call_records_the_build(self, conn):
        """THE ONE THAT MATTERS. Every row the fault panel reads is a
        skipped call, so a write on the success path only would leave the
        whole feature reading "not recorded" forever."""
        def transport(_payload):
            raise AssertionError("must not be reached")

        # An empty prompt is refused by the pre-flight guard, so nothing
        # is sent and nothing is paid for - the same shape as the
        # owner's fault.
        prompts.render_research_prompt = lambda c, **kw: ""
        log = investigate(self._candidate(), self._ctx(conn), transport)
        assert log.skipped_reason is not None, log.skipped_reason
        assert self._recorded(conn, log.id) == RUNNING

    def test_a_successful_call_records_the_build_too(self, conn):
        view = {"direction": "long", "conviction": 0.8,
                "thesis": "cluster of buys", "invalidation": "insiders sell",
                "expected_holding_days": 12, "priced_in": False,
                "priced_in_reasoning": "no move since filing"}
        responses = [
            {"content": [{"type": "text", "text": "looked"}],
             "stop_reason": "end_turn", "usage": dict(USAGE)},
            {"content": [{"type": "tool_use", "id": "toolu_01AAAA",
                          "name": "submit_research_view", "input": view}],
             "stop_reason": "tool_use", "usage": dict(USAGE)},
        ]
        log = investigate(self._candidate(), self._ctx(conn),
                          lambda p: responses.pop(0))
        assert log.skipped_reason is None, log.skipped_reason
        assert self._recorded(conn, log.id) == RUNNING

    def test_the_audit_trail_outranks_the_provenance(self, conn):
        """A call that has ALREADY BEEN BILLED must land in
        research_calls even if the build row cannot be written. A paid
        call missing from the audit trail is the worse failure by a
        distance, and an unstamped row reads as "not recorded", which
        the page says out loud."""
        conn.execute("DROP TABLE research_call_builds")
        conn.commit()
        prompts.render_research_prompt = lambda c, **kw: ""
        log = investigate(self._candidate(), self._ctx(conn),
                          lambda p: (_ for _ in ()).throw(AssertionError()))
        got = conn.execute(
            "SELECT COUNT(*) FROM research_calls WHERE id = ?",
            (log.id,)).fetchone()[0]
        assert got == 1


class TestTheDirtySuffixIsNotTypedTwice:
    def test_the_build_string_and_the_comparison_share_one_constant(self):
        """Two numbers meaning the same thing, quietly disagreeing, is
        this project's most repeated defect. The suffix the build string
        appends and the suffix the comparison looks for are one value."""
        import pathlib
        src = pathlib.Path(catalyst.__file__).read_text()
        assert src.count('"+dirty"') == 1, (
            "the literal appears more than once, so one copy can drift")
        assert 'DIRTY_SUFFIX if dirty' in src


class TestTheBundleCanReproduceTheVerdict:
    def test_the_logic_scope_carries_the_build_table(self):
        """A bundle showing a fault called settled, with nothing anywhere
        saying on what evidence, is the shape every "the numbers do not
        add up" report has taken."""
        from catalyst.dashboard.server import DIAGNOSTIC_SCOPES
        assert "research_call_builds" in DIAGNOSTIC_SCOPES["logic"]["tables"]
