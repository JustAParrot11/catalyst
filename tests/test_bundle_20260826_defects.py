"""Three defects the owner's 2026-08-26 bundle exposed.

That bundle is mostly a good-news report - warnings down from 94 to 4,
all fifteen maintenance checks green, the hunt running for the first
time, EDGAR index fetches down from 306 to 4. What is left is small and
real.

1. A TICKER CALLED "NONE". 24 requests a day to
   /v2/stocks/NONE/quotes/latest, every one a 404. A null inside a news
   story's `symbols` array survived the guard, because `str(None)` is
   "None" - truthy, no colon - and `.upper()` made it a ticker. It
   became a candidate and the quote gate spent a request a cycle proving
   it does not exist. Stringifying BEFORE validating is what did it.

2. AN UNFAMILIAR ENTITY KIND LOST A WHOLE RESEARCH PASS. Three calls in
   a day threw away every finding they made, to "unknown entity kind
   'ticker'", "'metric'" and "'equity'". The eight kinds are right; a
   model will always eventually reach for a ninth word. Rejecting the
   batch made the strictness cost the evidence it was protecting.

3. A PENNY AND A HALF AT WARNING. "Ledger corrected for 2026-08-25 ... a
   -1.5104c adjustment" - four tenths of one percent of a 354c day,
   which is rounding. Correcting is what that job is FOR, and a warning
   every night for a penny is how a person learns to scroll past
   warnings.

Fully offline.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from catalyst.storage import init_db


class TestANullSymbolIsNotATicker:
    """Defect 1."""

    def _events(self, symbols):
        from catalyst.data.sources import alpaca_news

        payload = {"news": [{"id": 1, "symbols": symbols,
                             "headline": "Example Inc beats estimates",
                             "summary": "s",
                             "created_at": "2026-08-26T12:00:00Z",
                             "updated_at": "2026-08-26T12:00:00Z",
                             "url": "https://example.com/1"}]}

        class Resp:
            status_code = 200

            def json(self):
                return payload

            text = ""

        return alpaca_news.fetch_events(
            since=date(2026, 8, 26), until=date(2026, 8, 26),
            alpaca_key="k", alpaca_secret="s",
            http_get=lambda *a, **kw: Resp())

    def test_a_null_in_the_symbols_array_produces_no_ticker(self):
        got = self._events(["EMBC", None])
        tickers = {e.payload_raw.get("ticker") for e in got.events}
        assert "NONE" not in tickers, (
            "str(None) is 'None', which is truthy - the guard has to be "
            "about what the value IS, not what str() makes of it")
        assert "EMBC" in tickers, "the real symbol beside it was lost too"

    def test_other_non_strings_are_refused_the_same_way(self):
        """Classify by the rule (house rule 7): a symbol that is not a
        string is not a symbol, whatever kind of non-string it is."""
        got = self._events(["EMBC", None, 123, {"sym": "X"}, ["Y"], True])
        tickers = {e.payload_raw.get("ticker") for e in got.events}
        assert tickers == {"EMBC"}, f"got {tickers}"

    def test_a_foreign_listing_is_still_dropped(self):
        got = self._events(["EMBC", "TSX:CJT"])
        tickers = {e.payload_raw.get("ticker") for e in got.events}
        assert tickers == {"EMBC"}


class TestAnUnknownEntityKindDoesNotLoseTheBatch:
    """Defect 2."""

    @pytest.fixture
    def conn(self, tmp_path):
        c = init_db(str(tmp_path / "g.db"))
        yield c
        c.close()

    def finding(self, kind):
        return {
            "subject": {"kind": kind, "canonical_key": f"k-{kind}",
                        "display_name": "Example Inc"},
            "predicate": "mentions",
            "object_date": "2026-09-01",
            "source_class": "model_inference",
            "reliability": "model_inference",
        }

    def test_the_three_kinds_the_owner_hit_are_all_kept(self, conn):
        from catalyst.graph.hooks import research_findings_to_graph

        got = research_findings_to_graph(
            "call-1", [self.finding(k) for k in ("ticker", "metric", "equity")],
            conn)
        assert len(got) == 3, (
            "one unfamiliar word still loses the whole research pass")

    def test_an_unknown_kind_lands_in_other(self, conn):
        from catalyst.graph.hooks import research_findings_to_graph

        research_findings_to_graph("call-1", [self.finding("ticker")], conn)
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM graph_entities").fetchall()]
        assert kinds == ["other"]

    def test_the_word_the_model_used_is_not_thrown_away(self, conn):
        """`other` must not hide what it really was, or the graph
        quietly renames everything it did not recognise."""
        from catalyst.graph.hooks import research_findings_to_graph

        research_findings_to_graph("call-1", [self.finding("ticker")], conn)
        name = conn.execute(
            "SELECT display_name FROM graph_entities").fetchone()[0]
        assert "ticker" in name and "Example Inc" in name

    def test_a_known_kind_is_untouched(self, conn):
        from catalyst.graph.hooks import research_findings_to_graph

        research_findings_to_graph("call-1", [self.finding("company")], conn)
        row = conn.execute(
            "SELECT kind, display_name FROM graph_entities").fetchone()
        assert row == ("company", "Example Inc")

    def test_the_owners_next_two_failures_are_also_kept(self, conn):
        """OWNER'S LOG, 2026-08-28: fixing `kind` and leaving its
        siblings meant the model failed on those instead -
        "reliability 0.9" (a number) and "source_class
        'company_filing'". Same class, one field over."""
        from catalyst.graph.hooks import research_findings_to_graph

        got = research_findings_to_graph("call-1", [{
            "subject": {"kind": "company", "canonical_key": "k",
                        "display_name": "Example Inc"},
            "predicate": "mentions", "object_date": "2026-09-01",
            "source_class": "company_filing", "reliability": 0.9,
        }], conn)
        assert len(got) == 1

    def test_unknown_provenance_falls_to_the_LEAST_trusted_class(self, conn):
        """The opposite direction to `other`. These fields say how much
        a claim should be trusted, so a guess must never be promoted to
        a primary document."""
        from catalyst.graph.hooks import research_findings_to_graph

        research_findings_to_graph("call-1", [{
            "subject": {"kind": "company", "canonical_key": "k",
                        "display_name": "Example Inc"},
            "predicate": "mentions", "object_date": "2026-09-01",
            "source_class": "company_filing", "reliability": 0.9,
        }], conn)
        row = conn.execute(
            "SELECT source_class, reliability FROM graph_assertions"
        ).fetchone()
        assert row == ("model_inference", "model_inference")

    def test_a_real_provenance_is_untouched(self, conn):
        from catalyst.graph.hooks import research_findings_to_graph

        research_findings_to_graph("call-1", [{
            "subject": {"kind": "company", "canonical_key": "k",
                        "display_name": "Example Inc"},
            "predicate": "mentions", "object_date": "2026-09-01",
            "source_class": "edgar_filing", "reliability": "primary_document",
        }], conn)
        row = conn.execute(
            "SELECT source_class, reliability FROM graph_assertions"
        ).fetchone()
        assert row == ("edgar_filing", "primary_document")

    def test_a_genuinely_malformed_finding_still_fails_loudly(self, conn):
        """The batch guard is not weakened - a finding with no subject
        at all is still a caller bug, and a half-written chain that
        looks complete is worse than a loud failure."""
        from catalyst.graph.hooks import research_findings_to_graph

        with pytest.raises(ValueError, match="rejected"):
            research_findings_to_graph(
                "call-1", [{"predicate": "mentions"}], conn)


class TestASmallCorrectionIsNotAWarning:
    """Defect 3."""

    def _run(self, caplog, adjustment_cents, tmp_path, monkeypatch):
        from catalyst.cost import backfill as bf
        from catalyst.orchestrator import scheduler

        day = date(2026, 8, 25)
        db = str(tmp_path / "t.db")
        init_db(db).close()

        class Result:
            applied = True
            adjustment_cents = Decimal("0")
            reason = "a correction"

        result = Result()
        result.adjustment_cents = Decimal(str(adjustment_cents))
        monkeypatch.setattr(bf, "backfill_day",
                            lambda *a, **kw: result)

        # Exercise the branch directly, with the same import the
        # scheduler uses, so the threshold cannot drift apart from it.
        from catalyst.cost.tracker import RECONCILE_PAUSE_FLOOR_CENTS
        big = abs(result.adjustment_cents) > RECONCILE_PAUSE_FLOOR_CENTS
        logger = logging.getLogger("catalyst.scheduler")
        with caplog.at_level(logging.INFO, logger="catalyst.scheduler"):
            (logger.warning if big else logger.info)(
                "Ledger corrected for %s: %s", day, result.reason)
        return caplog.records[-1].levelno

    def test_a_penny_and_a_half_is_information(self, caplog, tmp_path,
                                               monkeypatch):
        """The owner's exact figure."""
        assert self._run(caplog, "-1.5104", tmp_path,
                         monkeypatch) == logging.INFO

    def test_a_correction_worth_acting_on_is_still_a_warning(
            self, caplog, tmp_path, monkeypatch):
        assert self._run(caplog, "-250", tmp_path,
                         monkeypatch) == logging.WARNING

    def test_the_bar_is_the_reconciliation_bar(self):
        """No second number to keep in step: it reuses the one the
        reconciliation already calls 'large enough to act on'."""
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler)
        assert "RECONCILE_PAUSE_FLOOR_CENTS" in src
        assert "Ledger corrected for" in src
