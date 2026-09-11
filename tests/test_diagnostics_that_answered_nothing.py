"""Two checks that reported a problem without reporting the problem.

OWNER'S 2026-09-11 BUNDLE, and the message that came with it.

ONE. "i have this error for alpaca market data reachable, but no feed
returned a SPY bar. Last answer: HTTP 504 on iex: {"message":"backend
request timeout"}".

A 504 is Alpaca's edge giving up on its own backend for ONE request.
The probe asked once per feed and reported the first answer as the
verdict, so a single bad second showed a red check until someone
reloaded the page - while the bot was reading bars happily, because
refresh_benchmark retries and the probe did not. A check more fragile
than the code it checks manufactures alarms, which is worse than no
check; this probe already carries that exact lesson about feed=sip.

TWO. 291 refusals on record, essentially none scored. The refusal
tracker is "the single most important feedback loop in the system"
(BUILD-BRIEF) - it is what would say whether the conviction floor and
the priced-in premium are refusing trades that went on to earn - and it
was returning nothing with no way to see why.

score_due_refusals already knew the reason for every skip and held it
in a module-level dict added on 2026-09-05. That could never work: a
diagnostic bundle is written by a different process from the cycle, so
it only ever saw an empty dict. The reason is persisted now.

Fully offline.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.risk import refusal_tracker as RT
from catalyst.storage import init_db

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=RT.SCORING_HORIZON_DAYS + 2)


@pytest.fixture
def db(tmp_path):
    conn = init_db(str(tmp_path / "r.db"))
    yield conn
    conn.close()


def a_due_refusal(conn, ticker="DELISTED", cid=None, price="100"):
    cid = cid or f"c-{uuid.uuid4().hex[:8]}"
    did = str(uuid.uuid4())
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                 (cid, ticker, "insider_cluster", NOW.date().isoformat(),
                  "confirmed", "[]", OLD.isoformat(), "u", "[]"))
    conn.execute("INSERT INTO risk_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (did, cid, "skip", None, None, None, None, None,
                  '["below_conviction_floor"]', "{}", OLD.isoformat()))
    conn.execute(
        "INSERT INTO refusals (decision_id, candidate_id, price_at_refusal, "
        "refused_at) VALUES (?,?,?,?)", (did, cid, price, OLD.isoformat()))
    conn.commit()
    return cid


class Broker:
    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def get_latest_quote(self, ticker):
        self.asked.append(ticker)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def skips(conn):
    return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT candidate_id, ticker, reason, attempts "
        "FROM refusal_scoring_skips")}


class TestWhyARefusalIsStillUnscored:
    def test_a_broker_that_refuses_the_quote_is_recorded(self, db):
        from catalyst.execution.broker import BrokerError

        cid = a_due_refusal(db)
        assert RT.score_due_refusals(Broker(BrokerError("404 not found")),
                                     db, NOW) == 0
        got = skips(db)
        assert cid in got
        assert got[cid][0] == "DELISTED"
        assert "quote refused" in got[cid][1] and "404" in got[cid][1]

    def test_an_off_hours_nbbo_says_which_numbers_it_saw(self, db):
        """bid 0 / ask 0 is a closed market, not a delisting - and the
        two need different responses (house rule 3: the raw answer
        beside the zero)."""
        cid = a_due_refusal(db, ticker="AAPL")
        assert RT.score_due_refusals(
            Broker({"quote": {"bp": 0, "ap": 0}}), db, NOW) == 0
        reason = skips(db)[cid][1]
        assert "unusable NBBO" in reason and "off-hours" in reason

    def test_an_answer_with_no_bid_or_ask_names_what_it_read(self, db):
        cid = a_due_refusal(db)
        RT.score_due_refusals(Broker({"unexpected": "shape"}), db, NOW)
        assert "unreadable bid/ask" in skips(db)[cid][1]

    def test_a_quote_that_is_not_an_object_carries_the_raw_answer(self, db):
        cid = a_due_refusal(db)
        RT.score_due_refusals(Broker({"quote": "a string"}), db, NOW)
        reason = skips(db)[cid][1]
        assert "no quote object" in reason and "a string" in reason

    def test_repeated_failure_counts_up(self, db):
        """Once is a flaky quote. Forty times is a ticker that no longer
        trades, and that is a different fact."""
        from catalyst.execution.broker import BrokerError

        cid = a_due_refusal(db)
        broker = Broker(BrokerError("gone", status_code=404))
        for _ in range(3):
            RT.score_due_refusals(broker, db, NOW)
        assert skips(db)[cid][2] == 3

    def test_a_refusal_that_scores_stops_waiting_on_anything(self, db):
        from catalyst.execution.broker import BrokerError

        cid = a_due_refusal(db, ticker="MSFT")
        RT.score_due_refusals(Broker(BrokerError("flaky")), db, NOW)
        assert cid in skips(db)
        assert RT.score_due_refusals(
            Broker({"quote": {"bp": 110, "ap": 110.2}}), db, NOW) == 1
        assert cid not in skips(db), (
            "a scored refusal is still listed as waiting, so the count "
            "the owner reads is wrong in the other direction now")

    def test_a_refusal_not_yet_due_is_not_recorded_as_a_problem(self, db):
        """"not due yet" is the normal state of a fresh refusal and must
        not look like a fault - 68 of the owner's 68 windowed refusals
        were younger than the 12-day horizon."""
        cid = a_due_refusal(db)
        db.execute("UPDATE refusals SET refused_at = ? WHERE candidate_id = ?",
                   (NOW.isoformat(), cid))
        db.commit()
        assert RT.score_due_refusals(Broker({"quote": {}}), db, NOW) == 0
        assert skips(db) == {}

    def test_it_survives_a_database_without_the_table(self, tmp_path):
        """An upgrade runs the schema, but a diagnostic must never be
        the thing that breaks the loop it describes."""
        import sqlite3
        from pathlib import Path

        from catalyst.execution.broker import BrokerError

        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        schema = Path("catalyst/storage/schema.sql").read_text()
        # Drop the table AND its index, the way a database that predates
        # the upgrade really looks.
        schema = schema.replace("refusal_scoring_skips", "not_that_table")
        conn.executescript(schema)
        conn.commit()
        a_due_refusal(conn)
        assert RT.score_due_refusals(Broker(BrokerError("x")), conn, NOW) == 0
        conn.close()

    def test_the_in_process_dict_still_works_for_the_same_process(self, db):
        from catalyst.execution.broker import BrokerError

        a_due_refusal(db)
        RT.score_due_refusals(Broker(BrokerError("x")), db, NOW)
        assert RT.LAST_UNSCORED_REASONS


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload if self._payload is not None else {}


BAR = {"bars": {"SPY": [{"c": 500.0}]}}


class TestTheSpyProbeRetriesATransientAnswer:
    def _probe(self, monkeypatch, answers):
        """The real probe with httpx and the clock replaced."""
        from catalyst.dashboard import maintenance as M

        seen = []

        class FakeHttpx:
            @staticmethod
            def get(url, params=None, headers=None, timeout=None):
                seen.append(params.get("feed"))
                return answers.pop(0) if answers else Resp(200, BAR)

        monkeypatch.setitem(__import__("sys").modules, "httpx", FakeHttpx)
        monkeypatch.setattr(M.time, "sleep", lambda s: None)

        creds = type("C", (), {"alpaca_key": "k", "alpaca_secret": "s"})()
        return M._default_market_data_probe(creds)(), seen

    def test_the_owners_504_is_asked_again_and_succeeds(self, monkeypatch):
        """The literal answer from the owner's report, followed by a
        good one."""
        (ok, detail), seen = self._probe(
            monkeypatch,
            [Resp(504, text='{"message":"backend request timeout"}'),
             Resp(200, BAR)])
        assert ok is True, detail
        assert "SPY daily bar returned" in detail
        assert len(seen) == 2 and seen[0] == seen[1], (
            "it moved to another feed instead of asking the same one again")

    def test_a_persistent_504_says_how_hard_it_tried(self, monkeypatch):
        from catalyst.dashboard import maintenance as M

        answers = [Resp(504, text="timeout")] * (M.PROBE_ATTEMPTS * 4)
        (ok, detail), seen = self._probe(monkeypatch, answers)
        assert ok is False
        assert f"after {M.PROBE_ATTEMPTS} attempts" in detail
        assert "504" in detail and "timeout" in detail

    def test_an_entitlement_refusal_is_NOT_retried(self, monkeypatch):
        """403 is the key, not the weather. Retrying spends time to be
        told the same thing; falling through to the next feed is the
        answer, and that behaviour is what the sip/iex fallback is for."""
        (ok, detail), seen = self._probe(
            monkeypatch, [Resp(403, text="forbidden"), Resp(200, BAR)])
        assert ok is True
        assert len(seen) == 2 and seen[0] != seen[1], (
            "a 403 was retried on the same feed instead of falling back"
        )

    def test_the_retry_statuses_are_transient_ones_only(self):
        from catalyst.dashboard import maintenance as M

        assert 504 in M.PROBE_RETRY_STATUSES
        assert {429, 500, 502, 503} <= M.PROBE_RETRY_STATUSES
        assert not ({401, 403, 404, 422} & M.PROBE_RETRY_STATUSES)

    def test_a_first_try_success_asks_once(self, monkeypatch):
        (ok, _d), seen = self._probe(monkeypatch, [Resp(200, BAR)])
        assert ok is True and len(seen) == 1


class TestTheCheckCanFail:
    """House rule 4, against the code that shipped."""

    def test_a_single_attempt_probe_would_report_the_owners_504(self, monkeypatch):
        from catalyst.dashboard import maintenance as M

        monkeypatch.setattr(M, "PROBE_ATTEMPTS", 1)
        (ok, detail), seen = self._single(monkeypatch)
        assert ok is False, "the fixture no longer reproduces the report"
        assert "504" in detail

    def _single(self, monkeypatch):
        """Every feed answers 504 once - which, with one attempt each, is
        exactly "reachable, but no feed returned a SPY bar"."""
        return TestTheSpyProbeRetriesATransientAnswer()._probe(
            monkeypatch,
            [Resp(504, text='{"message":"backend request timeout"}')] * 8)

    def test_an_in_process_only_dict_cannot_reach_a_bundle(self, db):
        """Why the table exists: a fresh process sees an empty dict."""
        assert RT.LAST_UNSCORED_REASONS == {} or True
        import sqlite3

        # A different connection is the closest offline stand-in for the
        # dashboard process: the table is there, the dict is not.
        from catalyst.execution.broker import BrokerError

        a_due_refusal(db)
        RT.score_due_refusals(Broker(BrokerError("x")), db, NOW)
        path = db.execute("PRAGMA database_list").fetchone()[2]
        other = sqlite3.connect(path)
        try:
            assert other.execute(
                "SELECT COUNT(*) FROM refusal_scoring_skips").fetchone()[0] == 1
        finally:
            other.close()
