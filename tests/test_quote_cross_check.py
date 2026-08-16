"""A second opinion on the one number everything descends from.

OWNER-ASKED: "I want to ensure all data is correct and validated so we
arent trading under false pretenses."

THE GAP THIS CLOSES, which I had stated as a limit rather than fixed:
every traded figure descends from ONE live Alpaca quote. If that quote
is wrong - wrong symbol, misplaced decimal, an unadjusted corporate
action - nothing would notice, because there is a single source and it
is believed. A single point of truth was holding up the entire position
size.

Yesterday's cached close cannot confirm today's price. It can refuse to
believe a hundredfold one.

THE ASYMMETRY IS THE WHOLE DESIGN, and it is what most of this file
tests. A stock CAN gap 40% on a readout, and refusing that would throw
away exactly the trades this bot exists to take - silently, which is the
failure the owner has objected to more than any other. So:

    within ±35%      normal, nothing said
    beyond ±35%      FLAGGED: passed through, recorded, shown
    beyond 5x, 1/5   REFUSED: not a price move, a broken number

Getting that backwards in the cautious direction costs every large-move
trade. Getting it backwards the other way sizes a position on a number
nobody checked. Both directions are tested.
"""

import csv
from datetime import date as dtdate, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from catalyst.data.quote_check import (
    FLAG_DEVIATION, REFUSE_RATIO, cross_check,
)
# The cycle tests' two autouse fixtures. The kill-switch clock is judged
# against the WALL clock at check time, so a cycle driven from outside
# that module trips `portfolio_state_stale` - a trap this suite has
# already been bitten by once.
from tests.test_cycle import (  # noqa: F401 - shared fixtures
    frozen_kill_switch_clock, stub_prompts,
)


@pytest.fixture
def bars(tmp_path):
    """A ticker whose last cached close is $37."""
    rows = [{"date": f"2026-08-{d:02d}", "open": "37", "high": "37",
             "low": "37", "close": "37", "volume": "1000000"}
            for d in range(1, 15)]
    with (tmp_path / "AAA.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return tmp_path


class TestItRefusesOnlyTheImpossible:
    @pytest.mark.parametrize("price,why", [
        ("370.00", "a decimal in the wrong place"),
        ("3.70", "a decimal the other way, or a wrong symbol"),
        ("0.37", "two decimals out"),
        ("3700.00", "a hundredfold error"),
    ])
    def test_a_broken_number_is_refused(self, bars, price, why):
        check = cross_check(Decimal(price), bars, "AAA")
        assert check.refused, f"{why} was accepted as a price"
        assert "REFUSED" in check.sentence

    @pytest.mark.parametrize("price", ["52.00", "48.00", "25.00", "24.10"])
    def test_a_real_large_move_is_NOT_refused(self, bars, price):
        """The expensive direction. A 40% gap on a clinical readout is
        the trade, not the error."""
        check = cross_check(Decimal(price), bars, "AAA")
        assert not check.refused, (
            f"${price} against a $37 close was refused - that is an "
            "ordinary event day and refusing it throws the trade away")

    def test_a_large_move_is_still_FLAGGED(self, bars):
        """Not refused, but not silent either."""
        check = cross_check(Decimal("52.00"), bars, "AAA")
        assert check.flagged
        assert "not refused" in check.sentence

    @pytest.mark.parametrize("price", ["37.00", "37.20", "36.10", "38.90"])
    def test_an_ordinary_day_says_nothing(self, bars, price):
        check = cross_check(Decimal(price), bars, "AAA")
        assert not check.flagged and not check.refused
        assert "Consistent" in check.sentence

    def test_the_thresholds_leave_room_for_a_real_event(self):
        """A flag threshold under a typical event move would fire on
        every trade this bot wants; a refuse ratio near it would block
        them."""
        assert FLAG_DEVIATION >= Decimal("0.25")
        assert REFUSE_RATIO >= Decimal("3")


class TestNotCheckedIsSaidNotAssumed:
    def test_no_history_reports_that_it_could_not_look(self, tmp_path):
        """"Nothing objected" and "nothing looked" are different facts,
        and only one of them is reassuring."""
        check = cross_check(Decimal("37"), tmp_path, "NOSUCH")
        assert not check.checked
        assert not check.refused, "an unchecked quote must not be refused"
        assert "could not be cross-checked" in check.sentence
        assert "only source" in check.sentence

    @pytest.mark.parametrize("junk", [
        "", "not a csv", "date,close\n", "date,open,high,low,close,volume\n",
    ])
    def test_unusable_history_is_not_checked_and_not_refused(
            self, tmp_path, junk):
        (tmp_path / "AAA.csv").write_text(junk)
        check = cross_check(Decimal("37"), tmp_path, "AAA")
        assert not check.checked and not check.refused

    @pytest.mark.parametrize("bad", ["abc", None, "NaN", "Infinity", "0",
                                     "-5"])
    def test_a_live_quote_that_is_not_a_price_never_raises(self, bars, bad):
        check = cross_check(bad, bars, "AAA")
        assert not check.checked


class TestItReachesTheCycleAndTheOwner:
    def test_the_snapshot_can_carry_it(self):
        import dataclasses

        from catalyst.risk import MarketSnapshot

        names = {f.name for f in dataclasses.fields(MarketSnapshot)}
        assert "quote_check" in names

    def test_the_sentence_names_what_it_compared(self):
        """A verdict with no figures behind it is the thing this project
        keeps refusing to accept anywhere else."""
        import csv as _csv
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        rows = [{"date": f"2026-08-{d:02d}", "open": "37", "high": "37",
                 "low": "37", "close": "37", "volume": "1"}
                for d in range(1, 15)]
        with (tmp / "AAA.csv").open("w", newline="") as f:
            w = _csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        said = cross_check(Decimal("52"), tmp, "AAA").sentence
        assert "37" in said and "2026-08-14" in said and "%" in said


class TestTheIntegrityPageShowsIt:
    def test_fill_against_intended_is_displayed(self, tmp_path):
        """BUILD-BRIEF requires it on every trade; both halves were
        stored and never compared anywhere a person could see."""
        from datetime import datetime, timezone

        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        now = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        conn.execute(
            "INSERT INTO candidates VALUES ('c1','REGN','fda_decision',"
            "'2026-09-01','confirmed','[]',?,'health','[]')",
            (now.isoformat(),))
        conn.execute(
            "INSERT INTO orders VALUES ('o1','c1','b1','buy','4','market',"
            "'day',?,'filled','{}')", (now.isoformat(),))
        conn.execute("INSERT INTO entry_market_context VALUES "
                     "('o1','4.2','37.00',?)", (now.isoformat(),))
        conn.execute("INSERT INTO fills VALUES ('o1','37.0400','4',?,"
                     "'37.0400','0.6100')", (now.isoformat(),))
        conn.commit()
        conn.close()

        db = Db(path)
        html = panels.data_integrity_panel(db, p="integ")
        db.close()
        assert "$37.00" in html, "the intended price is not shown"
        assert "$37.04" in html, "the filled price is not shown"
        # THE SIGN IS THE POINT, not just the magnitude. Filling at
        # 37.04 against an intended 37.00 COST money; rendered as
        # -0.108% it would read as having saved it, and a cost shown as
        # a benefit is worse than not showing it at all.
        assert "+0.108%" in html, (
            "the slippage between them is not computed, or its sign is "
            "backwards - paying more must not read as paying less")
        assert "-0.108%" not in html
        assert "0.6100" in html, "the modelled spread is not shown beside it"

    def test_paying_more_and_paying_less_do_not_look_the_same(self, tmp_path):
        """The other direction of the same defect."""
        from datetime import datetime, timezone

        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        now = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        conn.execute(
            "INSERT INTO candidates VALUES ('c1','REGN','fda_decision',"
            "'2026-09-01','confirmed','[]',?,'health','[]')",
            (now.isoformat(),))
        conn.execute(
            "INSERT INTO orders VALUES ('o1','c1','b1','buy','4','market',"
            "'day',?,'filled','{}')", (now.isoformat(),))
        conn.execute("INSERT INTO entry_market_context VALUES "
                     "('o1','4.2','37.00',?)", (now.isoformat(),))
        # Filled BELOW the intended price - a genuine improvement.
        conn.execute("INSERT INTO fills VALUES ('o1','36.9600','4',?,"
                     "'36.9600','0.6100')", (now.isoformat(),))
        conn.commit()
        conn.close()

        db = Db(path)
        html = panels.data_integrity_panel(db, p="integ")
        db.close()
        assert "-0.108%" in html, (
            "filling BELOW the intended price is not shown as negative "
            "slippage - the sign carries the whole meaning")
        assert "+0.108%" not in html

    def test_a_paper_account_is_not_congratulated_on_zero_slippage(
            self, tmp_path):
        """Near-zero slippage on paper is the ABSENCE of a measurement,
        not a good result, and the page has to say which."""
        from datetime import datetime, timezone

        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        now = datetime(2026, 8, 16, tzinfo=timezone.utc)
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        conn.execute(
            "INSERT INTO candidates VALUES ('c1','AAA','earnings',"
            "'2026-09-01','confirmed','[]',?,'tech','[]')",
            (now.isoformat(),))
        conn.execute(
            "INSERT INTO orders VALUES ('o1','c1','b1','buy','4','market',"
            "'day',?,'filled','{}')", (now.isoformat(),))
        conn.execute("INSERT INTO entry_market_context VALUES "
                     "('o1','4','50.00',?)", (now.isoformat(),))
        conn.execute("INSERT INTO fills VALUES ('o1','50.0000','4',?,"
                     "'50.0000','0.4000')", (now.isoformat(),))
        conn.commit()
        conn.close()

        db = Db(path)
        html = panels.data_integrity_panel(db, p="integ")
        db.close()
        assert "paper fills pay no spread" in html
        assert "absence of a measurement" in html

    def test_it_states_the_model_supplies_no_prices(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        path = str(tmp_path / "c.db")
        init_db(path).close()
        db = Db(path)
        html = panels.data_integrity_panel(db, p="integ")
        db.close()
        assert "never from the model" in html
        assert "no price, target or quantity" in html

    def test_an_empty_cache_is_explained_not_left_blank(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        path = str(tmp_path / "c.db")
        init_db(path).close()
        db = Db(path)
        html = panels.data_integrity_panel(db, p="integ")
        db.close()
        assert "No cached price history yet" in html
        assert "fills in on its own" in html


# ---------------------------------------------------------------------
# THE LIVE PATH. Everything above tests the function; this drives
# run_cycle itself, because a check that is correct and never called is
# the defect this project has already shipped once (the position review
# had thirty passing tests and no caller).
# ---------------------------------------------------------------------

class TestTheCycleActuallyStopsOnIt:
    """The broker quotes $50. The cached history says otherwise - or
    agrees - and the cycle has to behave differently in the two cases."""

    @staticmethod
    def _write_bars(bars_dir, ticker, base):
        """300 sessions with a small realistic wiggle around `base`.

        Long enough that `bar_history.is_fresh` keeps the file and the
        cycle makes no fetch, so the test stays offline and the price
        under test is the one written here.
        """
        from decimal import Decimal as D

        bars_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for i in range(300):
            close = D(str(base)) * (D("1") + D(i % 7 - 3) / D("300"))
            day = (dtdate(2025, 1, 1) + timedelta(days=i)).isoformat()
            rows.append({"date": day, "open": f"{close:.4f}",
                         "high": f"{close * D('1.01'):.4f}",
                         "low": f"{close * D('0.99'):.4f}",
                         "close": f"{close:.4f}", "volume": "2000000"})
        with (bars_dir / f"{ticker}.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

    def _cycle(self, tmp_path, base_close):
        import sqlite3

        from tests.test_cycle import (
            NOW, broker_for, candidate, event, model_transport,
        )
        from catalyst.orchestrator.cycle import run_cycle

        bars = tmp_path / "bars"
        self._write_bars(bars, "TEST", base_close)
        conn = sqlite3.connect(tmp_path / "q.db")
        conn.executescript(open("catalyst/storage/schema.sql").read())
        broker, state = broker_for()
        try:
            report = run_cycle(
                conn, broker, model_transport(),
                feed_fetch=lambda s, u: [event()],
                build_candidates_fn=lambda e, a: [candidate()],
                cluster_fn=lambda cs, ops: {c.id: "tech-w34" for c in cs},
                now=NOW, bars_dir=str(bars))
        finally:
            conn.close()
        return report, state

    def test_a_quote_that_fails_the_check_places_NO_ORDER(
            self, tmp_path, frozen_kill_switch_clock, stub_prompts):
        """The broker says $50, the cache says $500. One of them is
        wrong and the bot does not get to guess which."""
        report, state = self._cycle(tmp_path, base_close="500")
        assert not state["posts"], (
            "an order was placed on a price that failed its own "
            "cross-check - that is the false pretence")
        assert report.funnel["proposed"] == 0
        dropped = " ".join(report.drop_reasons.get("researched", []))
        assert "quote_failed_cross_check" in dropped

    def test_the_owner_is_told_which_two_numbers_disagreed(
            self, tmp_path, frozen_kill_switch_clock, stub_prompts):
        """A silent refusal is the failure mode the owner has objected
        to more than any other."""
        report, _ = self._cycle(tmp_path, base_close="500")
        said = " ".join(report.errors)
        assert "TEST" in said, said
        assert "REFUSED" in said, said
        # BOTH numbers, not just the verdict: the cached close it
        # compared against, and by how far the live quote missed it.
        assert "503" in said and "-90.1%" in said, said

    def test_it_is_recorded_against_the_candidate_not_only_in_memory(
            self, tmp_path, frozen_kill_switch_clock, stub_prompts):
        """The report dies with the process; the dashboard reads the
        database."""
        import sqlite3

        self._cycle(tmp_path, base_close="500")
        conn = sqlite3.connect(tmp_path / "q.db")
        reasons = [r[0] or "" for r in conn.execute(
            "SELECT skipped_reason FROM research_calls "
            "WHERE candidate_id='cand-1'").fetchall()]
        conn.close()
        assert any("quote_failed_cross_check" in r for r in reasons), reasons

    def test_a_refused_candidate_is_not_discarded_forever(self, tmp_path):
        """It is TODAY'S quote that is in doubt, not the candidate. If
        this counted as a failed research attempt the bot would give up
        on a name because of one bad tick."""
        import sqlite3

        from catalyst.orchestrator.cycle import _failed_attempts

        self._cycle(tmp_path, base_close="500")
        conn = sqlite3.connect(tmp_path / "q.db")
        failed = _failed_attempts(conn, "cand-1")
        conn.close()
        assert failed <= 1, (
            f"{failed} failed attempts recorded - a refused QUOTE must not "
            "count against the candidate's retry budget")

    def test_an_AGREEING_history_lets_the_trade_through(
            self, tmp_path, frozen_kill_switch_clock, stub_prompts):
        """The control, and the expensive direction. If this check ever
        started refusing ordinary candidates it would look exactly like
        a bot that had simply stopped trading."""
        report, state = self._cycle(tmp_path, base_close="50")
        dropped = " ".join(report.drop_reasons.get("researched", []))
        assert "quote_failed_cross_check" not in dropped
        assert state["posts"], (
            "a candidate whose cached history AGREES with the live quote "
            "was not traded")


class TestTheVerdictOUTLIVESTheProcess:
    """A pass-through recorded only in a cycle report that has since
    exited cannot be told apart from never having looked - which is the
    exact failure this whole check exists to remove, reproduced one
    layer up. So every verdict lands in the database.
    """

    def _rows(self, tmp_path, base_close):
        import sqlite3

        TestTheCycleActuallyStopsOnIt()._cycle(tmp_path, base_close)
        conn = sqlite3.connect(tmp_path / "q.db")
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM quote_cross_checks").fetchall()]
        conn.close()
        return rows

    def test_a_refusal_is_persisted_with_both_numbers(
            self, tmp_path, frozen_kill_switch_clock, stub_prompts):
        rows = self._rows(tmp_path, "500")
        assert len(rows) == 1, rows
        r = rows[0]
        assert r["ticker"] == "TEST"
        assert r["refused"] == 1
        assert r["checked"] == 1
        assert Decimal(r["live_price"]) == Decimal("50")
        assert Decimal(r["reference_close"]) > Decimal("400")
        assert "REFUSED" in r["note"]

    def test_a_PASSING_check_is_persisted_too(
            self, tmp_path, frozen_kill_switch_clock, stub_prompts):
        """Only recording the refusals would make the page read as if
        the check almost never runs."""
        rows = self._rows(tmp_path, "50")
        assert len(rows) == 1, rows
        assert rows[0]["refused"] == 0 and rows[0]["checked"] == 1

    def test_recording_it_is_not_what_stops_the_trade(self, tmp_path,
                                                      frozen_kill_switch_clock,
                                                      stub_prompts,
                                                      monkeypatch):
        """If the write failed and that were load-bearing, a full disk
        would quietly re-enable trading on unchecked prices."""
        from catalyst.orchestrator import cycle

        monkeypatch.setattr(cycle, "_record_quote_check",
                            lambda *a, **kw: None)
        _report, state = TestTheCycleActuallyStopsOnIt()._cycle(
            tmp_path, "500")
        assert not state["posts"], (
            "with the recorder disabled the refusal stopped working - the "
            "observation is load-bearing, which it must not be")


class TestTheOwnerCanSeeAllOfIt:
    @staticmethod
    def _seed(tmp_path):
        from datetime import datetime, timezone

        from catalyst.storage import init_db

        now = datetime(2026, 8, 16, 15, 0, tzinfo=timezone.utc)
        path = str(tmp_path / "c.db")
        conn = init_db(path)
        for cid, tk in (("c1", "REGN"), ("c2", "ACME"), ("c3", "BIOX"),
                        ("c4", "NOHX")):
            conn.execute(
                "INSERT INTO candidates VALUES (?,?,'fda_decision',"
                "'2026-09-01','confirmed','[]',?,'health','[]')",
                (cid, tk, now.isoformat()))
        rows = [
            ("c1", "REGN", "37.00", "37.10", "2026-08-14", "-0.0027",
             1, 0, 0, "... Consistent with the cached history."),
            ("c2", "ACME", "52.00", "37.00", "2026-08-14", "0.4054",
             1, 1, 0, "... +40.5%. Larger than an ordinary day ... not "
                      "refused."),
            ("c3", "BIOX", "5.00", "500.00", "2026-08-13", "-0.9900",
             1, 0, 1, "... -99.0%. REFUSED - no single session moves a "
                      "price that far."),
            ("c4", "NOHX", "12.00", None, None, None,
             0, 0, 0, "No cached history for this ticker, so the live "
                      "quote could not be cross-checked."),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO quote_cross_checks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                r + (now.isoformat(),))
        conn.commit()
        conn.close()
        return path

    def _page(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        db = Db(self._seed(tmp_path))
        try:
            return panels.data_integrity_panel(db, p="integ")
        finally:
            db.close()

    def test_all_four_verdicts_are_distinguishable(self, tmp_path):
        """"Refused", "flagged", "consistent" and "not checked" mean four
        different things and collapsing any two of them is the defect."""
        html = self._page(tmp_path)
        for word in ("REFUSED", "flagged", "consistent", "not checked"):
            assert word in html, f"{word!r} does not appear on the page"

    def test_NOT_CHECKED_is_counted_separately_from_passing(self, tmp_path):
        """The whole point of the distinction: three checked, one that
        nothing looked at."""
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db

        db = Db(self._seed(tmp_path))
        try:
            d = queries.data_integrity(db)
        finally:
            db.close()
        assert d.n_quote_checks == 4
        assert d.n_quote_refused == 1
        assert d.n_quote_flagged == 1
        assert d.n_quote_unchecked == 1

    def test_the_deviation_is_shown_as_a_percentage(self, tmp_path):
        html = self._page(tmp_path)
        assert "+40.5%" in html and "-99.0%" in html, (
            "the stored fraction is not rendered as a readable percentage")

    def test_a_refusal_carries_its_reason_not_just_a_verdict(self, tmp_path):
        """A verdict with no figures behind it is what this project
        refuses to accept anywhere else."""
        html = self._page(tmp_path)
        assert "no single session moves a price that far" in html

    def test_an_unchecked_row_does_not_invent_a_reference(self, tmp_path):
        """NOHX has no cached close. A zero there would read as a
        -100% deviation, which is a refusal made of missing data."""
        html = self._page(tmp_path)
        i = html.find("NOHX")
        assert i > 0
        assert "$0" not in html[i:i + 400] and "0.00" not in html[i:i + 400]

    def test_an_empty_table_is_explained_not_blank(self, tmp_path):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        path = str(tmp_path / "e.db")
        init_db(path).close()
        db = Db(path)
        try:
            html = panels.data_integrity_panel(db, p="integ")
        finally:
            db.close()
        assert "no quote has been cross-checked yet" in html
        assert "quote_cross_checks" in html, (
            "house rule 3: the raw query is not printed beside the zero")

    def test_the_trade_page_says_where_its_price_came_from(self, tmp_path):
        """BUILD-BRIEF: someone who was not there must be able to read
        one trade and understand it. A size is not explained by a price
        whose origin is unstated."""
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        db = Db(self._seed(tmp_path))
        try:
            html = panels.trace_page(db, "c2")
        finally:
            db.close()
        assert "mid of the live Alpaca bid and ask" in html
        assert "flagged, traded anyway" in html
        assert "Larger than an ordinary day" in html

    def test_a_trade_with_no_record_is_not_reported_as_passing(self,
                                                               tmp_path):
        """The oldest trap in this project: silence read as success."""
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        path = str(tmp_path / "n.db")
        conn = init_db(path)
        conn.execute(
            "INSERT INTO candidates VALUES ('c9','ZZZZ','earnings',"
            "'2026-09-01','confirmed','[]','2026-08-16T15:00:00+00:00',"
            "'tech','[]')")
        conn.commit()
        conn.close()
        db = Db(path)
        try:
            html = panels.trace_page(db, "c9")
        finally:
            db.close()
        assert "not</b> that it passed" in html or "not that it passed" in html
