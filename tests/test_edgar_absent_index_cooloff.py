"""Do not ask sec.gov 93 times a day for a file that cannot exist.

OWNER'S BUNDLE, 2026-08-24: 252 requests to sec.gov in one day, every
one a 403, for three dates:

    form.20260822.idx   93 times   (a Saturday)
    form.20260823.idx   93 times   (a Sunday)
    form.20260824.idx   66 times   (today, before the evening publish)

Nothing was broken. The feed already reads an S3 "AccessDenied" as "no
file yet" rather than as a block, and it recorded every one with its raw
body. The cost is the rate limit: TRAPS.md warns it is 10 req/s across
ALL SEC APIs and that an overrun blocks the IP for every SEC feed in the
process. Spending 252 of that budget on files that cannot exist is a
real risk taken for nothing.

A COOL-OFF, NEVER A PERMANENT SKIP. Today's index does publish in the
evening, so a date has to be asked again eventually; the most a new
index can go unnoticed is one cool-off. And a date that publishes is
forgotten immediately, so a later pass over the same window can never be
told to skip a real index.

Fully offline: every response is a stub.
"""

from datetime import date

import pytest

from catalyst.data.sources import edgar_form4 as f4

#: HOUSE RULE 6 does not apply: the clock below is injected and the
#: module compares only against it. Nothing measures against wall time.
SATURDAY = date(2026, 8, 22)
FRIDAY = date(2026, 8, 21)

ABSENT_BODY = "<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
#: A real index that happens to contain no Form 4. What matters here is
#: that the .idx answered 200 - the submission fetches are another
#: module's job and are covered by tests/test_data_edgar.py.
INDEX_TEXT = (
    "Form Type Company Name CIK Date Filed File Name\n"
    "---------------------------------------------------\n"
    "8-K        EXAMPLE INC       0000320193 20260821 "
    "edgar/data/320193/0000320193-26-000001.txt\n"
)


class Stub:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class Clock:
    """An injected monotonic clock the test advances by hand."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture(autouse=True)
def clean_memo():
    f4.clear_absent_index_memo()
    yield
    f4.clear_absent_index_memo()


def sweep(day, responder, clock, calls):
    """One pass over a single date, with the clock and network injected."""

    def get(url, headers):
        calls.append(url)
        return responder(url)

    return f4.fetch_form4(
        since=day, until=day, http_get=get,
        monotonic=clock, sleep=lambda _s: None,
        rate_per_sec=f4.SEC_MAX_REQUESTS_PER_SEC)


def absent(_url):
    return Stub(403, ABSENT_BODY)


class TestADateThatSaidNoIsNotAskedAgainImmediately:
    def test_the_second_pass_spends_no_request(self):
        clock, calls = Clock(), []
        sweep(SATURDAY, absent, clock, calls)
        assert len(calls) == 1

        clock.advance(60)                     # one cycle later
        sweep(SATURDAY, absent, clock, calls)
        assert len(calls) == 1, (
            "the same date was requested again a minute later; that is the "
            "93-requests-a-day pattern from the owner's bundle")

    def test_the_date_is_still_reported_as_missing(self):
        """House rule 3: a zero is never left unexplained. Skipping the
        request must not also skip the explanation."""
        clock, calls = Clock(), []
        sweep(SATURDAY, absent, clock, calls)
        clock.advance(60)
        result = sweep(SATURDAY, absent, clock, calls)

        assert len(result.missing_index_dates) == 1
        entry = result.missing_index_dates[0]
        assert entry["date"] == SATURDAY.isoformat()
        assert "not requested" in entry["raw_text"]
        assert "rate limit" in entry["raw_text"], (
            "the entry has to say WHY it was not asked, or a reader sees a "
            "missing date with no reason at all")

    def test_the_saving_is_the_size_the_bundle_showed(self):
        """93 passes over one absent date across a day."""
        clock, calls = Clock(), []
        for _ in range(93):
            sweep(SATURDAY, absent, clock, calls)
            clock.advance(900)                # the real 15-minute cycle
        # 93 cycles x 900s = 23.25 hours; at a 1800s cool-off that is one
        # request every other cycle rather than every cycle.
        assert len(calls) <= 47, (
            f"{len(calls)} requests for one absent date across a day")
        assert len(calls) >= 40, (
            "too few: the cool-off must not become a permanent skip")


class TestItAlwaysAsksAgainEventually:
    def test_after_the_cool_off_it_asks(self):
        clock, calls = Clock(), []
        sweep(SATURDAY, absent, clock, calls)
        clock.advance(f4.ABSENT_RECHECK_SECONDS + 1)
        sweep(SATURDAY, absent, clock, calls)
        assert len(calls) == 2, (
            "today's index really does publish in the evening; a date that "
            "is never asked again is a feed that has stopped working")

    def test_a_date_that_publishes_is_forgotten_at_once(self):
        """The dangerous direction. If the memo outlived the publish, a
        later pass over the same window would skip a REAL index and the
        day's filings would be lost with no error anywhere."""
        clock, calls = Clock(), []
        sweep(FRIDAY, absent, clock, calls)

        clock.advance(f4.ABSENT_RECHECK_SECONDS + 1)

        def published(url):
            return Stub(200, INDEX_TEXT)

        result = sweep(FRIDAY, published, clock, calls)
        assert result.missing_index_dates == [], "the index published"

        # THE MEMO ITSELF, not just this pass's behaviour. A stale entry
        # left behind here is currently harmless only because its
        # timestamp has already expired - which makes it one edit away
        # from skipping a real index, and invisible from the outside.
        # Asserting the record directly is the only check that bites.
        assert FRIDAY not in f4._absent_since, (
            "a date that has published is still remembered as absent")

        # And the pass after it asks again rather than skipping.
        before = len(calls)
        clock.advance(60)
        sweep(FRIDAY, published, clock, calls)
        assert len(calls) > before


class TestTheCheckCanFail:
    """House rule 4, run against the code as shipped."""

    def test_a_memo_that_never_expired_would_be_caught(self):
        clock, calls = Clock(), []
        sweep(SATURDAY, absent, clock, calls)
        clock.advance(f4.ABSENT_RECHECK_SECONDS * 10)
        sweep(SATURDAY, absent, clock, calls)
        assert len(calls) == 2

    def test_no_memo_at_all_would_be_caught(self):
        clock, calls = Clock(), []
        sweep(SATURDAY, absent, clock, calls)
        clock.advance(1)
        sweep(SATURDAY, absent, clock, calls)
        assert len(calls) == 1
