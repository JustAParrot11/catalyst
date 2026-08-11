"""Offline tests for the live EDGAR Form 4 feed
(``catalyst/data/sources/edgar_form4.py``).

Every fixture below is a **verbatim excerpt of a real EDGAR response**
captured on 2026-08-10 (accessions 0001493152-26-036442 / PTIX,
0001225208-26-007021 / ANF, 0001231919-26-000838 / ATTO, and the
2026-08-06 daily index). No test opens a socket: ``conftest.py`` blocks
them for the whole session and every test injects an ``http_get`` stub.

--------------------------------------------------------------------
House rule 4 — a test that cannot fail is not a test
--------------------------------------------------------------------
Method: copy ``edgar_form4.py`` to the scratchpad, break the *source*
(never the test), run the one test, record the failure, restore from the
copy, confirm byte-identical with ``diff`` and green again.

1. ``test_4xx_raises_immediately_and_is_never_retried``
   **Broke:** in ``_request``, moved 4xx into the retry branch —
   ``if status in RETRYABLE_STATUSES or 400 <= status < 600:``
   (i.e. the exact bug TRAPS.md warns about: retrying a 4xx burns the
   shared SEC rate-limit budget and invites the IP block).
   **Failure:**
   ``AssertionError: a 4xx must never be retried (TRAPS.md) - got 4 calls``
   **Restored:** ``cp`` from the copy; ``diff`` identical; re-ran → pass.

2. ``test_10b5_1_flag_survives_all_four_spellings_edgar_uses``
   **Broke:** in ``_to_bool``, narrowed the true set to ``("1",)`` so
   ``<aff10b5One>true</aff10b5One>`` reads as "not a plan trade" — the
   silent-noise failure this feed exists to prevent, since the strategy
   excludes 10b5-1 plan trades.
   **Failure:**
   ``AssertionError: <aff10b5One>true</aff10b5One> must parse as a plan
   trade; EDGAR writes this boolean four ways``
   **Restored:** ``cp`` from the copy; ``diff`` identical; re-ran → pass.

3. ``test_rate_limiter_actually_spaces_calls``
   **Broke:** made ``RateLimiter.acquire`` return immediately (deleted
   the wait), i.e. "rate limiting by hope".
   **Failure:**
   ``AssertionError: requests were not spaced: gaps [0.0, 0.0] < 0.2s``
   **Restored:** ``cp`` from the copy; ``diff`` identical; re-ran → pass.

4. ``test_transport_failure_raises_rather_than_returning_empty``
   **Broke:** made ``fetch_events`` swallow ``FeedError`` and return
   ``[]`` — fail-soft taken one level too far, after which a dead
   network looks exactly like a quiet market.
   **Failure:** ``Failed: DID NOT RAISE FeedError``
   **Restored:** ``cp`` from the copy; ``diff`` identical; re-ran → pass.

After all four: source ``diff``-identical to the pre-sabotage copy and
the full suite green (232 passed).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import time

import pytest

from catalyst.data import RawEvent
from catalyst.data.sources import edgar_form4 as feed


# ---------------------------------------------------------------------------
# Fixtures: real EDGAR bytes, captured 2026-08-10
# ---------------------------------------------------------------------------

DAILY_INDEX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Aug 6, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/

Form Type   Company Name                                                  CIK
      Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
1-A              BLUEMOUNT INTERNATIONAL INC                                   2140965     20260806    edgar/data/2140965/0002140965-26-000001.txt
4                Protagenic Therapeutics, Inc.\\new                             1022899     20260806    edgar/data/1022899/0001493152-26-036442.txt
4                ARMEN GARO H                                                  935679      20260806    edgar/data/935679/0001493152-26-036442.txt
4                ABERCROMBIE & FITCH CO /DE/                                   1018840     20260806    edgar/data/1018840/0001225208-26-007021.txt
4/A              BELLEMARE ALAIN                                               1449365     20260806    edgar/data/1449365/0001683168-26-006059.txt
SCHEDULE 13G     SOME FUND MANAGER LP                                          1234567     20260806    edgar/data/1234567/0001234567-26-000001.txt
8-K              1ST SOURCE CORP                                               34782       20260806    edgar/data/34782/0000034782-26-000099.txt
"""

# Accession 0001493152-26-036442 — a genuine open-market purchase
# (transaction code P), aff10b5One = 0, booleans written as 0/1.
SUBMISSION_PURCHASE = r"""<SEC-DOCUMENT>0001493152-26-036442.txt : 20260806
<SEC-HEADER>0001493152-26-036442.hdr.sgml : 20260806
<ACCEPTANCE-DATETIME>20260806202933
ACCESSION NUMBER:		0001493152-26-036442
CONFORMED SUBMISSION TYPE:	4
PUBLIC DOCUMENT COUNT:		1
CONFORMED PERIOD OF REPORT:	20260805
FILED AS OF DATE:		20260806
DATE AS OF CHANGE:		20260806
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<SEQUENCE>1
<FILENAME>ownership.xml
<TEXT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0609</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>2026-08-05</periodOfReport>
    <notSubjectToSection16>0</notSubjectToSection16>
    <issuer>
        <issuerCik>0001022899</issuerCik>
        <issuerName>Protagenic Therapeutics, Inc.\new</issuerName>
        <issuerTradingSymbol>ptix</issuerTradingSymbol>
        <issuerForeignTradingSymbol></issuerForeignTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0000935679</rptOwnerCik>
            <rptOwnerName>ARMEN GARO H</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>1</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <isOther>0</isOther>
            <officerTitle>EXEC. CHAIR &amp; PRINCIPAL OFF</officerTitle>
            <otherText></otherText>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>0</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle>
                <value>COMMON STOCK</value>
            </securityTitle>
            <transactionDate>
                <value>2026-08-05</value>
            </transactionDate>
            <deemedExecutionDate></deemedExecutionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>P</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
            </transactionCoding>
            <transactionTimeliness></transactionTimeliness>
            <transactionAmounts>
                <transactionShares>
                    <value>5000</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>0.2588</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>A</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction>
                    <value>31294</value>
                </sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership>
                    <value>D</value>
                </directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <ownerSignature>
        <signatureName>/s/ Garo H. Armen</signatureName>
        <signatureDate>2026-08-06</signatureDate>
    </ownerSignature>
</ownershipDocument>
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""

# Accession 0001225208-26-007021 — aff10b5One = 1 *and* a footnote saying
# so. Note <isDirector> is simply absent, and a <footnoteId id="F1"/>
# child sits inside <transactionCoding>.
SUBMISSION_PLAN_SALE = r"""<SEC-DOCUMENT>0001225208-26-007021.txt : 20260806
<SEC-HEADER>0001225208-26-007021.hdr.sgml : 20260806
<ACCEPTANCE-DATETIME>20260806164654
ACCESSION NUMBER:		0001225208-26-007021
CONFORMED SUBMISSION TYPE:	4
CONFORMED PERIOD OF REPORT:	20260804
FILED AS OF DATE:		20260806
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<TEXT>
<XML>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2026-08-04</periodOfReport>
    <issuer>
        <issuerCik>0001018840</issuerCik>
        <issuerName>ABERCROMBIE &amp; FITCH CO /DE/</issuerName>
        <issuerTradingSymbol>ANF</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001718601</rptOwnerCik>
            <rptOwnerName>Lipesky Scott D.</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isOfficer>1</isOfficer>
            <officerTitle>EVP and COO</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>1</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle>
                <value>Class A Common Stock</value>
            </securityTitle>
            <transactionDate>
                <value>2026-08-04</value>
            </transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>S</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
                <footnoteId id="F1"/>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares>
                    <value>10000.0000</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>110.0000</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>D</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction>
                    <value>162534.0000</value>
                </sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature>
                <directOrIndirectOwnership>
                    <value>D</value>
                </directOrIndirectOwnership>
            </ownershipNature>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <derivativeTable></derivativeTable>
    <footnotes>
        <footnote id="F1">The reported sale of shares occurred automatically pursuant to a Rule 10b5-1 trading plan adopted by the reporting person on March 6, 2026.</footnote>
    </footnotes>
</ownershipDocument>
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""

# Excerpt of accession 0001231919-26-000838: three reporting owners, and
# the booleans are written BOTH ways inside the one filing - owner 1 uses
# true/false, owner 2 uses 1/0. aff10b5One is the word "false" here.
SUBMISSION_MIXED_BOOLEANS = r"""<SEC-DOCUMENT>0001231919-26-000838.txt : 20260806
<SEC-HEADER>0001231919-26-000838.hdr.sgml : 20260806
<ACCEPTANCE-DATETIME>20260806173551
ACCESSION NUMBER:		0001231919-26-000838
CONFORMED SUBMISSION TYPE:	4
FILED AS OF DATE:		20260806
</SEC-HEADER>
<DOCUMENT>
<TYPE>4
<TEXT>
<XML>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>2026-08-04</periodOfReport>
    <issuer>
        <issuerCik>0002058707</issuerCik>
        <issuerName>Atlas Therapeutics</issuerName>
        <issuerTradingSymbol>ATTO</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001911592</rptOwnerCik>
            <rptOwnerName>Frazier Life Sciences XI, L.P.</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>false</isDirector>
            <isOfficer>false</isOfficer>
            <isTenPercentOwner>true</isTenPercentOwner>
            <isOther>false</isOther>
            <officerTitle></officerTitle>
            <otherText></otherText>
        </reportingOwnerRelationship>
    </reportingOwner>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0001911580</rptOwnerCik>
            <rptOwnerName>FHMLS XI, L.P.</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector>
            <isOfficer>0</isOfficer>
            <isTenPercentOwner>1</isTenPercentOwner>
            <isOther>0</isOther>
            <officerTitle></officerTitle>
            <otherText></otherText>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>false</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle>
                <value>Common Stock</value>
            </securityTitle>
            <transactionDate>
                <value>2026-08-04</value>
            </transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>P</transactionCode>
                <equitySwapInvolved>0</equitySwapInvolved>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares>
                    <value>588235</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>17.00</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>A</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
    <derivativeTable>
        <derivativeTransaction>
            <securityTitle>
                <value>Series A Preferred</value>
            </securityTitle>
            <transactionDate>
                <value>2026-08-04</value>
            </transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>C</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares>
                    <value>16250000</value>
                </transactionShares>
                <transactionPricePerShare>
                    <value>0</value>
                </transactionPricePerShare>
                <transactionAcquiredDisposedCode>
                    <value>D</value>
                </transactionAcquiredDisposedCode>
            </transactionAmounts>
        </derivativeTransaction>
    </derivativeTable>
</ownershipDocument>
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""

# The real body EDGAR returns for a daily-index file that does not exist
# (weekend/holiday/before the evening publish). Status 403, not 404.
ABSENT_INDEX_BODY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Error><Code>AccessDenied</Code><Message>Access Denied</Message>"
    "<RequestId>23A92N48CB3CMGN2</RequestId><HostId>/0P9pP</HostId></Error>"
)

# What a real rate-limit block looks like: also 403, entirely different body.
BLOCKED_BODY = (
    "Your Request Originates from an Undeclared Automated Tool\n"
    "Request Rate Threshold Exceeded. Please declare your traffic by "
    "updating your user agent."
)

INDEX_URL_20260806 = (
    "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260806.idx"
)
PURCHASE_URL = (
    "https://www.sec.gov/Archives/edgar/data/1022899/0001493152-26-036442.txt"
)


# ---------------------------------------------------------------------------
# Offline plumbing
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class FakeClock:
    """Monotonic clock that only moves when something sleeps. Makes the
    rate limiter's spacing observable without waiting for real seconds."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0, f"negative sleep: {seconds}"
        self.sleeps.append(seconds)
        self.t += seconds


class Recorder:
    """An ``http_get`` stub. Routes by URL, records call order and the
    clock reading at each call."""

    def __init__(self, routes: dict, clock: FakeClock | None = None,
                 default=None):
        self.routes = routes
        self.clock = clock
        self.default = default
        self.calls: list[str] = []
        self.times: list[float] = []
        self.headers: list[dict] = []

    def __call__(self, url: str, headers: dict):
        self.calls.append(url)
        self.headers.append(headers)
        if self.clock is not None:
            self.times.append(self.clock.monotonic())
        handler = self.routes.get(url, self.default)
        if handler is None:
            raise AssertionError(f"test stub has no route for {url}")
        if callable(handler):
            return handler(url)
        return handler


def one_trading_day(recorder, clock, **kwargs):
    """fetch_form4 over 2026-08-06 only, wired to the fakes."""
    return feed.fetch_form4(
        date(2026, 8, 6),
        date(2026, 8, 6),
        recorder,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        now=lambda: datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_rawevents_with_verbatim_payload_and_parsed_fields():
    clock = FakeClock()
    recorder = Recorder(
        {
            INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX),
            PURCHASE_URL: FakeResponse(200, SUBMISSION_PURCHASE),
            "https://www.sec.gov/Archives/edgar/data/1018840/0001225208-26-007021.txt":
                FakeResponse(200, SUBMISSION_PLAN_SALE),
        },
        clock,
    )
    result = one_trading_day(recorder, clock)

    assert [e.source for e in result.events] == ["edgar_form4", "edgar_form4"]
    assert all(isinstance(e, RawEvent) for e in result.events)

    event = result.events[0]
    assert event.source_id == "0001493152-26-036442", "source_id is the accession"
    assert event.fetched_at == datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    # Verbatim upstream text is kept beside the parsed view, never instead.
    assert event.payload_raw["submission_text"] == SUBMISSION_PURCHASE
    assert event.payload_raw["source_url"] == PURCHASE_URL
    assert "0001493152-26-036442.txt" in event.payload_raw["daily_index_line"]

    parsed = event.payload_raw["parsed"]
    assert parsed["ticker"] == "PTIX"
    assert parsed["issuer_name"] == r"Protagenic Therapeutics, Inc.\new"
    assert parsed["issuer_cik"] == "0001022899"
    # acceptance_datetime, not filed_date, is the point-in-time truth:
    # 20:29 ET is after the close, so this is not tradable until 08-07.
    assert parsed["acceptance_datetime"] == "2026-08-06T20:29:33"
    assert parsed["filed_date"] == "2026-08-06"

    owner, = parsed["owners"]
    assert owner["name"] == "ARMEN GARO H"
    assert owner["cik"] == "0000935679"
    assert owner["is_officer"] is True and owner["is_director"] is True
    assert owner["is_ten_percent_owner"] is False
    assert owner["officer_title"] == "EXEC. CHAIR & PRINCIPAL OFF"
    assert owner["role"] == "officer:EXEC. CHAIR & PRINCIPAL OFF, director"

    txn, = parsed["transactions"]
    assert txn["code"] == "P", "open-market purchase - the strategy's whole signal"
    assert txn["acquired_disposed"] == "A"
    assert txn["transaction_date"] == "2026-08-05"
    assert txn["shares"] == "5000"
    assert txn["price_per_share"] == "0.2588"
    assert txn["value_usd"] == "1294.0000"
    assert txn["shares_owned_following"] == "31294"
    assert parsed["ten_b5_1"] == {
        "element": False, "footnote_mention": False, "plan_flagged": False
    }


def test_payload_is_json_serializable_because_storage_persists_it():
    import json

    clock = FakeClock()
    recorder = Recorder({
        INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX),
        PURCHASE_URL: FakeResponse(200, SUBMISSION_PURCHASE),
    }, clock, default=FakeResponse(200, SUBMISSION_PLAN_SALE))
    result = one_trading_day(recorder, clock)
    round_tripped = json.loads(json.dumps(result.events[0].payload_raw))
    assert round_tripped["parsed"]["transactions"][0]["shares"] == "5000"


def test_typed_parse_exposes_decimals_not_floats():
    """Money never becomes a float on the way through this feed."""
    parsed = feed.parse_submission(SUBMISSION_PURCHASE)
    txn, = parsed.transactions
    assert isinstance(txn.shares, Decimal)
    assert txn.shares == Decimal("5000")
    assert txn.price_per_share == Decimal("0.2588")
    assert txn.value_usd == Decimal("1294.0000")


def test_daily_index_dedups_accessions_and_filters_form_types():
    """One accession is listed once per CIK involved (issuer and each
    reporting owner). De-dup or pay double the request budget."""
    rows = feed.parse_daily_index(DAILY_INDEX)
    assert [r.form_type for r in rows] == ["4", "4", "4"], "4/A and 13G excluded"
    assert len({r.accession for r in rows}) == 2, "PTIX filing is listed twice"
    assert rows[0].accession == "0001493152-26-036442"
    assert rows[0].url == PURCHASE_URL
    assert rows[2].company_name == "ABERCROMBIE & FITCH CO /DE/"

    with_amendments = feed.parse_daily_index(DAILY_INDEX, forms=("4", "4/A"))
    assert sum(1 for r in with_amendments if r.form_type == "4/A") == 1


def test_only_one_request_per_unique_accession():
    clock = FakeClock()
    recorder = Recorder({
        INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX),
        PURCHASE_URL: FakeResponse(200, SUBMISSION_PURCHASE),
    }, clock, default=FakeResponse(200, SUBMISSION_PLAN_SALE))
    result = one_trading_day(recorder, clock)
    assert result.index_rows_seen == 3
    assert result.unique_accessions == 2
    assert len(recorder.calls) == 3, "1 index + 2 unique filings, not 4"
    assert result.requests_made == 3


def test_every_request_carries_a_contactable_user_agent():
    """Without one SEC answers 403 with a block page, not data."""
    clock = FakeClock()
    recorder = Recorder({INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX)},
                        clock, default=FakeResponse(200, SUBMISSION_PURCHASE))
    one_trading_day(recorder, clock)
    assert recorder.headers, "no requests were made"
    for headers in recorder.headers:
        agent = headers["User-Agent"]
        assert "@" in agent and "." in agent.split("@")[-1], (
            f"User-Agent {agent!r} carries no contactable address"
        )


# ---------------------------------------------------------------------------
# 10b5-1 — the flag the strategy excludes on
# ---------------------------------------------------------------------------


def test_10b5_1_flag_survives_all_four_spellings_edgar_uses():
    """Measured on 2026-08-10 over 120 filings: aff10b5One arrives as
    ``0`` (84), ``false`` (20), ``1`` (15) and ``true`` (1), and the
    relationship booleans mix both styles inside one filing. Code that
    tests ``== "1"`` reads every ``true`` filing as "not a plan trade"
    and quietly poisons the strategy's exclusion rule."""
    assert feed._to_bool("1") is True
    assert feed._to_bool("true") is True, (
        "<aff10b5One>true</aff10b5One> must parse as a plan trade; "
        "EDGAR writes this boolean four ways"
    )
    assert feed._to_bool("0") is False
    assert feed._to_bool("false") is False
    assert feed._to_bool("") is None and feed._to_bool(None) is None

    plan = feed.parse_submission(SUBMISSION_PLAN_SALE)
    assert plan.aff10b5one_element is True
    assert plan.footnote_mentions_10b5_1 is True
    assert plan.plan_flagged is True

    mixed = feed.parse_submission(SUBMISSION_MIXED_BOOLEANS)
    assert mixed.aff10b5one_element is False, "the word 'false' is still false"
    assert mixed.plan_flagged is False
    # Same filing, two spellings, one answer.
    assert [o.is_ten_percent_owner for o in mixed.owners] == [True, True]
    assert [o.is_director for o in mixed.owners] == [False, False]


def test_footnote_mention_flags_a_plan_trade_when_the_element_is_absent():
    """The element only exists from ~2023; plan trades are also disclosed
    in free text. Both signals are reported, neither is collapsed."""
    without_element = SUBMISSION_PLAN_SALE.replace(
        "<aff10b5One>1</aff10b5One>", ""
    )
    parsed = feed.parse_submission(without_element)
    assert parsed.aff10b5one_element is None, "absent element is None, not False"
    assert parsed.footnote_mentions_10b5_1 is True
    assert parsed.plan_flagged is True


def test_missing_relationship_elements_are_none_not_false():
    """ANF's filing omits <isDirector> entirely. 'Not stated' and
    'stated as no' are different facts."""
    parsed = feed.parse_submission(SUBMISSION_PLAN_SALE)
    owner, = parsed.owners
    assert owner.is_officer is True
    assert owner.is_director is None
    assert owner.role == "officer:EVP and COO"


def test_derivative_and_non_derivative_transactions_are_labelled_separately():
    """The strategy trades non-derivative code P only; nothing is dropped
    silently, so discovery can filter on an honest set."""
    parsed = feed.parse_submission(SUBMISSION_MIXED_BOOLEANS)
    assert [t.table for t in parsed.transactions] == ["non_derivative", "derivative"]
    purchase = parsed.transactions[0]
    assert purchase.code == "P" and purchase.value_usd == Decimal("9999995.00")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiter_actually_spaces_calls():
    """TRAPS.md: 10 req/s across ALL SEC APIs, an overrun blocks the IP
    for every SEC feed at once. Rate limiting by hope is not rate
    limiting - so measure the gaps on a fake clock."""
    clock = FakeClock()
    recorder = Recorder({
        INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX),
        PURCHASE_URL: FakeResponse(200, SUBMISSION_PURCHASE),
    }, clock, default=FakeResponse(200, SUBMISSION_PLAN_SALE))

    one_trading_day(recorder, clock, rate_per_sec=5.0)

    gaps = [b - a for a, b in zip(recorder.times, recorder.times[1:])]
    assert len(gaps) == 2
    assert all(gap >= 0.2 - 1e-9 for gap in gaps), (
        f"requests were not spaced: gaps {gaps} < 0.2s"
    )
    assert clock.t >= 0.4


def test_rate_limiter_refuses_to_be_configured_above_the_sec_ceiling():
    clock = FakeClock()
    with pytest.raises(ValueError, match="ceiling"):
        feed.RateLimiter(11.0, monotonic=clock.monotonic, sleep=clock.sleep)
    with pytest.raises(ValueError):
        feed.RateLimiter(0, monotonic=clock.monotonic, sleep=clock.sleep)
    # 10/s exactly is the documented limit, and is allowed.
    assert feed.RateLimiter(
        feed.SEC_MAX_REQUESTS_PER_SEC, monotonic=clock.monotonic, sleep=clock.sleep
    ).interval == 0.1


def test_rate_limiter_does_not_sleep_when_the_caller_is_already_slow():
    """A limiter that sleeps unconditionally wastes the whole schedule."""
    clock = FakeClock()
    limiter = feed.RateLimiter(5.0, monotonic=clock.monotonic, sleep=clock.sleep)
    limiter.acquire()
    clock.t += 10.0            # caller spent 10s doing other work
    limiter.acquire()
    assert clock.sleeps == [], f"slept needlessly: {clock.sleeps}"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_5xx_is_retried_with_backoff_then_raises_feederror_with_raw_text():
    clock = FakeClock()
    body = "<html><body>EDGAR is temporarily unavailable</body></html>"
    recorder = Recorder({}, clock, default=FakeResponse(503, body))

    with pytest.raises(feed.FeedError) as excinfo:
        one_trading_day(recorder, clock)

    error = excinfo.value
    assert error.status_code == 503
    assert error.attempts == 4
    assert len(recorder.calls) == 4, "transient 5xx must be retried"
    assert error.raw_text == body, "the raw upstream body travels with the error"
    assert error.url == INDEX_URL_20260806
    assert error.source == "edgar_form4"
    # Exponential backoff, and every wait went through the injected sleep.
    backoffs = [s for s in clock.sleeps if s in (0.5, 1.0, 2.0)]
    assert backoffs == [0.5, 1.0, 2.0], f"backoff was {clock.sleeps}"


def test_transient_5xx_that_recovers_returns_data():
    """Retry exists to succeed, not merely to delay the exception."""
    clock = FakeClock()
    attempts = {"n": 0}

    def flaky(url):
        if url != INDEX_URL_20260806:
            return FakeResponse(200, SUBMISSION_PURCHASE)
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FakeResponse(500, "internal error")
        return FakeResponse(200, DAILY_INDEX)

    recorder = Recorder({}, clock, default=flaky)
    result = one_trading_day(recorder, clock)
    assert attempts["n"] == 3
    assert len(result.events) == 2


def test_429_is_treated_as_transient_and_retried():
    clock = FakeClock()
    recorder = Recorder({}, clock, default=FakeResponse(429, "rate limited"))
    with pytest.raises(feed.FeedError) as excinfo:
        one_trading_day(recorder, clock)
    assert excinfo.value.status_code == 429
    assert len(recorder.calls) == 4


def test_4xx_raises_immediately_and_is_never_retried():
    """TRAPS.md: never retry a 4xx. The request itself is wrong, and the
    retry spends a rate-limit budget shared with every other SEC feed."""
    clock = FakeClock()
    recorder = Recorder({}, clock, default=FakeResponse(400, "Bad Request"))

    with pytest.raises(feed.FeedError) as excinfo:
        one_trading_day(recorder, clock)

    assert len(recorder.calls) == 1, (
        f"a 4xx must never be retried (TRAPS.md) - got {len(recorder.calls)} calls"
    )
    assert excinfo.value.status_code == 400
    assert excinfo.value.attempts == 1
    assert excinfo.value.raw_text == "Bad Request"
    assert clock.sleeps == [], "no backoff should have been slept"


def test_403_block_page_is_fatal_and_names_the_ip_block():
    """A real rate-limit block and an absent file are both 403. Confusing
    them either screams every weekend or swallows the block."""
    clock = FakeClock()
    recorder = Recorder({}, clock, default=FakeResponse(403, BLOCKED_BODY))
    with pytest.raises(feed.FeedError, match="IP block"):
        one_trading_day(recorder, clock)
    assert len(recorder.calls) == 1, "still a 4xx: not retried"


def test_absent_daily_index_403_is_a_missing_date_not_an_error():
    """Measured 2026-08-10: a weekend/holiday/not-yet-published daily
    index returns HTTP 403 with an S3 <Code>AccessDenied</Code> body -
    NOT the 404 the docs claim. The raw body is kept beside the zero."""
    clock = FakeClock()
    recorder = Recorder({}, clock, default=FakeResponse(403, ABSENT_INDEX_BODY))

    result = feed.fetch_form4(
        date(2026, 8, 8), date(2026, 8, 9), recorder,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )

    assert result.events == []
    assert [m["date"] for m in result.missing_index_dates] == [
        "2026-08-08", "2026-08-09"
    ]
    assert result.missing_index_dates[0]["status_code"] == 403
    assert result.missing_index_dates[0]["raw_text"] == ABSENT_INDEX_BODY
    assert "AccessDenied" in result.why_empty()
    assert len(recorder.calls) == 2, "absent files are not retried"


def test_transport_failure_raises_rather_than_returning_empty():
    """A dead network and a quiet market must not look identical."""
    clock = FakeClock()

    def explode(url):
        raise OSError("[Errno 101] Network is unreachable")

    recorder = Recorder({}, clock, default=explode)

    with pytest.raises(feed.FeedError) as excinfo:
        feed.fetch_events(
            date(2026, 8, 6), date(2026, 8, 6), recorder,
            sleep=clock.sleep, monotonic=clock.monotonic,
        )

    error = excinfo.value
    assert error.status_code is None
    assert "Network is unreachable" in error.raw_text
    assert error.attempts == 4, "transport errors are retried before giving up"
    assert len(recorder.calls) == 4


def test_one_bad_filing_does_not_lose_the_others():
    """A single 404 filing is recorded, not raised - one bad row must not
    cost the other 561 accessions of the day."""
    clock = FakeClock()
    recorder = Recorder({
        INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX),
        PURCHASE_URL: FakeResponse(404, "<Error><Code>NoSuchKey</Code></Error>"),
    }, clock, default=FakeResponse(200, SUBMISSION_PLAN_SALE))

    result = one_trading_day(recorder, clock)

    assert len(result.events) == 1
    assert result.events[0].source_id == "0001225208-26-007021"
    error, = result.filing_errors
    assert error.accession == "0001493152-26-036442"
    assert error.status_code == 404
    assert "NoSuchKey" in error.raw_text


def test_unparseable_filing_is_recorded_with_its_raw_text_not_raised():
    clock = FakeClock()
    recorder = Recorder({
        INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX),
        PURCHASE_URL: FakeResponse(200, "<SEC-HEADER>truncated junk"),
    }, clock, default=FakeResponse(200, SUBMISSION_PLAN_SALE))

    result = one_trading_day(recorder, clock)

    assert len(result.events) == 1
    error, = result.filing_errors
    assert "parse failure" in error.error
    assert error.raw_text == "<SEC-HEADER>truncated junk"


def test_wholesale_filing_failure_raises_instead_of_returning_empty():
    """If every filing fetch fails, [] would be a lie about the market."""
    clock = FakeClock()
    recorder = Recorder({INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX)},
                        clock, default=FakeResponse(404, "gone"))

    with pytest.raises(feed.FeedError, match="all 2 Form 4 submission fetches failed"):
        one_trading_day(recorder, clock)


def test_truncation_is_reported_never_silent():
    clock = FakeClock()
    recorder = Recorder({INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX)},
                        clock, default=FakeResponse(200, SUBMISSION_PURCHASE))
    result = one_trading_day(recorder, clock, max_filings=1)
    assert result.truncated_at == 1
    assert result.unique_accessions == 2
    assert len(result.events) == 1


def test_backwards_window_is_rejected():
    clock = FakeClock()
    recorder = Recorder({}, clock, default=FakeResponse(200, DAILY_INDEX))
    with pytest.raises(ValueError, match="before since"):
        feed.fetch_form4(date(2026, 8, 6), date(2026, 8, 1), recorder,
                         sleep=clock.sleep, monotonic=clock.monotonic)
    assert recorder.calls == []


def test_fetch_events_matches_the_architecture_signature():
    """data/sources/<source>.py contract: fetch_events(since, until)."""
    import inspect

    params = list(inspect.signature(feed.fetch_events).parameters)
    assert params[:3] == ["since", "until", "http_get"]

    clock = FakeClock()
    recorder = Recorder({INDEX_URL_20260806: FakeResponse(200, DAILY_INDEX)},
                        clock, default=FakeResponse(200, SUBMISSION_PURCHASE))
    events = feed.fetch_events(
        datetime(2026, 8, 6, 9, 30), datetime(2026, 8, 6, 16, 0), recorder,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert len(events) == 2
    assert all(e.source == "edgar_form4" for e in events)


# ---------------------------------------------------------------------------
# One pacer per PROCESS, not per call
# ---------------------------------------------------------------------------


def test_every_sec_caller_shares_one_pacer():
    """The SEC's 10 req/s ceiling is per IP and shared across all of its
    APIs, so a limiter created per call site is not a limit at all: two
    at 5/s each sit exactly on the ceiling and three are over it. The
    dashboard's reachability probe runs on a request thread while the
    trading cycle is fetching, so this is a live pairing, not a
    hypothetical."""
    assert feed.sec_pacer() is feed.sec_pacer()
    assert feed.sec_pacer().rate_per_sec <= feed.SEC_MAX_REQUESTS_PER_SEC


def test_the_edgar_probe_goes_through_the_shared_pacer():
    """Owner-asked: "are we adhering to all API limits, i dont want us to
    get IP banned". The probe used to call httpx.get directly."""
    from catalyst.dashboard import maintenance

    calls = {"paced": 0, "fetched": 0}
    pacer = feed.sec_pacer()
    before = pacer.acquisitions

    def fake_get(url, headers):
        calls["fetched"] += 1
        assert "User-Agent" in headers, (
            "the SEC answers a missing User-Agent with a 403 block page")

        class R:
            status_code = 200
            text = "ok"
        return R()

    original = feed._default_http_get
    feed._default_http_get = fake_get
    try:
        ok, message = maintenance._default_edgar_probe()()
    finally:
        feed._default_http_get = original
    assert ok and calls["fetched"] == 1
    assert pacer.acquisitions == before + 1, (
        "the probe reached sec.gov without spending from the shared "
        "per-IP budget")


def test_the_pacer_serialises_its_read_modify_write():
    """Two threads reading _next_at between the read and the write is
    exactly how a paced client emits a burst.

    Asserted on the lock rather than on wall-clock gaps: a timing test
    here PASSED with the lock deleted, because the GIL happened to
    serialise eight threads anyway. A test that cannot fail is not a
    test (house rule 4), so this one checks the mechanism that makes the
    guarantee rather than a symptom that may or may not appear.
    """
    limiter = feed.RateLimiter(10.0)
    real, seen = limiter._lock, []

    class Watched:
        def __enter__(self):
            seen.append("enter")
            return real.__enter__()

        def __exit__(self, *exc):
            seen.append("exit")
            return real.__exit__(*exc)

    limiter._lock = Watched()
    limiter.acquire()
    limiter.acquire()
    assert seen == ["enter", "exit", "enter", "exit"], (
        "acquire() updated the schedule outside the lock")


def test_the_pacer_still_spaces_requests_across_threads():
    """The end-to-end shape, as a smoke check on top of the lock test."""
    import threading as _threading

    limiter = feed.RateLimiter(10.0)
    stamps: list[float] = []
    lock = _threading.Lock()

    def hit():
        limiter.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [_threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 7
    assert all(gap > 0.05 for gap in gaps), f"burst: gaps {gaps}"
