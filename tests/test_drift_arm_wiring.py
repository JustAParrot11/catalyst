"""The drift arm has to reach the pipeline, and must not displace it.

OWNER-ASKED 2026-08-30: "ensure it is made better so it will trade
profitably, feels like its idling too much".

The bot ran one candidate arm. `strategies/earnings_drift.py` - the
better-graded of the two that were built - produced nothing in
production because nothing fetched the XBRL it needs. These hold the
wiring that connects it.

CLAUDE.md's containment rule for a second source, verbatim in spirit:
"Both candidate sources go through the identical research, pricing,
risk and execution path. Nothing downstream knows which found it. They
are stamped with their origin so the record can eventually say which is
worth the money."

So the tests below are about CONTAINMENT as much as connection: the
drift arm may add, never replace; it may not duplicate a ticker the
screen already found; and a failure in it must leave the graded Form 4
candidates exactly as they were.

Fully offline.
"""

from datetime import date, datetime, timezone

import pytest

from catalyst.orchestrator.scheduler import _issuer_pairs


class Ev:
    def __init__(self, payload):
        self.payload_raw = payload


def form4(symbol, cik):
    return Ev({"symbol": symbol, "issuer_cik": cik, "code": "P"})


class TestTheUniverseComesFromWhatTheBotAlreadySees:
    def test_it_pairs_each_company_with_its_cik(self):
        pairs = _issuer_pairs([form4("EMBC", "1724570"),
                               form4("AAPL", "320193")])
        assert pairs == [("AAPL", "320193"), ("EMBC", "1724570")]

    def test_a_company_seen_twice_is_asked_about_once(self):
        pairs = _issuer_pairs([form4("EMBC", "1724570"),
                               form4("embc", "1724570")])
        assert pairs == [("EMBC", "1724570")]

    def test_a_row_missing_either_half_is_skipped_not_guessed(self):
        """A CIK is what the SEC URL is built from. Half a pair is not
        a company, and inventing the other half would fetch somebody
        else's filings."""
        assert _issuer_pairs([form4("EMBC", ""), form4("", "123"),
                              Ev({"symbol": "X"}), Ev({"issuer_cik": "9"})]) == []

    def test_a_payload_that_is_not_an_object_drops_itself(self):
        """The same rule the Form 4 adapter learned: one malformed row
        must never take the batch with it."""
        pairs = _issuer_pairs([Ev("not a dict"), Ev(None),
                               form4("EMBC", "1724570")])
        assert pairs == [("EMBC", "1724570")]

    def test_a_non_string_symbol_is_refused(self):
        """str(None) is 'None' and truthy - the news feed produced a
        company called NONE that way."""
        assert _issuer_pairs([Ev({"symbol": None, "issuer_cik": "1"}),
                              Ev({"symbol": 123, "issuer_cik": "1"})]) == []

    def test_no_filings_means_no_universe_rather_than_an_error(self):
        assert _issuer_pairs([]) == []
        assert _issuer_pairs(None) == []


class TestTheArmIsContained:
    """CLAUDE.md: the graded screen must never be displaced by a second
    source, and a failure in one arm must not cost the other."""

    def test_the_wiring_runs_after_the_screen_and_only_extends(self):
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler.build_candidates_all) \
            if hasattr(scheduler, "build_candidates_all") \
            else inspect.getsource(scheduler)
        drift = src[src.index("THE SECOND GRADED ARM"):]
        assert "out.extend(fresh)" in drift, (
            "the drift arm must ADD to the list, never rebuild it")
        assert "out = " not in drift.split("CLAUDE'S OWN HUNT")[0], (
            "the drift arm reassigns the candidate list, so a bug in it "
            "can drop the graded Form 4 candidates")

    def test_a_ticker_the_screen_already_found_is_not_duplicated(self):
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler)
        drift = src[src.index("THE SECOND GRADED ARM"):
                    src.index("CLAUDE'S OWN HUNT")]
        assert "known = {c.ticker for c in out}" in drift
        assert "if c.ticker not in known" in drift

    def test_a_failure_in_the_arm_is_caught(self):
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler)
        drift = src[src.index("THE SECOND GRADED ARM"):
                    src.index("CLAUDE'S OWN HUNT")]
        assert "except Exception" in drift
        assert "unaffected" in drift, (
            "a failure has to say that the graded arm survived it")

    def test_its_candidates_are_stamped_with_their_origin(self):
        """'They are stamped with their origin so the record can
        eventually say which is worth the money.'"""
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler)
        drift = src[src.index("THE SECOND GRADED ARM"):
                    src.index("CLAUDE'S OWN HUNT")]
        assert '_record_origin(conn, fresh, "earnings_drift"' in drift


class TestTheGradedCodeIsNotReimplemented:
    """The bake-off measured build_events and build_candidates. A live
    copy of that arithmetic would make the grade meaningless."""

    def test_the_feed_calls_the_graded_functions(self):
        import inspect

        from catalyst.data.sources import edgar_xbrl

        src = inspect.getsource(edgar_xbrl.drift_candidates)
        assert "from catalyst.strategies.earnings_drift import" in src
        assert "build_events" in src and "build_candidates" in src

    def test_the_feed_computes_no_signal_of_its_own(self):
        import inspect

        from catalyst.data.sources import edgar_xbrl

        body = inspect.getsource(edgar_xbrl)
        for forbidden in ("stdev", "sue =", "def _sue", "conviction"):
            assert forbidden not in body, (
                f"{forbidden!r} in the feed: the signal belongs to the "
                "graded strategy module, not to the fetcher")

    def test_a_broken_strategy_import_returns_nothing_not_an_error(self):
        from catalyst.data.sources.edgar_xbrl import drift_candidates

        assert drift_candidates("/nonexistent/dir", ["AAPL"]) == ([], {})
