# Data sources — verified live, 2026-08-10

Every row below was checked with a real request from a real network, on
**2026-08-10**, not read from documentation. Where documentation and the
live response disagreed, the live response wins and the disagreement is
written down as a gotcha.

Re-check any time with:

```
python3 scripts/verify_sources.py            # everything
python3 scripts/verify_sources.py --only sec # substring filter
python3 scripts/verify_sources.py --json     # machine-readable too
```

Exit code is `1` if any source that is supposed to work has failed, `0`
otherwise. Alpaca checks **SKIP** (not fail) when `ALPACA_KEY` /
`ALPACA_SECRET_KEY` are absent from the environment.

Last full run: **24 PASS, 0 FAIL, 1 expected-FAIL (Stooq), 0 SKIP.**

---

## 0. Summary table

| Source | Keyless | History depth (measured) | Verified | Role |
|---|---|---|---|---|
| Alpaca daily bars (SIP) | No — subscription | **2016-01-04** | PASS | Backtest price spine, SPY total-return benchmark |
| Alpaca minute bars (SIP) | No — subscription | **2016-01-04** | PASS | Entry/exit and gap modelling |
| Alpaca assets | No — subscription | current only | PASS | Tradable universe |
| Alpaca news (Benzinga) | No — subscription | ≥ 2016-01-09 | PASS | Event provenance |
| Alpaca corporate actions | No — subscription | see gotchas | PASS | Splits/mergers/dividends |
| SEC `company_tickers.json` | Yes (UA) | current only | PASS | ticker → CIK |
| SEC `data.sec.gov` submissions | Yes (UA) | full filing history | PASS | Event clock (A, C) |
| SEC XBRL companyconcept | Yes (UA) | to first XBRL filing | PASS | **Candidate A core dependency** |
| SEC XBRL companyfacts | Yes (UA) | to first XBRL filing | PASS | Bulk fundamentals per company |
| SEC XBRL frames | Yes (UA) | ~2009 onward | PASS | Cross-section per period (but see gotcha) |
| SEC EDGAR daily index | Yes (UA) | **1994-07-01** onward | PASS | Every filing, by form, by day |
| SEC Insider Transactions data sets | Yes (UA) | **2006Q1** onward | PASS | **Candidate C core dependency** |
| SEC Financial Statement data sets | Yes (UA) | **2009Q1** onward (thin until 2009Q3) | PASS | As-filed fundamentals, no restatement bias |
| SEC Failure-to-Deliver | Yes (UA) | **~2017-07 onward, rolling** | PASS | Crowding context |
| SEC EDGAR full-text search | Yes (UA) | **2001 onward** | PASS | Phrase search inside filing bodies |
| FINRA Reg SHO daily short volume | Yes | **~2018-10-01 onward** (rolling) | PASS | Short-volume split of daily volume |
| FINRA consolidated short interest | Yes | **2020-04-15 onward** | PASS | Bi-monthly short interest |
| Nasdaq Trader halts RSS | Yes | **live only, no history** | PASS | LULD/news halts going forward |
| ClinicalTrials.gov v2 study | Yes | current record | PASS | Trial status/dates |
| ClinicalTrials.gov **version history** | Yes | full record history | **PASS** | **Candidate D's entire premise — resolved** |
| Federal Register API | Yes | 1994 onward | PASS | Scheduled agency actions |
| openFDA drugsfda | Yes | retrospective, full | PASS | Approvals/CRL base rates |
| FRED `fredgraph.csv` | Yes | **1981-09-01** (DGS3MO) | PASS | T-bill benchmark |
| Treasury FiscalData | Yes | long | PASS | Rates cross-check |
| Stooq CSV | Yes | — | **FAIL** | Rejected — JS anti-bot challenge |
| Yahoo Finance v8 chart | Yes | 1993 (SPY inception) | PASS but **not adopted** | See §6 |

---

## 1. Alpaca market data — the only paid source

**URLs**
- Market data: `https://data.alpaca.markets`
- Paper trading (assets, account): `https://paper-api.alpaca.markets`

**Auth** — headers `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`, read from
`ALPACA_KEY` and `ALPACA_SECRET_KEY`. Never logged, never in a diagnostic
bundle, never in this repository.

**Cost** — subscription, no per-call charge inside the rate limit. This
account has **SIP** (full consolidated tape) access, which is a paid market
data plan, not the free tier. Nothing in the backtest adds a marginal
dollar; the constraint is the rate limit, not the bill.

**Rate limit — measured, from the response headers**
```
x-ratelimit-limit: 200
x-ratelimit-remaining: 199
x-ratelimit-reset: <unix ts>
```
200 requests per minute.

### 1.1 Daily bars — history depth, measured by probing

`GET /v2/stocks/{symbol}/bars` and `GET /v2/stocks/bars?symbols=A,B,C`

| Start requested | feed=sip | feed=iex |
|---|---|---|
| 2010-01-04 | `{"bars":null}` HTTP 200 | — |
| 2014-01-02 | `{"bars":null}` HTTP 200 | — |
| 2015-01-02 | `{"bars":null}` HTTP 200 | — |
| 2015-06-01 | `{"bars":null}` HTTP 200 | — |
| 2015-12-01 | `{"bars":null}` HTTP 200 | — |
| **2016-01-04** | **real bars** | first bar returned is **2018-11-01** |
| 2019-01-02 | real bars | first bar returned is **2020-07-27** |
| 2020-01-02 | real bars | first bar returned is 2020-07-27 |

**SIP daily history begins 2016-01-04.** SPY over 2016-01-04 → 2026-08-07 is
**2,664 daily bars** with no gaps.

Minute bars have the same floor: `1Min` `feed=sip` returns real bars for
2016-01-04T14:30Z and for 2026-08-05, so **minute history is also 2016
onward** — about 10.5 years, enough for every candidate in
STRATEGY-PROPOSALS.md except Candidate D's ≥7-year *event* requirement,
which is limited by event density rather than by price data.

**Response shape** (single symbol):
```json
{"bars":[{"c":201.0192,"h":201.03,"l":198.59,"n":655489,"o":200.49,
          "t":"2016-01-04T05:00:00Z","v":225903783,"vw":199.753436}],
 "next_page_token":"U1BZfER8MTQ1MjE0MjgwMDAwMDAwMDAwMA==","symbol":"SPY"}
```
Multi-symbol form nests under a dict: `{"bars":{"SPY":[...],"AAPL":[...]}}`.

**Fields relied on:** `t` (bar timestamp, UTC), `o h l c`, `v` (volume),
`vw` (VWAP), `n` (trade count). `n` is genuinely useful — it separates a
thin tape from a busy one without needing quote data.

### 1.2 The SPY total-return benchmark — how to get it right

`adjustment` takes `raw` (default) | `split` | `dividend` | `all`.
An invalid value returns `400 {"message":"invalid adjustment: bogus"}`.

Measured on SPY, 2016-01-04 → 2026-08-07:

| adjustment | first close | last close | return |
|---|---|---|---|
| `raw` (default) | 201.0192 | 773.26 | **+284.7%** — price return |
| `all` | 171.10 | 773.26 | **+351.9%** — total return |

**The benchmark the brief requires is the `adjustment=all` series.** The
default is `raw`, so a backtest that forgets the parameter will under-report
SPY by roughly 67 percentage points over ten years and make the strategy
look far better than it is. For SPY specifically `split` == `raw` and
`dividend` == `all` (SPY has never split); do not generalise that to single
stocks.

### 1.3 Bulk pull throughput — measured

100 symbols × 2016-01-04 → 2026-08-08, daily, `adjustment=all`, `limit=10000`:

```
requests=27   bars=265,545   wall=5.9s   -> ~44,600 bars/sec
```

Extrapolated: a **1,000-symbol, 10.5-year daily universe is ~2.66M bars,
~266 requests, ~80 seconds** — bounded by the 200 req/min limit rather than
by bandwidth. Building the whole backtest cache locally is a one-off of a
couple of minutes. There is no reason for the backtest to ever hit the
network mid-run.

`limit` maxes at 10,000: `20000` returns
`400 {"message":"invalid limit: larger than the allowed maximum of 10000"}`.

### 1.4 Assets — the universe, and its survivorship hole

`GET https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity`

- **14,210** active US equities. `tradable`=13,351, `fractionable`=7,589,
  `shortable`=5,227 (irrelevant — the cash account cannot short).
- Exchanges present: `AMEX, ARCA, BATS, NASDAQ, NYSE, OTC`.
- Fields: `symbol, name, exchange, status, tradable, marginable, shortable,
  easy_to_borrow, fractionable, maintenance_margin_requirement`.

### 1.5 Gotchas — Alpaca (TRAPS.md style)

- **`feed=iex` is not a cheaper substitute for `feed=sip` on history.** For
  SPY with `start=2016-01-04`, IEX returns its first bar at **2018-11-01
  with `n:1, v:200`** — a single 200-share trade dressed up as a daily bar.
  A backtest on IEX bars is a backtest on one exchange's fragmentary print
  record, and pre-2020 it is essentially fiction. **Always pass
  `feed=sip` explicitly** rather than relying on a default that could change
  with the subscription.
- **`adjustment` defaults to `raw`.** See §1.2. This silently inflates
  strategy-vs-benchmark comparisons.
- **A start date before the data floor returns HTTP 200 with `bars: null`,
  not an error.** Indistinguishable from "this symbol had no trades" unless
  you know the floor. Print the raw body beside any empty bar list.
- **Delisted symbols return bars but are not in the assets list.**
  `SIVB` bars for 2022-06 come back fine, but
  `GET /v2/assets/SIVB` → `404 {"code":40410000,"message":"asset not found for SIVB"}`,
  and `status=inactive` (19,201 rows) does **not** contain `SIVB`, `FRC`,
  `TWTR` or `ATVI`. **You can price a dead ticker if you already know its
  name; you cannot enumerate dead tickers from Alpaca.** Any universe built
  from the assets endpoint is survivorship-biased by construction. Fix it
  with a point-in-time universe reconstructed from SEC filings or from
  archived index membership — and until that exists, say so in the backtest
  output rather than quietly shipping the bias.
- Corporate actions live at **`/v1/corporate-actions`**; `/v2/` 404s. Type
  enum and dividend-dominance already recorded in TRAPS.md. A live check
  over 2026-07-24 → 2026-08-07 returned
  `{'cash_mergers': 4, 'reverse_splits': 4, 'stock_and_cash_mergers': 1, 'stock_mergers': 1}`.
- News: an empty `symbols` list is treated as a filter, not "everything"
  (TRAPS.md, unchanged). Benzinga history reaches at least **2016-01-09**.

---

## 2. SEC — keyless, but the User-Agent is load-bearing

**Every SEC request must carry a contactable User-Agent.** The script uses
`Catalyst Research <contact email>` (override with `CATALYST_CONTACT_EMAIL`).

**Rate limit: 10 requests/second across *all* SEC APIs**, and an overrun
gets the IP temporarily blocked — which takes down `data.sec.gov`,
`www.sec.gov` and `efts.sec.gov` together. `verify_sources.py` paces itself
at roughly 3 req/s. Retry 5xx and 429 with backoff; **never retry a 4xx**.

### 2.1 `company_tickers.json`
`https://www.sec.gov/files/company_tickers.json` — **10,398** entries,
shape `{"0":{"cik_str":1045810,"ticker":"NVDA","title":"NVIDIA CORP"}}`.
Note `cik_str` is an **int** despite the name; zero-pad to 10 digits for the
`CIK##########` URL forms.

### 2.2 Submissions — the event clock
`https://data.sec.gov/submissions/CIK0000320193.json` → 200, Apple Inc.,
**1,000 recent filings** in `filings.recent`, with parallel arrays:
`accessionNumber, filingDate, reportDate, acceptanceDateTime, form,
primaryDocument, items, size, isXBRL`.

**`acceptanceDateTime` is the point-in-time truth**, not `filingDate`. A
filing accepted at 20:15 ET on day *T* is not tradable information until the
open on *T+1*; `filingDate` alone will hand the backtest an extra session of
foresight. Older filings than the recent 1,000 live in
`filings.files[]` as additional JSON documents.

### 2.3 XBRL — Candidate A's core dependency: **PASS**

- `companyconcept`:
  `https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax.json`
  → 200, **117 USD facts**, first `filed=2019-10-31`. Shape:
  `units.USD[].{start, end, val, fy, fp, form, filed, accn, frame}`.
- `companyfacts`: `https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json`
  → **271,819 bytes** for Apple. Every tag, every unit, every period, each
  with `filed`.
- `frames`:
  `https://data.sec.gov/api/xbrl/frames/us-gaap/EarningsPerShareDiluted/USD-per-shares/CY2024Q1.json`
  → 200, **4,937 filers** in one response.

**Candidate A is buildable.** The surprise measure can be constructed from
`companyconcept`/`companyfacts` alone, with no analyst data, and the `filed`
field makes it point-in-time honest.

**Gotchas — SEC XBRL**
- **`frames` has no `filed` date.** It is the *current* value for a period,
  so it silently includes restatements. Using `frames` to build a historical
  surprise series imports look-ahead bias that will make Candidate A look
  much better than it is. Use `companyconcept`/`companyfacts` and filter on
  `filed <= as_of`.
- **HEAD on `data.sec.gov` returns 403.** To size a document without pulling
  it, use a ranged GET (`Range: bytes=0-300` → HTTP 206 with a
  `content-range` giving the full length). A naive HEAD-then-GET wrapper
  will report every XBRL endpoint as forbidden.
- The tag you want is not always the tag they used. `Revenues`,
  `RevenueFromContractWithCustomerExcludingAssessedTax` and
  `SalesRevenueNet` are all live in the wild across eras; a single hard-coded
  tag returns an empty series for a large fraction of filers.

### 2.4 EDGAR daily index
`https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260807.idx`
→ 200, 6,127 lines, **6,116 filing rows** for one day. Fixed-width:
`Form Type | Company Name | CIK | Date Filed | File Name`.

A directory listing is available as `.../QTR3/index.json`, and the quarterly
roll-up as `full-index/{YYYY}/QTR{n}/master.idx` (2026Q1 = **33,215,017
bytes**, `accept-ranges: bytes`).

**Depth, measured:** `1994/QTR3` is the earliest directory that responds;
`1993/QTR1/index.json` returns `403 AccessDenied`. The 1994 file listing
starts at `form.070194.idx` — **1994-07-01**.

**Gotchas**
- The file is `form.YYYYMMDD.idx`; there is no file on weekends or holidays,
  and the current day's file does not appear until the evening (2026-08-07's
  was last modified 22:0x ET). **Correction, measured 2026-08-10 while
  building the live feed: an absent daily index returns `403
  AccessDenied` (an S3 error body), NOT 404.** `form.20260808.idx`
  (Saturday) and `form.20260810.idx` (same day, pre-publish) both 403'd.
  An earlier revision of this section said 404; that was wrong. The live
  feed (`catalyst/data/sources/edgar_form4.py`) distinguishes the
  absent-file 403 (`AccessDenied`/`NoSuchKey` body → recorded missing
  date, raw body kept) from a genuine rate-limit/IP-block 403 (fatal).
- **The filename format changed.** 1994 uses `form.MMDDYY.idx`
  (`form.070194.idx`); from 2000 onward it is `form.YYYYMMDD.idx`
  (`form.20000103.idx`). Constructing 1990s filenames with the modern
  pattern gives a 403, not a 404. **Read `{year}/{qtr}/index.json` and take
  the filenames from it** rather than generating them — that also handles
  the holidays for free.

### 2.5 Insider Transactions data sets — Candidate C's core dependency: **PASS**

`https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{YYYY}q{N}_form345.zip`

Confirmed live for `2026q1` (13,874,904 bytes), `2025q4` (8,309,708),
`2016q1` (13,630,106), `2010q1` (13,765,513), `2006q2` (14,116,730) and
`2006q1` (17,306,804). **`2005q4` returns 404** — so the series starts
**2006Q1**, measured, and the URL pattern is stable across all twenty years.

**File format** (read from the real 2026Q1 archive, not documentation):

| Member | Bytes |
|---|---|
| `SUBMISSION.tsv` | 7,487,317 |
| `REPORTINGOWNER.tsv` | 10,439,470 |
| `NONDERIV_TRANS.tsv` | 11,452,996 |
| `NONDERIV_HOLDING.tsv` | 2,729,055 |
| `DERIV_TRANS.tsv` | 6,110,613 |
| `DERIV_HOLDING.tsv` | 3,014,509 |
| `FOOTNOTES.tsv` | 43,870,427 |
| `OWNER_SIGNATURE.tsv` | 5,537,136 |
| `FORM_345_metadata.json` | 39,131 |
| `FORM_345_readme.htm` | 393,653 |

Tab-separated, joined on `ACCESSION_NUMBER`.

```
SUBMISSION.tsv
  ACCESSION_NUMBER  FILING_DATE  PERIOD_OF_REPORT  DATE_OF_ORIG_SUB
  NO_SECURITIES_OWNED  NOT_SUBJECT_SEC16  FORM3_HOLDINGS_REPORTED
  FORM4_TRANS_REPORTED  DOCUMENT_TYPE  ISSUERCIK  ISSUERNAME
  ISSUERTRADINGSYMBOL  REMARKS  AFF10B5ONE
  e.g. 0001193125-26-134840  31-MAR-2026  27-MAR-2026 ... 4  0001825079  Velo3D, Inc.  VELO   false

NONDERIV_TRANS.tsv
  ACCESSION_NUMBER  NONDERIV_TRANS_SK  SECURITY_TITLE  TRANS_DATE
  TRANS_FORM_TYPE  TRANS_CODE  EQUITY_SWAP_INVOLVED  TRANS_TIMELINESS
  TRANS_SHARES  TRANS_PRICEPERSHARE  TRANS_ACQUIRED_DISP_CD
  SHRS_OWND_FOLWNG_TRANS  DIRECT_INDIRECT_OWNERSHIP ...

REPORTINGOWNER.tsv
  ACCESSION_NUMBER  RPTOWNERCIK  RPTOWNERNAME  RPTOWNER_RELATIONSHIP
  RPTOWNER_TITLE  RPTOWNER_TXT  ... FILE_NUMBER
  e.g. ... Ong Sie Hou Raymond  Director ...
```

**What Candidate C needs is all present:** `TRANS_CODE` (`P` = open-market
purchase, `A` = award/grant, `G` = gift, `S` = sale), `TRANS_SHARES`,
`TRANS_PRICEPERSHARE`, `SHRS_OWND_FOLWNG_TRANS` (purchase size relative to
existing holding), `RPTOWNER_RELATIONSHIP`/`RPTOWNER_TITLE` (role), and
crucially **`AFF10B5ONE`** — the 10b5-1 plan flag, which is exactly the
routine, uninformative trading C must exclude.

**Gotchas**
- Dates are `DD-MON-YYYY` (`31-MAR-2026`), not ISO.
- `FILING_DATE` and `PERIOD_OF_REPORT` differ — Form 4 is due two business
  days after the transaction, so the *tradable* date is `FILING_DATE`, never
  `TRANS_DATE`. Backtesting off `TRANS_DATE` buys two free days of foresight.
- `FOOTNOTES.tsv` is the largest member (43 MB) and is almost never needed.
  Extract selectively.
- ~42 quarters at ~10 MB each ≈ 420 MB of downloads for a full history.
  Cache them; do not re-fetch per backtest run.

### 2.6 Financial Statement data sets
`https://www.sec.gov/files/dera/data/financial-statement-data-sets/{YYYY}q{N}.zip`
— 2026Q1 = **85,259,424 bytes**. `sub.txt, num.txt, pre.txt, tag.txt`;
`sub.txt` carries `adsh, cik, form, period, filed, accepted`. As-filed, so
free of restatement look-ahead.

**Depth, measured:** the series starts **2009Q1**, but the early quarters are
nearly empty — 2009q1 = **13,540 bytes**, 2009q2 = **144,894**, 2009q3 =
**3,544,077**, against ~85 MB today. XBRL was phased in by filer size, so a
backtest that starts in 2009 is quietly running on a few hundred large-cap
filers, not on the market. Treat anything before ~2011 as a different
universe, and say so in the results.

### 2.7 Failure-to-Deliver
`https://www.sec.gov/files/data/fails-deliver-data/cnsfails{YYYYMM}{a|b}.zip`
→ 200/206, ~1.0–1.3 MB per half-month. Pipe-delimited
`SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE`.

**Gotchas**
- The URL in wide circulation,
  `/files/data/frequently-requested-foia-document-fails-deliver-data/…`,
  now **404s**. The live path is `/files/data/fails-deliver-data/`.
- **This is a rolling window, not the full archive the SEC publishes
  elsewhere.** Measured 2026-08-10: `201707a` → 206, `201701a` → 404,
  `201601a` → 404, `201501a` → 404. Roughly nine years. If FTD is ever
  load-bearing, archive it rather than assuming the depth stays put.

### 2.8 EDGAR full-text search
`https://efts.sec.gov/LATEST/search-index?q="PDUFA date"&forms=8-K` → 200.
Elasticsearch-shaped: `hits.total.value`, `hits.hits[]._id`
(`accession:document`), `_source.{ciks, display_names, file_date, file_type,
root_form, period_ending}`. `"PDUFA date"` in 8-Ks: **2,490 hits** all-time;
narrowed to 2026-06-01 → 2026-08-01, **14 hits**.

**Gotchas**
- **Coverage effectively starts 2001**, measured: `"merger"` over
  1999-01-01 → 2000-12-31 returns **58** hits; over 2001 alone it returns
  **10,000+ (`relation: gte`)**. Those 58 are stragglers, not coverage —
  treat pre-2001 as invisible.
- A blank query returns **HTTP 200** with
  `{"error":"Blank search not valid...","hits":{"hits":[]}}`. A caller that
  only checks the status code sees an empty result and calls it "no data".
  Check for the `error` key.
- `hits.total.relation` is `gte` when the count is capped at 10,000 — the
  number is a floor, not a count. Report it as such.
- Per TRAPS.md, do not use `DEF 14A` as a blanket form filter.

---

## 3. FINRA — keyless

### 3.1 Reg SHO daily short volume
`https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`

2026-08-07 → 200, **12,182 lines**.
```
Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
20260807|A|316055.595634|21|554553.519523|B,Q,N
```
Fields: `Date, Symbol, ShortVolume, ShortExemptVolume, TotalVolume, Market`
(`B` = FINRA/Nasdaq TRF Carteret, `Q` = Nasdaq TRF, `N` = NYSE TRF).

**Gotchas**
- **History is a rolling window, not an archive.** Measured 2026-08-10:
  `2018-10-01` → 200, `2018-09-03` → `403 <Error><Code>AccessDenied</Code>`.
  The monthly zip archive (`/equity/regsho/monthly/CNMSshvol202607.zip`)
  also 403s. **Archive the daily file yourself, every day**, or this
  history evaporates from underneath the backtest.
- A 403 is also what a non-trading day looks like. Distinguish by checking
  the market calendar, not by the status code.
- **Volumes are fractional** (`316055.595634`) — odd-lot allocation. Do not
  cast to int.
- This is *reported* short volume across TRFs, not short *interest*, and it
  includes market-maker hedging. It is a texture signal, not a positioning
  signal.

### 3.2 Consolidated short interest
`POST https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest`
with `{"limit":1}` → 200. **Keyless** — no credentials needed, contrary to
the "may need credentials" note in STRATEGY-PROPOSALS.md §5.

Fields: `settlementDate, symbolCode, issueName, marketClassCode,
currentShortPositionQuantity, previousShortPositionQuantity,
averageDailyVolumeQuantity, daysToCoverQuantity, changePercent,
changePreviousNumber, stockSplitFlag, revisionFlag`.

**Gotchas**
- **Default response is CSV**, not JSON. Send `Accept: application/json`.
- Unfiltered results begin **2020-04-15** and come back in that order —
  a naive "latest" read gets the *oldest* row.
- `sort` is rejected unless the partition key is pinned:
  `400 ... "Sorting is allowed only if all partitions keys are specified in
  EQUAL CompareFilter"`. Filter instead:
  `{"limit":2,"compareFilters":[{"fieldName":"settlementDate","fieldValue":"2026-07-15","compareType":"EQUAL"}]}`
  → returns that settlement date's rows.

---

## 4. Nasdaq Trader trade halts

`https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts` → 200, RSS 2.0,
**26 `<item>` entries** on the checked run.

Per item: `ndaq:IssueSymbol, ndaq:IssueName, ndaq:HaltDate, ndaq:HaltTime,
ndaq:ReasonCode, ndaq:PauseThresholdPrice, ndaq:ResumptionDate,
ndaq:ResumptionQuoteTime, ndaq:ResumptionTradeTime`, plus
`ndaq:numItems` on the channel.

**Gotcha — this is the one that changes what stage 2 can build.**
The feed is **live only**. `&haltdate=08/07/2026` is accepted and returns
`<ndaq:numItems>0</ndaq:numItems>` — it does not serve history, even for
three days ago. So:

- **Candidate B cannot backtest halt classification from this source.**
  Halts must either be captured forward from the day the collector starts,
  or inferred from minute bars (a multi-minute volume-zero gap inside RTH),
  which is an approximation and should be labelled one.
- Start the forward capture **now**, independent of the rest of the build.
  Every day not captured is a day of history that cannot be recovered later.

---

## 5. ClinicalTrials.gov — the versioning question, resolved

**Yes. Historical versions of a study record are retrievable, and a
point-in-time replay works.** This is the load-bearing answer for
Candidate D, and it was demonstrated end to end, not read from docs.

**The endpoint is not the documented v2 one.**
`GET https://clinicaltrials.gov/api/v2/studies/{nctId}/history` → **404**.
The working endpoint is the site's own internal API:

```
GET https://clinicaltrials.gov/api/int/studies/{nctId}/history
GET https://clinicaltrials.gov/api/int/studies/{nctId}/history/{version}
```

**Change list** — `NCT04368728` (Pfizer/BioNTech C4591001), **53 versions**
from 2020-04-29 to 2026-03-03:
```json
{"changes":[
  {"version":0,"date":"2020-04-29","status":"NOT_YET_RECRUITING",
   "studyType":"INTERVENTIONAL","moduleLabels":[],
   "lastUpdateSubmitQcDate":"2020-04-29"},
  {"version":1,"date":"2020-05-04","status":"RECRUITING",
   "moduleLabels":["Study Identification","Study Status","Study Design",
                   "Contacts/Locations","References"], ...}]}
```

**A single version** — `/history/1` returns
`{"studyVersion":1,"study":{"protocolSection":{...}}}`, where `study` has
the **same shape as the v2 current-record response**, so one parser serves
both.

**Worked point-in-time replay, run live:**
```
as-of 2020-11-18
  -> version 18, dated 2020-11-11, status RECRUITING,
     moduleLabels ["Study Status","Outcome Measures","Contacts/Locations"]
  -> fetched: overallStatus = RECRUITING
              primaryCompletionDate = 2021-06-13 (ESTIMATED)
              lastUpdatePostDate    = 2020-11-13 (ACTUAL)
```
That is precisely the operation Candidate D needs: pick the last version
dated on or before the decision date, read the record *as it stood*, and
never see a field that was written later.

**Gotchas — ClinicalTrials.gov**

- **`/api/int/` is undocumented and unversioned.** It is the browser UI's
  own API. It can change or disappear without notice, and it carries no
  stability promise. **Snapshot every version you pull into local storage
  the first time you touch it**, and have the backtest read the snapshot.
  A dependency you cannot re-fetch is not a dependency you should re-fetch
  on every run.
- **The edge cross-checks the declared User-Agent against the TLS client
  fingerprint.** This cost an hour to find and is completely
  counter-intuitive:

  | client | User-Agent sent | result |
  |---|---|---|
  | httpx | *(default `python-httpx/0.28.1`)* | **200** |
  | httpx | `catalyst-research/0.1` | **403** |
  | httpx | `curl/8.5.0` | **403** |
  | httpx | `python-requests/2.31.0` | **403** |
  | httpx | `Mozilla/5.0 … Chrome/126` | **403** |
  | httpx | `catalyst-research/0.1 python-httpx` | **200** |
  | curl  | `catalyst-research/0.1` | **200** |
  | curl  | `catalyst-research (billysawyer0@gmail.com)` | **200** |

  Deterministic over 8 repeats, 1.5s apart. The rule is that the UA must be
  consistent with the client actually making the connection. **So there is
  no single global User-Agent for this project:** SEC *requires* a
  contactable UA and blocks generic ones; ClinicalTrials.gov *rejects*
  that same UA from httpx. `verify_sources.py` keeps `SEC_UA`, `CTGOV_UA`
  and `GENERIC_UA` separate for exactly this reason, and the 403 branch of
  the CT.gov probe prints the explanation rather than "no data".
- Per TRAPS.md, unchanged and reconfirmed by the version history above: a
  primary completion date is **not** an announcement date. Version 18 lists
  primary completion as 2021-06-13 *estimated* — the record itself tells
  you it is a projection. Use the `type` field (`ESTIMATED` vs `ACTUAL`);
  a `type: ACTUAL` transition appearing in a later version is a much better
  event marker than the date value.
- Rate limit: not published as a number. Requests here are paced at ~1/s.
  Bulk version pulls should run once, into a cache, off the live path.

**Verdict for stage 4: Candidate D is gradeable.** The point-in-time
premise holds. The remaining risk is event *density*, not data access.

---

## 6. Benchmarks and rejected sources

### 6.1 FRED — T-bill benchmark, keyless, no API key
`https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO` → 200, CSV,
**11,724 rows from 1981-09-01 to 2026-08-06**. Header
`observation_date,DGS3MO`; last row `2026-08-06,3.90`.

This is the keyless CSV download, not the `api.stlouisfed.org` endpoint that
needs a registered key. Any FRED series id works (`DGS1MO`, `DGS3MO`,
`SP500`, `VIXCLS`). **Gotcha:** holidays are present as rows with an empty
value, not omitted — parse blanks as missing, not as zero. A rate that
parses as `0.0` on Thanksgiving quietly flatters every excess-return figure.

### 6.2 Treasury FiscalData — keyless
`https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/avg_interest_rates?page[size]=2&sort=-record_date`
→ 200. `data[].{record_date, security_type_desc, security_desc,
avg_interest_rate_amt}`. Newest: `2026-07-31, Treasury Bills, 3.758`.
Monthly averages — useful as a cross-check on FRED, too coarse to be the
primary daily risk-free series.

### 6.3 Stooq — **rejected, verified failing**

`https://stooq.com/q/d/l/?s=spy.us&i=d`

The widely-cited free daily CSV source. It does not work unattended from a
server as of 2026-08-10, and it fails in two *different* ways depending on
the client, which is why it looks intermittent:

```
curl  -> HTTP 200, 796 bytes, content-type text/html:
   <!DOCTYPE html><html><head><meta charset="utf-8">
   <meta name="robots" content="noindex,nofollow"></head><body>
   <noscript>This site requires JavaScript to verify your browser.
   Please enable JavaScript an...

httpx -> HTTP 404, 271 bytes, content-type text/html:
   ...The page you requested does not exist or has been moved...

httpx (later run) -> ConnectError: [Errno 104] Connection reset by peer
```

Same for `stooq.pl`, for `^SPX`, and for date-ranged requests. **A JS
browser challenge returning HTTP 200 is worse than an error** — a naive
CSV parser sees a 200 and produces zero rows, and the pipeline reports "no
data" for what is actually a blocked request.

`verify_sources.py` keeps a Stooq probe that is **expected to FAIL**, so
that if it ever starts serving CSV again we find out from a run rather than
from a rumour. An expected-FAIL does not set the script's exit code.

### 6.4 Yahoo Finance v8 chart — works, deliberately not adopted

`https://query1.finance.yahoo.com/v8/finance/chart/SPY?period1=…&period2=…&interval=1d&events=div,split`
→ **200**, real data, keyless, `meta.firstTradeDate=728317800`
(1993-01-29 — SPY's inception), with `indicators.adjclose` and
`events.dividends`.

It is genuinely the only verified source here that reaches back before 2016.
It is **not adopted** because:

1. It is an undocumented private endpoint with no stability or availability
   promise, and Yahoo's terms do not permit programmatic redistribution.
   This system must run unattended for months; a silent breakage in the
   benchmark series is exactly the failure the brief calls out.
2. Alpaca SIP already covers 2016 onward with a real subscription behind it,
   and 10.5 years is enough for every candidate on the table.

Documented here so the option is a decision rather than an oversight. If a
strategy ever genuinely needs pre-2016 daily history, this is the fallback
to re-evaluate — with the licensing question answered first.

---

## 7. What this changes for stages 2 and 4

**Stage 2 (backtest harness)**

1. **The universe is 2016-01-04 onward, 10.5 years, ~2,650 sessions.** Not
   negotiable on Alpaca; pre-2016 needs a source we have chosen not to
   depend on (§6.4).
2. **SPY total return = `feed=sip&adjustment=all`.** +351.9% over the full
   window versus +284.7% price-only. Hard-code neither; fetch both and
   report which is which, per the brief's "every number says where it came
   from".
3. **Build a local cache once, then never touch the network mid-run.**
   ~266 requests and ~80 seconds for a 1,000-name daily universe.
4. **Survivorship bias is real and currently unmitigated.** Alpaca prices
   delisted tickers but cannot enumerate them. Until a point-in-time
   universe exists, every backtest result must carry that caveat visibly,
   not in a footnote.
5. **Halt history does not exist to be replayed.** Start capturing the
   Nasdaq RSS daily from now. Anything Candidate B claims about halts before
   the capture starts is inference, and must be labelled inference.
6. **FINRA Reg SHO history is a rolling window** that currently begins
   ~2018-10. Archive daily or lose it.

**Stage 4 (strategy dependencies)**

| Candidate | Core dependency | Status after this pass |
|---|---|---|
| A — XBRL post-earnings drift | `data.sec.gov` companyconcept/companyfacts | **Confirmed.** `filed` gives honest point-in-time. Avoid `frames`. |
| B — gap classification by provenance | EDGAR daily index + Alpaca news + halts | **Two of three.** Halt history is unavailable — B's headline event type cannot be backtested, only forward-captured. |
| C — insider clusters | SEC Insider Transactions data sets | **Confirmed.** Role, size, clustering and the 10b5-1 flag are all present. Use `FILING_DATE`, not `TRANS_DATE`. |
| D — post-resolution drift | ClinicalTrials.gov **version history** | **Confirmed and demonstrated.** Point-in-time replay works. Endpoint is undocumented — snapshot it. |
| E — ETF relative strength (control) | Alpaca daily bars only | **Confirmed.** |
