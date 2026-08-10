# Strategy proposals — for grading, not for building

Status: **proposals only. Nothing here is graded. Nothing here is a
recommendation to build.**

Owner: `strategy-analyst`. This document is the input to build-order step 3
(BUILD-BRIEF.md § "Build order" → "Strategy bake-off"), and it deliberately
arrives *before* `backtest-engineer`'s harness exists. Its job is to give
that harness five concrete, mutually distinguishable things to grade, and to
state in advance what measurement would kill each one — so that the grading
is a test rather than a search for confirmation.

**Every quantitative figure below is an unvalidated estimate.** They are
marked `[EST]`. They exist to make each candidate falsifiable and to show the
arithmetic a real number would have to beat, not to describe reality. Per
CLAUDE.md house rule 1, an asserted fact is not a checked fact; none of these
are checked.

**No claim here rests on a backtest, because there is no backtest.**
`catalyst/backtest/` does not exist (`git log` shows two commits, both
documentation). BUILD-BRIEF.md is explicit that the previous build shipped
"an assumed 60% adverse gap and a 0.65 conviction floor, both invented,
neither validated." Producing a confident single answer now would repeat that
exact error with better prose. What follows is a set of hypotheses with
pre-registered kill conditions.

---

## 0. Contents

1. [What I could not verify, and why](#1-what-i-could-not-verify-and-why)
2. [Two structural constraints that reshape every candidate](#2-two-structural-constraints-that-reshape-every-candidate)
3. [The arithmetic every candidate must clear](#3-the-arithmetic-every-candidate-must-clear)
4. [Re-reading the previous build's result](#4-re-reading-the-previous-builds-result)
5. [The free data sources these candidates link](#5-the-free-data-sources-these-candidates-link)
6. Candidates:
   - [A — Post-earnings drift, sourced from XBRL rather than analyst consensus](#candidate-a--post-earnings-drift-sourced-from-xbrl-rather-than-analyst-consensus)
   - [B — Intraday gap classification by information provenance](#candidate-b--intraday-gap-classification-by-information-provenance)
   - [C — Insider-cluster buying, with the catalyst feeds used as an exclusion filter](#candidate-c--insider-cluster-buying-with-the-catalyst-feeds-used-as-an-exclusion-filter)
   - [D — Post-resolution drift on regulatory and clinical events](#candidate-d--post-resolution-drift-on-regulatory-and-clinical-events)
   - [E — Control arm: cross-sectional relative strength on liquid ETFs](#candidate-e--control-arm-cross-sectional-relative-strength-on-liquid-etfs)
7. [Structural fit against ARCHITECTURE.md](#7-structural-fit-against-architecturemd)
8. [What the bake-off must hold constant](#8-what-the-bake-off-must-hold-constant)
9. [Proposed runtime budget](#9-proposed-runtime-budget)
10. [If forced to guess right now](#10-if-forced-to-guess-right-now)
11. [What would change my mind](#11-what-would-change-my-mind)
12. [Open items this document does not resolve](#12-open-items-this-document-does-not-resolve)

---

## 1. What I could not verify, and why

I attempted to confirm that the free data sources below actually respond,
because naming an endpoint is not the same as checking one. Every request was
refused by this session's egress policy before it left the machine. Raw
failure record, printed beside the zero per CLAUDE.md house rule 3:

```
curl: (56) CONNECT tunnel failed, response 403      www.federalregister.gov:443
curl: (56) CONNECT tunnel failed, response 403      clinicaltrials.gov:443
curl: (56) CONNECT tunnel failed, response 403      api.fda.gov:443
curl: (56) CONNECT tunnel failed, response 403      efts.sec.gov:443

http://127.0.0.1:45399/__agentproxy/status ->
  "kind": "connect_rejected",
  "detail": "gateway answered 403 to CONNECT (policy denial or upstream failure)"
```

Per `/root/.ccr/README.md`, a 403 from the gateway is an organisation policy
denial and must be reported rather than retried or routed around. So it is
reported here, and it has a consequence for the reader: **every data source
in §5 is marked `[UNVERIFIED]`, including the four BUILD-BRIEF.md already
lists as "verified working."** They may well work — data-engineer should
confirm each from the VPS, where egress is presumably open — but they are not
confirmed *by me*, and this document does not pretend otherwise.

This matters more than it looks. Three of the five candidates below depend on
a source's *latency* and *point-in-time fidelity* (when a filing becomes
visible, whether a record can be retrieved as it stood on a past date), and
those properties cannot be inferred from documentation. They are the first
thing data-engineer should measure, because a candidate can die on them
before any price data is touched.

---

## 2. Two structural constraints that reshape every candidate

These were not in the brief, and both change what is buildable. Both are
`[UNVERIFIED]` against Alpaca specifically and must be confirmed before any
candidate is graded, because they alter the backtest's rules, not just its
inputs.

### 2.1 A $1,000 account is a cash account, so short selling is likely unavailable

TRAPS.md states the $2,000 minimum equity for margin still applies after the
PDT retirement, so the account is unleveraged. The consequence that follows,
which the brief does not draw out: **short selling requires a margin
account.** A cash account cannot borrow stock. If that holds at Alpaca, then:

- Every candidate is **long-only**, or must express bearish views through
  inverse ETFs (crude, decay-prone, and a different instrument with a
  different cost profile).
- `ResearchView.direction` can still return `"short"`; the risk engine must
  treat it as an automatic skip with an explicit `skip_reason`, so the
  refusal tracker records how much opportunity the constraint is costing.
  That is a config decision inside `risk/`, not an interface change.
- Any candidate whose edge is predominantly on the short side is dead on
  arrival regardless of what the backtest says about it. **The backtest must
  therefore report long-only and long/short results separately**, so we can
  see what the constraint costs rather than silently averaging it away.

### 2.2 "Unlimited day trades" is throttled by T+1 settlement, not by PDT

The PDT rule is retired (TRAPS.md, effective 4 June 2026) and that genuinely
reopens intraday trading. But a *cash* account is separately governed by
settlement: proceeds from a sale settle T+1, and buying with unsettled
proceeds and then selling before settlement is a good-faith violation; three
in twelve months restricts the account to settled cash only for 90 days.

Practical effect on $1,000 `[EST, needs Alpaca confirmation]`: the account can
turn over roughly **once per day**, not repeatedly. Deploying $900 across
three day trades on Monday leaves that cash unsettled until Tuesday. So the
sustainable intraday rate is on the order of **20-30 day trades per month**,
not hundreds — enough to accumulate a sample quickly, but nowhere near
unconstrained. Candidate B is designed around this ceiling rather than
against it.

### 2.3 The corollary nobody has drawn: stops only work intraday

TRAPS.md says stop orders do not trigger outside regular hours, and
fractional stop orders are DAY-only and expire at the close. ARCHITECTURE.md
§4.4 already responds correctly, sizing off
`max(adverse_gap_assumption, nominal stop distance)`.

Follow that through and it produces a structural, not statistical, ranking:
**for a strategy that is flat at the close, the stop is enforceable, so the
stop distance *is* the worst case. For every strategy that holds overnight,
the stop is decorative on the one move that matters and sizing is governed by
an assumed gap.** Since sizing is `risk_budget / worst_case`, an intraday
strategy can carry three to ten times the notional of an overnight strategy
at identical risk `[EST]`.

That is the single most consequential structural fact in this document, and
it is exactly the property the previous build ran into from the other side.
It does not make intraday strategies profitable. It makes them *sizeable*,
which is a different claim and the one the previous build could not satisfy.

---

## 3. The arithmetic every candidate must clear

### 3.1 Cost hurdle

Fixed monthly API cost is amortised across trades; spread and slippage are
proportional to each trade. So:

```
break-even return per trade (as % of position notional)
    = annual_API_cost / (trades_per_year x notional)      <- falls with frequency
    + round_trip_transaction_cost_pct                     <- flat, paid every trade
```

At the brief's two workable budgets:

| Budget | Annual $ | Annual hurdle on $1,000 |
|---|---|---|
| $5/month (the brief's hard base cap) | $60 | 6.0% |
| $8/month | $96 | 9.6% |

Fixed-cost component alone, at $96/year `[EST — depends on trade counts that
are themselves estimates]`:

| Trades/month | Notional/trade | API cost as % of notional per trade |
|---|---|---|
| 5 | $80 | **2.00%** |
| 8 | $150 | 0.67% |
| 8 | $200 | 0.50% |
| 20 | $250 | 0.16% |
| 40 | $250 | 0.08% |

Add round-trip transaction cost `[EST, must be measured from real quotes:
0.05-0.15% liquid large cap, 0.4-1.5% micro cap]` to get the true floor.

Two things fall out immediately, and they are the most useful conclusions in
this document because they are arithmetic rather than opinion:

1. **A low-frequency, small-notional strategy is structurally disadvantaged
   on this account regardless of its edge.** Five trades a month at $80
   notional must return 2% per trade *before* spread just to pay the API
   bill. That is a hard argument against Candidate D as a primary strategy,
   made before any backtest runs.
2. **Frequency is not merely a learning requirement, it is a cost
   requirement.** The brief asks for "a few trades a month" so a sample
   accumulates; the arithmetic says the same thing for a different reason.

### 3.2 Sample size — the number that decides how long we are guessing

To distinguish a mean per-trade return `μ` from zero, with per-trade standard
deviation `σ`:

```
n ~ (1.96 x σ/μ)^2     for a 95% CI that excludes zero
n ~ (2.80 x σ/μ)^2     for ~80% power to detect it
```

| Plausible profile | σ/μ | n for CI excluding 0 | n for 80% power |
|---|---|---|---|
| μ=2%, σ=8% (strong event edge) | 4 | 61 | 125 |
| μ=1%, σ=8% (typical drift edge) | 8 | 246 | 502 |
| μ=0.4%, σ=3% (intraday) | 7.5 | 216 | 441 |
| μ=0.5%, σ=8% (weak edge) | 16 | 983 | 2,007 |

Time to reach n=246 live:

| Trades/month | Months to n=246 |
|---|---|
| 5 | 49 |
| 8 | 31 |
| 20 | 12 |
| 40 | 6 |

**This is the argument for building the backtest before anything else, in one
table.** Live paper trading cannot establish edge for any of these candidates
within a year; only history has enough trades. Live trading's job is to check
that reality still resembles the backtest, and to run the refusal tracker —
not to discover the edge. Any candidate whose forward sample takes three
years to become meaningful is a candidate we will never actually evaluate.

`backtest-engineer` should treat these as *minimum* sample targets per
candidate, and report `BacktestSampleStats.sample_size` against them
explicitly. ARCHITECTURE.md §6.1's `MIN_SAMPLE_SIZE` placeholders (30 for
`conviction_floor`, 20 for `adverse_gap_assumption`) are, on this arithmetic,
almost certainly too small — §9.9 of that document already flags them as
provisional, and this table is the beginning of the power analysis §12 defers.

---

## 4. Re-reading the previous build's result

Eight candidates, eight declines, zero trades in five days. The brief's own
reading: "edge and un-sizeable risk were the same property of the same
trades."

I think that diagnosis is right and the conclusion drawn from it was too
broad. Decomposed, the failure had four independent causes, and only one of
them is about catalysts:

1. **It traded the binary itself.** Position held *through* an unknown
   resolution, overnight, where stops do not fire. Worst case = the full gap.
   This is a choice about *when* to trade relative to the event, not about
   whether events carry information.
2. **The gap assumption was invented at 60%** and never measured, so sizing
   was governed by a number with no evidence behind it. If the true
   conditional adverse gap for a given catalyst class is 25%, that single
   unmeasured parameter cut sizeable positions by 58%.
3. **Five days is not a sample.** Eight candidates cannot distinguish "the
   filter is correctly strict" from "the filter is broken." Nothing about
   the outcome was measurable at that n.
4. **Fixed cost per trade was never confronted.** At the frequency that
   approach implied, §3.1's arithmetic says each trade needed ~2% just to
   clear the API bill.

Three of the four are fixable without abandoning event data. The candidates
below fix them in different combinations, and that is deliberate — it means
the bake-off can attribute the previous failure rather than merely avoid it:

- **A and D** trade *after* resolution, so cause 1 disappears by construction:
  the unknown outcome is known before capital is committed.
- **B** is flat at the close, so causes 1 and 2 both disappear — there is no
  overnight gap to assume.
- **C** uses the catalyst feeds *inverted*: not to select trades but to
  **exclude** any name with a pending binary, converting the previous build's
  primary signal into a safety filter.
- **All five** are designed for frequency high enough to make cause 3 and
  cause 4 tractable, with the honest exception of D.

Catalysts are not the problem. Holding through an unknown outcome, overnight,
on an account too small to absorb the gap, is the problem.

---

## 5. The free data sources these candidates link

The owner's requirement is strategies built from **cross-referenced** feeds,
not single-source filters. Every candidate below is specified as a join
across at least three independent sources, and for each I have tried to name
what the join *adds* that no single source provides — because "we use five
APIs" is not an edge, and a linkage that only adds confirmation adds nothing.

All rows `[UNVERIFIED]` from this sandbox (§1). "Keyless" claims are from
documentation, not from a successful request.

| Source | What it gives | Keyless? | Role in the linkage |
|---|---|---|---|
| SEC EDGAR daily/full index | Every filing, by form type, near real time | Yes, UA required | Event clock for A, C. Filing **acceptance timestamp** is the point-in-time truth, not filing date |
| SEC EDGAR full-text search (`efts.sec.gov`) | Phrase search across filing bodies | Yes, UA required | Finds PDUFA dates, lockup language, offering terms that no structured feed carries |
| `data.sec.gov` submissions / companyfacts / frames | Per-company filing history; XBRL fundamentals | Yes, UA required | Supplies the reported earnings number for A's surprise measure without any analyst data |
| SEC Insider Transactions Data Sets (quarterly Form 3/4/5 flat files) | Historical Form 4 in tabular form | Yes | C's **backtest** universe; the live path needs daily-index + ownership XML |
| SEC Financial Statement Data Sets (quarterly XBRL extract) | Historical fundamentals, as-filed | Yes | A's backtest universe, avoids restatement look-ahead |
| SEC Failure-to-Deliver data | Settlement failures per symbol, twice monthly | Yes | Crowding/short-pressure context for B, C |
| FINRA Reg SHO daily short sale volume (CDN files) | Daily short volume per symbol, posted ~6pm ET | Yes (files); the query API may need credentials | Distinguishes "buyers arriving" from "shorts covering" in A, B, C |
| FINRA equity short interest | Bi-monthly short interest | Yes | Squeeze/crowding filter |
| Nasdaq Trader trading halts RSS + halt history | LULD pauses and news halts, live and historical | Yes | B's highest-information event type; also a required backtest realism input |
| Federal Register API | Scheduled agency actions, advisory committee meetings | Yes | D's forward calendar; C's exclusion filter |
| ClinicalTrials.gov API v2 | Trial status, completion dates, **record versions** | Yes | D's event clock. Versioned records are what make a point-in-time replay possible at all |
| openFDA | Approvals, CRLs — retrospective only | Yes | Retrospective is a *defect* for anticipation and an *asset* for D: it is a clean post-event source |
| Alpaca market data | Bars, quotes, trades, news | Subscription | Price, volume, spread, the reaction measure in every candidate |
| Treasury FiscalData API | Rates, auctions | Yes | T-bill benchmark for the dashboard's comparison |

**What the joins actually add, candidate by candidate** — this is the part
that has to justify itself, not the list above:

- **A**: XBRL fundamentals alone give a surprise with no market context; the
  price reaction alone gives a move with no cause. Joined, they separate "the
  number beat and the stock rose" (drift continues) from "the stock rose on a
  guidance comment while the number missed" (different animal entirely). No
  single source distinguishes those.
- **B**: A gap alone is ambiguous. A gap joined to *whether an EDGAR filing
  was accepted overnight*, *whether Alpaca carried news*, and *whether the
  name was halted* classifies the gap by **information provenance** — which
  is the actual hypothesis, and is unobtainable from price data alone.
- **C**: A Form 4 buy alone is noisy (routine grants, 10b5-1 plans). Joined
  to role, to purchase size versus the insider's existing holding, to
  clustering across multiple insiders, and then **negatively** joined against
  Federal Register / ClinicalTrials / EDGAR full-text to reject any name with
  a pending binary, it becomes a filter no single feed can express.
- **D**: ClinicalTrials status transitions, Federal Register meeting
  outcomes, openFDA approvals and 8-K text are each partial views of the same
  underlying event; the join is what establishes *that the event resolved and
  in which direction*, which no one of them states.
- **E**: Deliberately uses **only** price data, as the control.

---

## Candidate A — Post-earnings drift, sourced from XBRL rather than analyst consensus

### 1. Thesis and why the edge should persist

Prices under-react to earnings surprises and continue drifting in the
direction of the surprise for weeks afterwards, and the drift is largest in
names with thin analyst coverage where the information diffuses slowly.

Who is on the other side: nobody sophisticated, specifically. The
counterparty is index and passive flow that transacts without reference to
the surprise at all, plus retail rebalancing. The reason a documented
anomaly of this age might persist is that its per-trade magnitude is small
relative to the cost of trading the names where it is largest — which is
exactly why it may *not* survive this account's cost structure. That tension
is the point of grading it rather than assuming it.

### 2. Data sources linked

1. **EDGAR daily index** → 8-K filings, filtered to Item 2.02 (results of
   operations). Acceptance timestamp, not filing date.
2. **`data.sec.gov` companyfacts (XBRL)** → the reported figure, and the
   same figure four quarters prior. Surprise is computed as a seasonal
   random walk against the year-ago quarter, standardised by its own
   historical volatility — **no analyst consensus required**, which matters
   because consensus is not free and not keyless.
3. **Alpaca bars** → the announcement-window return and volume, which acts as
   a second, independent surprise measure that captures everything XBRL
   misses (guidance, tone).
4. **FINRA Reg SHO daily short volume** → whether the announcement-day move
   was buying or short covering.
5. **SEC Financial Statement Data Sets** → the as-filed historical panel for
   backtesting, avoiding restatement look-ahead.

The trade is taken only where the XBRL surprise and the price reaction
**agree in sign**. Disagreement is the interesting reject case and should be
tracked in the refusal tracker.

### 3. Expected frequency

`[EST]` 8-15 trades/month averaged, but **badly clustered**: US earnings
arrive in four three-week bursts. Two months a quarter could produce almost
nothing. This satisfies "a few trades a month" only on average, which is not
the same as satisfying it. Mitigation is to include off-cycle filers
(non-December fiscal year ends), which is a real universe of a few hundred
names. `[EST — must be counted from EDGAR history, not assumed]`

### 4. Position size and worst case

All `[EST]`, all pending measurement. Illustrative risk budget of 2% of
account ($20) per position — the actual figure is a `HARD_BOUNDS` decision
for a human (ARCHITECTURE §12), not mine.

| Quantity | Estimate | Basis |
|---|---|---|
| Assumed adverse overnight gap | 8% | `[EST]` — non-binary name, no scheduled event in window. **The single most important number to measure.** |
| Implied notional | $250 (25% of account) | $20 / 0.08 |
| Reduced for 4 concurrent positions | $200 (20%) | Exposure limit binds before risk limit |
| Worst case, 20% tail gap | $40 | **4.0% of account** |
| Hold | 10-15 trading days, hard exit | Within the brief's window |
| Break-even/trade at $96/yr, 8 trades/mo | 0.50% + ~0.3% spread = **~0.8%** | §3.1 |

The uncomfortable observation: published post-earnings drift magnitudes are
in the same order of magnitude as that 0.8% break-even. This candidate could
easily be *real and unprofitable*. The backtest must be able to tell those
apart, which means it must report net-of-cost returns, not gross.

### 5. What would falsify it, and how soon

- **Backtest, days:** mean net return per trade below break-even; or top-
  quintile surprise indistinguishable from bottom-quintile after costs; or
  the edge concentrated entirely in names below a liquidity floor we cannot
  trade at acceptable spread.
- **Backtest, days:** drift present gross but reversed by realistic fills.
- **Live, ~4 months:** hit rate more than ~15 points below backtest over 30+
  trades — evidence the free XBRL surprise measure is a worse proxy live than
  in a clean historical panel.
- **Live, immediately:** if fewer than 3 tradeable candidates appear in a
  non-earnings-season month, the frequency assumption is wrong and the
  candidate cannot be evaluated in reasonable time.

### 6. Cannot be graded yet — what the harness must measure

No backtest exists. To validate or kill this, `backtest/structural.py` needs:

- **Sample:** ≥300 trades (§3.2, assuming σ/μ ≈ 8), split in/out of sample.
- **Date range:** ≥5 years, and it must include 2022 (a sustained drawdown
  regime) and 2020Q1. A post-2023-only sample would grade this in a single
  regime and tell us nothing about the drawdown that matters.
- **Universe:** point-in-time, **including delisted names.** A survivorship-
  filtered universe will make this candidate look good and it will be wrong.
- **Point-in-time discipline:** 8-K acceptance timestamps; as-filed XBRL, not
  restated; entry no earlier than the next regular-hours open after
  acceptance.
- **Pessimistic cost assumptions I am asking backtest-engineer to apply:**
  fill at the far side of the NBBO, plus 0.10% slippage; a floor of 0.30%
  round trip regardless of quoted spread; no fill in the first minute after
  the open; no fill at all on a day the name was halted; $96/yr API cost
  allocated per trade at the *realised* trade count, not the hoped-for one.
- **Reported separately:** results with and without the announcement-day
  reaction filter, so we can see whether the *linkage* earns its complexity
  or the XBRL surprise alone does the same work. If the join adds nothing,
  the honest move is to drop the join, not to keep it because it is
  interesting.

### 7. Rejected alternative framing

**Rejected: trading the earnings announcement itself.** It is the higher-
variance version of the same information and it was already tried — the
previous build researched confirmed earnings dates and correctly found them
priced in. More decisively, it reinstates the un-sizeable overnight binary
that the previous build could not size. Post-event drift keeps the
information channel and discards the gap, which is the whole point.

**Also rejected: buying a consensus-estimate feed to compute a conventional
SUE.** It would be a better surprise measure. It is not free, the brief says
paid data is a last resort, and a seasonal-random-walk surprise computed from
XBRL is a documented and defensible substitute. If the backtest shows the
strategy works *and* that surprise measurement quality is the binding
constraint, that is the moment to revisit — with evidence, not before.

---

## Candidate B — Intraday gap classification by information provenance

### 1. Thesis and why the edge should persist

Overnight gaps split into two populations that look identical on a price
chart: gaps caused by *information* (a filing, a halt, real news), which tend
to continue as the information is absorbed through the session, and gaps
caused by *flow* (thin overnight liquidity, index effects, no identifiable
cause), which tend to revert; classifying the gap by whether a primary-source
document actually exists is a signal available from free structured data and
not from price alone.

Who is on the other side: on the reversion leg, overnight liquidity providers
and momentum-chasing retail at the open. Why it might persist — a genuinely
open question — is that the classification requires joining EDGAR acceptance
timestamps and halt records to price in the first minutes of the session,
which is fiddly and low-capacity. Why it might **not** persist, stated plainly
because it is the strongest objection to my own recommendation: this is the
most surveilled part of the market, and the counterparty on the continuation
leg may be faster and better informed than we will ever be.

### 2. Data sources linked

1. **Alpaca** → previous close, pre-market and opening prints, quoted spread,
   relative volume. Gap universe.
2. **EDGAR daily index** → was any filing *accepted* between yesterday's
   close and this morning's open, and of what form/item? This is the
   provenance test and it is the core of the candidate.
3. **Nasdaq Trader trading halts RSS / halt history** → was the name halted,
   and under what code (LULD volatility pause versus T1 news pause)? A T1
   halt is an explicit exchange assertion that material news exists.
4. **Alpaca news** → secondary corroboration where no filing exists.
5. **FINRA Reg SHO daily short volume** (prior day, T-1) → crowding context.
6. **Federal Register / ClinicalTrials.gov** → exclusion: if a scheduled
   binary resolves today, this is not a gap trade, it is a binary bet.

Entry after the opening auction settles; **hard exit before the close, every
time, no exceptions.** Flat overnight by construction.

### 3. Expected frequency

`[EST]` 20-30 trades/month, ceilinged by T+1 settlement (§2.2), not by
signal availability. This is the only candidate that reaches §3.2's sample
threshold inside a year — roughly 12 months to n≈250 versus 31 months for A
and C. That is not a small advantage; it is the difference between a
strategy we can evaluate and one we can only hope about.

### 4. Position size and worst case

All `[EST]`.

| Quantity | Estimate | Basis |
|---|---|---|
| Intraday stop distance | 2.5% | **Enforceable** — regular hours only (§2.3) |
| Notional if risk-sized alone | $800 | $20 / 0.025 — absurd on a $1,000 account |
| Notional after exposure cap | $250-300 (25-30%) | Exposure limit binds, not the stop |
| Risk per trade at the stop | $6.25 | **0.63% of account** |
| Worst case: stop jumped by a LULD halt, 8% adverse | $20-24 | **2.0-2.4% of account** |
| Overnight gap exposure | **zero** | Flat at the close |
| Break-even/trade at $96/yr, 25 trades/mo, $275 | 0.12% + ~0.20% = **~0.32%** | §3.1 |

Note what happened there: the exposure limit binds before the risk limit,
which is the inverse of every overnight candidate. That is §2.3's structural
argument showing up as arithmetic — and the lowest break-even hurdle of any
candidate here.

The honest counterweight: 0.32% per trade is a low bar, but intraday
per-trade edges are also small, and the estimate assumes we can trade names
liquid enough for a 0.20% round trip. If the signal only works in $2 stocks
with 1% spreads, break-even rises to ~1.1% and the candidate is probably
dead. **The backtest must report edge conditional on spread bucket.**

### 5. What would falsify it, and how soon

- **Backtest, days:** filing-backed gaps and unexplained gaps show
  statistically indistinguishable intraday continuation. That is the core
  hypothesis; if the classification carries no information, the candidate is
  finished and the remaining details do not matter.
- **Backtest, days:** edge exists but only below a spread threshold we cannot
  trade.
- **Backtest, days:** edge is entirely on the short side (§2.1) — plausible,
  since gap-down reversion is often the stronger leg, and it would make the
  candidate untradeable on a cash account.
- **Live, ~6 weeks:** realised slippage against the modelled fill exceeding
  ~2x. At this hurdle, a 2x slippage miss consumes the entire edge, and
  reconciling broker fills against modelled fills (ARCHITECTURE §3.1, `Fill.
  broker_reported_price` kept distinct from modelled) shows it within weeks.
- **Live, ~2 weeks:** any good-faith settlement violation. That is a design
  error surfacing, and it invalidates the frequency assumption immediately.

### 6. Cannot be graded yet — what the harness must measure

No backtest exists. This candidate is the most demanding on the harness and
that cost should be counted against it:

- **Data:** minute bars (ideally quotes) for a multi-year gap universe. If
  Alpaca's historical minute data does not extend far enough, or the free
  tier is IEX-only rather than consolidated SIP, **the gap and volume
  measures are distorted and the backtest is invalid**. `[UNVERIFIED — this
  is the first thing to check, before any modelling work]`
- **Sample:** ≥400 trades (σ/μ ≈ 7.5, §3.2). Achievable from history.
- **Date range:** ≥3 years including a high-volatility regime; gap strategies
  behave very differently across VIX regimes and a single-regime grade would
  be misleading.
- **Point-in-time:** EDGAR acceptance timestamps to the minute — the entire
  hypothesis rests on "was this document public before the open," and an
  hour of error inverts the classification.
- **Pessimistic cost assumptions requested:** fill at the far side of the
  NBBO plus 0.15% slippage on entry (opening prints are the worst moment for
  slippage); floor of 0.25% round trip; no fill within the first 60 seconds;
  a halted name is untradeable for the entire halt plus 5 minutes; assume the
  stop is filled 0.5% *beyond* its trigger, never at it.
- **Also requested:** results reported separately by gap size decile, by
  spread bucket, and long-only versus long/short (§2.1).

### 7. Rejected alternative framing

**Rejected: holding the gap trade overnight for multi-day continuation.** It
would raise the per-trade expected move and reduce the relative cost drag.
Rejected because it reintroduces the exact un-hedgeable overnight gap that
made the previous build unable to size anything, and it forfeits the one
structural advantage this candidate has (§2.3). The whole reason B exists is
that it is flat at the close.

**Also rejected: a pure price-based opening-range breakout.** Simpler, no
data linkage, and it is the most heavily mined pattern in retail trading —
which is a reason to expect the counterparty to be better prepared than we
are. The provenance join is what makes this candidate something other than a
pattern anyone can find in a charting package. That said, Candidate E exists
partly so we can check whether the join is really doing the work.

---

## Candidate C — Insider-cluster buying, with the catalyst feeds used as an exclusion filter

### 1. Thesis and why the edge should persist

When several insiders at the same company make open-market purchases within
a short window — particularly officers rather than directors, in size that is
material relative to their existing holdings — the stock earns positive
abnormal returns over the following weeks, and the effect is strongest in
under-covered small caps.

Who is on the other side: insiders buy from whoever is selling, which is
usually somebody with no company-specific information at all. The edge should
persist because the information is genuinely private until the Form 4 lands,
the two-business-day filing deadline means the disclosure is fresh, and the
names where it is strongest are too small to interest institutions. It is
public information that is under-consumed rather than mispriced — the most
durable kind.

### 2. Data sources linked

This candidate has the richest join, and each element does distinct work:

1. **EDGAR daily index** → Form 4 filings, near real time.
2. **Ownership XML in each Form 4** → transaction code (`P` = open-market
   purchase, the only code that matters; `A` grants and `S` sales are noise
   here), the insider's role, the dollar value, and the resulting holding.
   The signal is `P`-code purchases by ≥2 distinct insiders within ~10 days,
   weighted by size relative to prior holding.
3. **`data.sec.gov` submissions + companyfacts** → company size, share count,
   filer status; used to bucket by coverage and to compute purchase size
   against float.
4. **Alpaca bars/quotes** → has the market already moved on the filing (in
   which case the information is consumed), and is the spread tradeable.
5. **FINRA Reg SHO daily short volume + short interest** → is the name
   heavily shorted, which changes the distribution's shape materially.
6. **Federal Register + ClinicalTrials.gov v2 + EDGAR full-text search** →
   **as an exclusion filter.** Any name with a scheduled binary resolving
   inside the intended holding window is rejected, because that trade's worst
   case is the binary's gap, not the equity's ordinary gap.
7. **SEC Insider Transactions Data Sets** → the historical panel for
   backtesting.

Point 6 is the design idea worth defending. The previous build used the
catalyst feeds to *find* trades and discovered that the trades they found
could not be sized. Here the same feeds do the opposite job: they identify
positions whose worst case is un-sizeable and remove them. The feeds are
already built and already free; their information content did not change,
only the sign with which it is used.

### 3. Expected frequency

`[EST]` 8-15 trades/month, evenly distributed rather than seasonal — Form 4
purchases arrive continuously, with a mild uptick after earnings blackout
windows lift. Better distributed than A, lower peak volume than B. This is
the candidate most naturally suited to the brief's "3-5 concurrent positions,
days to three weeks."

### 4. Position size and worst case

All `[EST]`.

| Quantity | Estimate | Basis |
|---|---|---|
| Assumed adverse overnight gap | 15% | `[EST]` — small caps gap harder than A's universe. **Must be measured per size bucket.** |
| Implied notional | $133 (13.3% of account) | $20 / 0.15 |
| 4 concurrent | $533 (53% exposure) | Comfortable |
| Worst case, 35% tail gap | $47 | **4.7% of account** |
| Hold | 10-15 trading days, hard exit | Within brief's window |
| Break-even/trade at $96/yr, 12 trades/mo, $133 | 0.50% + ~0.6% spread = **~1.1%** | §3.1 |

The spread term is doing real damage here and it is the candidate's main
vulnerability: the names where the signal is strongest are the names where
the spread is widest. A liquidity floor helps the cost and may remove the
edge. **The backtest must report edge by market-cap and spread bucket, so we
can find whether a tradeable intersection exists at all.** If it does not,
this candidate dies — and that is a clean, early, cheap answer.

### 5. What would falsify it, and how soon

- **Backtest, days:** cluster purchases show no abnormal return over 10-15
  days after realistic costs.
- **Backtest, days:** the edge exists only in names below a spread or price
  floor we cannot trade (my prior: this is the most likely way C dies).
- **Backtest, days:** single-insider and cluster purchases perform
  identically — the "cluster" refinement is then noise, and the candidate
  should be re-graded as a simpler signal rather than defended.
- **Backtest, days:** the exclusion filter (point 6) removes no meaningful
  tail risk. That would be a genuinely useful negative result about the
  catalyst feeds' value in *any* role.
- **Live, ~4 months:** 30+ trades with a hit rate 15+ points below backtest.
- **Live, ~2 months:** filing-to-fill latency worse than modelled. If the
  edge decays within hours of the Form 4 landing, a daily cycle cannot
  capture it, and that shows up quickly by comparing fills against the
  filing-time price.

### 6. Cannot be graded yet — what the harness must measure

- **Sample:** ≥300 trades (§3.2). Achievable — Form 4 `P`-code clusters are
  plentiful across a multi-year panel.
- **Date range:** ≥5 years including 2022. Insider buying clusters spike in
  drawdowns, so a bull-only sample would badly overstate the signal by
  loading it with dip-buying that happened to work.
- **Universe:** point-in-time including delisted names. **Critical here** —
  insiders buy heavily into companies that later fail, and a survivorship-
  filtered universe would delete precisely the losing tail this strategy is
  exposed to. If backtest-engineer can only build one point-in-time universe,
  build it for this candidate.
- **Point-in-time:** Form 4 acceptance timestamps; entry no earlier than the
  next open after acceptance.
- **Pessimistic cost assumptions requested:** far side of NBBO plus 0.15%;
  floor of 0.60% round trip for sub-$1bn names, 1.2% for micro caps; no fills
  in names below a stated ADV floor; assume the position cannot be exited on
  the planned date if the name is halted, and mark it to the next available
  open.
- **Also requested:** measure the realised adverse gap distribution for this
  universe directly, and hand the number to `risk/adaptive_params.py` as the
  *initial* `adverse_gap_assumption` for `catalyst_type="insider_cluster"`.
  That single output would replace the previous build's invented 60% with a
  measured figure, which is the specific defect BUILD-BRIEF.md names.

### 7. Rejected alternative framing

**Rejected: trading insider *selling* as a bearish signal.** Better known,
and far weaker — insiders sell for diversification, tax and 10b5-1 reasons
that have nothing to do with prospects, so the signal-to-noise is much worse.
It is also unimplementable on a cash account (§2.1). Rejected on both counts.

**Also rejected: 13F institutional-holdings changes as the ownership
signal.** Same conceptual family — informed ownership — and it is free and
structured. Rejected because 13F is filed 45 days after quarter end, so the
information is up to 135 days stale on arrival, which is incompatible with a
days-to-weeks holding period. Form 4's two-day deadline is the entire reason
this family is tradeable at all.

---

## Candidate D — Post-resolution drift on regulatory and clinical events

### 1. Thesis and why the edge should persist

After a binary regulatory or clinical event resolves, the initial repricing
is incomplete, and the stock continues drifting in the direction of the
resolution for days to weeks as the market works out what it means for
revenue.

Who is on the other side: event-driven specialists exit into the resolution
print, and generalists re-rate slowly. The edge, if present, is a
re-valuation lag rather than an information asymmetry.

### 2. Data sources linked

1. **ClinicalTrials.gov API v2, including record versions** → status
   transitions (Active → Completed / Terminated), which are the earliest
   structured evidence that something resolved.
2. **Federal Register API** → advisory committee meetings that have now
   occurred; the meeting date is the anchor.
3. **openFDA** → approvals and CRLs appearing retrospectively. TRAPS.md
   correctly calls openFDA useless for anticipation; **retrospective is
   exactly what a post-event strategy wants**, and this candidate exists
   partly to test whether that reframing has value.
4. **EDGAR daily index + full-text search** → the 8-K (Item 8.01/7.01) that
   discloses the outcome, with an acceptance timestamp.
5. **Alpaca bars** → the resolution-day move: direction, magnitude, volume.

The join defines the event: no single source says "this resolved, in this
direction, at this time" — ClinicalTrials says the status changed, the 8-K
says what management chose to disclose, openFDA eventually confirms, and the
price says how the market took it. Agreement across sources is the signal;
disagreement is the reject.

### 3. Expected frequency

`[EST]` 3-8 trades/month, and this is the candidate's fatal-looking problem.
It sits at or below the brief's "few trades a month" floor, it needs ~49
months to reach n=246 live (§3.2), and §3.1's arithmetic says a 5-trade
month at $80 notional needs **2% per trade just to pay the API bill**.

Per the brief's instruction to "say so early rather than waiting six months
to find out": **on frequency and cost arithmetic alone, D is unlikely to
survive as a primary strategy.** I propose it anyway, for two specific
reasons: it is the cleanest test of whether the catalyst family has any
post-event edge at all, and that answer is reusable as an *overlay* on C
(where a resolved binary might justify a larger position) even if D never
trades standalone.

### 4. Position size and worst case

All `[EST]`.

| Quantity | Estimate | Basis |
|---|---|---|
| Assumed adverse overnight gap | 25% | `[EST]` — post-resolution biotech remains gap-prone; follow-on binaries are common |
| Implied notional | $80 (8% of account) | $20 / 0.25 |
| 3 concurrent | $240 (24% exposure) | Correlation limits bite hard: biotech names resolving the same fortnight are one bet (BUILD-BRIEF) |
| Worst case, 50% tail gap | $40 | **4.0% of account** |
| Break-even/trade at $96/yr, 5 trades/mo, $80 | 2.00% + ~1.0% spread = **~3.0%** | §3.1 — the worst hurdle here by a wide margin |

A required 3% net per trade is not impossible for post-binary biotech drift,
but it is a demanding bar and it is demanded by our *cost structure*, not by
the market. That is worth stating plainly: D is disadvantaged by the size of
the account more than by anything about the strategy.

### 5. What would falsify it, and how soon

- **Backtest, days:** no drift after resolution once the announcement-day
  move is excluded, or drift below the ~3% net hurdle.
- **Backtest, days:** drift exists only for a direction we cannot trade
  (negative resolutions, requiring shorts — §2.1). My prior is that this is
  likely, since post-CRL drift is plausibly the stronger leg.
- **Backtest, days:** fewer than ~120 identifiable events across five years,
  i.e. the frequency estimate is optimistic — which would settle it.
- **Live, ~3 months:** fewer than 3 candidates a month reaching research.

### 6. Cannot be graded yet — what the harness must measure

- **Sample:** ≥250 events; my honest expectation is that history may not
  contain enough tradeable ones, and discovering that is itself a decisive
  result obtainable in days.
- **Date range:** ≥7 years, because event density is low.
- **Point-in-time — the hardest requirement in this document:**
  ClinicalTrials.gov records are **revised retroactively**, and openFDA is
  wholly retrospective. Replaying either naively imports look-ahead bias that
  would make this candidate look excellent and be worthless. The harness must
  use ClinicalTrials.gov's versioned record history, and must treat openFDA
  as available only at its own publication lag. `[UNVERIFIED — that the v2
  API exposes retrievable historical versions is from documentation, not from
  a request I was able to make]`
- **Pessimistic cost assumptions requested:** far side of NBBO plus 0.25%;
  floor of 1.0% round trip; no entry before the second open after the
  resolution 8-K; assume a 20% chance of a follow-on adverse binary inside
  any 15-day holding window unless the calendar proves otherwise.

### 7. Rejected alternative framing

**Rejected: anticipating the resolution.** Precisely what the previous build
did. Its own conclusion — edge and un-sizeable risk are the same property —
stands unrefuted, and nothing in this document refutes it. Post-event
trading keeps the event universe and discards the un-sizeable part.

**Also rejected: trading a basket of small equal-weight pre-event positions
to diversify the binary risk.** Superficially it solves sizing. Rejected
because BUILD-BRIEF is explicit that correlated positions are one bet wearing
several hats: eight biotech binaries in a fortnight is a single leveraged bet
on biotech sentiment, and at $1,000 each position would be too small for the
API cost per trade to make any sense (§3.1).

---

## Candidate E — Control arm: cross-sectional relative strength on liquid ETFs

### 1. Thesis and why the edge should persist

A small set of liquid sector and asset-class ETFs, ranked by trailing
relative strength and rotated on a fixed schedule, captures short-horizon
cross-sectional momentum with almost no API cost and almost no spread.

Honestly stated: I do not expect this to be the most profitable candidate.
Its purpose is different, and that purpose is the reason it belongs here.

**E is the null hypothesis.** Every other candidate spends money and
complexity to link primary-source feeds. E links nothing. If the backtest
cannot show A, B, C or D beating E *net of their higher API and spread
costs*, then the data linkage is not earning its keep, and the correct
decision is to run E and stop paying for cleverness. Without a control arm,
"our strategy returned 11%" is unfalsifiable — it needs a comparison that is
not the S&P, because the S&P is not exposure-matched and not rebalanced on
the same clock. ARCHITECTURE §3.1's `BacktestResult` already carries
`market_regime_notes` for related reasons; E is the strategy-level version of
the same instinct.

Who is on the other side: essentially, nobody in particular — this is a risk
premium harvested from slow rebalancers, and its expected size is small.

### 2. Data sources linked

**Deliberately one: Alpaca bars.** That is the design. Any additional source
would compromise its role as a control. Treasury FiscalData supplies the
T-bill benchmark for the dashboard comparison, not the signal.

### 3. Expected frequency

`[EST]` 2-6 trades/month with a weekly rebalance across 4 held positions. Low
by design; the API cost is near zero so §3.1's fixed-cost hurdle barely
applies.

### 4. Position size and worst case

All `[EST]`.

| Quantity | Estimate | Basis |
|---|---|---|
| Assumed adverse overnight gap | 3% | Broad ETF |
| Notional if risk-sized alone | $667 | Absurd — exposure caps first |
| Notional after exposure cap | $250 (25%), 4 positions | 100% invested |
| Worst case, 8% tail gap on one | $20 | **2.0% of account** |
| Worst case, all four gap 8% together | $80 | **8.0% of account** — sector ETFs are highly correlated in a selloff; this is the honest number and it is the worst in this document |
| Break-even/trade at ~$6/yr API, 4 trades/mo, $250 | 0.05% + ~0.05% = **~0.10%** | 6/(48x250); effectively free |

Note the inversion: E has by far the lowest cost hurdle and by far the
*highest* correlated worst case. The other candidates hold idiosyncratic
single-name risk that diversifies; E holds four expressions of one market
beta. `discovery/correlation.py` would need to recognise that, and its
current cluster key (sector + catalyst_type + resolution-week) does not —
see §7.

### 5. What would falsify it, and how soon

- **Backtest, days:** no positive net return, or returns not distinguishable
  from simply holding SPY at matched exposure (the sterner test).
- **Backtest, days:** all of the return coming from one regime.
- **Live, ~6 months:** underperformance versus buy-and-hold SPY net of costs.
- **As a control it cannot really be falsified**, only outperformed — and if
  it outperforms the data-linked candidates, that is E succeeding at its job,
  not E winning.

### 6. Cannot be graded yet — what the harness must measure

- **Sample:** ≥200 rebalance decisions; cheapest candidate to sample.
- **Date range:** ≥10 years — ETF history supports it and momentum's regime
  dependence demands it.
- **Point-in-time:** ETF inception dates and any constituent changes.
- **Pessimistic costs requested:** far side of NBBO plus 0.05%; floor of
  0.10% round trip; no fill in the first minute.
- **Most important output:** E's net return is the **benchmark line** every
  other candidate must clear in the bake-off. It should appear in the
  dashboard comparison beside the S&P and T-bills (BUILD-BRIEF's dashboard
  requirement), because it is the only one of the three that is exposure- and
  frequency-matched to what we would actually be running.

### 7. Rejected alternative framing

**Rejected: using buy-and-hold SPY as the control instead.** Simpler and
requires no strategy at all. Rejected because it is not exposure-matched, not
rebalanced on the same clock, and pays none of the same transaction costs, so
it cannot isolate the contribution of *data linkage* from the contribution of
*being in the market*. A rotational control shares the trading mechanics and
differs only in the information used, which is the comparison that matters.
SPY should still appear on the dashboard — as a market benchmark, a different
question.

---

## 7. Structural fit against ARCHITECTURE.md

I have checked each candidate against the interfaces ARCHITECTURE.md froze
(§3.1, §3.2). Four of five need **no interface change**. The exceptions are
flagged as proposed amendments rather than assumed, per instruction.

### 7.1 Fits with no change

**`Candidate.catalyst_type`** — a `str` with an open enum comment. Values I
would populate from `discovery/candidates.py` (which §8 of ARCHITECTURE
assigns to me):

| Candidate | `catalyst_type` |
|---|---|
| A | `earnings_drift` |
| B | `gap_information` |
| C | `insider_cluster` |
| D | `post_regulatory_drift`, `post_clinical_drift` |
| E | `cross_sectional_momentum` |

These matter beyond labelling: `adaptive_params` keys `adverse_gap_
assumption`, `stop_width` and `holding_period_estimate` *per catalyst type*
(ARCHITECTURE §6.1), so the type is the unit at which the system learns.
Types must therefore be narrow enough to be homogeneous — which is why D
splits into two rather than sharing one.

**`Candidate.catalyst_date`** — documented as "best estimate of resolution
date." For post-event candidates (A, C, D) the event is in the *past* at
discovery. A past date is representable, and `planned_exit_date` on
`RiskDecision` carries the forward horizon, so no change is needed. I am
recording the semantic reading explicitly so a future reader does not think
it is a bug: **for post-event candidates, `catalyst_date` is the date the
generating event occurred.** This is also the correct input to
`correlation.cluster()`'s resolution-week key — names reacting to the same
week's information *are* correlated.

**`Candidate.correlation_tags`** — populated as e.g.
`["sector:biotech", "type:insider_cluster", "cap:micro", "week:2026-W34",
"source:edgar_form4"]`.

**`ResearchView`** — every candidate's model question fits the existing
fields (direction, conviction, thesis, invalidation, expected_holding_days,
priced_in). No new field is needed, and I am not proposing one; §4.1 of
ARCHITECTURE is right that the absence of size-shaped fields is the
enforcement mechanism.

**Pipeline order** — all five are `discovery -> research -> risk ->
execution` with no new stage.

### 7.2 Proposed amendment 1 — intraday exit time (needed only if B wins)

`RiskDecision.planned_exit_date` is a `date`. B requires "exit at 15:50 ET
today," and a `date` cannot distinguish that from "exit any time tomorrow" —
which is the difference between a strategy that is flat overnight and one
that is not. `execution/exits.py:manage_exits(portfolio, as_of: datetime)`
already takes a `datetime`, so the exit machinery can act intraday; only the
*instruction* cannot be expressed.

Proposed, **only if B is graded and wins**: change `planned_exit_date: date`
to `planned_exit_at: datetime`, or add `planned_exit_time: time | None`
alongside. This touches `RiskDecision`, `risk_decisions` in `schema.sql`, and
`execution/exits.py` — all human-review-required or shared-session files, so
it routes through the single coordinating session per ARCHITECTURE §8. **I am
not assuming it. Flagging it now so the cost of B includes it.**

### 7.3 Proposed amendment 2 — batch research, or a tighter pre-screen (B)

`research.investigate(candidate, cost_context)` is per-candidate. B produces
10-20 gap candidates each morning; at `[EST]` $0.03 per investigation that is
$6-12/month for one candidate alone, breaching the $5 base cap.

Two resolutions:

- **(a) No amendment, preferred:** deterministic screening narrows to ≤2
  candidates per day *before* any model call. This fits the existing
  interface exactly and matches "build rich, run cheap." It also fits the
  non-negotiable rule better — more of the decision is deterministic.
- **(b) Amendment:** add `investigate_batch(candidates, cost_context) ->
  list[ResearchCallLog]` sharing one turn set. Cheaper per candidate, but it
  touches `research/boundary.py` (human-review-required) and weakens the
  one-view-per-candidate audit trail.

**I recommend (a) and propose no amendment.** Recording (b) so the option is
visible if measured costs make it necessary.

### 7.4 Proposed amendment 3 — correlation clustering for E

`correlation.cluster()` keys on sector + catalyst_type + resolution-week. For
E, four sector ETFs have four different sectors and no resolution week, so
the cluster key would report them as uncorrelated when they are four
expressions of one market beta (§E.4: an 8% correlated worst case, the
largest in this document). If E is ever run as more than a backtest
benchmark, the cluster key needs a factor/beta axis. **Only relevant if E is
promoted from control arm to live strategy; flagged, not assumed.**

### 7.5 One thing I am *not* proposing

I am not proposing that `ResearchView` gain a confidence-weighted sizing hint
or that conviction map to size in any way. ARCHITECTURE §9.4/§9.8 settled
that, the reasoning is right, and every candidate above is compatible with
conviction as a pure gate.

---

## 8. What the bake-off must hold constant

For the grades to be comparable — the brief's "grade them all on the same
backtest" — these must be identical across candidates, and stated in
`BacktestResult.costs_applied`:

1. **Same account model:** $1,000, no leverage, no shorting (§2.1), T+1
   settlement enforced (§2.2), fractional shares allowed.
2. **Same risk budget:** the same illustrative max-loss-per-position, so
   differences come from the strategies and not from sizing choices. The real
   `HARD_BOUNDS` values are a human decision (ARCHITECTURE §12); the bake-off
   needs consistency, not correctness, on this point.
3. **Same API cost allocation:** annual cost divided by *realised* trade
   count for that candidate. A candidate that trades less carries more cost
   per trade, which is the truth (§3.1) and must not be smoothed away.
4. **Same in/out-of-sample split**, with the split date chosen *before* any
   result is looked at, and the out-of-sample window untouched until each
   candidate's in-sample work is finished. `BacktestSampleStats` already
   carries both.
5. **Same pessimism defaults**, overridden upward per candidate where §6 of
   each proposal asks for more: far side of NBBO, explicit slippage, a
   per-tier round-trip floor, no first-minute fills, halts untradeable,
   delisted names included.
6. **Every result reported with its sample size beside it**, and no candidate
   declared a winner on fewer than the §3.2 threshold for its σ/μ profile.
7. **E's net return reported as the benchmark line** beside every other
   candidate (§E.6).

If a candidate cannot be graded because the data does not exist or the
point-in-time reconstruction is impossible, **that is a result and it should
be recorded as one** — not silently dropped. A candidate we cannot backtest
is a candidate we cannot run, because the brief requires the choice to be
defended with backtest results.

---

## 9. Proposed runtime budget

**Proposal: hold the $5/month base cap and design every candidate to fit
inside it. Do not propose $8 until a backtest shows a candidate clearing
~10% annually net, with margin.**

Reasoning, and the numbers are `[EST]`:

- $5/month = 6.0% annual hurdle; $8/month = 9.6%. The difference is 3.6% of
  the account per year, which is a large fraction of any realistic edge here.
- At `[EST]` $0.03 per investigation (Sonnet, ~6k input + ~800 output tokens,
  no web search), $5/month buys ~165 investigations — comfortably enough for
  8-15 trades/month plus declines, if and only if deterministic screening
  does the filtering first.
- **Web search is the budget's main failure mode.** At $10/1,000 queries
  (TRAPS.md) plus tokens, three queries per candidate over 8 candidates a day
  is ~$7.20/month in search fees *alone*, before any tokens. Web search must
  be reserved for the shortlist actually being proposed for a trade, never
  for screening. This is the single largest controllable cost decision in the
  live path.
- The candidates differ in cost profile and it should be part of their grade:
  **E** is near-free (~$0.50/month, arguably zero model calls); **B** is
  cheapest per trade if pre-screened per §7.3(a); **A** and **C** are
  moderate; **D** is the worst, since its low frequency makes every dollar of
  fixed cost expensive per trade (§3.1).

The brief asks what annual return justifies the spend. At $5/month the
strategy must clear **6.0%** to break even against the API bill alone, and
should clear T-bills plus that hurdle to justify the risk at all. Whether any
candidate clears it is exactly what the backtest must answer, and I am not
going to guess at it here.

---

## 10. If forced to guess right now

**This section is intuition. It is not a result. It has no backtest behind
it, and it should carry no weight against the first real measurement that
contradicts it.**

If made to bet today: **Candidate C — insider-cluster buying with the
catalyst feeds used as an exclusion filter.**

Why, as reasoning rather than evidence:

- The information channel is the most plausible of the five. Insiders
  genuinely know things; the other four rest on lags and under-reaction,
  which are real but thinner.
- Its natural holding period *is* the brief's required holding period, with
  no contortion.
- Its frequency (8-15/month `[EST]`) satisfies both the learning requirement
  and §3.1's cost arithmetic without needing intraday infrastructure.
- The counterparty is a slow one. C does not compete on speed, which is where
  a $1,000 account run from a VPS loses every time.
- It gets the most out of the work already done: the catalyst feeds the
  previous build built stay in the system, doing a job they are good at.
- Its API cost profile fits the $5 cap without argument.

**And immediately, the case against my own pick:** C's break-even is ~1.1%
per trade `[EST]`, the second-worst here, because the names where insider
signals are strongest are the names with the widest spreads. If the backtest
shows the edge lives only below a liquidity floor we cannot trade, C dies and
nothing about the elegance of the design saves it. I would put that at
`[EST]` a real possibility, not a remote one.

**Grade B in parallel, at equal priority.** Not as a hedge — for a reason
§3.2 makes concrete: B is the only candidate that can accumulate a decisive
sample within a year (~12 months to n≈250, versus ~31 for C). And §2.3's
structural point is B's alone: it is the only candidate whose stop is
actually enforceable, which is precisely the property whose absence stopped
the previous build from sizing anything. If B's backtest shows any positive
net edge at all after honest slippage, I would switch to it, because a
strategy we can *evaluate* beats a strategy we can only *believe*.

**Do not build the pipeline around anything until the harness has graded all
five.** That is the brief's build order and I have no evidence that would
justify departing from it.

---

## 11. What would change my mind

Stated in advance so that changing my mind later is a measurement rather than
a rationalisation:

| Finding | Effect |
|---|---|
| B shows positive net edge after far-side fills + 0.15% slippage, n≥400 | **Switch to B.** Sample speed and enforceable stops outweigh C's better story |
| C's edge exists only below a tradeable liquidity floor | **Drop C.** Its main weakness, confirmed |
| E matches or beats A/B/C/D net of costs | **Run E, stop paying for data linkage.** The complexity would have failed to justify itself, and that is a legitimate outcome |
| A's drift survives realistic costs at n≥300 | **Promote A** — it has the deepest literature and the most tradeable universe |
| D yields <120 events in 7 years | **Drop D** as standalone; keep post-event resolution as an overlay input to C |
| Alpaca confirms shorting is available at $1,000 | Re-grade B and D long/short; both may improve materially |
| Alpaca historical minute data is IEX-only or too short | **B becomes ungradeable**, therefore unbuildable. Decisive, and cheap to check first |
| ClinicalTrials.gov v2 has no retrievable point-in-time versions | **D becomes ungradeable** without look-ahead bias |
| Measured adverse gaps come in far below the estimates in §6 of each candidate | All position sizes rise, all break-evens fall, and the previous build's "un-sizeable" conclusion needs revisiting on evidence |
| Measured adverse gaps come in far above | Overnight candidates (A, C, D) shrink toward untradeable, and B's structural advantage becomes decisive |

---

## 12. Open items this document does not resolve

- **Every data source is `[UNVERIFIED]`** (§1). data-engineer should confirm
  each from the VPS and report latency and point-in-time fidelity, not just
  a 200 response.
- **Whether shorting is available at $1,000 on Alpaca** (§2.1) — changes the
  grading of B and D.
- **Whether T+1 settlement throttles day trading as described** (§2.2) —
  changes B's frequency ceiling and therefore its cost arithmetic.
- **Whether Alpaca historical minute/quote data supports B's backtest at all**
  (§B.6) — check before any modelling work; it is potentially decisive and
  costs an afternoon.
- **`HARD_BOUNDS` values** — a human decision (ARCHITECTURE §12). The 2%-per-
  position figure used throughout §6 is illustrative arithmetic, not a
  proposal.
- **Initial `adverse_gap_assumption` per catalyst type** — must come from the
  backtest measuring realised gaps per universe (§C.6), never from an
  estimate in this document. This is the specific defect BUILD-BRIEF names in
  the previous build and the one place this document must not repeat it.
- **`MIN_SAMPLE_SIZE` in ARCHITECTURE §6.1** — §3.2's arithmetic suggests the
  placeholders are too small. Resolving that is the power analysis
  ARCHITECTURE §12 assigns jointly to strategy-analyst and backtest-engineer.
- **Which candidate wins.** Unknown, unknowable today, and the only honest
  answer until `backtest/` exists.
