"""Up to ten stocks drawn beside the bot, each with its own money.

OWNER-ASKED 2026-09-12: *"on the tab where i can set where to track SPY
from, can you edit it a bit so i can track up to 10 stocks at once, I
type the stock name exactly and set the date and amount, set SPY as
default, but then show as different colours on the graph so I can track
how we are beating multiple stocks."*

WHAT THESE TESTS PROTECT, in the order it can go wrong:

    1. **SPY DOES NOT VANISH WHEN A SECOND STOCK IS ADDED.** The list is
       synthesised from the account baseline while empty, so the first
       real write would otherwise make the new stock the only row and
       silently drop the line the owner was reading.
    2. **THIS TABLE NEVER TOUCHES THE ACCOUNT BASELINE.** That row has
       already been reset under the owner once (see §16 of the memory
       doc); a per-ticker row landing in `benchmark_baselines` would be
       returned by `benchmark.current()` as the account's own comparison.
    3. **A COMPARISON'S REFRESH MUST NOT REWRITE SPY'S FEED PIN.** One
       shared `cache_meta.json` meant fetching AAPL could leave SPY's
       series half consolidated tape and half one exchange's prints -
       the exact failure `catalyst/data/benchmark.py` exists to prevent,
       and a silent one.
    4. **A MISSING LINE SAYS WHY.** Mistyped ticker, bars not fetched
       yet, and window too short are three different answers that look
       identical as a gap in a chart.
    5. **ELEVEN LABELS DO NOT OVERPRINT OR ESCAPE THE VIEWBOX**, measured
       from the rendered SVG rather than read from the code.
    6. **NOTHING HERE CAN SIZE, SPEND OR TRADE.**

Fully offline.
"""

import csv
import os
import re
import sqlite3
from datetime import date, timedelta
from decimal import Decimal

import pytest

from catalyst import benchmark
from catalyst.benchmark import comparisons as cmp
from catalyst.dashboard import charts

AUG14 = date(2026, 8, 14)


@pytest.fixture
def bars(tmp_path, monkeypatch):
    root = tmp_path / "bars"
    root.mkdir()
    monkeypatch.setenv("CATALYST_BARS", str(root))
    return root


def write_bars(root, symbol, n=30, drift=0.002, start=AUG14):
    px = 100.0
    with (root / f"{symbol}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        day = start
        for _ in range(n):
            px *= (1 + drift)
            w.writerow([day.isoformat(), px, px, px, round(px, 4), 1000])
            day += timedelta(days=1)


@pytest.fixture
def db(tmp_path):
    from catalyst.storage import init_db

    path = str(tmp_path / "t.db")
    conn = init_db(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
        "the fixture must match production or it will agree with a bug")
    benchmark.record(conn, capital_cents=Decimal("200000"), start_date=AUG14,
                     source="owner_set", account_fingerprint="",
                     reason="owner set it: $2,000 on 14 August")
    conn.close()
    return path


class TestSpyIsTheDefaultAndDoesNotVanish:

    def test_an_untouched_list_is_spy_from_the_account_baseline(self, db):
        conn = sqlite3.connect(db)
        try:
            rows = cmp.tracked(conn, benchmark.current(conn))
        finally:
            conn.close()
        assert [c.ticker for c in rows] == ["SPY"]
        assert rows[0].is_default is True, (
            "the page must be able to say this is the default rather than a "
            "choice the owner made")
        assert rows[0].start_date == AUG14
        assert rows[0].capital_cents == Decimal("200000")

    def test_nothing_is_written_just_by_reading_the_default(self, db):
        conn = sqlite3.connect(db)
        try:
            for _ in range(3):
                cmp.tracked(conn, benchmark.current(conn))
            assert conn.execute(
                "SELECT COUNT(*) FROM benchmark_comparisons").fetchone()[0] == 0
        finally:
            conn.close()

    def test_adding_the_first_stock_KEEPS_spy(self, db):
        """The defect this guards: with nothing stored the list is
        synthesised, so a naive first write makes the new stock the only
        row and SPY disappears from a chart the owner was reading."""
        from catalyst.dashboard import server

        ok, _msg = server.track_stock(db, {
            "ticker": "AAPL", "amount_usd": "2000",
            "start_date": "2026-08-14"})
        assert ok
        conn = sqlite3.connect(db)
        try:
            tickers = [c.ticker for c in cmp.stored(conn)]
        finally:
            conn.close()
        assert "SPY" in tickers, (
            "SPY was dropped when the first stock was added")
        assert tickers == ["SPY", "AAPL"]

    def test_seeding_an_existing_list_reports_that_it_wrote_nothing(self, db):
        """`seed_from_baseline` is called on EVERY add, so its "already
        has rows" answer has to be truthful - a caller that believes it
        wrote SPY when it did not is the shape of the bug above, one
        indirection away. (`INSERT OR IGNORE` also protects the rows, so
        the two cover each other; this asserts the contract that is only
        held here.)"""
        from catalyst.dashboard import server

        server.track_stock(db, {"ticker": "AAPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        conn = sqlite3.connect(db)
        try:
            before = [(c.ticker, c.slot) for c in cmp.stored(conn)]
            assert cmp.seed_from_baseline(
                conn, benchmark.current(conn)) is False
            assert [(c.ticker, c.slot) for c in cmp.stored(conn)] == before
        finally:
            conn.close()

    def test_removing_everything_falls_back_to_the_default(self, db):
        from catalyst.dashboard import server

        server.track_stock(db, {"ticker": "AAPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        for t in ("SPY", "AAPL"):
            ok, _ = server.untrack_stock(db, {"ticker": t})
            assert ok
        conn = sqlite3.connect(db)
        try:
            rows = cmp.tracked(conn, benchmark.current(conn))
        finally:
            conn.close()
        assert [c.ticker for c in rows] == ["SPY"], (
            "a page built to compare cannot do it with one line")
        assert rows[0].is_default is True


class TestTheAccountBaselineIsNeverTouched:
    """§16 of the memory doc: this baseline has already been reset under
    the owner once."""

    def test_tracking_stocks_writes_no_baseline_row(self, db):
        from catalyst.dashboard import server

        before = sqlite3.connect(db)
        n_before = before.execute(
            "SELECT COUNT(*) FROM benchmark_baselines").fetchone()[0]
        before.close()
        for t, amt in (("AAPL", "2000"), ("NVDA", "500"), ("MSFT", "1000")):
            server.track_stock(db, {"ticker": t, "amount_usd": amt,
                                    "start_date": "2026-08-14"})
        server.untrack_stock(db, {"ticker": "NVDA"})
        conn = sqlite3.connect(db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM benchmark_baselines"
            ).fetchone()[0] == n_before, (
                "a tracked stock wrote to the ACCOUNT baseline table")
            base = benchmark.current(conn)
        finally:
            conn.close()
        assert base.start_date == AUG14
        assert base.capital_cents == Decimal("200000")
        assert base.source == "owner_set"

    def test_the_two_tables_are_separate_objects(self):
        """A ticker column on benchmark_baselines would be returned by
        benchmark.current() as the account's own comparison."""
        from pathlib import Path

        schema = Path("catalyst/storage/schema.sql").read_text()
        block = schema.split("CREATE TABLE IF NOT EXISTS benchmark_baselines")[1]
        block = block.split(");")[0]
        assert "ticker" not in block.lower(), (
            "the account baseline must not gain a ticker column")


class TestSlotsAreStableAndReused:

    def test_a_stock_keeps_its_colour_when_its_amount_is_corrected(self, db):
        from catalyst.dashboard import server

        server.track_stock(db, {"ticker": "AAPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        conn = sqlite3.connect(db)
        first = {c.ticker: c.slot for c in cmp.stored(conn)}
        conn.close()
        server.track_stock(db, {"ticker": "AAPL", "amount_usd": "3500",
                                "start_date": "2026-08-14"})
        conn = sqlite3.connect(db)
        try:
            after = {c.ticker: c.slot for c in cmp.stored(conn)}
            amounts = {c.ticker: c.capital_cents for c in cmp.stored(conn)}
        finally:
            conn.close()
        assert after == first, (
            "correcting an amount re-coloured a line the owner has been "
            "watching")
        assert amounts["AAPL"] == Decimal("350000")

    def test_the_returned_row_carries_the_slot_that_is_stored(self, db):
        """The stored slot is protected twice - `add` reuses the existing
        one AND the ON CONFLICT clause does not update the column - so
        breaking either alone changes nothing on disk. What only the first
        holds is the object handed back, which the success message and
        any caller read: a Comparison claiming slot 3 for a row stored at
        slot 2 is a lie about which colour the line will be."""
        server_add = __import__(
            "catalyst.dashboard.server", fromlist=["track_stock"])
        server_add.track_stock(db, {"ticker": "AAPL", "amount_usd": "2000",
                                    "start_date": "2026-08-14"})
        conn = sqlite3.connect(db)
        try:
            stored_slot = {c.ticker: c.slot for c in cmp.stored(conn)}["AAPL"]
            returned = cmp.add(conn, ticker="AAPL", amount="3500",
                               start="2026-08-14")
        finally:
            conn.close()
        assert returned.slot == stored_slot, (
            f"returned slot {returned.slot} but stored slot is {stored_slot}")

    def test_a_removed_colour_is_reused_rather_than_walked_past(self, db):
        from catalyst.dashboard import server

        for t in ("AAPL", "NVDA", "MSFT"):
            server.track_stock(db, {"ticker": t, "amount_usd": "2000",
                                    "start_date": "2026-08-14"})
        server.untrack_stock(db, {"ticker": "NVDA"})
        conn = sqlite3.connect(db)
        freed = cmp.free_slot(conn)
        conn.close()
        server.track_stock(db, {"ticker": "AMZN", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        conn = sqlite3.connect(db)
        try:
            slots = {c.ticker: c.slot for c in cmp.stored(conn)}
        finally:
            conn.close()
        assert slots["AMZN"] == freed
        assert slots["AMZN"] < slots["MSFT"], (
            "walking off the end of the palette instead of reusing a freed "
            "slot runs out of colours after ten add/removes")

    def test_no_two_tracked_stocks_share_a_colour(self, db):
        from catalyst.dashboard import server

        names = ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "TSLA", "META",
                 "JPM", "XOM"]
        for t in names:
            ok, msg = server.track_stock(db, {
                "ticker": t, "amount_usd": "2000",
                "start_date": "2026-08-14"})
            assert ok, msg
        conn = sqlite3.connect(db)
        try:
            rows = cmp.stored(conn)
        finally:
            conn.close()
        slots = [c.slot for c in rows]
        assert len(rows) == 10, "SPY plus nine"
        assert len(set(slots)) == len(slots)
        assert cmp.BOT_SLOT not in slots, (
            "a comparison took the colour the reader has learned means 'us'")

    def test_the_eleventh_stock_is_refused_with_a_sentence(self, db):
        from catalyst.dashboard import server

        for t in ["AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "TSLA", "META",
                  "JPM", "XOM"]:
            server.track_stock(db, {"ticker": t, "amount_usd": "2000",
                                    "start_date": "2026-08-14"})
        ok, msg = server.track_stock(db, {"ticker": "KO", "amount_usd": "2000",
                                          "start_date": "2026-08-14"})
        assert ok is False
        assert "Nothing was changed" in msg
        assert str(cmp.MAX_COMPARISONS) in msg
        # THE LIMIT IS ENFORCED TWICE - `add` counts the rows and
        # `free_slot` runs out of slots - so breaking either alone still
        # refuses. What only `add`'s check gives is the REJECTED STOCK'S
        # NAME, which is the difference between "remove one before adding
        # KO" and a sentence about slots the owner never asked about.
        assert "KO" in msg, f"the refusal does not say which stock: {msg!r}"


class TestEveryRefusalIsASentence:

    @pytest.mark.parametrize("form,expect", [
        ({"ticker": "Apple Inc", "amount_usd": "2000",
          "start_date": "2026-08-14"}, "not a ticker"),
        ({"ticker": "", "amount_usd": "2000",
          "start_date": "2026-08-14"}, "ticker symbol"),
        ({"ticker": "AAPL", "amount_usd": "", "start_date": "2026-08-14"},
         "amount"),
        ({"ticker": "AAPL", "amount_usd": "lots", "start_date": "2026-08-14"},
         "not an amount of money"),
        ({"ticker": "AAPL", "amount_usd": "NaN", "start_date": "2026-08-14"},
         "not a finite amount"),
        ({"ticker": "AAPL", "amount_usd": "0", "start_date": "2026-08-14"},
         "below the $1 minimum"),
        ({"ticker": "AAPL", "amount_usd": "99999999",
          "start_date": "2026-08-14"}, "above the $10,000,000 maximum"),
        ({"ticker": "AAPL", "amount_usd": "2000", "start_date": "not-a-date"},
         "not a date"),
        ({"ticker": "AAPL", "amount_usd": "2000", "start_date": "1980-01-01"},
         "before 1993-01-29"),
    ])
    def test_a_bad_field_never_reaches_a_traceback(self, db, form, expect):
        from catalyst.dashboard import server

        ok, msg = server.track_stock(db, form)
        assert ok is False
        assert expect in msg, f"got {msg!r}"
        assert "Traceback" not in msg
        assert "Nothing was changed" in msg
        # AND IT IS NOT DRESSED UP AS A WRITE FAILURE. The generic
        # handler below it also produces a sentence, so removing the
        # Invalid branch still "works" - but it prefixes "the tracked
        # list could not be written", which sends the owner to look at
        # their database when they mistyped a ticker.
        assert "could not be written" not in msg, (
            f"a mistyped field is reported as a database failure: {msg!r}")

    def test_a_future_date_is_refused_against_now_not_a_fixture_date(self, db):
        """House rule 6: measured against datetime.now(), so this cannot
        drift out of the window a day at a time."""
        from datetime import datetime, timezone

        from catalyst.dashboard import server

        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        ok, msg = server.track_stock(db, {
            "ticker": "AAPL", "amount_usd": "2000",
            "start_date": tomorrow.isoformat()})
        assert ok is False
        assert "in the future" in msg

    def test_removing_something_not_tracked_says_so(self, db):
        from catalyst.dashboard import server

        ok, msg = server.untrack_stock(db, {"ticker": "ZZZZ"})
        assert ok is False
        assert "nothing to remove" in msg

    def test_the_ticker_is_cleaned_not_guessed_at(self):
        assert cmp.clean_ticker("  aapl ") == "AAPL"
        assert cmp.clean_ticker("brk.b") == "BRK.B"
        with pytest.raises(cmp.Invalid):
            cmp.clean_ticker("123")
        with pytest.raises(cmp.Invalid):
            cmp.clean_ticker("AAPL;DROP")


class TestAMissingLineSaysWhy:
    """House rule 3. Three causes, three answers, one symptom."""

    def test_a_ticker_with_no_cached_bars_names_itself(self, db, bars):
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import server

        write_bars(bars, "SPY")
        server.track_stock(db, {"ticker": "APPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        d = Db(db)
        try:
            series = {c.ticker: c for c in queries.benchmark_view(d).comparisons}
        finally:
            d.close()
        missing = series["APPL"]
        assert missing.points == []
        assert missing.value_cents is None, "never 0 - that would be a figure"
        assert "APPL" in (missing.error or ""), (
            "with ten lines the reader must be told WHICH one has no bars: "
            f"got {missing.error!r}")
        assert "APPL.csv" in missing.source

    def test_the_owner_is_never_told_to_run_a_script(self, db, bars):
        """`BarCache.load_bars` raises a message written for a developer
        running a backtest. The owner is not a developer and the brief
        says they must never be told to run something - and the honest
        answer here is "the bot fetches it tonight", which the raw
        exception cannot say."""
        from catalyst.dashboard import panels, queries, server
        from catalyst.dashboard.db import Db

        write_bars(bars, "SPY")
        server.track_stock(db, {"ticker": "APPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        d = Db(db)
        try:
            series = {c.ticker: c for c in queries.benchmark_view(d).comparisons}
            html = panels.benchmark_panel(d)
        finally:
            d.close()
        why = series["APPL"].error or ""
        assert "fetch_history" not in why.split("Raw:")[0], (
            f"the owner is told to run a script: {why!r}")
        # HOUSE RULE 3: the raw upstream text still follows the sentence.
        assert "Raw: KeyError" in why
        # THE ADVICE IS ON THE PAGE, not in the loader. The loader also
        # serves the ACCOUNT's own SPY, where "check the spelling" would
        # be nonsense - and a 59-word paragraph there broke the page's
        # measured words-per-figure budget, which is how this was found.
        block = html[html.find("bench-tracked"):][:8000]
        assert "next daily refresh" in block
        # A mistyped ticker and a stock added a minute ago look the same, so
        # the row has to cover both - and since the owner asked (2026-09-12)
        # it must name the OTHER cause of an unknown symbol too: a London
        # listing such as VUAG has no bars from a US broker.
        assert "spelling" in block
        assert "US-listed" in block, (
            "the row must say why a real, correctly spelled symbol can still "
            "have no prices")
        assert "VOO" in block, "and what to use instead"

    def test_the_DEFAULT_row_is_not_told_to_check_its_spelling(self, tmp_path,
                                                               monkeypatch):
        """A BRAND-NEW INSTALL: no baseline, no bars, so the list holds the
        synthesised SPY default and it has no series. The owner never typed
        SPY, so telling them to check its spelling is the same nonsense
        that was moved out of the loader, reappearing one level up.

        Uses its own fresh database rather than the `db` fixture, because
        the defect only exists when the row is the DEFAULT.
        """
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.storage import init_db

        monkeypatch.setenv("CATALYST_BARS", str(tmp_path / "no-bars"))
        path = str(tmp_path / "fresh.db")
        init_db(path).close()
        d = Db(path)
        try:
            html = panels.benchmark_panel(d)
        finally:
            d.close()
        block = html[html.find("bench-tracked"):][:4000]
        assert "SPY" in block and "the default" in block
        assert "the spelling is wrong" not in block, (
            "the default row was told to check the spelling of a ticker the "
            f"owner never typed: {block[:400]!r}")
        assert "next daily refresh" in block, (
            "it must still say when the line will appear")

    def test_a_short_window_is_not_told_to_check_its_spelling(self, db, bars):
        """The advice is for ONE cause - no cached file at all. A stock
        whose bars exist but predate its start date has a real ticker and
        a real cache; telling that reader to check the spelling sends them
        after a problem they do not have."""
        from catalyst.dashboard import panels, server
        from catalyst.dashboard.db import Db

        write_bars(bars, "SPY")
        write_bars(bars, "NVDA", n=5, start=date(2026, 7, 1))
        server.track_stock(db, {"ticker": "NVDA", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        d = Db(db)
        try:
            html = panels.benchmark_panel(d)
        finally:
            d.close()
        block = html[html.find("bench-tracked"):][:9000]
        row = block[block.find("NVDA"):]
        row = row[:row.find("</tr>") + 5] if "</tr>" in row else row
        assert "the spelling is wrong" not in row, (
            f"a short window was told to check its spelling: {row[:400]!r}")

    def test_the_account_benchmark_is_not_told_to_check_its_spelling(
            self, db, bars):
        """SPY is not a ticker the owner typed. The advice for a typed
        stock must not leak onto the account's own benchmark, whose error
        text the performance page prints verbatim."""
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db

        # No bars for anything, so the account benchmark has no series.
        d = Db(db)
        try:
            perf = queries.performance(d)
        finally:
            d.close()
        why = perf.spy_error or ""
        assert "check the spelling" not in why, f"got {why!r}"
        assert "fetch_history" not in why.split("Raw:")[0]

    def test_a_short_window_is_not_reported_as_a_broken_feed(self, db, bars):
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import server

        write_bars(bars, "SPY")
        # Bars exist but all of them predate this comparison's start.
        write_bars(bars, "NVDA", n=5, start=date(2026, 7, 1))
        server.track_stock(db, {"ticker": "NVDA", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        d = Db(db)
        try:
            series = {c.ticker: c for c in queries.benchmark_view(d).comparisons}
        finally:
            d.close()
        why = series["NVDA"].error or ""
        assert "NVDA" in why
        assert "not a fault" in why or "has not been updated" in why, (
            f"got {why!r}")

    def test_the_panel_prints_the_reason_beside_the_dash(self, db, bars):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import server

        write_bars(bars, "SPY")
        server.track_stock(db, {"ticker": "APPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        d = Db(db)
        try:
            html = panels.benchmark_panel(d)
        finally:
            d.close()
        i = html.find("bench-tracked")
        assert i >= 0
        block = html[i:i + 6000]
        assert "APPL" in block
        assert "APPL.csv" in block, (
            "the raw source must sit beside the missing figure")

    def test_an_unreadable_stored_row_is_counted_not_hidden(self, db, bars):
        from catalyst.dashboard import queries
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import server

        write_bars(bars, "SPY")
        server.track_stock(db, {"ticker": "AAPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        conn = sqlite3.connect(db)
        conn.execute("UPDATE benchmark_comparisons SET start_date = 'nope' "
                     "WHERE ticker = 'AAPL'")
        conn.commit()
        assert cmp.unreadable(conn) == 1
        assert [c.ticker for c in cmp.stored(conn)] == ["SPY"], (
            "one bad row must not hide the others")
        conn.close()
        d = Db(db)
        try:
            assert queries.benchmark_view(d).comparisons_unreadable == 1
        finally:
            d.close()


class TestTheFeedPinIsPerSymbol:
    """A comparison's refresh rewriting SPY's basis would leave the SPY
    series half consolidated tape and half one exchange's prints - the
    failure catalyst/data/benchmark.py exists to prevent."""

    def test_spy_keeps_the_unqualified_metadata_file(self):
        from catalyst.data.benchmark import _meta_key

        assert _meta_key("SPY") is None
        assert _meta_key("spy") is None
        assert _meta_key("AAPL") == "AAPL"

    def test_two_symbols_do_not_share_a_metadata_file(self, tmp_path):
        from catalyst.backtest.data import BarCache

        cache = BarCache(str(tmp_path))
        cache.write_meta({"symbol": "SPY", "feed": "sip"})
        cache.write_meta({"symbol": "AAPL", "feed": "iex"}, "AAPL")
        assert cache.read_meta()["feed"] == "sip", (
            "AAPL's refresh overwrote SPY's feed pin")
        assert cache.read_meta("AAPL")["feed"] == "iex"

    def test_refreshing_a_comparison_leaves_spys_pin_alone(self, tmp_path):
        from catalyst.backtest.data import Bar, BarCache
        from catalyst.data import benchmark as bench

        root = str(tmp_path)
        cache = BarCache(root)
        cache.write_meta({"symbol": "SPY", "feed": "sip",
                          "adjustment": "all"})
        cache.write_bars("SPY", [Bar(day=AUG14, open=Decimal("1"),
                                     high=Decimal("1"), low=Decimal("1"),
                                     close=Decimal("1"),
                                     volume=Decimal("1"))])

        def client_factory(_headers):
            class C:
                @staticmethod
                def close():
                    pass
            return C()

        def fake_fetch(_client, symbols, _start, _end, feed="iex"):
            day = AUG14 + timedelta(days=1)
            return ({symbols[0]: [Bar(day=day, open=Decimal("2"),
                                      high=Decimal("2"), low=Decimal("2"),
                                      close=Decimal("2"),
                                      volume=Decimal("9"))]}, [])

        import catalyst.data.benchmark as mod
        real = mod.fetch_daily_bars
        mod.fetch_daily_bars = fake_fetch
        try:
            out = bench.refresh_comparisons(
                root, "k", "s", ["AAPL"], today=date(2026, 8, 20),
                client_factory=client_factory)
        finally:
            mod.fetch_daily_bars = real
        assert out["AAPL"].written == 1
        assert cache.read_meta()["feed"] == "sip", (
            "SPY's pin was rewritten by a comparison refresh")
        assert cache.read_meta()["symbol"] == "SPY"
        assert cache.read_meta("AAPL")["symbol"] == "AAPL"

    def test_spy_is_skipped_by_the_comparison_refresher(self, tmp_path):
        """refresh_benchmark already owns SPY on its own schedule;
        fetching it twice a cycle spends requests to learn nothing."""
        from catalyst.data import benchmark as bench

        out = bench.refresh_comparisons(str(tmp_path), "", "", ["SPY", "spy"])
        assert out == {}

    def test_one_bad_symbol_does_not_cost_the_others(self, tmp_path):
        from catalyst.data import benchmark as bench

        calls = []

        def boom(*_a, **kw):
            calls.append(kw.get("symbol"))
            if kw.get("symbol") == "BAD":
                raise RuntimeError("upstream exploded")
            return bench.RefreshResult(written=1)

        real = bench.refresh_benchmark
        bench.refresh_benchmark = boom
        try:
            out = bench.refresh_comparisons(
                str(tmp_path), "k", "s", ["AAPL", "BAD", "NVDA"])
        finally:
            bench.refresh_benchmark = real
        assert calls == ["AAPL", "BAD", "NVDA"]
        assert out["AAPL"].written == 1
        assert out["NVDA"].written == 1
        assert "refresh_raised_RuntimeError" in out["BAD"].skipped_reason


class TestTheSchedulerActuallyRefreshesThem:
    """Section 6, "a helper nobody calls": assert the CALL SITE."""

    def test_the_cycle_calls_the_comparison_refresher(self):
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler._maybe_refresh_benchmark)
        assert "_refresh_tracked_comparisons(" in src, (
            "nothing fetches bars for the tracked stocks, so every line "
            "but SPY would be permanently empty")

    def test_it_has_its_own_marker_so_spy_is_never_held_up(self):
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler._refresh_tracked_comparisons)
        assert "comparison_key" in src
        assert "benchmark_day" not in src, (
            "sharing SPY's marker means a comparison ticker Alpaca will "
            "not answer can stop SPY being refreshed")

    def test_a_stock_added_today_is_fetched_on_the_NEXT_cycle(self):
        """OWNER-ASKED: add a stock mid-afternoon and compare it against
        the last ten days. The date-only marker meant a day on which
        nothing extra was tracked set it and returned, so the new stock
        waited until TOMORROW - an empty row for up to a day, looking
        exactly like a mistyped ticker.

        The marker has to name the tracked SET, so adding one stops it
        matching. Asserted by calling the function twice with a real
        state dict, which is the behaviour rather than the source text.
        """
        import sqlite3 as _sq

        from catalyst.orchestrator import scheduler

        calls = []

        class Creds:
            alpaca_key = "k"
            alpaca_secret = "s"

        import catalyst.data.benchmark as bench

        real = bench.refresh_comparisons

        def spy(_root, _k, _s, symbols, **_kw):
            calls.append(tuple(sorted(symbols)))
            return {s: bench.RefreshResult(written=1) for s in symbols}

        bench.refresh_comparisons = spy
        try:
            import tempfile
            from catalyst.storage import init_db
            from catalyst.dashboard import server

            with tempfile.TemporaryDirectory() as d:
                path = f"{d}/t.db"
                conn = init_db(path)
                benchmark.record(conn, capital_cents=Decimal("200000"),
                                 start_date=AUG14, source="owner_set",
                                 account_fingerprint="", reason="x")
                conn.close()
                state = {"something": "else"}   # non-empty: falsy is a trap
                today = date(2026, 9, 12)
                c = _sq.connect(path)
                try:
                    # Pass one: only the default SPY, nothing to fetch.
                    scheduler._refresh_tracked_comparisons(
                        c, path, Creds(), state, today)
                    assert calls == [], "SPY is the refresher's own job"
                    marked = state.get("comparison_key")
                    assert marked, "the quiet pass must still mark itself"
                    # The owner adds a stock, same day.
                    server.track_stock(path, {
                        "ticker": "AAPL", "amount_usd": "2000",
                        "start_date": "2026-09-02"})
                    scheduler._refresh_tracked_comparisons(
                        c, path, Creds(), state, today)
                    # AND A SETTLED SET IS NOT RE-FETCHED. Without the
                    # short-circuit the add still works, so "it got
                    # fetched" alone does not prove the marker is read -
                    # a third pass with nothing changed does.
                    scheduler._refresh_tracked_comparisons(
                        c, path, Creds(), state, today)
                finally:
                    c.close()
            assert calls == [("AAPL",)], (
                "the stock added today was not fetched until tomorrow: "
                f"{calls}")
            assert state["comparison_key"] != marked
        finally:
            bench.refresh_comparisons = real

    def test_a_stuck_symbol_does_not_burn_the_days_only_try(self):
        import inspect

        from catalyst.orchestrator import scheduler

        src = inspect.getsource(scheduler._refresh_tracked_comparisons)
        marker = src.index('state["comparison_key"] = done_key',
                           src.index("stuck"))
        guard = src.index("if not stuck:")
        assert guard < marker, (
            "the marker is set before the failures are checked, so one "
            "transient failure costs the whole day - the defect that left "
            "the SPY line 48 hours stale")


class TestTheChartDrawsEveryLineDistinguishably:

    def _eleven(self):
        series = [charts.Series("bot", [(0, 100.0), (10, 103.0)],
                                charts.comparison_color(0), end_label="BOT",
                                emphasis=True)]
        names = ["SPY", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "BRK.B",
                 "TSLA", "META", "JPM"]
        for i, t in enumerate(names):
            # Deliberately near-flat against each other: the worst case
            # for end labels, and the NORMAL case at ten lines.
            series.append(charts.Series(
                t, [(0, 100.0), (10, 103.0 + i * 0.05)],
                charts.comparison_color(i + 1),
                dash=charts.comparison_dash(i + 1), end_label=t))
        return series, names

    def _svg(self):
        series, names = self._eleven()
        return charts.index_chart(
            series, chart_id="c",
            x_labels=[(0, "2026-08-14"), (10, "2026-09-12")],
            start_capital_dollars=2000.0), names

    def test_no_label_escapes_the_viewbox(self):
        svg, _ = self._svg()
        assert charts.labels_outside_viewbox(svg) == [], (
            "the defect this module's docstring opens with, one series at "
            "a time")

    def test_no_two_end_labels_overprint(self):
        """This dashboard has already shipped labels that overprinted into
        "skipp/jjjgted". Measured from the rendered SVG, with the same
        conservative box `text_boxes` uses."""
        svg, names = self._svg()
        ends = re.findall(
            r'<text x="[\d.]+" y="([\d.]+)" font-size="([\d.]+)" '
            r'text-anchor="start" fill="var\(--cmp-\d+\)">([A-Z.]+)</text>',
            svg)
        assert len(ends) == 11, f"expected 11 end labels, got {len(ends)}"
        rows = sorted((float(y), float(size), t) for y, size, t in ends)
        for (y0, size0, t0), (y1, _s1, t1) in zip(rows, rows[1:]):
            bottom = y0 + size0 * (charts.LINE_H - 1.0)
            assert bottom <= y1 - size0, (
                f"{t0} and {t1} overprint at y={y0} and y={y1}")

    def test_labels_clustered_at_the_BOTTOM_do_not_run_off_the_plot(self):
        """Pushing labels DOWN to space them apart runs out of plot when
        the lines are already near the bottom - eleven losers, which is
        the case an owner tracking ten stocks in a drawdown sees. The
        stack has to slide back up. Nothing in the flat-near-the-top
        fixture reaches this branch, which is why it needs its own test."""
        series = [charts.Series("bot", [(0, 100.0), (10, 120.0)],
                                charts.comparison_color(0), end_label="BOT",
                                emphasis=True)]
        names = ["SPY", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "BRK.B",
                 "TSLA", "META", "JPM"]
        for i, t in enumerate(names):
            # all crammed against the FLOOR of the y range
            series.append(charts.Series(
                t, [(0, 100.0), (10, 80.0 + i * 0.05)],
                charts.comparison_color(i + 1),
                dash=charts.comparison_dash(i + 1), end_label=t))
        svg = charts.index_chart(
            series, chart_id="c",
            x_labels=[(0, "2026-08-14"), (10, "2026-09-12")],
            start_capital_dollars=2000.0)
        assert charts.labels_outside_viewbox(svg) == [], (
            "the pushed label stack ran off the bottom of the chart")
        ends = re.findall(
            r'<text x="[\d.]+" y="([\d.]+)" font-size="([\d.]+)" '
            r'text-anchor="start" fill="var\(--cmp-\d+\)">([A-Z.]+)</text>',
            svg)
        assert len(ends) == 11
        rows = sorted((float(y), float(s), t) for y, s, t in ends)
        for (y0, size0, t0), (y1, _s, t1) in zip(rows, rows[1:]):
            assert y0 + size0 * (charts.LINE_H - 1.0) <= y1 - size0, (
                f"{t0} and {t1} overprint")

    def test_a_stack_that_FITS_but_sits_too_low_is_slid_back_inside(self):
        """The case the even-spacing fallback does NOT cover, and the only
        one where the slide is load-bearing.

        EVERY line ends clustered at the bottom - seven stocks all down the
        same amount - so the labels' true span is tiny, the fallback sees a
        stack that fits, and the push alone would walk the last label out
        through the axis and the legend. My first attempt put the bot's
        line at the TOP, which widened the span, fired the fallback, and
        left the slide untested: the sabotage came back green.
        """
        names = ["BOT", "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "TSLA"]
        series = [charts.Series(
            t, [(0, 100.0), (10, 80.0 + i * 0.01)],
            charts.comparison_color(i), dash=charts.comparison_dash(i),
            end_label=t, emphasis=(i == 0)) for i, t in enumerate(names)]
        svg = charts.index_chart(
            series, chart_id="c",
            x_labels=[(0, "2026-08-14"), (10, "2026-09-12")],
            start_capital_dollars=2000.0)
        assert charts.labels_outside_viewbox(svg) == [], (
            "the label stack walked out through the bottom of the chart")
        ends = re.findall(
            r'<text x="[\d.]+" y="([\d.]+)" font-size="([\d.]+)" '
            r'text-anchor="start" fill="var\(--cmp-\d+\)">([A-Z.]+)</text>',
            svg)
        assert len(ends) == len(names)
        rows = sorted((float(y), float(s), t) for y, s, t in ends)
        # Still spaced by the REAL gap, not squeezed - so this genuinely
        # took the slide branch rather than the fallback.
        gaps = [round(b - a, 1) for (a, _s, _t), (b, _s2, _t2)
                in zip(rows, rows[1:])]
        assert all(abs(g - (charts.FONT_SIZE * charts.LINE_H + 1.0)) < 0.2
                   for g in gaps), (
            f"the fallback fired on a stack that fits: gaps {gaps}")

    def test_every_line_has_its_own_colour_and_its_own_dash(self):
        series, _ = self._eleven()
        colours = [s.color for s in series]
        assert len(set(colours)) == len(colours)
        dashes = [s.dash for s in series]
        assert len(set(dashes)) == len(dashes), (
            "colour alone cannot carry eleven identities - measured worst "
            "CVD dE falls to 5.8, so the dash has to differ too")

    def test_the_bot_is_solid_and_the_reference(self):
        assert charts.comparison_dash(0) == "", (
            "the line everything is compared against should read as the "
            "reference, not as one of the comparisons")
        assert charts.comparison_dash(1) != ""

    def test_identity_is_never_colour_alone(self):
        """The project's standing rule. Each line's ticker is printed at
        its own end, so a reader who cannot separate two hues still knows
        which line is which."""
        svg, names = self._svg()
        for t in names + ["BOT"]:
            assert f">{t}</text>" in svg, f"{t} is identified by colour only"

    def test_a_slot_past_the_palette_wraps_rather_than_raising(self):
        assert charts.comparison_color(99).startswith("var(--cmp-")
        assert charts.comparison_dash(99) in charts.COMPARISON_DASHES

    def test_the_performance_chart_draws_one_line_per_tracked_stock(
            self, db, bars):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import server

        for sym, drift in (("SPY", 0.002), ("AAPL", 0.004), ("NVDA", -0.001)):
            write_bars(bars, sym, drift=drift)
        for t in ("AAPL", "NVDA"):
            server.track_stock(db, {"ticker": t, "amount_usd": "2000",
                                    "start_date": "2026-08-14"})
        d = Db(db)
        try:
            html = panels.performance_panel(d)
        finally:
            d.close()
        for t in ("BOT", "SPY", "AAPL", "NVDA"):
            assert f">{t}</text>" in html, f"{t} is not on the chart"

    def test_spy_is_not_drawn_twice(self, db, bars):
        """The account benchmark and the tracked list both hold SPY.
        Drawing both puts two identical lines on the chart."""
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        write_bars(bars, "SPY")
        d = Db(db)
        try:
            html = panels.performance_panel(d)
        finally:
            d.close()
        assert html.count(">SPY</text>") == 1, (
            f"SPY end label drawn {html.count('>SPY</text>')} times")

    def test_the_account_benchmark_is_still_drawn_if_spy_is_untracked(
            self, db, bars):
        """Every tile and alarm on the page is computed from the account's
        own SPY baseline, so a chart without it would disagree with the
        figures above it."""
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db
        from catalyst.dashboard import server

        write_bars(bars, "SPY")
        write_bars(bars, "AAPL", drift=0.004)
        server.track_stock(db, {"ticker": "AAPL", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        server.untrack_stock(db, {"ticker": "SPY"})
        d = Db(db)
        try:
            html = panels.performance_panel(d)
        finally:
            d.close()
        assert html.count(">SPY</text>") == 1
        assert ">AAPL</text>" in html


class TestNothingHereCanSizeSpendOrTrade:

    def test_the_risk_engine_never_reads_the_tracked_list(self):
        import subprocess

        out = subprocess.run(
            ["grep", "-rn", "benchmark_comparisons", "catalyst/risk",
             "catalyst/execution", "catalyst/cost"],
            capture_output=True, text=True)
        assert out.stdout.strip() == "", (
            "a display preference reached the money path:\n" + out.stdout)

    def test_the_comparisons_module_imports_nothing_that_trades(self):
        import subprocess

        out = subprocess.run(
            ["grep", "-nE", r"^\s*(from|import)\s+catalyst\.(risk|execution)",
             "catalyst/benchmark/comparisons.py"],
            capture_output=True, text=True)
        assert out.stdout.strip() == ""

    @staticmethod
    def _regions(html):
        """The three places the US-listed rule has to appear, as separate
        strings.

        SEPARATE ON PURPOSE. The first version of these tests asserted
        "US-listed" was in the panel SOMEWHERE - and it appears in the
        label, the note and the empty row, so deleting any one of them
        left the others and every sabotage came back green. A test that
        cannot tell which of three copies it found is not testing any of
        them.
        """
        block = html[html.find("bench-tracked"):]
        label = re.search(
            r'<label class="prov">([^<]*)<input[^>]*id="[^"]*track-ticker"',
            block)
        note = re.search(r"Up to \d+ stocks.*?</p>", block, re.DOTALL)
        return (label.group(1) if label else ""), (note.group(0) if note else "")

    def test_the_INPUT_LABEL_says_US_listed_before_anything_is_typed(self, db):
        """OWNER-ASKED 2026-09-12: *"Can we make it clear only add US
        stocks that are listed if not already"*.

        On the label itself, in visible text - not in a `title` attribute
        a mouse has to hover to find, and not only in the note below.
        """
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        d = Db(db)
        try:
            label, _note = self._regions(panels.benchmark_panel(d))
        finally:
            d.close()
        assert label, "the ticker input has no label at all"
        assert "US-listed" in label, (
            f"the field the owner types into does not say so: {label!r}")

    def test_the_NOTE_states_the_rule_and_names_the_trap(self, db):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        d = Db(db)
        try:
            _label, note = self._regions(panels.benchmark_panel(d))
        finally:
            d.close()
        assert note, "the tracked-stocks note is missing"
        assert "US-listed" in note
        assert "VUAG" in note, (
            "name the trap the owner actually asked about, not just the rule")
        assert "VOO" in note, "and what to use instead"
        # And it must not promise a check the code does not make: the
        # ticker is validated by SHAPE so a brand-new listing is never
        # wrongly refused, which means an unknown symbol IS accepted.
        assert "does not exist is accepted" in note, (
            "saying US-listed only, where nothing enforces it, is a promise "
            "the code does not keep - the note has to say a bad symbol is "
            "taken and reports itself")

    def test_the_EMPTY_ROW_names_the_non_US_cause_too(self, db, bars):
        """A correctly spelled, real symbol can still have no prices. The
        row is where the owner looks when a line does not appear, so
        "check the spelling" alone sends them after the wrong thing."""
        from catalyst.dashboard import panels, server
        from catalyst.dashboard.db import Db

        write_bars(bars, "SPY")
        server.track_stock(db, {"ticker": "VUAG", "amount_usd": "2000",
                                "start_date": "2026-08-14"})
        d = Db(db)
        try:
            html = panels.benchmark_panel(d)
        finally:
            d.close()
        block = html[html.find("bench-tracked"):]
        i = block.find("VUAG")
        row = block[i:block.find("</tr>", i) + 5]
        assert "no daily closes are cached" in row
        assert "US-listed" in row, (
            f"the row blames only the spelling: {row[-500:]!r}")
        assert "VOO" in row, "and says what to use instead"

    def test_the_success_message_says_it_changes_only_the_comparison(self, db):
        from catalyst.dashboard import panels
        from catalyst.dashboard.db import Db

        d = Db(db)
        try:
            html = panels.benchmark_panel(d)
        finally:
            d.close()
        i = html.find("bench-tracked")
        block = html[i:i + 6000]
        assert "never what it may spend, size or trade" in block
