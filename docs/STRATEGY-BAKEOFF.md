# Strategy bake-off — results

Owner: `strategy-analyst`. Graded 2026-08-10 on the Stage-2 harness
(`catalyst/backtest/`), same date range, same costs, same account rules
for every arm. Every number below was produced by
`scripts/run_bakeoff.py` and persisted to `data/bakeoff.db`
(19 results, 38 sample-stat rows). Nothing in this document is an
estimate; where a number could not be measured, that is stated instead.

**Headline, stated first because the brief requires the honest outcome:
nothing beats SPY out-of-sample net of all costs, robustly.** One arm —
insider-cluster buying (C) — shows a nominal out-of-sample excess of
**+6.73pp** over SPY at the harness's default costs, but that beat
inverts to **-15.17pp** the moment the per-side spread assumption moves
from 15bp to 30bp (the very cost floor C's own pre-registered proposal
demanded for its small-cap universe), and an unconditional event study
shows the portfolio's winning subsample overstates the population mean
by more than 2x. Two arms (A and C) do show **real, statistically
detectable per-trade drift edges** (t ≈ 2.1-2.6 in both samples) —
the edges exist; they are simply too small, at achievable frequency, to
outrun a compounding index plus ~10%/yr of fixed API cost on a $1,000
account. The previous build's conclusion generalises: on this account
size, the binding constraint is cost structure, not signal quality.

---

## 1. What was graded, and how

| Held constant | Value |
|---|---|
| Harness | `catalyst.backtest.harness.replay_detailed`, unmodified |
| Account | $1,000 cash, long-only, no leverage, T+1 settlement, max 5 slots, equal-weight equity/5 per position, fractional shares |
| Fills | signal at close of D → fill at open of D+1; entry pays open×(1+cost), exit receives open×(1−cost); never a same-day fill |
| Costs (primary) | 15bp per side (30bp round trip) + $8/month API, deducted from final equity |
| Full range | 2016-01-04 → 2026-08-07 (SIP floor to cache end; 2,664 sessions) |
| IS / OOS split | chronological at **2023-12-31** (fixed before any grading) |
| OOS-only run | 2024-01-02 → 2026-08-07, fresh $1,000 (gives the OOS excess-vs-SPY) |
| Benchmark | SPY, feed=sip, adjustment=all (total return) |
| Max hold | 15 trading days (brief's ceiling); strategies request ≤ that |

Discipline followed:

- **Pre-registration.** Each arm was implemented exactly from its
  STRATEGY-PROPOSALS.md design and graded once before any tuning.
  Tuned variants were selected **on the in-sample window only**
  (`run_bakeoff.py --is-only`); both results are reported below. No
  parameter was ever chosen by looking at the OOS window.
- **Point-in-time.** Signal functions receive only a `PointInTimeView`
  plus event tables derived offline from `filed`/`FILING_DATE`-stamped
  primary sources (details per arm).
- **Sensitivity runs are labeled** ($0/mo API isolates market edge from
  fixed-cost drag; 30bp/side stresses the spread assumption). The
  primary grade is always $8/mo, 15bp/side.

## 2. The comparison table

All returns net of spread/slippage; "net" and "EXCESS" also net of
$8/mo API. Sample size beside every number. BE = per-trade return needed
to cover the API bill at the realised trade count and notional.

### Full range 2016-01-04..2026-08-07 (per-trade stats split at 2023-12-31)

| Arm | Sample | n | hit | mean/trade | median | worst trade | maxDD | BE/trade |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **E** ETF rotation (pre) | IS | 1,485 | 48.1% | −0.16% | −0.08% | −13.48% | 43.9% | 0.32% |
| | OOS | 524 | 49.4% | −0.07% | −0.05% | −24.81% | 17.1% | 0.43% |
| **A** XBRL earnings drift (pre) | IS | 268 | 57.1% | **+0.55%** | +0.78% | −29.89% | 15.9% | 1.26% |
| | OOS | 84 | 57.1% | **+1.59%** | +0.51% | −18.51% | 8.8% | 1.02% |
| **C** insider clusters (pre) | IS | 710 | 53.1% | **+0.28%** | +0.64% | −89.64% | 52.6% | 0.52% |
| | OOS | 203 | 49.3% | **+0.87%** | −0.06% | −57.43% | 41.2% | 0.64% |

### Headline: excess return over SPY, net of all costs

| Arm / run | window | SPY | strategy net | **EXCESS net** |
|---|---|---:|---:|---:|
| E pre, full | 2016..2026 | +353.13% | −146.74% | **−499.87%** |
| E pre, IS-only | 2016..2023 | +170.36% | −116.67% | **−287.03%** |
| E pre, OOS-only | 2024..2026 | +68.72% | −31.41% | **−100.13%** |
| A pre, full | 2016..2026 | +353.13% | −34.52% | **−387.64%** |
| A pre, IS-only | 2016..2023 | +170.36% | −45.85% | **−216.21%** |
| A pre, OOS-only | 2024..2026 | +68.72% | +4.62% | **−64.10%** |
| C pre, full | 2016..2026 | +353.13% | −65.21% | **−418.34%** |
| C pre, IS-only | 2016..2023 | +170.36% | −27.02% | **−197.38%** |
| C pre, OOS-only | 2024..2026 | +68.72% | **+75.45%** | **+6.73%** |

OOS-only stats for the arms (fresh $1,000, all trades out-of-sample):
E n=520, hit 49.8%, mean −0.05%; A n=84, hit 57.1%, mean +1.59%,
maxDD 8.8%; C n=229, hit 52.8%, mean +1.75%, median +0.69%,
worst −57.43%, maxDD 13.9%.

### Sensitivity (labeled, not the primary grade)

| Run | change | OOS-only EXCESS |
|---|---|---:|
| C pre | API $8 → $0/mo | +6.73% → **+31.64%** |
| C pre | 15bp → 30bp per side | +6.73% → **−15.17%** |
| A pre | API $8 → $0/mo | −64.10% → −39.18% |
| E pre | API $8 → $0/mo | −100.13% → −75.21% (absolute net still −6.49%) |

**The C result flips sign inside the plausible spread band for its own
universe.** That is the whole verdict on C in one row.

### Tuning log (in-sample only; OOS never consulted)

| Variant | IS n | IS mean/trade | vs pre-registered | Decision |
|---|---:|---:|---|---|
| E tuned: biweekly, hold 9 | 739 | −0.06% | pre: −0.16% (n=1,485) | Better but still negative before API. **E dead in both variants.** |
| A tuned: SUE ≥ 2.0 | 144 | −0.13% | pre: +0.58% (n=268) | Worse. **Tuned A rejected; pre-registered A stands.** |
| C | — | — | — | Not tuned: no variant was tried, because the pre-registered version's failure mode (spread sensitivity) is not a parameter problem. |

## 3. Per-arm detail

### E — ETF cross-sectional relative strength (control arm)

- **Thesis:** top-4 of 23 liquid sector/asset-class ETFs by 60-session
  return, weekly rotation, price data only. The null hypothesis the
  data-linked arms must beat.
- **Data / point-in-time:** Alpaca SIP daily bars, adjustment=all.
  Clean — prices only, no event timing to get wrong. ETFs with short
  histories (XLC 2018-) excluded until 60 sessions exist.
- **Result:** mean per-trade return is **negative before any API cost**
  in both samples (IS −0.16% n=1,485; OOS −0.07% n=524). The 30bp
  round trip plus one settlement day of cash drag per cycle exceeds the
  gross momentum edge. At $0 API it still loses money outright
  (−45% absolute over the decade). The in-sample-tuned biweekly variant
  narrows the loss (−0.06%, n=739) but stays under water.
- **Verdict: fails absolutely, not just relatively.** But it did its
  control-arm job: **no data-linked arm needed to be cheaper than E to
  win, and E set the bar at "any positive net edge at all" — A and C
  both cleared that per-trade bar while still losing to SPY.**
- **Falsified by:** its own pre-registered criterion ("no positive net
  return") — triggered, n=2,009.

### A — post-earnings drift from XBRL (no analyst data)

- **Thesis:** prices under-react to earnings surprises; surprise =
  seasonal random walk on first-filed quarterly NetIncomeLoss,
  standardised by its own history (SUE ≥ 1.0), traded only when the
  3-session price reaction agrees in sign. Hold 12 trading days.
- **Data / point-in-time:** `data.sec.gov` companyfacts for the 100
  cached large caps (all 100 fetched, 0 failures). Every value is the
  **first-filed** figure for its period (min `filed` per period), so
  restatements cannot leak backwards; fiscal Q4 is derived as FY minus
  three quarters, dated by the 10-K's `filed`. The `frames` endpoint is
  never touched (restatement-contaminated, no filed date). Event date =
  the XBRL filing date, which is **later** than the earnings press
  release — measured drift is biased **down**, not up, by this choice.
- **Result:** the only arm with a positive per-trade edge in **both**
  samples (IS +0.55% n=268, OOS +1.59% n=84, hit 57% both, worst
  −29.9%, maxDD 15.9%/8.8%). But at 2.8 trades/month realised, its
  break-even is 1.26%/trade IS — **the edge did not clear its own API
  bill in-sample** — and its exposure (≤5 × $200 slots, often fewer
  filled) cannot compound with an index that returned +353%. OOS excess
  −64.10pp.
- **Position size / worst case:** equity/5 ≈ $200/position at start;
  worst realised single trade −29.9% of one slot ≈ **−6% of account**.
- **Falsified by (pre-registered):** "mean net return below break-even"
  — triggered in-sample. The n=84 OOS mean above break-even is real but
  n=84 is too small to overturn n=268, per the proposal's own §3.2.

### C — insider-cluster open-market buying

- **Thesis:** ≥2 distinct insiders buying (Form 4, code P, non-10b5-1
  where flagged) within 10 days, ≥$50k combined, in names ≥$5 with
  ≥$1M median dollar volume; public-but-under-consumed information.
  Hold 12 trading days.
- **Data / point-in-time:** all 41 SEC quarterly insider datasets
  2016q1-2026q1 (2026q2 not yet published, so **C's events stop
  2026-03-31**); 444,358 open-market purchase rows; 19,079 cluster
  events across 5,220 symbols; Alpaca bars fetched for 4,860 of them
  (the 361 missing are OTC/fund tickers the liquidity floor rejects
  regardless). Tradable date = `FILING_DATE`, never `TRANS_DATE`.
  Known honest gaps: the 10b5-1 flag only exists from ~2023 (earlier
  plan trades add noise, not look-ahead); the proposal's
  pending-binary exclusion filter could not be replayed (no historical
  catalyst calendar exists) — its absence leaves MORE tail risk in the
  measured sample, i.e. biases against C; a reused ticker symbol can
  splice two companies' price histories (guarded by a 5-day staleness
  check, not eliminated).
- **Result:** per-trade edge positive but small in-sample (+0.28%,
  n=710, worst single trade **−89.6%**, maxDD 52.6%) and larger
  out-of-sample (+1.75%, n=229). The OOS-only portfolio **nominally
  beat SPY: +75.45% vs +68.72%, excess +6.73pp** — the only positive
  excess in the bake-off. Three measurements say it is not robust:
  1. **Spread sensitivity:** at 30bp/side — the round-trip floor C's
     own proposal specified for sub-$1bn names — OOS excess =
     **−15.17pp**. The verdict flips inside the plausible cost band.
  2. **Population vs subsample:** slot contention meant the portfolio
     took 229 of 1,522 eligible OOS signals, path-dependently. The
     unconditional event study (all eligible signals, 30bp RT) shows
     population mean **+0.73%** OOS (n=1,522, t≈2.4) vs the
     portfolio's +1.75% — the beat rode a lucky right-tail subsample.
     Fully-deployed arithmetic on the population mean (~19 slot-cycles
     x +0.73%/yr ≈ +15%/yr gross, ~+5%/yr after $8/mo API) **lags
     SPY's OOS pace (~+22%/yr)**.
  3. **In-sample it lost to SPY by 197pp**, with a −97.5% single trade
     in the IS-only run and 61% max drawdown. The OOS window is
     bull-only and contains no 2020/2022-style regime.
- **Position size / worst case:** $200 slots; worst realised trade
  −89.6% of a slot ≈ **−18% of account** on one position. That is the
  un-excluded binary tail the proposal's filter was designed to remove.
- **Falsified by (pre-registered):** "edge exists only below a spread
  threshold we cannot trade" — this is C's own predicted death, now
  measured rather than argued, pending one open question (§6).

### B — intraday gap classification: not graded, and why that is honest

The harness replays **daily** bars with next-open fills; B is flat by
the close, so every number the harness could produce for B would be a
fiction. Grading it would require: (1) minute bars 2016-2026 (~2GB+,
verified available on this account but a separate cache build), (2) an
intraday replay mode with enforceable stops — a harness change owned by
backtest-engineer, (3) halt history, which **does not exist to
replay** (Nasdaq RSS is live-only; DATA-SOURCES.md §4) — forward
capture only, and it has not been running. B stays ungraded rather than
dishonestly graded. Cost to make it gradeable: minute-bar cache
(~1-2 days), intraday harness mode (owned elsewhere), and months of
forward halt capture for its headline event type.

### D — post-resolution drift: not graded, and why that is honest

ClinicalTrials.gov version history works (verified, DATA-SOURCES.md §5)
but building a point-in-time **event set** means bulk version pulls
over an undocumented internal API (~1/s pacing, thousands of studies),
plus sponsor→ticker mapping for which no free structured source exists
— hand-mapping invites cherry-picking, and the biotech universe needs
delisted names more than any other arm (survivorship is worst exactly
where D trades). The proposal's own frequency arithmetic (3-8
trades/month, ~3% break-even at $8/mo) already made D unlikely as a
primary. Not graded; the cost of grading it honestly (weeks of
snapshot-building) exceeds its prior.

## 4. Recommendation

**Do not build the live pipeline around any of these arms on the "beat
SPY" measure — nothing cleared it robustly.** The specific findings:

1. **E is dead** (negative per-trade before API, both variants,
   n=2,009). The control did its job: complexity was not the reason A
   and C failed — cost structure was.
2. **A has the cleanest real edge** (positive both samples, t≈2.1-2.6,
   modest drawdowns) but at 2.8 trades/month on 100 large caps it
   cannot pay its own fixed costs, let alone catch SPY.
3. **C has the most signal supply** (49 eligible/month OOS) and a
   population edge that is real (t≈2.4) but ~0.4-0.7%/trade — smaller
   than the spread uncertainty on its own universe.

If the owner wants a next step that the numbers actually support, it is
**measurement, not deployment**: (a) have market-structure measure real
NBBO spreads on C's liquid-filtered universe at $200-330 clip sizes —
if true round-trip cost is ≤20bp, C's population edge nets positive
per-trade and the question reopens; if it is ≥40bp, C is dead by its
own pre-registered criterion and this bake-off closes it; (b) A's edge
should be re-graded on a broader, smaller-cap universe (where PEAD is
documented to be larger) **only if** a point-in-time universe with
delisted names exists first — on the current survivorship-biased 100
large caps, A's +0.55%/trade is an upper bound estimate of a lower
bound phenomenon.

**What would change my mind** (pre-stated, measurable):
- C surviving 30bp/side with positive OOS excess on the population
  (not the slot-contended subsample) → promote C.
- Measured real spreads on C's universe ≤20bp round trip → re-open C.
- A producing ≥8 trades/month with mean ≥ its break-even on a
  delisting-complete universe → promote A.
- Any arm beating SPY over a window that includes a drawdown regime,
  not only 2024-2026 → revisit that arm.
- The $8/mo assumption falling to ~$2/mo (C's screen is fully
  mechanical; model research adds cost without demonstrated benefit
  here) cuts every break-even ~4x — worth re-running the table at the
  cost the pipeline actually proposes.

## 5. Open questions for market-structure (leader-specific)

For C (and A if revived), at $200-330 position sizes:
1. Real NBBO spreads, by dollar-volume bucket, for the $5+/$1M+ median
   dollar-volume universe — the graded results bracket the answer
   (15bp/side: C wins OOS; 30bp/side: C loses). **This single number
   decides C.**
2. Fill realism at the next-day open for small caps: opening auction
   participation at $200 clips, and whether the open is the wrong
   moment (the harness's only available fill point on daily bars).
3. Whether a $200 order moves these names at all (median $1M+ dollar
   volume → $200 ≈ 0.02% of a day's volume; presumably no, but
   presume nothing).

## 6. Defects found while grading (for the owning agents)

- **Harness end-of-range bug (backtest-engineer):** a signal on the
  second-to-last session queues its entry for the last session; entries
  run after that day's exits, so the position survives the replay and
  trips the `assert not positions` guard. Reproduce: any candidate with
  `catalyst_date` = second-to-last calendar session. Worked around in
  `scripts/run_bakeoff.py` (drops candidates that cannot complete a
  round trip); needs a real fix + failing test (test-writer).
- **`fetch_history.py` overwrites `cache_meta.json`** on every run, so
  the shared `data/bars` meta now describes the last (ETF) fetch, not
  the union. Cosmetic but violates "every number says where it came
  from" (data-engineer).
- **Slot-contention path dependence:** with far more signals than
  slots, *which* trades the replay takes is arbitrary (first-come);
  reported portfolio stats are one draw from a wide distribution. The
  event-study population numbers in §3 are the stable statistic; the
  harness could offer a shuffled-priority Monte Carlo mode to quantify
  this (backtest-engineer, nice-to-have).

## 7. Reproduction

```
python3 scripts/fetch_history.py                         # 100 names + SPY (done)
python3 scripts/fetch_history.py --symbols "XLB,...,VNQ" # ETFs (done)
python3 scripts/fetch_xbrl_facts.py                      # 100 companyfacts (done)
python3 scripts/fetch_insider_data.py                    # 41 quarterly zips (done)
python3 scripts/fetch_history.py --symbols "$(cat data/insider/symbols.txt)" --cache data/bars_insider
python3 scripts/build_events.py                          # derive event tables
python3 scripts/run_bakeoff.py --only E,A,C --api0       # primary + $0 sensitivity
python3 scripts/run_bakeoff.py --only C --cost-bps 30    # spread sensitivity
python3 scripts/run_bakeoff.py --only E,A --variant tuned --is-only  # tuning, IS only
```

Disk: `data/` totals ~850MB (insider zips 450MB, insider bars ~330MB,
XBRL 27MB, bars 15MB). All gitignored.

## Market-structure verdict on C (measured)

Owner: `market-structure`. Measured 2026-08-10, answering §5 Q1-Q3 and
the §4 pre-registered gate: **per-side cost ≤20bp reopens C; ≥40bp
closes it.** Every number below is a measurement against live Alpaca
SIP historical NBBO, not an estimate.

### Which quotes were used, and why (staleness disclosed)

- Measurement ran Monday 2026-08-10 ~10:05-10:20 UTC (06:05 ET,
  pre-market). The latest-quote endpoint was checked first and was
  **stale/unusable as required to disclose**: timestamps came back
  `2026-08-07T20:00:01Z` (Friday's close), and this subscription
  rejects recent SIP (`"subscription does not permit querying recent
  SIP data"`); the IEX latest feed is not an NBBO (AAPL ask=0,
  OPK 1.15/1.58). Off-hours quotes would have biased the verdict
  toward killing C, so none were used.
- Instead: **historical SIP NBBO for the last regular session's final
  30 minutes** — 2026-08-07 19:30:00Z-20:00:00Z — via
  `GET /v2/stocks/{symbol}/quotes?feed=sip`, one page of up to 10,000
  quotes per symbol (median covered window 1,800s, i.e. full coverage;
  75/794 liquid names truncated, time-weighted over the covered span).
  Where the window's head was uncovered >60s, the prevailing quote
  before 19:30Z was fetched and carried. Half-spread per symbol =
  time-weighted mean of `(ask-bid)/2/mid`, valid intervals only
  (bid>0, ask≥bid).

### Sample construction

C's OOS event list was reconstructed by replaying
`insider_cluster.py`'s exact signal-time filter (last close ≥$5,
median 20-session dollar volume ≥$1M, 5-day staleness guard) over
`data/insider/cluster_events.csv` against `data/bars_insider/`:
3,521 OOS events (2024-01-02..2026-08-07) → **1,526 eligible events,
865 distinct symbols** — matching the bake-off's 1,522 (difference is
the end-of-range workaround). 794/865 symbols were measurable;
**71 returned `quotes:null` + `trades:null` for all of 2026** (raw
responses checked — delisted/acquired/renamed since their events, e.g.
ATSG, BERY; they carry 124/1,526 = 8% of events). Bias direction:
dead names' spreads are unmeasurable and were likely wider, so the
distribution below is slightly flattered; all of them did pass the
$1M floor at event time.

### The distribution — half-spread, bp, per side

| Universe / weighting | n | p25 | **median** | mean | p75 | p90 | worst-decile mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| **C, equal-weight per symbol** | 794 | 4.4 | **8.3** | 13.8 | 15.1 | 29.2 | 57.8 |
| **C, event-frequency-weighted** | 1,402 ev | 4.4 | **8.2** | 13.4 | 15.0 | 29.2 | 54.1 |
| A baseline (100 large caps) | 100 | 0.9 | **1.5** | 1.7 | 2.3 | 3.0 | 3.6 |

The A baseline landing at the expected 1-3bp/side validates the
method; C's number is not an artifact of the measurement.

By half-spread bucket (event-weighted): <10bp = 818 events, 10-20bp =
356, 20-40bp = 146, ≥40bp = 82. So **84% of measured events cost
<20bp/side; 6% sit at ≥40bp/side** (worst names: CMIIU 348bp,
ECOR 234bp, EDSA 206bp — exactly the SPAC/micro-biotech tail).

### Depth and slippage at $200-330 clips (§5 Q3)

Displayed inside size (SIP sizes are **shares**, shown in round-lot
multiples since the Nov-2025 tiered round-lot change — verified on
AAPL, sizes all multiples of its 40-share lot): median min(bid,ask)
dollar depth = **21x a $330 clip**; 5th percentile 4.2x; one symbol
of 794 below 1x. A $330 order is ~0.03% of a $1M ADV. **Depth is not
binding at this size — confirmed, not presumed.** Note the backtest
fills at the next-day opening auction, which pays the auction clearing
price, not the quoted spread; the numbers above are the honest cost of
any continuous-session entry/exit (stops, early exits) and a ceiling
for auction fills. Opening-auction slippage at these clips remains
unmeasured (§5 Q2 stays open).

### Verdict, per the pre-registered gates

**Median round-trip cost = 16.4bp event-weighted (2 × 8.2bp/side),
16.6bp equal-weight — under the 40bp (2×20bp) reopen gate with 2.4x
margin, and a quarter of the 80bp kill gate. C's pre-registered kill
condition is NOT triggered; by its own reopen condition, C reopens.**

- The harness's primary grade (15bp/side, OOS excess +6.73pp) assumed
  a cost *above* the measured event-weighted mean (13.4bp/side). The
  30bp/side stress that flipped C to −15.17pp corresponds to ~p90 of
  the measured distribution — the typical trade does not pay it.
- **Tradeable — with one required modification:** the worst decile
  (≥29bp/side, tail mean 54-58bp) breaches the kill gate individually
  and must be excluded by a deterministic max-spread gate at entry
  (skip if measured half-spread >20bp). That keeps 84% of event flow
  (~41/month of the 49) and caps every taken trade inside the reopen
  gate. Without the gate: tradeable-smaller at best; with it:
  **tradeable** at $200-330 clips.
- What would change this verdict: opening-auction fills measurably
  worse than the continuous quote (§5 Q2), or the delisted-8% turning
  out to have carried a disproportionate share of the *edge* rather
  than just the cost.

Raw per-symbol measurements: scratchpad `c_spreads.json` /
`a_spreads.json` (session-local); method script `measure_spreads.py`.

---

## Sector enrichment, measured 2026-08-22

The graded insider-cluster arm keyed every candidate's sector as
`"unknown"`, because Form 4 payloads carry no sector field. Since
`correlation.py` clusters on `sector|catalyst_type|resolution_week`,
every same-week insider cluster collapsed into ONE key and
`max_correlated_cluster_pct` capped unrelated companies as a single bet.

7,668 issuer SIC codes were fetched from EDGAR's submissions API
(`scripts/fetch_sic.py`), joined to ticker symbols via `purchases.csv`
(`scripts/build_symbol_sic.py`), and the arm re-graded both ways.
**92.6% of traded symbols resolved to a SIC, across 379 distinct codes**
— pharma (28xx) is the largest group at 20%, so the split is real rather
than one bucket becoming another.

`--no-sectors` reproduces the originally graded run exactly
(OOS api$0 excess **−20.09%** against the recorded −0.2009), so the two
columns below are comparable rather than merely different.

### Excess return vs SPY

| Run | Baseline | Sector-enriched | Change |
|---|---|---|---|
| OOS 2024-01..2026-08, api $0 | −20.09% | **+1.83%** | **+21.9 pp** |
| OOS 2024-01..2026-08, api $8/mo | −45.00% | **−23.09%** | +21.9 pp |
| Full 2016-01..2026-08, api $0 | −296.40% | −234.23% | +62.2 pp |
| Full 2016-01..2026-08, api $8/mo | −398.07% | −335.89% | +62.2 pp |

### The mechanism is confirmed

`max_correlated_cluster` skips: **OOS 55 → 0**, full period **204 → 1**.
The bound essentially stopped binding, which is exactly the predicted
cause. Per-trade quality improved with the SAME trade count (230 OOS
either way), which is the harness's "selection effect, not a scale one"
showing up as predicted:

| OOS metric | Baseline | Enriched |
|---|---|---|
| hit rate | 0.500 | **0.535** |
| mean per trade | +1.26% | **+1.58%** |
| max drawdown | 20.12% | **19.23%** |

The binding constraint moved from the cluster cap to `no_free_slot`
(1,241 → 1,290) and settled cash (0 → 6).

### What this does NOT show — read this before acting on the table

**The strategy still loses to SPY at any realistic API cost.** The only
column that beats the benchmark is out-of-sample with the API bill set
to **zero**, which is not a configuration that can be run. At $8/month
it is **−23.09%**; at the owner's current $100/month cap the hurdle is
far higher again (60% a year on $2,000).

The full ten-year period remains catastrophic: **+118.9% against SPY's
+353.1%** with no API cost at all.

So this validates **the fix**, not **the strategy**. Sector enrichment
recovered roughly 22 points of a 30-point defect, exactly where the
harness said the defect was. It did not make this arm viable, and
nothing here supports expecting it to beat holding the index.
