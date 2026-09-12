# What we tried — the project's memory

Owner-asked 2026-09-11: *"start to make a doc of everything we have
tried successfully and unsuccessfully, i want a massive sheet so you can
reflect back on it and slowly understand actions we've taken and build
from it. A doc you can reference so it can help you with your memory"*.

**What this is for.** Every session starts with no memory of the last
one. Without this file, each one re-derives the same diagnosis, re-tries
things that were already measured and rejected, and re-learns lessons
that cost real money the first time. CLAUDE.md says what the code does
*now*; this says **what was tried, what the evidence was, and what
happened** — including the failures, which are the more useful half.

**Rules for editing it.** Append, do not rewrite history. Every row
carries the evidence that produced it. If a later measurement overturns
an earlier entry, mark the earlier one `SUPERSEDED` and say by what —
deleting it loses the fact that we once believed it. Never enter a
number this file cannot source.

**Read this first when:** the bot is not trading; a threshold looks
wrong; an arm looks dead; you are about to add a feed, an arm or a
gate; or you are about to "fix" something that has been fixed before.

---

## 1. The one-line state, as of 2026-09-11

- **122 commits. It has never placed a live order.** One trade exists on
  record (EMBC, closed at a loss). That is the only thing that has ever
  happened with money.
- **The reason it was not trading has changed fourteen times.** Every one
  was real, every one was fixed, and each fix revealed the next. That
  pattern is the single most important thing in this file — see §3.
- As of the last change, **15 of the 25 directional views the bot has
  ever produced would now place an order**, against 1 before, and it
  fills its five-position cap and stops. The gates are no longer the
  blocker. Whether the trades make money is untested.

---

## 2. The strategy bake-off — what was graded and what survived

Five candidates were pre-registered and replayed against real price
history (`docs/STRATEGY-BAKEOFF.md`, `docs/STRATEGY-PROPOSALS.md`). Only
three were built far enough to grade.

| arm | sample | n | hit | mean/trade | maxDD | worst trade | verdict |
|---|---|---|---|---|---|---|---|
| **A** XBRL earnings drift | IS | 268 | 57.1% | +0.55% | 15.9% | −29.9% | **kept** |
| | OOS | 84 | 57.1% | +1.59% | 8.8% | −18.5% | best-graded |
| **C** insider clusters | IS | 710 | 53.1% | +0.28% | 52.6% | −89.6% | **kept** |
| | OOS | 203 | 49.3% | +0.87% | 41.2% | −57.4% | worse OOS than a coin flip |
| **E** ETF rotation | IS | 1,485 | 48.1% | −0.16% | 43.9% | −13.5% | **killed** |
| | OOS | 524 | 49.4% | −0.07% | 17.1% | −24.8% | −499.87% excess |

### Excess return over SPY, net of costs — the number that matters

| arm | window | SPY | strategy | excess |
|---|---|---|---|---|
| C, OOS only | 2024–2026 | +68.72% | **+75.45%** | **+6.73%** |
| C, full | 2016–2026 | +353.13% | −65.21% | −418.34% |
| A, OOS only | 2024–2026 | +68.72% | +4.62% | −64.10% |
| A, full | 2016–2026 | +353.13% | −34.52% | −387.64% |
| E, OOS only | 2024–2026 | +68.72% | −31.41% | −100.13% |

**The uncomfortable reading, kept here deliberately:** the only
positive excess in the whole bake-off is insider clusters
out-of-sample, at +6.73%, and it is fragile. Remove the API cost and it
becomes +31.64%; raise slippage from 15bp to 30bp a side and it becomes
**−15.17%**. So the strategy the bot actually runs is one whose measured
edge is smaller than the difference between two plausible slippage
assumptions.

### Things tried on the strategies and rejected on evidence

| tried | result | verdict |
|---|---|---|
| E tuned: biweekly rebalance, hold 9 | −0.06%/trade (n=739) vs −0.16% pre | better and still negative. **E dead in both variants** |
| A tuned: SUE ≥ 2.0 | −0.13%/trade (n=144) vs +0.58% pre | **worse. Tuned A rejected, pre-registered A stands** |
| Sector enrichment on insider candidates | recovers **21.9 points** of excess | **kept** — the cluster cap was treating a bank, a biotech and a miner as one bet |

**Lesson that generalises:** in-sample tuning made both arms worse out
of sample, every time it was tried. The pre-registered version won in
both cases.

---

## 3. Why it was not trading — the blocker kept moving

This is the spine of the project. Each row was a genuine, measured
cause of zero trades; each was fixed; each fix revealed the next one.
**Read this before concluding you have found "the" reason.**

| # | date | the blocker | evidence | what changed |
|---|---|---|---|---|
| 1 | 08-14 | `priced_in` was an **outright veto** | every catalyst days old reads as partly priced | became a conviction premium instead of a veto |
| 2 | 08-17 | **Conviction had no definition at all** | 31 live views: every long scored 0.30–0.45 against a floor of 0.60. Two scales, never reconciled | defined as a frequency: 0.50 coin flip, 0.60 six in ten |
| 3 | 08-20 | The **daily cost check halted the bot on 41 cents** | rounding alone reached the threshold | threshold raised to an absolute floor AND a real fraction |
| 4 | 08-23 | **Unpriced cost rows halted it silently** | a halted bot looked exactly like a quiet one | the halt says which test fired |
| 5 | 08-30 | **Only one arm was running.** Drift was built, pre-registered and graded, and nothing ever fetched the XBRL it needs | 363 cycles, 28 research calls, 0 trades | drift wired into discovery |
| 6 | 09-05 | A **reconciliation discrepancy paused spending for 3.5 of 7 days** | 201 × `budget_denied: reconciliation_discrepancy_unacknowledged`, 0 research calls, 3 trading days lost. The arithmetic error was a *pricing forecast*, which the reconciliation corrects itself | a discrepancy is recorded and **never gates spending**. One integrity gate left: an unpriced row |
| 7 | 09-05 | The **conviction floor at 0.60 passed one long in twenty-one** | n=21, median 0.54, max 0.60. The floor's own evidence sample was 15 against a 30 minimum, so it could never have moved on its own | floor → 0.50, the definition's own line |
| 8 | 09-05 | The **drift arm's universe was wrong** — the 141 companies insiders had traded in, none of which filed a 10-Q | arm produced nothing all week | universe is every 10-Q/10-K filer from the daily index |
| 9 | 09-05 | **The hunt had never run.** `Decimal` was not imported at module level, so every hunt that came due raised `NameError` | Claude's half of discovery had never executed once | import fixed |
| 10 | 09-11 | The **priced-in premium of 0.15 on a 0.50 floor is a 0.65 bar** | UBER: $10.0M CEO open-market buy, model wanted it long at **0.62**, refused. `priced_in` set on 62 of 65 no_trades | premium → 0.05 |
| 11 | 09-11 | **Half the directional views were shorts** a cash account cannot take | 2 of 4 (CASY 0.58, COO 0.60), each costing a paid call | prompt states the account is long-only |
| 12 | 09-11 | The **hunt could not see outside a filing** | billed **0 web searches across 18 calls** — it had never been offered the tool. The owner's war → supplier → beneficiary chain cannot start in a filing | web_search, 5/hunt, plus a second-order brief |
| 13 | 09-11 | **48% of the research budget went to an arm with 0 directional views in 89 calls** | conjunction: 33 of 69 paid calls, $9.15 of $15.70, $0.277/call. Allocation was by **list position** | rotation one per arm per round; probe share for a proven non-converter; conjunction searches 10 → 3 |
| 14 | 09-11 | **Conviction rewarded declining over committing** | 268 no_trade views median 0.68 with 97 at ≥0.80; all 25 directional views ever between 0.30 and 0.62. One column, two incompatible quantities | the tool says they are not comparable; the prompt anchors on measured hit rates |

### The pattern to internalise

**Eleven of those fourteen were not strategy problems.** Only rows 1, 7
and 10 — the priced-in veto, the conviction floor and the priced-in
premium — were a threshold being wrong. The other eleven were a missing
import, a wrong universe, a halted budget, an undefined field, an
unprivileged tool, a list ordering, and two scales sharing a column.

**So the bot has spent most of its life prevented from trading by
plumbing, not by judgement.** When it is not trading, measure the funnel
before rewriting the strategy — and the funnel means the drop reason at
*every* stage, **per arm**, because the per-arm split is what made row
13 visible after 89 wasted calls.

---

## 3b. Where the gates stand now — replayed 2026-09-11

Every directional view the bot has ever produced, put back through the
live risk engine on a liquid snapshot (8bp half-spread). This is the
measurement that says whether the gates are still the blocker:

| | before 09-11 | after |
|---|---|---|
| directional views that place an order | **1 of 25** | **15 of 25** |

Sequenced with the portfolio filling up, it opens five and stops:
$400, $400, $400, $400, $200 — $1,800 of $2,000 — and the sixth is
refused `no_free_slot`. **The gates are no longer what produces zero.**

The 10 that still do not trade, and why each is correct:

| why | n | correct? |
|---|---|---|
| `below_conviction_floor` (0.30–0.45) | 3 | yes — under the definition's own coin-flip line |
| `priced_in_below_raised_floor` (0.30–0.45, priced in) | 5 | yes — same, plus the premium |
| `short_unavailable_cash_account` | 2 | yes — the account cannot short |

**Spread is the remaining cliff.** All 15 pass at ≤20bp and **every one
fails at 21bp** — a hard bound, so the owner's. Only one real spread is
on record: BWFG at 99.2bp, refused, which on a $400 position is a $7.94
round trip against an 8% assumed move. The real spread distribution of
the insider-cluster universe is **unmeasured**, and insider clusters
skew small-cap, so this is the most likely next blocker.

---

## 4. Per-arm production record — measured 2026-09-11

Lifetime, from `research_calls` joined to `candidate_origin` and
`research_views`:

| arm | paid calls | $/call | directional views | conversion |
|---|---|---|---|---|
| insider (`screen`) | 189 | $0.178 | **23** | 12.2% |
| conjunction | 89 | $0.277 | **0** | 0% |
| earnings_drift | 13 | $0.189 | 2 (both shorts) | 15% unusable |
| hunt | 2 | — | 0 | too few to say |

**Every order the bot has ever considered came from insider clusters —
the arm that graded worst out of sample.** The best-graded arm has
produced nothing this account could act on. The unbacktested arm has
produced nothing at all, at the highest price per call.

**Open trigger:** if conjunctions produce no directional view over the
next measured window, unwire the arm as `etf_rotation` already is.

---

## 5. Cost — what it actually costs to run

| measure | value | source |
|---|---|---|
| research call, blended | $0.228 | 69 paid calls / $15.70, 09-11 window |
| research call, insider | $0.178 | per-arm split |
| research call, conjunction (at 10 searches) | $0.277 | per-arm split |
| hunt call | $0.116 | 18 calls / $2.08 |
| sustainable daily rate at a $100 cap | **$3.33/day** | $100 / 30 |
| actual, on active days | **$3.50/day** | $13.98 over 4 days |

**SUPERSEDED READING, recorded because I got it wrong:** "$21.25 of $100
month-to-date, so the budget is not the constraint." Four of that
month's first days were the pause. On a run-rate basis the bot is at
**105% of its sustainable rate**, projecting to $105/month. There is no
headroom. Raising `research_per_cycle` was considered and **rejected**:
it front-loads the month and then goes dark, which is what
`DAILY_BURST_DAYS` exists to prevent, and it makes the live record
incomparable to a backtest that trades every day.

**So the only cost levers are allocation and price per call.** Both were
pulled on 09-11.

### Is any API cost hard-coded? — audited 2026-09-11

Owner: *"i want to be absolutely certain aswell we have not hard coded
api costs, remember we have access with the admin API... we dont want to
be changing estimates manually."*

The audit found two categories and only one was already right.

**Already self-correcting — the PRICES, which decide what a call cost
once it happened:**

| what | how it corrects |
|---|---|
| per-token rates | `measured_rates.py` divides Anthropic's charge for a closed day by its own token counts and calls `set_override()`, so `pricing.py`'s table is a cold start nothing reads afterwards |
| cache and web-search multipliers | `factors.py` derives them from the itemised bill, discarding any derivation whose components do not add back up to the billed total |
| input tokens per web search | `boundary.py` seeds 12k and replaces it with the observed 75th percentile after 8 searching turns |

**NOT self-correcting — the ESTIMATES, which decide *before* a call
whether it is affordable and how many to allow:**

| constant | typed value | measured |
|---|---|---|
| `TYPICAL_RESEARCH_CALL_CENTS` | 50c | **22.8c** blended |
| `HUNT_ESTIMATE_CENTS` | 60c | **11.6c** (18 calls, $2.08) |
| `HUNT_TURN_ESTIMATE_CENTS` | 20c | never measured at all |

Wrong by two to five times, each a number somebody typed after reading
one bundle, with nothing anywhere to say so. They now read the ledger
through `cost/observed.py` — minimum sample of 8, 75th percentile, a
30-day window, the constant demoted to a cold-start seed.

**The chain, end to end, and it is now closed:**

```
Admin API charge for a closed day
  -> measured_rates.learn_from_closed_day -> set_override
  -> pricing_overrides table
  -> price() -> cost_events.priced_cents
  -> observed_call_cents -> the estimate for the NEXT call
```

So **no API cost in the live path is a typed number once one day has
closed with spend on it.** The cost panel shows which estimates are
measured and which are still seeds, with the sample size, so this is
checkable without reading the source.

**What is still typed, correctly:** the CAPS. A cap is a decision the
owner makes, not a measurement — the monthly cap, `DAILY_CAP_CENTS`,
the reconciliation floors. `tests/test_no_cost_is_hard_coded.py` holds
the distinction with a permit list: a new `*_CENTS` constant appearing
outside the modules that account for one fails the suite.

**A note on the guard's first version**, because it is the pattern to
avoid: it matched `_USD` too, and flagged `MIN_TOTAL_VALUE_USD` — the
minimum insider purchase a cluster must total. That is a strategy
threshold, not an API cost. A guard that cries wolf teaches the next
reader to add files to the permit list without thinking, so it was
narrowed to cost-shaped names only.

### The hurdle, which every "is this working" conversation starts from

At **$100/month on $2,000 the bot must clear roughly 60% a year to match
holding cash.** The S&P long-run average is about 10%. Lowering the cap
is the single cheapest improvement available and has been from the
start.

---

## 6. Recurring failures in my own work

These have each happened more than once **across different sessions**.
They are the most expensive entries in this file because they waste the
owner's time on a fix that was never applied or a test that never ran.

| pattern | how it shows up | the check |
|---|---|---|
| **A test that cannot fail** | `assert X not in text or "0.60 means" in text`, where the escape phrase is in the test module's own source. Unfailable from the day it was written | house rule 4: sabotage a copy, watch it go red |
| **A helper nobody calls** | the adaptation loop was a function no caller reached for a week. First sabotage round on the arm rotation caught *nothing* for the same reason | assert the call site, not just the function |
| **A test anchored to a calendar date** | fixture drifts out of the window and the suite goes red for an unrelated reason. Happened twice | house rule 6: measure against `datetime.now()` |
| **A test anchored to two constants that later became equal** | `BASE_SEARCHES` vs `CONJUNCTION_SEARCHES` used as "two different values"; went red on a strategy decision unrelated to what it tested | assert the property with explicit values |
| **A multi-edit script that writes nothing** | one `old` string does not match exactly, so neither edit applies and the script reports success. Happened twice | assert every replacement happened |
| **Sabotage that is a syntax error** | proves the file is broken, not that the test catches the behaviour | verify the sabotaged build still imports |
| **Reporting a fix that never landed on `main`** | six commits of green work sat on a branch while the owner ran the upgrade and saw no change | `git log --oneline origin/main -1` and an empty `git log origin/main..HEAD` |
| **Believing a month-to-date number is a run rate** | see §5 | divide by *active* days |
| **A vacuously true assertion** | `nums == sorted(nums)` and `nums == range(1, len+1)` are BOTH true for an empty list, so removing the numbering entirely passed the test that existed to check it | assert the collection is non-empty *before* asserting anything about its contents |
| **A hand-numbered sequence across conditional sections** | headings hard-coded "6." while the orders section only renders when orders exist, so a card showed 1,2,3,4,5,7 | number at render time from a counter |

---

## 7. Traps that cost real money

Full list in `docs/TRAPS.md` — read it before touching cost tracking, a
data feed, or broker code. The ones that have bitten this project more
than once:

- **Cache tokens are billed and are not in `input_tokens`.** Missing
  them understates the bill by about half.
- **The Cost API reports whole days only.** Today's spend is not
  queryable, so a naive read shows $0 and looks like a broken account.
  The local ledger is the only real-time number there is.
- **Web search costs $10/1,000 queries on top of tokens**, and the
  results arrive as *input tokens* — which is the dominant cost, not the
  query fee. 34k median input tokens, 166k max.
- **EDGAR is 10 req/s across all its APIs**; an overrun is a temporary
  IP block. Retry transient 500s, never a 4xx.
- **A missing EDGAR daily index returns 403 + AccessDenied, not 404.**
- **`/v2/corporate-actions` 404s** — the endpoint is `/v1/`.
- **Stop orders do not trigger outside regular hours**, and fractional
  stops need `time_in_force=DAY`, so they expire at the close.
- **The PDT rule was retired 4 June 2026** and Alpaca removed the API
  fields in July 2026. Code referencing `daytrade_count` breaks.

---

## 8. Owner decisions, with dates

These are settled. Do not re-litigate them; do surface evidence if it
changes.

| date | decision |
|---|---|
| 08-14 | A daily cost figure is fine; the lag is accepted. A discrepancy is a correction, not an alarm |
| 08-14 | Block spending only on a *large* discrepancy — later removed entirely (09-05) |
| 08-31 | **"merge all, change rules so you dont want me everytime, i just want you to give me results."** House rule 5 rewritten: money-critical code reviews itself and lands without waiting |
| 08-31 | **"i just want you to give me results"** — decide it, do it, then report. A question to the owner is a last resort |
| 09-05 | **"i dont really need any hard limit except a hard stop to stop bot using all the budget"** — no pause but the budget |
| 09-05 | Trading capital $2,000; runtime budget $100/month |
| 09-11 | Asked for aggression and creative cross-domain reasoning, not more hard limits |

**The one thing that is still the owner's, and does not move:** hard
bounds — max loss per position (2%), max total exposure, max positions
(5), the daily-loss (4%) and drawdown (12%) kill switches, and the
entry spread gate (20bp half-spread). The system may *propose* a change
and must never make one.

---

## 9. What is still unproven

- **It has never traded.** Every claim about whether any of this works
  is untested in the only way that counts.
- **The backtest graded the mechanical screen, not Claude.** All 23
  runs are `mode='structural'` — a replay with no model in the loop.
  `backtest/judgement.py` is a three-line stub. A backtest largely
  *cannot* grade the model: it already knows what happened to any stock
  before its training cutoff.
- **The refusal tracker has scored essentially nothing.** 291 refusals
  on record. It is the brief's "single most important feedback loop",
  and until it produces numbers **every adaptive threshold is sitting
  on an estimate** — including the two moved by hand this month.
- **The conviction scale is uncalibrated.** Whether a 0.6 call resolves
  six in ten is unknown.
- **Whether the conviction anchor helps is unmeasured.** Telling the
  model what 57% means could equally teach it to cluster around the
  number it was shown.
- **The spread gate is a cliff at 20bp** and the real spread
  distribution of the candidate universe is unmeasured. One spread is
  on record: BWFG at 99.2bp, refused. If the insider universe is mostly
  wide-spread microcaps this gate is the next blocker.

---

## 10. What to check first, next session

In this order, because this is the order in which things have actually
been wrong:

1. **Read the funnel per stage AND per arm.** `candidates → screened →
   researched → views → proposed → orders`, with the drop reason at
   each. Most blockers were visible here and nowhere else.
2. **Check the governor actually authorised spending.** Look for
   `budget_denied` and for a pause that outlived its cause.
3. **Check each arm produced something.** A wired arm that emits zero
   candidates has usually lost an import, a universe or a date window.
4. **Check cost per call and the daily run rate on ACTIVE days.**
5. **Check whether the refusal tracker has scored anything yet.** If it
   has, that is the first real evidence this project has ever had about
   whether its thresholds are right, and it outranks every argument in
   this file.

---

## 10b. The dashboard — what has been rebuilt, and why

Owner reports on the dashboard are worth their own section because the
same defect keeps recurring in a different panel: **a figure is on the
page, correct, and unreadable.** The page holds the fact and does not
answer the question.

| date | reported | measured cause | what changed |
|---|---|---|---|
| 08-21 | "on the trade info this bar is broken" | entry label anchored end-at-52 while 72px wide, so it drew at x=−5; review rules landed on one pixel and labels overprinted into "skipp/jjjgted" | label clearance, same-day collapse, skipped reviews not drawn |
| 08-21 | "still dont understand what beating it by 0.89pp" | "pp" on every page | `signed_pp` deleted outright, and a test forbids the package mentioning it again |
| 08-21 | "loads of why is this here dropdown with no data" | nested panels: the outer provenance harvest cut lines out of a fold the inner call had already made, leaving the promise with no contents | already-folded regions masked before harvest |
| 08-24 | SPY line flat / missing | benchmark gaps; a refused feed never recovered | retry, self-rebuild, lag measured against a real close |
| **09-11** | **"i want easy summaries of what happened and decisions and drop downs if I want more detail... This graph feels a bit dumb"** | see below | the trade card rebuilt summary-first |

### What the 09-11 trade card actually got wrong

Six things, each measured off the owner's screenshot of the EMBC card
rather than judged by eye:

1. **The chart never drew the exit.** On a closed trade the most
   important mark is where it sold; the chart drew entry, stop and a
   price line while the sale price lived only in a tile. EMBC sold at
   $4.9736 against a $4.55 stop, so the **calendar** ended that trade,
   not the risk engine — and the picture could not say which. Those two
   need opposite responses.
2. **It drew a full time chart with no series in it.** No daily closes
   are cached for EMBC, so the plot was a 60-day run-up window, a
   full-width axis, date labels at both ends and a full-width risk block
   around an empty middle. A chart with no series is not a chart.
3. **Five reviews printed as a picket fence** — a full-height dashed
   rule each, with "held" overprinting itself.
4. **The tiles were unreadable at a glance:** `$4.9736`,
   `79.1295 @ $5.06`, `$-6.84`, `hard_exit`.
5. **No sentence anywhere said what happened.** The reader assembled
   the narrative out of a dozen figures, every time.
6. **One dropdown existed and five of them were labelled identically.**
   "why this matters" ×5 is no better than five unlabelled buttons.

### What it is now

- **A plain-English paragraph first**, before any figure: what was
  bought, how much, why that stock, what Claude rated it, what ended the
  trade, and the result in money and as a share of the position. Then a
  second line for the decision: the size, the bound that set it, the
  worst case in dollars, and that the model cannot touch any of it.
- **Rounded tiles, exact figures behind "The exact numbers".** Anything
  you intend to *check* belongs unrounded in a fold; anything you intend
  to *read* belongs rounded on a tile.
- **Named dropdowns**, one per subject, no duplicates.
- **A chart with two kinds.** With bars: time across, price line, entry
  and stop rules, the **exit dot labelled with its price**, review ticks
  in their own lane under the plot with a single count, risk band
  spanning only the days actually held, and the axis starting at the
  first bar that exists rather than 60 days back. With no bars: a
  **price ladder** — entry, stop and sale as levels on a real price
  scale with the gap measured — and a caption saying plainly that no
  closes are cached.

**Colour was computed, not chosen.** The palette validator
(`dataviz` skill) was run against this dashboard's own light and dark
surfaces. Blue price line plus red stop threshold passes every check in
both themes. **Profit and loss are deliberately NOT green against red:**
that pair measures ΔE 4.1 under deuteranopia on the light surface, which
is not a distinction a reader can rely on. The outcome is carried by the
dot's **position** against the entry rule, by its own price label, and by
the sentence above the chart. A test asserts the exit dot does not change
colour with the outcome.

### The 09-11 second pass, same day

Owner, on the rebuilt card: *"more detail, link the different articles
from the news, i feel we're heavily looking at insider trades not just
claude spotting potential. Edit this page more to be more fluid and read
in order of process. Also touch on the is it mainly looking at insider
trades or can we get it to do even more agentic research."*

- **The evidence is linked now.** The card carried the thesis, the
  invalidation and the priced-in call — all of them Claude's *reading*
  of the sources — and not one link to a source. The rows existed the
  whole time: `raw_events.payload_raw` has carried the news `url` since
  the feed was written, and the decision page has read them since
  August. A Form 4 is now described from its own payload ("Bern Richard
  (CEO) bought 141,000 shares at $70.96") rather than by its accession,
  filings link to the SEC archive, and the web searches Claude chose are
  listed. Only `http(s)` URLs are accepted and every link carries
  `rel="noopener noreferrer"` — a payload is upstream data, and the
  dashboard holds an access code.
- **Seven numbered steps, in the order the trade happened.** Numbered
  **at render time from a counter**, not in the heading strings: the
  first attempt hard-coded them, and the orders section is conditional,
  so a position with no recorded orders rendered 1, 2, 3, 4, 5, **7**.
- **The origin panel answers the concentration question with a table.**
  Candidates, paid calls, spend, cost per call, directional views and
  orders per arm, then the plain reading: *23 of 25 directional views
  came from insider clusters — so yes, on the record this is close to a
  single-arm system.* It also distinguishes an arm that spent 40+ calls
  producing nothing from one that is merely new.

### TRIED AND REJECTED the same day: halving the hunt divisor

To answer "can we get it to do even more agentic research", I halved
`hunts_per_day`'s divisor to take the hunt from 2/day to 4/day at the
$100 cap. **A test caught it and it was reverted.**

That `2x` is not a rate dial — it is the **reserve for researching what
a hunt finds**, which is what "a hunt plus the candidates it produces"
means. At `1x` a $20/month cap returns one hunt a day costing 60c of a
67c daily allowance, leaving nothing to judge the nominations with:
exactly the failure the function's own docstring warns about. Halving it
did not buy more discovery, it bought nominations nobody could afford to
research.

**So the hunt rate is not the lever.** The honest answer is arithmetic:
at $100 the budget supports two hunts a day *and* researching what they
find; more agentic discovery costs more money and the cap is the dial.
What actually moved on that date was **where research slots go** — the
conjunction arm's 48% share went to the arms that convert, the hunt
among them. And the hunt is self-limiting either way: no directional
view in 40+ paid calls demotes it to a probe share like any other arm.

### The 09-12 pass: the links were broken and the card had no time axis

Owner: *"when it references an old trade it references edgar and one url
content is ttached, they all appear to say that... i want each stage it
took in chronological info and the data that was available and how price
changed and what the bot thought when it re-evaluated... It should be
able to set itself a next to check in tab"* — and separately *"remove
emojis also we dont need them"*.

**THE EDGAR LINKS ALL 404'd, AND THAT WAS MINE.** The fix I shipped the
day before stripped the dashes out of the accession number. Checked
against the real SEC rather than reasoned about:

| URL | result |
|---|---|
| `edgar/data/1872789/000094787126000787.txt` | **404** — what I shipped |
| `edgar/data/1872789/0000947871-26-000787.txt` | 200 |
| `…/000094787126000787/0000947871-26-000787-index.htm` | 200 |

The accession keeps its dashes in the **filename**, and the index page
additionally needs the undashed accession as a **directory**. Worse, the
Form 4 payload already stored `source_url` — the URL the feed itself
fetched, which resolved by definition — so I derived a link when a
working one was sitting in the same dict. It prefers the stored URL now
and derives the index page only as a fallback. The owner's exact broken
key is a regression test.

**The lesson:** when a payload already contains a URL that was used,
that is the link. Deriving one is a guess with extra steps.

**The card had no time axis.** It was grouped by topic — found,
evidence, concluded, sized — which is the order the decision was made in
but not a sequence, and no price sat beside any moment. There is now one
dated table per trade: catalyst date, bought, every review with what
Claude actually said, and the exit, each row carrying **the close on or
before that date** and its move against the fill. Never a later price
than the row's own date, so a decision is never shown the benefit of
hindsight.

**Claude sets its own next check-in.** Reviews ran on a flat 24-hour
clock brought forward by news, so a quiet position was paid for six
times to be told nothing had changed. The review tool now takes
`next_check_in_days`, and code bounds it: clamped to the hard exit date,
capped at `MAX_CHECK_IN_DAYS` = 7, nothing scheduled on an `exit_now`
review, and **news still overrides it** — the one property that must
survive, with its own test. Both the request and the honoured date are
stored, so "it asked for 30 and got 7" is readable.

Why a longer wait is safe: a missed review cannot cost money beyond the
stop. The stop rests at the broker, the hard exit date stands, and a
review can only ever bring an exit **forward**. So the cost of waiting
is a forgone early exit, not a larger loss.

**Emoji removed.** Every step icon and action icon was decorative by
construction — aria-hidden, beside a heading that already said the same
thing — so there was nothing to replace them with. The four status
glyphs stay: they are geometric shapes, not emoji, and they exist so a
status is never carried by colour alone.

### A latent flake the suite finally hit

`test_a_position_opened_today_still_says_when` failed once and passed on
its own seconds later. Cause: the test module captures `NOW` at import
and positions every fixture against it, while `next_actions()` defaulted
to `datetime.now()` — so **a suite run that crossed UTC midnight** judged
a position seeded as "opened today" to be a day old, the age gate opened,
and the test failed for a reason unrelated to what it tests. House rule 6
exactly. Fixed by handing the code the same clock the rows were written
against.

**The lesson that generalises, and it has now cost four reports:** a
number being present is not the same as a question being answered. When
a panel is reported as confusing, ask what question the reader brought
to it and whether any single element answers that question — not whether
the facts are all there.

---

## 11. Sizing — what scales with the account and what does not

Owner-asked 2026-09-11: *"does that number of position cap change
dynamically as obviously we currently arent at exactly 2000, i need to
to change based on account value"*.

**It already does, and always has.** `risk/sizing.py` contains no
capital constant. Every bound is a fraction of `portfolio.equity_usd`,
which `build_portfolio_state` reads from `broker.get_account()["equity"]`
fresh on every cycle:

| bound | formula |
|---|---|
| risk budget per position | `equity × 0.02` |
| slot ceiling | `equity ÷ 5` |
| total exposure room | `equity × 0.90 − deployed` |
| correlated cluster room | `equity × 0.35` |

Measured, first position with no others open:

| equity | position | % of account | risk on it |
|---|---|---|---|
| $500 | $100 | 20.0% | $10 |
| $1,000 | $200 | 20.0% | $20 |
| $1,900 | $380 | 20.0% | $38 |
| $2,000 | $400 | 20.0% | $40 |
| $5,000 | $1,000 | 20.0% | $100 |

So the $400 figure that appears in earlier notes is not a setting — it
is 20% of $2,000, and at $1,900 the same code sizes $380. **Any
hardcoded dollar figure found in this project's money path is a bug;
there are currently none.**

Two details worth remembering:

- **Both bounds coincide at 20% by arithmetic, not by design.** The slot
  ceiling is `equity ÷ 5` = 20%, and the risk route is
  `(equity × 2% max loss) ÷ 10% stop width` = 20%. Change the stop width
  or the position count and they stop agreeing, at which point the
  smaller one binds and the dashboard should say which.
- **There is a minimum notional floor.** Below roughly $5 of equity a
  candidate is refused with `notional_below_minimum` rather than sized
  into a meaningless order.

**What does NOT scale, correctly: `max_open_positions = 5` is a count.**
Five genuinely uncorrelated bets is the right shape at any account size,
and scaling the count with equity would mean a smaller account
concentrating rather than diversifying. It is also a hard bound, so it
is the owner's to change and the system may only propose.

**The consequence for trade frequency:** five concurrent positions on
holds of days to weeks caps the rate near ten trades a month whatever
the account value. That covers the owner's stated want of five trades
per week or two, but it is the ceiling, and raising it is a hard-bound
decision rather than a tuning one.

---

## 12. Switching the model — what was manual, and what is now not

Owner-asked 2026-09-12: *"how easy is it to change the model claude is
using aswell? e.g. when sonnet 5 goes how easy can i switch to a later
version and will pricing change accordingly"*, then *"i want nothing
manual, i want it to auto add the models and also where can i actually
change the dropdown to try a different model, e.g. i wanted to switch to
opus 5, the pricing is ok as it calls per day doesnt it so it will just
mark a higher price"*.

**The answer before this change was "you cannot", and the code looked
like it could.** Four separate things were wrong, and three of them were
invisible from reading the module that owned them.

| # | what was wrong | evidence | what changed |
|---|---|---|---|
| 1 | **`setup/models.list_models` had NO production caller.** The live dropdown rendered exactly one hard-coded option | `grep -rn "list_models\|render_setup_page" catalyst/` — the only call site was `render_setup_page(self.path_prefix)`, with no `models` argument, so `_field_input` fell through to its `if not opts` branch every time | `SetupApp._models_for_page()`, called on both pages; `tests/test_the_model_dropdown_is_reachable.py` asserts the **call site** |
| 2 | **The dropdown only existed on the first-run form.** After setup, the only route was "open the full setup form" and re-pasting all three secrets | `render_configured_page` offered budget + billing key + key replacement, and no model field. Its own docstring describes rescuing the BUDGET from exactly this trap, which was still set for the model | the model dropdown is in the same settings form as the budget, saved by the same POST |
| 3 | **A model with no published rate could not be selected at all**, so "auto add the models" was structurally impossible | `_field_input` rendered `disabled` for `not m.priceable`; `selected_model` silently replaced such a choice with the default | `pricing.cold_start_rates()`; nothing is disabled; the owner's choice is honoured verbatim |
| 4 | **`REVIEW_MODEL` was a separate hard-coded constant** from the research model | `position_review.py:389` `REVIEW_MODEL = "claude-sonnet-5"`. Switching research alone would bill TWO models on one day, and `measured_rates._sole_model` returns None on a two-model day — so the new model's cold-start rate would **never** be corrected by the bill | the review takes the owner's selected model; `run_cycle` threads it through `_review_open_positions` |

### The owner's own assumption, corrected

*"the pricing is ok as it calls per day doesnt it so it will just mark a
higher price"* — **almost right, and the gap was the blocker.** The daily
reconciliation corrects a rate by **ratio**: Anthropic's charge for a
closed day ÷ what the ledger priced that day locally. With no local rate
there is nothing to divide. `rates_for` raised, `record_usage` wrote an
**unpriced** row, and `governor.authorize` refuses *all* spend while one
exists — so the bot halted before any bill could teach it anything.
**The mechanism can correct a number; it cannot bootstrap from none.**

### The seed, and why 2x

An unknown model is priced at `max(published input) x 2` /
`max(published output) x 2` — currently 1000/5000 cents per MTok.

- **High on purpose.** Over-pricing throttles: fewer calls inside the
  same cap, which costs opportunity and cannot overspend. Under-pricing
  authorises calls the budget cannot afford, and the owner's one stated
  hard requirement is *"a hard stop to stop bot using all the budget"*.
- **2x and not 10x** because `measured_rates.SANITY_MULTIPLE` is 4: a
  measured rate more than 4x from the one in force is refused as a
  credit or a misread bill. A seed further out than that would be
  rejected as impossible on every clean day and **never corrected**.
  The two constants live in different files, so a test asserts the
  relationship.
- **Derived from the table, not typed.** Adding a dearer model raises
  the seed with no second number for anyone to remember.

What still refuses, and must: a call naming **no** model. There is no
rate for "unnamed", and pooling that spend would also make a two-model
day read as one to `_sole_model`.

### The chain, verified end to end offline

    a model nobody has billed us for
      -> priced from cold_start_rates(), deliberately high
      -> the row lands PRICED, so the governor keeps authorising
      -> the closed day's real bill measures what it actually cost
      -> set_override writes the measured rate for THAT model
      -> the next call prices at the measured rate

`set_override` **refused a model absent from the table** before this, so
the measured rate was computed and thrown away and the guess stayed in
force forever. That link was broken in the same direction as the others:
everything was ready to self-correct except the one write that would
have done it.

### Recurring failure, fourth instance

**A helper nobody calls passes its own tests.** `list_models` was
written, tested, documented as "the dropdown is populated by asking
Anthropic rather than from a list in this file that would go stale" —
and never called. Section 6 of this file already carries three instances
of this pattern. The check that catches it is asserting the **call
site**, and it is now asserted for both pages.

### Where the owner changes it

Dashboard → **Setup** (`/setup`), the box titled *"Which Claude model
does the thinking"*, in the same form as the monthly budget. The list
comes from `GET /v1/models` on the regular Anthropic key each time the
page opens, so a model released after Catalyst was installed appears on
its own. Saving takes effect on the next research cycle; nothing is
installed and no price is typed.

**Unproven:** no model other than Sonnet 5 has ever been billed on this
account, so the cold-start seed has never been corrected by a real bill
in production. The chain is verified offline only.

### The adversarial read found three defects in my own change

House rule 5's written read, done by hand (the `risk-reviewer` subagent
hit a rate limit mid-run). Two of the three would have shipped.

**1. A cheap new model could never escape its own cold-start guess.**
The worst of them, because it made "nothing manual" false. Measured:

```
seed for an unknown model  1000/5000   (2x the dearest known)
a Haiku-class release       100/500
ratio                       0.10  ->  beyond SANITY_MULTIPLE (4x)
result before the fix       applied=False, rate still 1000/5000, forever
```

`SANITY_MULTIPLE` refuses a measured rate more than 4x from the one in
force, on the reasoning that no published price has ever moved that far,
so such a reading is a credit or a misread bill. **That reasoning depends
on the rate in force being evidence.** Against a cold-start guess it is
simply wrong: there was no price for the reading to be an implausible
move away from. So the bot would have throttled itself at ten times the
true price with a hand-typed rate the only way out.

Fixed by `_is_cold_start_guess()` — no `pricing_overrides` row and no
published entry means the day was priced at a guess, so the **first**
measurement applies in full. The bound returns the instant a rate has
been measured, and a test holds both halves. Verified: the same cheap
model now corrects to exactly 100/500 on the first closed day, and an
absurd second reading is refused.

**2. A rate rounding to zero returned silently.** Only reachable once
the bound stopped applying to a first measurement. `return None` left
the seed in force with nothing anywhere saying why — the price-at-zero
failure with no record, which is the shape TRAPS.md is entirely about.
It is recorded as a refusal now. The branch is close to unreachable in
production (`MIN_DAY_CENTS` refuses the billed side first — it needs a
$1,000 day against a 30c bill), so this is defence against arithmetic,
not against an expected input.

**3. An empty pricing table raised a bare `ValueError`.** `max()` on an
empty dict, and `UnknownModelError` is the *only* exception the
recording path catches — so it would escape `record_usage`, abandon the
cycle, and lose the record of spend that had already happened. Now
raises `UnknownModelError`.

**Also corrected while reading:** `backfill.py` matched `if not model:`
without stripping, so a whitespace-only model skipped its own clear
"refusing to treat it as free" message and surfaced as a raw
`UnknownModelError` naming neither the day nor the group.

**And the inverse direction, checked deliberately:** the backfill's
unknown-model path used to *raise*, which was the **worse** direction
there. Raising abandoned the whole day, so the ledger kept its hole and
**under**-stated spend — the "$3.64 here, $2.95 in the console" class of
report that module exists to answer. The usage report carries
Anthropic's own model names, so an unrecognised one is a real model
really billed: it is priced at the seed, which over-states and therefore
throttles, and the next day's `cost_report` corrects the rate.

**What the read cleared:** the review still cannot extend an exit date,
size anything, or place an order — the `model` parameter reaches only
the payload, the pre-call estimate and the recorded cost row, and all
three take the same variable, so the estimate and the record cannot
disagree about which model was billed. `model=None` and `model=""` both
fall back to `DEFAULT_REVIEW_MODEL`, so a blank can never reach
`record_usage` from the review path. And every consumer of the seed —
`governor.authorize`, the `boundary` estimates, `hunts_per_day`,
`research_per_cycle`, `observed_call_cents` — tightens on a higher
number: fewer calls, less headroom. There is no consumer where a dearer
estimate loosens a limit.

**Sabotage: 19 breakages, all 19 caught red** (12 on the main change, 7
on these three fixes), each verified to still import first.

### The manual price form needed a new rule, not the old one

"Is it in pricing.py's table" was the check the whole change removed, so
the dashboard's hand-typed rate form could not keep using it — but
dropping the check entirely let a rate be stored against `gpt-9`, which
would sit in the audit trail forever and price nothing. The rule now is
**a published rate, or a model the ledger has actually billed** — both
checkable offline, and a newly selected model qualifies from its first
call onward. Before that it does not need a hand-typed rate, which is
the entire point.

---

## 13. The weekend was collecting evidence and never judging it

Owner-asked 2026-09-12: *"news is released on the weekend aswell right?
is there any harm in doing a deep dive into the news to find potential
for monday. e.g. it finds a good connection and market opportunity, it
says if price is less than this on monday buy, if not resume as normal?
I want proper connections being made here. Ensure we keep API cost in
mind still."*

### What was actually happening, measured before building anything

The cycle runs **every fifteen minutes, seven days a week** — there is no
weekday gate anywhere in the scheduler. Feeds collect, the screens build,
and the hunt nominates. Then **one** gate, `market_closed`, stopped
research *as well as* entries (`cycle.py`, the `block_entries` check
ahead of `investigate`). So the whole weekend was spent finding things
and never forming a view on any of them, and Monday's queue had to do the
thinking at the worst possible moment.

**What is and is not released on a weekend:** EDGAR is shut — no Form 4s,
no filings. Federal Register is business days only. What *is* live is
news and the hunt's own web searches, which is also the **expensive**
input (results arrive as input tokens: 34k median, 166k max). So a
weekend deep dive is disproportionately a search exercise, and that is
worth knowing before pointing money at it.

### SUPERSEDED: "the bot is at 105% of its sustainable rate"

§5 recorded *"$3.50/day against the $3.33/day the $100 cap sustains —
105% of the rate, projecting to $105/month."* **That is wrong**, and it
is the same error as the entry above it in the opposite direction: it
multiplied a **trading-day** rate by 30 **calendar** days. Research is
structurally impossible on the ~9 weekend days a month, so:

```
~21 trading days x $3.50   =  $73.50
~9 weekend days x ~$0.23   =   $2.07   (hunts only; research was blocked)
                              -------
                               ~$75/month, about 75% of a $100 cap
```

So **there is roughly $25/month of headroom and it sits precisely on the
weekend.** This change spends money the weekend could not spend, and
needs no cap increase. Recorded as a correction because §5's number was
what justified rejecting `research_per_cycle` increases, and the
recurring-failure table in §6 already carries "believing a
month-to-date number is a run rate" — this is its mirror image, and it is
now two entries for the same lesson: **divide by the days the bot can
actually spend on.**

### The three things that had to change, and only one was the feature

| # | what | why |
|---|---|---|
| 1 | Research may run against the newest **cached daily close** while the market is shut | The feature. `build_closed_market_snapshot` |
| 2 | `already_researched` **DROPPED the candidate** | A prerequisite, not a nicety. A view formed on Saturday would have been written and then thrown away on Monday, so the paid call bought **nothing**. What finishes a candidate is a risk **decision**, not a view |
| 3 | The owner's Monday condition | `risk/stale_view.py` |

**Number 2 was found by reading the loop before building the feature, not
by a test.** That is the fourth time in this project that the thing which
broke a change was plumbing one level away from it, and it is why the new
tests assert the funnel outcome rather than only the new function.

### The owner's condition, with the number taken off the model

*"it says if price is less than this on monday buy"* puts a **price that
gates an order** in the model's hands. That is the one rule that does not
move: the model decides *what and whether*, code decides *how much and at
what price*. This bot's own record has a candidate scoring **0.82
conviction** on a compelling thesis whose conclusion was *do not trade* —
a thesis that also set its own entry price would convert persuasiveness
straight into position size.

So the behaviour shipped and the threshold is **measured**: that stock's
own **95th-percentile daily move**, from `stock_gap.daily_move_percentile`
— the same function that already decides where its stop sits. One idea,
one number, so the two cannot drift apart.

**Both directions refuse, named separately** so the refusal tracker can
score them apart:

| reason | when | why it is not obviously right |
|---|---|---|
| `moved_up_past_view_price` | gapped up past its own noise | the owner's case: the move already happened, you are paying for it |
| `moved_down_past_view_price` | gapped down past its own noise | **cheaper is not better** — something happened the thesis never saw. If the record later shows these were money left on the table, that is evidence to loosen |
| `view_price_move_unmeasurable` | the stock's own noise cannot be measured | no way to tell an ordinary move from a violent one. Refusing is the tight direction |

A refusal is **not a discard**: it writes a `risk_decisions` row and a
`refusals` row with the price, so it is scored like every other decline,
and the stale view is **superseded** so the candidate is researched again
at the price actually on offer — the owner's own *"if not, resume as
normal"*.

**Only a view that crossed a session boundary is gated.** Re-gating an
intraday view would refuse candidates for ordinary drift that sizing and
the spread gate already handle.

### Risk review F5 is unchanged, and the refusal moved into the gate

F5: *"sizing and the spread gate off Friday's book is not a decision,
it's a guess."* A closed-market snapshot now has to be able to **exist**,
so the refusal had to move from "such a snapshot cannot be built" to
"such a snapshot cannot size". `MarketSnapshot.priced_off` carries the
provenance and **`risk/evaluate.py` refuses anything that is not
`live_nbbo`** — in the single gate every candidate passes through, rather
than trusted to each caller. Belt and braces: the closed snapshot carries
`half_spread_bp = 100000`, because **zero** is the one figure that would
sail through the owner's 20bp hard bound as the tightest book ever
measured.

### TRIED AND REJECTED: replacing the bundle's time-column list with a rule

The new table failed `test_bundle_time_window.py`, and the obvious
house-rule-7 fix — "window on anything ending in `_at`" instead of a
30-name list — was **measured against the live schema and is wrong.**
Five existing tables carry two timestamps where only one is the row's own
age:

| table | the row's age | the other one |
|---|---|---|
| `refusals` | `refused_at` | `scored_at` — usually NULL |
| `position_review_checkins` | `recorded_at` | `next_check_at` — a **future** date |
| `kill_switch_events` | `triggered_at` | `cleared_at` — usually NULL |
| `adaptive_param_log` | `changed_at` | `reverted_at` — usually NULL |
| `cost_reconciliation_events` | `reconciled_at` | `acknowledged_at` |

Windowing `refusals` on `scored_at` would drop **every unscored refusal**
from the diagnostic bundle — all 291 — which is the exact evidence the
refusal tracker exists to accumulate. **Which timestamp is a row's age is
not derivable from its name.** So the list stays, and what keeps it
honest is the test: a table whose time column is missing fails the suite
loudly rather than being exported whole behind a window the bundle
claims. **It rots noisily, which is the design** — and that is the
counter-example to house rule 7 worth remembering.

### Also corrected: the id-collision check now runs first

Once a traded candidate stopped being screened out by its *view* and
started being screened out by its *decision*, a colliding id arriving
after a trade was reported as `already_decided` — true, and a description
of the wrong problem. Defect 19's protection held either way; what was
lost was being told which thing had happened. An id collision means the
**record is wrong**, so it is checked ahead of everything else.

### Verification

- **18 sabotage breakages, all 18 caught red**, each verified to still
  import. The first round left **one uncaught**: an unmeasurable move
  waving through instead of refusing, which had no test at all. Tests
  added, re-sabotaged, red.
- Full suite green offline: **3818 tests**.

### What is NOT claimed

- **Whether a Friday-close view is still good on Monday is unmeasured.**
  The gate bounds the damage; it does not prove the premise. If weekend
  views are systematically refused on Monday, the money spent forming
  them is wasted and the honest response is to stop — which the funnel
  will show, per arm, because the refusal reasons are named.
- The weekend has never actually run this way. Zero weekend views exist.

---

## 14. Which arm earns its money, and the typed number that was capping the hunt

Owner-asked 2026-09-12: *"how do we know if the form 4 and insider data
is actually helping or not? is it easy to determine this? Can we get a
trade purely from claude research and one as normal, e.g. if we have 10
trades in 2 weeks at least 5 are fully claude research from news and
trade deals etc"*, then, when told a comparison page would be full of
zeros: *"add the comparison page 0s are fine if it means itll populate
it it goes on"*.

### The honest answer to "is the insider data helping": it cannot be told yet

Not because the data is missing, but because **three of the four arms
have produced nothing an engine could act on.** There is one arm to
judge, so there is no comparison. And the uncomfortable part is on the
record: the arm doing all the work graded **worst** out of sample
(49.3%, 41.2% max drawdown) while the best-graded arm (57.1%, 8.8%) has
produced nothing this account could take.

### Where the hunt was really being limited — measured

At the owner's $100 cap `hunts_per_day` returned:

```
min(4, per_day // (per_hunt x 2))  =  min(4, 333c // 23.2c)  =  min(4, 14)
```

**The budget afforded fourteen. A hard-coded `4` was the limiter** — and
the same `4` was capping the $300 row, so *tripling the cap bought
nothing*, which breaks the project's own rule that throttles derive from
the budget. It is also why CLAUDE.md's table still said "2 at $100" long
after the measured 11.6c had moved it to 4: the table was written against
the 60c seed and nothing updated it.

The `x 2` research reserve that a test correctly protected on 09-11 was
**not** what was binding at this cap. Two different bounds, and the wrong
one was blamed.

### Removing the ceiling removed the bound — caught by three tests

Uncapping returned **166 hunts/day** on a 1c measured cost and **277,777**
on an absurd cap. "Bounded by the record" is not enough on its own,
because the demotion only bites an arm that converts *nothing* — one
early directional view restores a full allowance and leaves it unbounded.

So two derived bounds replaced the typed one:

| bound | what it is | why it is not a guess |
|---|---|---|
| **cadence** | a day cannot hold more hunts than cycles (96 at 900s) | a hunt is asked once per cycle; read from the scheduler's own interval, including the env override, so it moves on its own |
| **the arm's own record** | no directional view in 40+ paid calls → probe share (1 in 4, never zero) | the same measured rule that cut the conjunction allowance. It was bounding RESEARCH slots only, so a non-converting hunt kept nominating at full rate while its nominations were rationed — the spend continued and the bound never reached it |

At $100 the budget binds first (14 against 96), so the cadence only ever
catches the pathological case. **40 paid hunt calls at the measured 11.6c
is under $5 for the whole experiment**, which is what makes giving the
hunt a real run at it cheap rather than reckless.

### And a hunt is only paid for when the feed has changed

The old ceiling's reasoning was right and its *shape* was wrong: "the
feed does not change materially between 15-minute cycles, so hunting
every cycle would pay to re-read the same digest ~26 times a day for the
same nominations." That is a statement about the **input**, so bounding
it with a spend-shaped constant answered the wrong question.
`feed_changed_since_last_hunt` states it as a rule: at least one event
must have arrived since the last hunt was billed. Zero new events is the
identical digest.

Also fixed while there: `_hunt_due` **incremented the day's counter
before** every reason to decline had been checked, so a declined hunt
consumed the allowance.

### The weekend gets a different job, not a bigger allowance

Owner: *"well surely the weekend search will be more purely agentic as it
isnt influenced by SEC of insider trade info"* — correct, structurally.
EDGAR does not file at weekends or holidays, so the mechanical screens
have nothing new to match and whatever a closed-market hunt produces is
Claude's own reasoning. **One correction, in the model's favour:** the
hunt still *reads* the week's stored filings, so it is not blind to
insider data on a Saturday — it just gets no *new* filings, which suits a
second-order chain better because the week's filings become context to
reason outward *from*.

**What the brief deliberately does NOT do is hand out more searches.**
That is the conjunction mistake exactly: ten searches instead of three,
justified by "the answer lives in reporting the feeds do not carry", and
after 89 paid calls at the larger allowance and zero directional views
the allowance was cut back. Evidence buys budget; hope does not. A test
asserts the brief grants no extra searches, and another asserts it still
states the date rule — 88% of hunt nominations were once rejected for a
past catalyst date, and *"the price will react on Monday"* is precisely
the shape of nomination a weekend brief could invite.

The market state comes from **the broker clock, not `weekday() < 5`** — a
market holiday is the case nobody thinks of and a weekday test calls it
open (house rule 7). A test asserts `weekday` does not appear in that
code path.

### The Arms page

One tab, built with zeros showing because the owner asked for that:

1. **A plain sentence first** answering "is insider data helping" —
   today that sentence says it cannot be told yet, and why.
2. **Nomination to banked money per arm**: candidates, paid calls,
   spend, directional views, conversion, cost per view, orders, closed,
   hit rate, realised P&L. Every figure counted from rows.
3. **The live record beside the out-of-sample grade**, read from this
   database's own `backtest_results` rows rather than copied off a
   document. An arm with no run says **"never replayed"**, which is the
   true answer for conjunctions and the hunt.
4. **Whether the feedback loop has produced anything**, per arm —
   because with ~0 of 291 declines scored, every adaptive threshold in
   the bot is still sitting on an estimate, and that belongs on a page
   rather than in a doc.

**The one thing NOT shown as zero is a ratio with no denominator.** "No
calls yet" and "0% conversion" are different facts and only one is a
verdict; conversion and hit rate come back `None` and render as a dash.
A failed query is flagged as a failure rather than rendering as zeros —
on a page this full of zeros, "nothing happened" and "the query is
broken" look identical otherwise, and telling them apart is repeatedly
the whole diagnosis.

**Why a graded arm that never fires gets its own sentence:** an arm can
fail two ways needing opposite responses. Graded well and never fires is
a plumbing or threshold problem, the cheapest kind to fix. Fires
constantly and loses money is a strategy problem, and means unwiring it.

### Two tests that could not fail, both found by sabotage

Worth recording because both are the pattern §6 opens with.

1. **"a declined hunt does not consume the allowance"** used an empty
   `state` dict. Empty is falsy, so a regression written as
   `daily_state or {}` would operate on a throwaway dict and the test
   would pass for the wrong reason. Fixed by seeding a non-empty dict.
2. **"an in-sample run is not used as the grade"** inserted both sample
   kinds for the *same* run and asserted the sample size. The
   out-of-sample row happened to be inserted first, so the assertion held
   whatever the query did — unfailable from the day it was written.
   Rebuilt as two runs where the **newer** one carries only in-sample
   stats, so `ORDER BY created_at DESC` puts it first and correct code
   must skip past it. `sample_kind` is now carried in the result and
   asserted directly rather than inferred from a number that can
   coincide.

Also learned: two of the sabotages came back GREEN because each was
neutralised by the *other's* guard — the WHERE clause and the
`sample_kind` check are defence in depth. Breaking **both at once** goes
red, which is the right way to show a pair is load-bearing.

### Verification

- **19 sabotage breakages.** 17 red on the first pass; the 2 that were
  green were a flawed sabotage and a genuinely weak test, both fixed and
  re-run red (the pair above needing both removed).
- Full suite green offline: **3848 tests**.

### What is NOT claimed

- **No arm has closed a trade, so every money column is a zero** and
  the page says so in a sentence before any table.
- **Whether more hunt calls produce more tradeable views is unmeasured.**
  The hunt has 2 lifetime paid calls. The bound is that 40 calls costs
  under $5 and the demotion rule then throttles it automatically — not
  that it will work.
- **The owner's "5 of 10 trades from Claude's own research" is not
  guaranteed by this.** Research slots already rotate one per arm per
  round, so the allocation exists; what was missing was hunt supply, and
  this removes the cap on supply. Whether supply converts is the open
  question.

### The Arms page would have shown zeros forever — found on the upgrade check

Owner, 2026-09-12: *"ok im ready to upgrade, anything else i should do
first?"* — and the answer turned out to be yes.

**`orders.decision_id` holds a CANDIDATE id, not a risk-decision id.**
The column name lies. The schema settles it: the foreign key on that
column is `REFERENCES candidates(id)`, `execution/orders.py` passes
`decision.candidate_id` into it, and every pre-existing join in the
codebase reads it that way (`risk_decisions d ON d.candidate_id =
o.decision_id`).

The Arms page's money query joined it **as a decision id**:

```sql
JOIN risk_decisions rd ON rd.id = ord.decision_id   -- matches NOTHING
```

So orders, closed trades, wins and realised P&L would have read **zero
forever, even after trades closed** — the worst possible failure for a
page built to answer "is the insider data helping", and a silent one,
because zeros are exactly what it is supposed to show before any trade
exists. The owner would have watched it stay empty and concluded the bot
had not traded.

### Why every test missed it: the fixtures did not match production

`catalyst.storage.init_db` runs `PRAGMA foreign_keys = ON`. A raw
`sqlite3.connect` + `executescript(schema.sql)` does **not** — and **23
test files in this suite take the raw route.** With foreign keys off, the
fixture could seed `decision_id` as a risk-decision id, which is
impossible in production, and the wrong fixture agreed with the wrong
query. Both were consistent and both were wrong.

Found by running the weekend path against `init_db` while checking what
the upgrade would do to a live database — not by a test. The first
`closed_trades` insert raised `FOREIGN KEY constraint failed` immediately.

**Fixed:** the query joins `candidate_origin.candidate_id =
orders.decision_id` (and filters `side = 'buy'`, so a stop order is not
counted as a second order); both new test files use `init_db` and
**assert the pragma is on**, so an impossible row is impossible in the
tests too. Sabotaging the join back to its original form now goes red.

**The generalising lesson, and it is expensive:** a test fixture that
differs from production in a way the production code depends on will
agree with a bug. The 21 other files on the raw pattern are a known blind
spot, deliberately left for a change of their own rather than churned the
night before an upgrade — several of them insert rows without parents on
purpose, so switching them is not mechanical.

### Verified against the upgrade itself

Built a database from the schema at `4a34e46` (before this session), then
ran today's `init_db` over it — which is what the service does on start
after `upgrade.sh` pulls:

| check | result |
|---|---|
| tables added | `research_view_context` |
| tables lost | none |
| existing rows preserved | yes |
| the weekend → Monday chain under `foreign_keys = ON` | research 1, context recorded `daily_close` at 50.5, no orders, no proposals; then at +5.2% the gate fired `moved_up_past_view_price`, no orders, **no second research call** |

---

## 15. The weekend researched for half an hour, not all weekend

Owner-asked 2026-09-12: *"ok so when over the weekend should it research
and where can i see evidence of this"* — and answering it honestly meant
measuring it, which found the feature barely working.

### Measured: eight consecutive closed-market cycles, 30 candidates, belt of 6

```
cycle 1: researched 6      cycle 3: researched 0, 12 skipped market_closed
cycle 2: researched 6      cycles 4-8: researched 0
```

**Twelve candidates got a weekend view and then it stopped forever.** The
eighteen behind them were never looked at.

**Cause.** A candidate holding a weekend view has to stay in `fresh` —
that is the whole point, it is how the view reaches sizing on Monday
without paying for a second call. But it also kept its place in
`fresh[:max_research]`, was skipped as `market_closed` for nothing, and
occupied a slot it could not use while the market was shut. So the belt
filled with candidates that had already been judged.

**Fix.** The belt is filtered by what *this cycle can actually give a
candidate*, which is a different question from what the screen let
through: while the market is shut, a candidate that already holds a view
is not asking for a research slot. It stays in `fresh` when the market is
**open**, because then it is exactly the candidate that needs a slot — to
be sized, not researched. After the fix, all 30 got a view in three
cycles and then it correctly went quiet.

**So the honest answer to "when over the weekend":** in the first few
cycles after Friday's close, working through the backlog the screens
built from the week's filings, and then **quiet** — because EDGAR is shut
and the only new candidates a weekend can produce are the hunt's own
nominations. Not "all weekend". A few cycles, then as fast as the hunt
feeds it.

### The funnel would have been red all weekend

`skip_kind` defaults an unrecognised reason on the `researched` stage to
**FAULT** (`UNKNOWN_IS_FAULT_ON = ("researched", "orders")`). None of the
three new reasons was classified, so every closed-market cycle would have
painted the funnel red for a bot that had just done its job — the
"routine attrition reading as damage" failure CLAUDE.md says has already
cost real debugging time twice. The owner would have opened the dashboard
on Monday to a weekend of red.

Now classified:

| reason | kind | why |
|---|---|---|
| `researched_while_closed_awaiting_open` | ROUTINE | the feature working |
| `has a view already, waiting for the open` | ROUTINE | not asking for a slot |
| `market_closed_and_no_cached_close` | ROUTINE | bars are cached on first research, so a new ticker legitimately has none |
| `moved_up_past_view_price` | LIMIT | a gate doing its job, and worth counting |
| `moved_down_past_view_price` | LIMIT | same |
| `view_price_move_unmeasurable` | LIMIT | same |
| `price_not_live_cannot_size` | LIMIT | risk review F5, by design |

### And the decisions page said the opposite of the truth

A weekend research call **succeeds** — it writes a view and no
`skipped_reason` at all. `_why_not_researched` looks for a skip row,
found none, and fell through to **"not researched yet"**: the exact
opposite of what happened. It now checks for a view first, and
distinguishes a view formed off a cached close ("researched while the
market was shut … will be sized at the next open") from one formed at a
live mid ("waiting for the risk engine"), because telling the owner the
market was shut about a Tuesday afternoon is simply false.

### A test that would have hidden this, caught immediately

The first version of the label test grepped the module source behind an
`if`:

```python
said = panels._research_state(...) if hasattr(panels, "_research_state") else None
if said is None:
    assert "researched while the market was shut" in inspect.getsource(panels)
```

The helper did not exist under that name, so the test took the grep
branch, found the string in the friendly map, and **passed while the
string was unreachable**. Rewritten to call the real function, it failed
on the first run and exposed the "not researched yet" bug. Section 6's
first row, again: a test that cannot fail is not a test — and a
conditional fallback inside a test is one of the ways it happens.

### Verification

- 8 sabotage breakages. 2 came back green: one because no test covered
  the intraday branch (added, re-sabotaged red), one because the grep
  test above could not fail (rewritten).
- Full suite green offline: **3856 tests**.

### Where the evidence appears

| what to look at | what it shows |
|---|---|
| **Pipeline** (`/funnel`) | `researched` climbing on Saturday, with `researched_while_closed_awaiting_open` beside it, tagged ROUTINE not FAULT |
| **Decisions** (`/decisions`) | per candidate: "researched while the market was shut, against the last cached close" |
| **Arms** (`/arms`) | paid calls and spend rising for the arms that ran, hunt included |
| **Cost** (`/costs`) | weekend spend as its own days; the monthly cap is what bounds it |
| **Logs** (`/logs`) | `Researched <TICKER> while the market was shut, against the cached close of <price>`, and `Hunt not due: no event has arrived since the last hunt at …` |
| **Monday, Pipeline** | either an order, or `moved_up_past_view_price` tagged LIMIT with the price on the refusals page |
